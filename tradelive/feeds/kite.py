"""Zerodha Kite Connect live feed ("Zerodha (live)").

Why it's attractive for this app:
  * Past 1-minute candles include OPEN INTEREST (historical API, oi=1), so
    the whole day shows OI even if TradeLive starts late or the PC slept.
  * The live stream uses the same binary format as m.Stock (m.Stock copied
    Kite's), so the shared stream hub / tick parser are reused.

Login: Zerodha does not allow automated logins. Each day TradeLive opens
Zerodha's login page; after logging in, Zerodha redirects the browser to
http://127.0.0.1:8765/kite/callback (the "Redirect URL" set in the Kite
Connect app), TradeLive exchanges the one-time request token for the day's
access token, and everything continues automatically.

Docs: https://kite.trade/docs/connect/v3/
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import logging
import secrets
import sys
from datetime import date, datetime
from typing import Optional
from urllib.parse import quote, urlencode

import httpx

from tradelive.feeds.mstock import (INDEX_NAMES, MStockError, MStockFeed,
                                    _check, _f, parse_candles)
from tradelive.models import Candle, Instrument
from tradelive.settings import Settings

log = logging.getLogger("tradelive.kite")

API = "https://api.kite.trade"
LOGIN_URL = "https://kite.zerodha.com/connect/login"
WS = "wss://ws.kite.trade"
HDR = {"X-Kite-Version": "3"}
CALLBACK_PATH = "/kite/callback"


class NeedsBrowserLogin(Exception):
  """Raised by login(): the user must log in on Zerodha's page."""

  def __init__(self, url: str):
    super().__init__("Finish the Zerodha login in your browser.")
    self.url = url


def checksum(api_key: str, request_token: str, api_secret: str) -> str:
  """SHA-256 of api_key + request_token + api_secret (Kite session API)."""
  return hashlib.sha256(
      (api_key + request_token + api_secret).encode("utf-8")).hexdigest()


def parse_kite_instruments(text: str) -> list[Instrument]:
  """Index + stock F&O (NFO; BSE index options on BFO) and the NSE cash
  share of every F&O stock, from Kite's instrument dump CSV."""
  out: list[Instrument] = []
  fno_stocks: set[str] = set()
  eq_rows: list[dict] = []
  for r in csv.DictReader(io.StringIO(text)):
    exch, seg = r.get("exchange", ""), r.get("segment", "")
    name, typ = r.get("name", "").strip('"'), r.get("instrument_type", "")
    if exch == "NSE" and seg == "NSE" and typ == "EQ":
      eq_rows.append(r)
      continue
    if exch not in ("NFO", "BFO") or not name:
      continue
    if exch == "BFO" and name not in INDEX_NAMES:
      continue
    if typ not in ("CE", "PE", "FUT"):
      continue
    kind = "index" if name in INDEX_NAMES else "stock"
    if kind == "stock":
      fno_stocks.add(name)
    out.append(Instrument(
        key=f"{exch}:{r['instrument_token']}", symbol=sys.intern(name),
        expiry=sys.intern(r.get("expiry", "")), strike=_f(r.get("strike")),
        option_type=sys.intern(typ), label=r.get("tradingsymbol", ""),
        kind=sys.intern(kind), ref_price=_f(r.get("last_price"))))
  for r in eq_rows:
    if r.get("tradingsymbol") in fno_stocks:
      out.append(Instrument(
          key=f"NSE:{r['instrument_token']}", symbol=r["tradingsymbol"],
          expiry="", strike=0, option_type="EQ",
          label=f"{r['tradingsymbol']} (cash)", kind="stock",
          ref_price=_f(r.get("last_price"))))
  out.sort(key=lambda i: (i.kind != "index", i.symbol, i.expiry,
                          i.option_type, i.strike))
  return out


class KiteFeed(MStockFeed):
  name = "kite"
  label = "Zerodha"
  HIST_SPACING = 0.35  # Kite historical API: 3 requests/second
  login_fields = {
      "api_key": "API key (developers.kite.trade → your app)",
      "api_secret": "API secret (same page)",
  }
  login_help = ("In your Kite Connect app, set the Redirect URL to "
                "http://127.0.0.1:8765/kite/callback . Then click Connect: "
                "Zerodha's login page opens; log in there and you'll be sent "
                "back automatically. Zerodha requires this once a day.")
  remember_fields = ("api_key",)
  browser_login = True  # the daily login happens on Zerodha's site
  # Past candles include OI, so contracts don't need to keep recording in
  # the background: reopening one reloads the full day with OI.
  history_has_oi = True

  def __init__(self, settings: Settings):
    super().__init__(settings)
    self._client = httpx.AsyncClient(base_url=API, headers=HDR, timeout=30)
    self.api_secret = ""
    self._state = ""
    self.user_id = ""

  # ---- login ---------------------------------------------------------------
  def login_url(self) -> str:
    """Zerodha's login page; `state` comes back to us to prevent forgery."""
    self._state = secrets.token_urlsafe(16)
    q = urlencode({"v": "3", "api_key": self.api_key,
                   "redirect_params": f"state={self._state}"})
    return f"{LOGIN_URL}?{q}"

  async def login(self, fields: dict[str, str]) -> None:
    self.api_key = fields.get("api_key", "").strip()
    self.api_secret = fields.get("api_secret", "").strip()
    if not (self.api_key and self.api_secret):
      raise MStockError("API key and API secret are required.")
    token = fields.get("request_token", "")
    if not token:
      raise NeedsBrowserLogin(self.login_url())
    await self.complete_login(token)

  def check_state(self, state: str) -> bool:
    return bool(self._state) and secrets.compare_digest(state or "",
                                                        self._state)

  async def complete_login(self, request_token: str) -> None:
    """Exchanges the one-time request token for today's access token."""
    data = _check(await self._client.post("/session/token", data={
        "api_key": self.api_key, "request_token": request_token,
        "checksum": checksum(self.api_key, request_token, self.api_secret)}))
    self.access_token = data["access_token"]
    self.user_id = data.get("user_id", "")
    self.token_date = date.today()
    self._state = ""
    log.info("Zerodha login OK for %s", self.user_id or "user")

  def clock_warning(self) -> str:
    return ""  # no TOTP codes involved on our side

  # ---- instruments ---------------------------------------------------------
  async def instruments(self) -> list[Instrument]:
    parts = []
    for exch in ("NFO", "BFO", "NSE"):
      resp = await self._client.get(f"/instruments/{exch}",
                                    headers=self._auth())
      resp.raise_for_status()
      parts.append(resp.text)
    header, *rest = parts[0].splitlines(keepends=True)
    body = "".join(rest) + "".join(
        "".join(p.splitlines(keepends=True)[1:]) for p in parts[1:])
    return parse_kite_instruments(header + body)

  async def fill_ref_prices(self, items: list[Instrument]) -> None:
    """Kite's instrument list has no prices: fetch the nearest future's LTP
    (one request) so the picker can preselect the at-the-money strike."""
    futs = sorted((i for i in items if i.option_type in ("FUT", "EQ")),
                  key=lambda i: (i.option_type != "FUT", i.expiry))
    if not futs or futs[0].ref_price > 0:
      return
    exch = futs[0].key.split(":")[0]
    key = f"{exch}:{futs[0].label.replace(' (cash)', '')}"
    try:
      data = _check(await self._client.get("/quote/ltp", params={"i": key},
                                           headers=self._auth()))
      futs[0].ref_price = float(data.get(key, {}).get("last_price") or 0)
    except Exception as e:  # ATM preselection is a convenience only
      log.info("LTP for %s unavailable: %s", key, e)

  # ---- candles (with OI) ----------------------------------------------------
  async def _candles(self, token: str, day: date, to: str) -> list[Candle]:
    data = _check(await self._client.get(
        f"/instruments/historical/{token}/minute",
        params={"from": f"{day.isoformat()} 09:00:00",
                "to": f"{day.isoformat()} {to}", "oi": "1"},
        headers=self._auth(), timeout=15))
    return [c for c in parse_candles(data) if c.ts.date() == day]

  async def _intraday(self, exch: str, token: str) -> list[Candle]:
    return await self._candles(token, date.today(),
                               datetime.now().strftime("%H:%M:%S"))

  async def _historical_day(self, exch: str, token: str,
                            day: date) -> list[Candle]:
    while self._pending_today:  # today's data first
      await asyncio.sleep(0.2)
    async with self._hist_lock:
      loop = asyncio.get_running_loop()
      wait = self._last_hist + self.HIST_SPACING - loop.time()
      if wait > 0:
        await asyncio.sleep(wait)
      try:
        return await self._candles(token, day, "15:59:00")
      finally:
        self._last_hist = loop.time()

  # ---- live stream ------------------------------------------------------------
  def _stream_url(self) -> str:
    keep = "/@:=+$,;!*'()~-._"
    return (f"{WS}?api_key={quote(self.api_key, safe=keep)}"
            f"&access_token={quote(self.access_token, safe=keep)}")

  async def _stream_hello(self, ws) -> None:
    return None  # Kite authenticates via the URL; no LOGIN message
