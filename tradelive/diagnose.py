"""m.Stock connection diagnostic: `./TradeLive.command --diagnose`
(Windows: `TradeLive.exe --diagnose`).

Uses the login saved in TradeLive (Keychain / encrypted file), runs every
step TradeLive needs and prints a report you can share: passwords, keys,
tokens and the TOTP secret are never printed (masked as ****).
"""

from __future__ import annotations

import asyncio
import json
import platform
import sys
import time
import urllib.request
from datetime import datetime

import httpx

from tradelive import __version__, secure_store
from tradelive.feeds import mstock
from tradelive.settings import Settings

SECRET_KEYS = ("password", "totp_secret", "api_key", "code")


def _mask(text: str, secrets: list[str]) -> str:
  for s in secrets:
    if s and len(s) >= 4:
      text = text.replace(s, "****")
  return text


async def _ws_attempt(label: str, url: str, extra: dict, secrets: list[str],
                      token: int | None, access_token: str) -> dict:
  import websockets
  out = {"variant": label}
  t0 = time.time()
  try:
    async with websockets.connect(url, open_timeout=10, **extra) as ws:
      out["result"] = "CONNECTED"
      await ws.send(f"LOGIN:{access_token}")
      if token:
        await ws.send(json.dumps({"a": "subscribe", "v": [token]}))
        await ws.send(json.dumps({"a": "mode", "v": ["full", [token]]}))
      lengths: dict[int, int] = {}
      texts, oi_vals = [], []
      end = time.time() + 12
      while time.time() < end:
        try:
          msg = await asyncio.wait_for(ws.recv(), timeout=end - time.time())
        except asyncio.TimeoutError:
          break
        if isinstance(msg, str):
          texts.append(msg[:200])
          continue
        for t in mstock.parse_ticks(msg):
          if "oi" in t:
            oi_vals.append(t["oi"])
        if len(msg) >= 4:
          n, pos = int.from_bytes(msg[0:2], "big"), 2
          for _ in range(n):
            if pos + 2 > len(msg):
              break
            plen = int.from_bytes(msg[pos:pos + 2], "big")
            lengths[plen] = lengths.get(plen, 0) + 1
            pos += 2 + plen
      out.update(packet_lengths=lengths, text_messages=texts[:5],
                 oi_samples=oi_vals[:3])
  except Exception as e:  # report everything useful about the refusal
    out["result"] = f"FAILED: {type(e).__name__}: {e}"
    resp = getattr(e, "response", None)
    if resp is not None:
      out["http_status"] = getattr(resp, "status_code", None)
      hdrs = getattr(resp, "headers", None)
      if hdrs:
        out["http_headers"] = {k: v for k, v in hdrs.raw_items()} if hasattr(
            hdrs, "raw_items") else dict(hdrs)
      body = getattr(resp, "body", b"") or b""
      out["http_body"] = body[:300].decode("utf-8", "replace")
  out["seconds"] = round(time.time() - t0, 2)
  return json.loads(_mask(json.dumps(out, default=str), secrets))


async def run() -> None:
  print(f"TradeLive {__version__} m.Stock diagnostic  {datetime.now():%Y-%m-%d %H:%M:%S}")
  print(f"Python {platform.python_version()} on {platform.platform()}")
  try:
    import websockets
    print(f"websockets {websockets.__version__}, httpx {httpx.__version__}")
  except Exception as e:
    print("websockets missing:", e)
  try:
    ip = urllib.request.urlopen("https://api.ipify.org", timeout=5).read().decode()
  except Exception as e:
    ip = f"unknown ({e})"
  print(f"This computer's internet IP: {ip}  (must match the Primary/"
        "Secondary IP registered for the API key)")
  print(f"System proxies: {urllib.request.getproxies() or 'none'}")

  if "mstock" in (Settings.load().disabled_feeds or []):
    print("\nm.Stock is switched off in TradeLive, so nothing is sent to "
          "m.Stock. (Re-enable it in settings.json if ever needed.)")
    return
  creds = secure_store.load()
  if not creds or creds.get("feed") != "mstock":
    print("\nNo saved m.Stock login. In TradeLive: Connect broker → fill in → "
          "keep 'Remember' ticked → Connect. Then run this again.")
    return
  fields = creds["fields"]
  secrets = [fields.get(k, "") for k in SECRET_KEYS]
  print(f"Saved login: client {fields.get('client_code', '?')}, TOTP secret "
        f"{'present' if fields.get('totp_secret') else 'MISSING'}")

  feed = mstock.MStockFeed(Settings())
  print("\n[1] REST login ...", end=" ")
  try:
    await feed.login(dict(fields))
    secrets.append(feed.access_token)
    print("OK")
  except Exception as e:
    print("FAILED:", _mask(str(e), secrets))
    return
  if feed.clock_skew is not None:
    print(f"    clock difference vs m.Stock: {feed.clock_skew:+.1f} s")

  print("[2] contract list ...", end=" ")
  token = None
  try:
    items = await feed.instruments()
    nifty = sorted((i for i in items if i.symbol == "NIFTY" and
                    i.option_type == "FUT"), key=lambda i: i.expiry)
    pick = nifty[0] if nifty else items[0]
    token = int(pick.key.split(":")[1])
    print(f"OK ({len(items)} contracts; testing with {pick.label})")
  except Exception as e:
    print("FAILED:", _mask(str(e), secrets))

  if token:
    print("[3] today's candles ...", end=" ")
    try:
      bars = await feed._intraday("NFO", str(token))
      print(f"OK ({len(bars)} bars; last {bars[-1].ts:%H:%M} close "
            f"{bars[-1].close})" if bars else "OK (no bars yet)")
    except Exception as e:
      print("FAILED:", _mask(str(e), secrets))

  print("[4] live stream (wss://ws.mstock.trade), several variants:")
  q = urllib.parse.quote
  a, k = feed.access_token, feed.api_key
  variants = [
      ("official SDK format", mstock.stream_url(k, a),
       {"compression": None, "user_agent_header": "AutobahnPython/23.1.2"}),
      ("API_KEY first (docs)", f"{mstock.WS_URL}?API_KEY={k}&ACCESS_TOKEN={a}",
       {"compression": None}),
      ("fully URL-encoded", f"{mstock.WS_URL}?API_KEY={q(k, safe='')}"
       f"&ACCESS_TOKEN={q(a, safe='')}", {}),
      ("with trailing slash", f"{mstock.WS_URL}/?ACCESS_TOKEN={a}&API_KEY={k}",
       {"compression": None}),
      ("with Origin header", mstock.stream_url(k, a),
       {"compression": None, "origin": "https://trade.mstock.com"}),
  ]
  for label, url, extra in variants:
    try:
      r = await _ws_attempt(label, url, extra, secrets, token, a)
    except TypeError:  # older websockets without some keyword
      r = await _ws_attempt(label, url, {}, secrets, token, a)
    print("   ", json.dumps(r, ensure_ascii=False))
    if r.get("result") == "CONNECTED":
      break
  print("\nDone. Copy everything above and send it (no secrets are shown).")


def main() -> None:
  import urllib.parse  # noqa: F401  (used via urllib.parse.quote)
  asyncio.run(run())
