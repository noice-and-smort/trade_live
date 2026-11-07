"""m.Stock (Mirae Asset) Type A live feed.

How data is assembled for the watched contract:
  * Today's 1-minute OHLCV so far comes from the REST intraday endpoint.
    (m.Stock candles carry no OI.)
  * Live ticks come from the WebSocket in "full" mode, which includes OI.
    They are folded into 1-minute candles.
  * OI per minute is saved under data/live/, so restarting the app during
    the day keeps the OI history. Start the app before 09:15 to get OI for
    the whole session.

Docs: https://tradingapi.mstock.com/ . Points that could not be verified
without an account are marked VERIFY.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import json
import logging
import os
import struct
import sys
from datetime import date, datetime, time as dtime
from typing import Optional
from urllib.parse import quote

import httpx

from tradelive.candles import CandleBuilder
from tradelive.feeds.base import Feed, OnUpdate
from tradelive.models import Candle, Instrument
from tradelive.settings import Settings, app_dir
from tradelive import totp as totp_mod
from tradelive.totp import totp

log = logging.getLogger("tradelive.mstock")

BASE = "https://api.mstock.trade"
WS_URL = "wss://ws.mstock.trade"
HDR = {"X-Mirae-Version": "1"}
PRICE_DIV = 100.0
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
OI_DIV = 1.0  # VERIFY: the official SDK divides OI by 100; we believe raw is right.
INDEX_NAMES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"}
EXCHANGE_ID = {"NSE": 1, "NFO": 2, "CDS": 3, "BSE": 4, "BFO": 5}


class MStockError(Exception):
  pass


class HistoryError(MStockError):
  """Today's candles couldn't be loaded (e.g. m.Stock maintenance)."""


def clock_skew_seconds(resp: httpx.Response) -> Optional[float]:
  """PC clock minus the server's clock (from the HTTP Date header)."""
  from email.utils import parsedate_to_datetime
  try:
    server = parsedate_to_datetime(resp.headers["date"]).timestamp()
  except (KeyError, TypeError, ValueError):
    return None
  import time
  return time.time() - server


def _check(resp: httpx.Response) -> dict:
  try:
    body = resp.json()
  except ValueError as e:
    raise MStockError(f"HTTP {resp.status_code}: {resp.text[:200]}") from e
  if isinstance(body, list):
    body = body[0] if body else {}
  if not isinstance(body, dict):
    raise MStockError(f"HTTP {resp.status_code}: unexpected reply")
  if resp.status_code >= 400 or body.get("status") != "success":
    raise MStockError(body.get("message") or f"HTTP {resp.status_code}")
  return body.get("data") or {}


def parse_ticks(frame: bytes) -> list[dict]:
  """Parses a binary WebSocket frame (Type A, big-endian)."""
  if len(frame) < 2:
    return []  # heartbeat
  out = []
  count = struct.unpack(">H", frame[0:2])[0]
  pos = 2
  for _ in range(count):
    if pos + 2 > len(frame):
      break
    plen = struct.unpack(">H", frame[pos:pos + 2])[0]
    pkt = frame[pos + 2:pos + 2 + plen]
    pos += 2 + plen

    def u32(a: int) -> int:
      return struct.unpack(">I", pkt[a:a + 4])[0]

    if plen < 8:
      continue
    if plen not in (8, 28, 32, 44, 48, 184, 200):
      log.debug("unknown packet length %d: %s", plen, pkt[:64].hex())
    tick = {"token": u32(0), "ltp": u32(4) / PRICE_DIV}
    if plen >= 44 and plen not in (48,):  # non-index quote / full
      tick.update(volume=u32(16), atp=u32(12) / PRICE_DIV,
                  open=u32(28) / PRICE_DIV,
                  high=u32(32) / PRICE_DIV, low=u32(36) / PRICE_DIV)
    if plen >= 64 and plen not in (48,):
      tick["oi"] = u32(48) / OI_DIV
    out.append(tick)
  return out


def parse_candles(data) -> list[Candle]:
  """Candles from an intraday/historical response, tolerating what m.Stock
  sends before the market opens or when there's nothing: `null`, an empty
  list, a missing key, or the odd malformed row (skipped)."""
  if isinstance(data, dict):
    raw = data.get("candles")
  elif isinstance(data, list):
    raw = data
  else:
    raw = None
  out = []
  for c in raw or []:
    try:
      # "2025-03-13T12:03:00+05" -> IST wall clock (offset ignored).
      ts = datetime.strptime(str(c[0])[:16], "%Y-%m-%dT%H:%M")
      oi = float(c[6]) if len(c) > 6 and c[6] is not None else None
      out.append(Candle(ts, float(c[1]), float(c[2]), float(c[3]),
                        float(c[4]), float(c[5] or 0), oi))
    except (TypeError, ValueError, IndexError):
      log.debug("skipping malformed candle: %r", c)
  out.sort(key=lambda c: c.ts)
  return out


def _f(v: str) -> float:
  try:
    return float(v or 0)
  except ValueError:
    return 0.0


def parse_scrip_master(text: str) -> list[Instrument]:
  """Index + stock F&O contracts (NFO, and BSE index options on BFO), plus
  the NSE cash share of every F&O stock (prices only; cash has no OI)."""
  out: list[Instrument] = []
  fno_stocks: set[str] = set()
  eq_rows: list[dict] = []
  for r in csv.DictReader(io.StringIO(text)):
    exch, seg = r.get("exchange", ""), r.get("segment", "")
    name, typ = r.get("name", ""), r.get("instrument_type", "")
    if exch == "NSE" and seg == "EQ" and typ == "EQ":
      eq_rows.append(r)
      continue
    if exch == "BFO" and name not in INDEX_NAMES:
      continue  # BSE stock options: skip (NSE is where the volume is)
    if exch not in ("NFO", "BFO") or not name:
      continue
    if seg in ("OPTIDX", "OPTSTK") and typ in ("CE", "PE"):
      pass
    elif seg in ("FUTIDX", "FUTSTK"):
      typ = "FUT"
    else:
      continue
    kind = "stock" if seg.endswith("STK") else "index"
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


class OiStore:
  """Keeps the last OI per minute on disk so restarts don't lose history."""

  def __init__(self, token: str, day: Optional[date] = None):
    d = os.path.join(app_dir(), "data", "live")
    os.makedirs(d, exist_ok=True)
    day = day or date.today()
    self.path = os.path.join(d, f"{day.isoformat()}_{token}.json")
    self.oi: dict[str, float] = {}
    try:
      with open(self.path, encoding="utf-8") as f:
        self.oi = json.load(f)
    except (OSError, ValueError):
      pass
    self._dirty = False

  def put(self, minute: datetime, oi: float) -> None:
    k = minute.strftime("%H:%M")
    if self.oi.get(k) != oi:
      self.oi[k] = oi
      self._dirty = True

  def get(self, minute: datetime) -> Optional[float]:
    return self.oi.get(minute.strftime("%H:%M"))

  def flush(self) -> None:
    if self._dirty:
      with open(self.path, "w", encoding="utf-8") as f:
        json.dump(self.oi, f)
      self._dirty = False


class MStockFeed(Feed):
  name = "mstock"
  label = "m.Stock"
  HIST_SPACING = 1.1  # seconds between data requests (m.Stock: 1/second)
  login_fields = {
      "client_code": "m.Stock client code",
      "password": "Password",
      "api_key": "API key (from trade.mstock.com → Trading APIs)",
      "totp_secret": ("TOTP secret key (shown when you 'Enable TOTP' in "
                      "m.Stock) – enables fully automatic daily login"),
      "code": "…or today's 6-digit code (authenticator app / SMS OTP)",
  }
  remember_fields = ("client_code", "api_key")

  def __init__(self, settings: Settings):
    self.settings = settings
    self.api_key = ""
    self.access_token = ""
    self.token_date: Optional[date] = None
    self._otp_pending = False
    self._client = httpx.AsyncClient(base_url=BASE, headers=HDR, timeout=30)
    self._hub = _Hub()
    self.clock_skew: Optional[float] = None
    # Created on first use inside the running event loop: on Python 3.9
    # (macOS's built-in python3) a Lock made here would bind to a different
    # loop ("Future attached to a different loop").
    self._hist_lock_obj: Optional[asyncio.Lock] = None
    self._last_hist = -10.0
    self._pending_today = 0

  @property
  def _hist_lock(self) -> asyncio.Lock:
    if self._hist_lock_obj is None:
      self._hist_lock_obj = asyncio.Lock()
    return self._hist_lock_obj

  # ---- auth ----------------------------------------------------------------
  def is_ready(self) -> bool:
    return bool(self.access_token) and self.token_date == date.today()

  def _auth(self) -> dict:
    return {"Authorization": f"token {self.api_key}:{self.access_token}"}

  async def login(self, fields: dict[str, str]) -> None:
    user, pwd = fields.get("client_code", ""), fields.get("password", "")
    self.api_key = fields.get("api_key", "")
    code = fields.get("code", "").strip()
    secret = fields.get("totp_secret", "").strip()
    if secret.isdigit() and len(secret) in (6, 8) and not code:
      # The 6-digit code was typed into the secret box: use it as the code.
      code, secret = secret, ""
      fields = {**fields, "totp_secret": "", "code": code}
    if secret:
      problem = totp_mod.secret_problem(secret)
      if problem:
        raise MStockError(problem)
    if secret and not code:
      code = totp(secret)  # automatic: no phone needed
      self._otp_pending = False
    if not (user and self.api_key):
      raise MStockError("Client code and API key are required.")
    if self._otp_pending and code:
      # Second step of SMS-OTP login.
      data = _check(await self._client.post("/openapi/typea/session/token",
                                            data={"api_key": self.api_key,
                                                  "request_token": code,
                                                  "checksum": "L"}))
    else:
      if not pwd:
        raise MStockError("Password is required.")
      resp = await self._client.post("/openapi/typea/connect/login",
                                     data={"username": user,
                                           "password": pwd})
      self.clock_skew = clock_skew_seconds(resp)
      _check(resp)
      if not code:
        self._otp_pending = True
        raise MStockError(
            "An OTP has been sent to your phone. Enter it in the 6-digit code "
            "box and click Connect again.")
      # TOTP login (authenticator app enabled in m.Stock settings).
      try:
        data = _check(await self._client.post(
            "/openapi/typea/session/verifytotp",
            data={"api_key": self.api_key, "totp": code}))
      except MStockError as e:
        hint = self.clock_warning()
        raise MStockError(f"{e}. {hint}" if hint else str(e)) from e
    self._otp_pending = False
    self.access_token = data["access_token"]
    self.token_date = date.today()
    log.info("m.Stock login OK for %s (exchanges: %s)", user,
             data.get("exchanges"))

  # ---- live stream hooks (overridden by other brokers) ---------------------
  def _stream_url(self) -> str:
    return stream_url(self.api_key, self.access_token)

  async def _stream_hello(self, ws) -> None:
    await ws.send(f"LOGIN:{self.access_token}")  # m.Stock requires this

  def stream_error(self) -> str:
    """Current live-stream problem ('' when connected or idle)."""
    h = self._hub
    return "" if h.connected or not h.subs else h.error

  def interruption_notice(self) -> str:
    oi = self._hub.oi_problem()
    if oi:
      return oi
    gaps = self._hub.recent_interruptions()
    if not gaps:
      return ""
    parts = [f"{a:%H:%M}–{b:%H:%M}" if b else f"since {a:%H:%M}"
             for a, b in gaps[-3:]]
    return ("Live data was interrupted " + ", ".join(parts) +
            " (computer asleep or internet down?). OI for those minutes is "
            "missing. Tip: set the PC to never sleep while plugged in.")

  def clock_warning(self) -> str:
    """Advice if the PC clock is off (breaks 6-digit codes and bar times)."""
    if self.clock_skew is None or abs(self.clock_skew) < 30:
      return ""
    secs = int(round(self.clock_skew))
    return (f"This computer's clock is {abs(secs)} seconds "
            f"{'fast' if secs > 0 else 'slow'}. Turn on 'Set time "
            "automatically' in Windows Settings > Time & language (Mac: "
            "System Settings > General > Date & Time), then try again.")

  # ---- instruments ---------------------------------------------------------
  async def instruments(self) -> list[Instrument]:
    resp = await self._client.get("/openapi/typea/instruments/scriptmaster",
                                  headers=self._auth())
    resp.raise_for_status()
    return parse_scrip_master(resp.text)

  # ---- data ----------------------------------------------------------------
  async def _intraday(self, exch: str, token: str) -> list[Candle]:
    url = f"/openapi/typea/instruments/intraday/{EXCHANGE_ID[exch]}/{token}/minute"
    data = _check(await self._client.get(url, headers=self._auth()))
    return parse_candles(data)

  async def _historical_day(self, exch: str, token: str,
                            day: date) -> list[Candle]:
    """1-minute candles of one past day (historical endpoint, IST)."""
    url = f"/openapi/typea/instruments/historical/{exch}/{token}/minute"
    params = {"from": f"{day.isoformat()} 09:00:00",
              "to": f"{day.isoformat()} 15:59:00"}
    # Today's data for any contract goes first; warm-ups wait their turn.
    while self._pending_today:
      await asyncio.sleep(0.2)
    async with self._hist_lock:
      loop = asyncio.get_running_loop()
      wait = self._last_hist + self.HIST_SPACING - loop.time()
      if wait > 0:
        await asyncio.sleep(wait)
      try:
        data = _check(await self._client.get(url, params=params,
                                             headers=self._auth(),
                                             timeout=10))
      finally:
        self._last_hist = loop.time()
    return [c for c in parse_candles(data) if c.ts.date() == day]

  async def _warmup(self, exch: str, token: str) -> list[Candle]:
    """Previous trading day's bars (to seed PSAR/OBV like broker charts).
    Cached on disk per day, so restarts don't refetch."""
    today = date.today()
    cache = os.path.join(app_dir(), "data", "live",
                         f"{today.isoformat()}_{token}_warmup.json")
    try:
      with open(cache, encoding="utf-8") as f:
        raw = json.load(f)
      return [Candle(datetime.fromisoformat(r[0]), *r[1:6], r[6], warm=True)
              for r in raw]
    except (OSError, ValueError, TypeError):
      pass
    bars: list[Candle] = []
    for back in range(1, 6):  # skip weekends/holidays, at most 5 days back
      day = date.fromordinal(today.toordinal() - back)
      if day.weekday() >= 5:
        continue
      try:
        bars = await self._historical_day(exch, token, day)
      except Exception as e:
        log.info("warm-up history unavailable (%s): %s", day, e)
        return []
      if bars:
        prev_oi = OiStore(token, day)  # OI we recorded that day, if any
        for c in bars:
          if c.oi is None:
            c.oi = prev_oi.get(c.ts)
          c.warm = True
        break
    with contextlib.suppress(OSError):
      os.makedirs(os.path.dirname(cache), exist_ok=True)
      with open(cache, "w", encoding="utf-8") as f:
        json.dump([[c.ts.isoformat(), c.open, c.high, c.low, c.close,
                    c.volume, c.oi] for c in bars], f)
    return bars

  async def _history(self, exch: str, token: str) -> list[Candle]:
    """Today's candles, spaced >= 1.1 s apart across contracts because the
    data API allows 1 request per second (matters for big watchlists).
    These take priority over previous-day warm-up requests."""
    self._pending_today += 1
    try:
      async with self._hist_lock:
        loop = asyncio.get_running_loop()
        wait = self._last_hist + self.HIST_SPACING - loop.time()
        if wait > 0:
          await asyncio.sleep(wait)
        try:
          return await self._intraday(exch, token)
        finally:
          self._last_hist = loop.time()
    finally:
      self._pending_today -= 1

  async def run(self, instrument: Instrument, on_update: OnUpdate) -> None:
    """Streams one contract. All contracts share ONE WebSocket (the hub)."""
    exch, token = instrument.key.split(":")
    store = OiStore(token)
    try:
      history = await self._history(exch, token)
    except Exception as e:
      log.warning("intraday history failed for %s: %s", instrument.label, e)
      if not _market_hours_now():
        # Nothing live to show either: report it and let the caller retry.
        if isinstance(e, (MStockError, httpx.HTTPError)):
          raise HistoryError(str(e)) from e
        raise  # a bug on our side: show it as such, not as maintenance
      history = []  # during market hours live data still flows
    for c in history:
      if c.oi is None:  # m.Stock history has no OI: use what we recorded
        c.oi = store.get(c.ts)
    warm: list[Candle] = []
    sub = _Sub(instrument.label, store)

    def merged() -> list[Candle]:
      return merge_history_live(warm, history, sub.builder.candles)

    # Show today's bars and start live data right away; the previous-day
    # warm-up (only refines PSAR/OBV) follows in the background.
    await on_update(merged())
    self._hub.register(int(token), sub, self)

    async def load_warmup():
      nonlocal warm
      try:
        warm = await self._warmup(exch, token)
      except Exception as e:  # warm-up is optional
        log.info("warm-up failed for %s: %s", instrument.label, e)
        return
      if warm:
        sub.dirty = True  # recompute with the warm-up on the next refresh

    warm_task = asyncio.create_task(load_warmup())
    try:
      while True:
        await asyncio.sleep(1.0)  # UI refresh at most once a second
        if sub.dirty:
          sub.dirty = False
          store.flush()
          await on_update(merged())
    finally:
      warm_task.cancel()
      self._hub.unregister(int(token), sub)
      store.flush()


def _market_hours_now() -> bool:
  now = datetime.now()
  return now.weekday() < 5 and MARKET_OPEN <= now.time() <= MARKET_CLOSE


def _ws_connect(websockets, url: str):
  """Opens the stream like m.Stock's official SDK (Autobahn): no
  permessage-deflate compression offer, plain user agent."""
  kw = dict(ping_interval=10, ping_timeout=10, max_size=2**22,
            compression=None)
  for extra in ({"user_agent_header": "AutobahnPython/23.1.2"}, {}):
    try:
      return websockets.connect(url, **kw, **extra)
    except TypeError:  # older websockets without that keyword
      continue
  return websockets.connect(url, ping_interval=10, ping_timeout=10)


def stream_url(api_key: str, access_token: str) -> str:
  """Live-stream address in the exact format of m.Stock's official SDK
  (tradingapi_a.mticker, "changed format on 06-02-25"): ACCESS_TOKEN first,
  values inserted as-is. Only characters that are illegal in a URL query
  (space, '#', '&', '%'...) are escaped; '/', '@', '=', '+' stay as they are,
  because the server compares the raw text."""
  keep = "/@:=+$,;!*'()~-._"
  return (f"{WS_URL}?ACCESS_TOKEN={quote(access_token, safe=keep)}"
          f"&API_KEY={quote(api_key, safe=keep)}")


def merge_history_live(warm: list[Candle], history: list[Candle],
                       live: list[Candle]) -> list[Candle]:
  """warm-up + today's history up to the first live minute + live bars.

  If the app started mid-minute, the first live bar only saw part of that
  minute; combine it with m.Stock's candle for the same minute.
  """
  if not live:
    return warm + history
  first = live[0]
  base = [c for c in history if c.ts < first.ts]
  same = next((c for c in history if c.ts == first.ts), None)
  if same is None:
    return warm + base + live
  combined = Candle(first.ts, same.open, max(same.high, first.high),
                    min(same.low, first.low), first.close,
                    same.volume + first.volume,
                    first.oi if first.oi is not None else same.oi,
                    ext_vwap=first.ext_vwap)
  return warm + base + [combined] + live[1:]


class _Sub:
  """One watched contract inside the shared stream."""

  def __init__(self, label: str, store: OiStore):
    self.label = label
    self.store = store
    self.builder = CandleBuilder()
    self.dirty = False
    self.first_tick = True

  def on_tick(self, t: dict) -> None:
    if "volume" not in t:
      return
    if self.first_tick:
      # Handy for checking the OI scale against the m.Stock app.
      log.info("first tick for %s: %s", self.label, t)
      self.first_tick = False
    now = datetime.now()  # the PC clock is IST for users in India
    last = self.builder.candles[-1].ts if self.builder.candles else None
    if last is not None and last.date() != now.date():
      self.builder = CandleBuilder()  # new day: don't keep yesterday growing
    self.builder.on_tick(now, t["ltp"], t["volume"], t.get("oi"), t.get("atp"))
    if t.get("oi") is not None:
      self.store.put(now.replace(second=0, microsecond=0), t["oi"])
    self.dirty = True


class _Hub:
  """A single m.Stock WebSocket shared by every watched contract.

  Contracts register/unregister their token; the hub (re)connects as needed,
  subscribes all tokens in "full" mode and routes ticks to them.
  """

  def __init__(self):
    self.subs: dict[int, set[_Sub]] = {}
    self.task: Optional[asyncio.Task] = None
    self.ws = None
    self.connected = False
    self.error = ""
    self._feed: Optional["MStockFeed"] = None
    # Periods when live data was NOT flowing although contracts were watched
    # (PC asleep, internet down...): [(start, end), ...]; end None = ongoing.
    self.interruptions: list[list] = []
    self.last_msg: Optional[datetime] = None
    # Diagnostics: what m.Stock actually sends (packet sizes, OI or not).
    self.pkt_lengths: dict[int, int] = {}
    self.ticks_with_oi = 0
    self.ticks_without_oi = 0
    self.connected_at: Optional[datetime] = None
    self._sample_logged = False
    self._delay = 5.0          # back-off, shared by all contracts
    self._next_try = 0.0       # loop time before which we must not connect

  def register(self, token: int, sub: _Sub, feed: "MStockFeed") -> None:
    self._feed = feed
    new = token not in self.subs
    self.subs.setdefault(token, set()).add(sub)
    if self.task is None or self.task.done():
      self.error = ""
      self.task = asyncio.create_task(self._loop())
    elif new and self.connected:
      asyncio.create_task(self._send_sub([token]))

  async def _resend_mode(self, delays: list[float]) -> None:
    waited = 0.0
    for d in delays:
      await asyncio.sleep(d - waited)
      waited = d
      if not self.connected or not self.subs:
        return
      if self.ticks_with_oi and not self.ticks_without_oi:
        return  # OI is arriving: nothing to fix
      await self._send({"a": "mode", "v": ["full", list(self.subs)]})
      log.info("re-sent 'full' mode request for %d contract(s)", len(self.subs))

  def _note_packets(self, frame: bytes) -> None:
    if len(frame) < 4:
      return
    count = struct.unpack(">H", frame[0:2])[0]
    pos = 2
    for _ in range(count):
      if pos + 2 > len(frame):
        break
      plen = struct.unpack(">H", frame[pos:pos + 2])[0]
      self.pkt_lengths[plen] = self.pkt_lengths.get(plen, 0) + 1
      if not self._sample_logged and plen >= 8:
        self._sample_logged = True
        log.info("first stream packet: length %d, bytes %s", plen,
                 frame[pos + 2:pos + 2 + min(plen, 72)].hex())
      pos += 2 + plen

  async def _log_stats_later(self, after: float) -> None:
    await asyncio.sleep(after)
    log.info("stream stats: packet lengths %s; ticks with OI %d, without OI "
             "%d", dict(sorted(self.pkt_lengths.items())), self.ticks_with_oi,
             self.ticks_without_oi)

  def oi_problem(self) -> str:
    """Explains a missing-OI situation for the screen ('' if fine)."""
    if not self.connected or not self.connected_at:
      return ""
    if (datetime.now() - self.connected_at).total_seconds() < 60:
      return ""
    if self.ticks_without_oi and not self.ticks_with_oi:
      sizes = ", ".join(str(k) for k in sorted(self.pkt_lengths))
      return (f"{self._feed.label if self._feed else 'The broker'} is "
              "sending live prices but WITHOUT open interest "
              f"(packet size {sizes}), so OI, OI Δ and Signal stay empty. "
              "Please send tradelive.log (lines 'first stream packet' and "
              "'stream stats').")
    return ""

  def _prune_interruptions(self) -> None:
    today = date.today()
    self.interruptions = [i for i in self.interruptions
                          if i[1] is None or i[1].date() == today][-50:]

  def _open_interruption(self) -> None:
    self._prune_interruptions()
    if not self.interruptions or self.interruptions[-1][1] is not None:
      self.interruptions.append([self.last_msg or datetime.now(), None])

  def _close_interruption(self) -> None:
    if self.interruptions and self.interruptions[-1][1] is None:
      self.interruptions[-1][1] = datetime.now()

  def _record_gap(self, start: datetime, end: datetime) -> None:
    self._prune_interruptions()
    self.interruptions.append([start, end])

  def recent_interruptions(self, min_seconds: int = 60) -> list[tuple]:
    """Today's interruptions longer than `min_seconds` during market hours."""
    today = date.today()
    out = []
    for start, end in self.interruptions:
      e = end or datetime.now()
      if start.date() != today or (e - start).total_seconds() < min_seconds:
        continue
      if e.time() < MARKET_OPEN or start.time() > MARKET_CLOSE:
        continue
      out.append((start, end))
    return out

  def unregister(self, token: int, sub: _Sub) -> None:
    subs = self.subs.get(token)
    if subs:
      subs.discard(sub)
      if not subs:
        del self.subs[token]
        if self.connected:
          asyncio.create_task(self._send(
              {"a": "unsubscribe", "v": [token]}))
    if not self.subs and self.task:
      self.task.cancel()
      self.task = None

  async def _send(self, msg: dict) -> None:
    with contextlib.suppress(Exception):
      await self.ws.send(json.dumps(msg))

  async def _send_sub(self, tokens: list[int]) -> None:
    await self._send({"a": "subscribe", "v": tokens})
    await self._send({"a": "mode", "v": ["full", tokens]})

  async def _loop(self) -> None:
    import urllib.request
    import websockets  # imported lazily so replay-only use needs no extra deps
    loop = asyncio.get_running_loop()
    while self.subs:
      wait = self._next_try - loop.time()
      if wait > 0:
        await asyncio.sleep(wait)  # respect back-off even after a restart
        if not self.subs:
          return
      f = self._feed
      url = f._stream_url()
      # Diagnostics only: a 502 here usually comes from a network proxy.
      log.info("connecting live stream %s for %d contract(s) (system "
               "proxies: %s)", url.split("?")[0], len(self.subs),
               urllib.request.getproxies() or "none")
      try:
        async with _ws_connect(websockets, url) as ws:
          self.ws, self.connected, self.error = ws, True, ""
          self._delay = 5.0
          self.connected_at = datetime.now()
          self._close_interruption()
          await f._stream_hello(ws)
          await self._send_sub(list(self.subs))
          # The server may still be processing LOGIN when the first "full
          # mode" request arrives; repeat it (harmless if already applied).
          asyncio.create_task(self._resend_mode([2, 8, 30]))
          asyncio.create_task(self._log_stats_later(45))
          async for msg in ws:
            now = datetime.now()
            if self.last_msg and (now - self.last_msg).total_seconds() > 180:
              # e.g. the PC slept and the socket survived: a silent gap.
              self._record_gap(self.last_msg, now)
            self.last_msg = now
            if isinstance(msg, str):
              log.debug("ws text: %s", msg[:500])
              if '"error"' in msg:
                log.warning("ws: %s", msg)
              continue
            self._note_packets(msg)
            for t in parse_ticks(msg):
              if "volume" in t:
                if t.get("oi") is not None:
                  self.ticks_with_oi += 1
                else:
                  self.ticks_without_oi += 1
              for sub in list(self.subs.get(t["token"], ())):
                sub.on_tick(t)
        self.error = "live stream closed by server"
      except asyncio.CancelledError:
        raise
      except Exception as e:
        self.error = f"{type(e).__name__}: {e}"
        log.warning("live stream error: %s", self.error)
      finally:
        self.ws, self.connected = None, False
        self._open_interruption()
      self._next_try = loop.time() + self._delay
      log.info("live stream: next attempt in %.0f s", self._delay)
      self._delay = min(self._delay * 2, 60.0)  # 5, 10, 20, 40, 60 s
