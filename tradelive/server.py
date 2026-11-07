"""Local web server: serves the dashboard and pushes live updates."""

from __future__ import annotations

import asyncio
import html
import json
import contextlib
import logging
import os
import secrets
import sys
from datetime import datetime, time as dtime, timedelta
from typing import Optional

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import (FileResponse, HTMLResponse, JSONResponse,
                                 Response)
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from tradelive import __version__, desktop, indicators, secure_store, totp
from tradelive.candles import resample
from tradelive.export_xlsx import export_xlsx
from tradelive.feeds.base import Feed
from tradelive.feeds.replay import ReplayFeed
from tradelive.models import Candle, Instrument
from tradelive.settings import Settings, app_dir

log = logging.getLogger("tradelive")

FEED_LABELS = {"replay": "Replay", "mstock": "m.Stock", "kite": "Zerodha"}

# m.Stock's morning maintenance ends at 08:30; log in a few minutes after.
AUTO_LOGIN_AFTER = dtime(8, 35)


def market_open(now: Optional[datetime] = None) -> bool:
  """NSE F&O regular session, with a few minutes of slack (local PC time,
  which is IST for users in India). Exchange holidays are not modelled."""
  now = now or datetime.now()
  return now.weekday() < 5 and dtime(9, 0) <= now.time() <= dtime(15, 40)


def _stream_msg(err: str, feed_name: str = "mstock") -> str:
  if not err:
    return ""
  hint = ""
  if "502" in err and feed_name == "mstock":
    hint = (" m.Stock's server is refusing the live stream. Most often the "
            "computer's internet IP is not the 'Primary IP' registered for "
            "your m.Stock API key – run TradeLive on that network, or update "
            "the IP on trade.mstock.com → Trading APIs.")
  return (f"Live connection problem: {err}. Retrying automatically (up to "
          f"once a minute).{hint}")


def _static_dir() -> str:
  # Works both from source and inside the PyInstaller bundle, where the
  # static folder is shipped as tradelive/static next to this module.
  return os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


MAX_WATCH = 20  # m.Stock's per-connection limit is undocumented; stay modest
# Contracts tracked at once (watchlist + recently viewed, recording in the
# background so switching back never shows OI gaps).
MAX_TRACKED = 20


class Entry:
  """One contract being tracked (the detailed view and/or the watchlist)."""

  def __init__(self, feed: Optional[Feed] = None,
               instrument: Optional[Instrument] = None):
    self.feed = feed
    self.instrument = instrument
    self.task: Optional[asyncio.Task] = None
    self.candles: list[Candle] = []
    self.error: str = ""
    self.pinned = False  # in the watchlist (keeps running when not selected)
    self.summary: dict = {}
    self.viewed_at = datetime.now()  # for dropping the oldest recent one
    self.loaded = False  # True once the first data (even empty) arrived
    self.dirty = True

  @property
  def id(self) -> str:
    return f"{self.feed.name}|{self.instrument.key}" if self.feed else ""


class App:

  def __init__(self):
    self.settings = Settings.load()
    data_dir = os.path.join(app_dir(), "data")
    os.makedirs(data_dir, exist_ok=True)
    self.feeds: dict[str, Feed] = {
        "replay": ReplayFeed(data_dir, self.settings.replay_seconds_per_bar),
    }
    off = set(self.settings.disabled_feeds or [])
    with contextlib.suppress(ImportError):
      from tradelive.feeds.kite import KiteFeed
      if "kite" not in off:
        self.feeds["kite"] = KiteFeed(self.settings)
    with contextlib.suppress(ImportError):
      from tradelive.feeds.mstock import MStockFeed
      if "mstock" not in off:
        self.feeds["mstock"] = MStockFeed(self.settings)
    self._browser_login_opened: dict[str, str] = {}  # feed -> ISO date
    self.entries: dict[str, Entry] = {}  # insertion order = watchlist order
    self.selected: str = ""  # contract of the most recently used window
    # Several windows can show different contracts: window id -> entry id.
    self.views: dict[str, str] = {}
    self.ws_client: dict = {}  # websocket -> window id
    self.payload: dict = {"type": "idle"}
    self.clients: set[WebSocket] = set()
    self._instrument_cache: dict[str, list[Instrument]] = {}
    self.notice = ""  # neutral status line shown under the toolbar
    self.auto_status = ""
    self.auto_task: Optional[asyncio.Task] = None
    self._next_auto_try: Optional[datetime] = None
    self._bcast_pending = False
    self.on_all_windows_closed = None
    self.close_grace_seconds: float = 1.5

  async def forget_view(self, client: str) -> None:
    """A window closed: release its contract unless another window shows
    it, it's in the watchlist, or the window came back (reload)."""
    if client in self.ws_client.values():
      return
    eid = self.views.pop(client, None)
    if eid is None:
      return
    e = self.entries.get(eid)
    with contextlib.suppress(Exception):
      if (e is not None and not e.pinned and eid not in self.on_screen()
          and getattr(e.feed, "history_has_oi", False)):
        await self._stop_entry(eid)  # nothing lost: history has OI
      await self._trim()
    await self.broadcast()

  def claim_view(self, client: str) -> None:
    """A window locks onto what it currently shows, so choosing a contract
    in ANOTHER window never changes it."""
    if client and client not in self.views and self.selected:
      self.views[client] = self.selected

  def view_of(self, client: str = "") -> str:
    """Entry id shown in a window (new windows mirror the last used one)."""
    return self.views.get(client) or self.selected

  def on_screen(self) -> set:
    """Entry ids visible in any window (never stopped or trimmed)."""
    return {v for v in self.views.values() if v} | (
        {self.selected} if self.selected else set())

  @property
  def session(self) -> Entry:
    """The contract shown in the detailed view (an empty Entry if none)."""
    return self.entries.get(self.selected) or Entry()

  # ---- resuming (auto-login / restart) -----------------------------------
  async def _resolve(self, feed_name: str, key: str,
                     meta: dict) -> Optional[str]:
    """Returns `key`, or the same symbol/strike/type on the nearest expiry if
    the contract has expired (weekly options)."""
    items = await self.instruments(feed_name)
    if any(i.key == key for i in items):
      return key
    if not meta:
      return None
    today = datetime.now().date().isoformat()
    same = [i for i in items
            if i.symbol == meta.get("symbol") and
            i.option_type == meta.get("option_type") and
            i.strike == meta.get("strike") and i.expiry >= today]
    return min(same, key=lambda i: i.expiry).key if same else None

  async def resume_after_login(self, feed_name: str) -> bool:
    """Restores the watchlist and the last viewed contract for this feed.
    Returns True if something was opened."""
    s = self.settings
    opened = False
    for w in list(s.watchlist):
      if w.get("feed") != feed_name:
        continue
      with contextlib.suppress(Exception):
        key = await self._resolve(feed_name, w.get("key", ""),
                                  w.get("meta") or {})
        if key:
          await self.add_entry(feed_name, key, pinned=True, save=False)
          opened = True
    last = s.last_by_feed.get(feed_name) or (
        {"key": s.last_instrument, "meta": s.last_instrument_meta}
        if s.last_feed == feed_name else {})
    if last.get("key"):
      try:
        key = await self._resolve(feed_name, last["key"],
                                  last.get("meta") or {})
        if key:
          await self.select(feed_name, key)
          return True
      except Exception:  # resuming is best-effort
        log.warning("could not resume last contract", exc_info=True)
    if opened and not self.selected:
      first = next(iter(self.entries.values()))
      await self.select(first.feed.name, first.instrument.key)
    self._save_watchlist()
    return opened

  async def show_idle(self, notice: str) -> None:
    """Clears the detailed view (watchlist keeps running) with a hint."""
    self.selected = ""
    self.views.clear()
    self.notice = notice
    await self.broadcast()

  async def housekeeping_loop(self) -> None:
    """Daily clean-up so disk use stays bounded (runs at start, then every
    6 hours)."""
    from tradelive import housekeeping
    while True:
      with contextlib.suppress(Exception):
        await asyncio.to_thread(housekeeping.run_all)
      await asyncio.sleep(6 * 3600)

  async def auto_login_loop(self) -> None:
    """Logs in by itself every trading morning using the saved login.

    m.Stock runs maintenance until 08:30 and tokens expire at midnight, so we
    try from AUTO_LOGIN_AFTER onwards whenever the feed is not logged in.
    """
    while True:
      try:
        await self._auto_login_once()
      except asyncio.CancelledError:
        raise
      except Exception:  # never let the loop die
        log.exception("auto-login loop")
      await asyncio.sleep(30)

  async def _auto_login_once(self) -> None:
    for feed_name, fields in secure_store.load_all().items():
      feed = self.feeds.get(feed_name)
      if feed is None or feed.is_ready():
        continue
      if getattr(feed, "browser_login", False):
        await self._prompt_browser_login(feed, fields)
      elif fields.get("totp_secret"):
        await self._auto_login_feed(feed, fields)

  async def _prompt_browser_login(self, feed: Feed, fields: dict) -> None:
    """Brokers like Zerodha forbid automated logins: open their login page
    once each morning so it's one step for the user."""
    now = datetime.now()
    if now.time() < AUTO_LOGIN_AFTER or now.weekday() >= 5:
      return
    today = now.date().isoformat()
    if self._browser_login_opened.get(feed.name) == today:
      return
    self._browser_login_opened[feed.name] = today
    feed.api_key = fields.get("api_key", "")
    feed.api_secret = fields.get("api_secret", "")
    url = feed.login_url()
    with contextlib.suppress(Exception):
      import webbrowser
      webbrowser.open(url)
    self.notice = ("Zerodha needs today's login: a Zerodha login page was "
                   "opened in your browser. Log in there and TradeLive "
                   "continues automatically. (Or click Connect broker.)")
    await self.broadcast()

  async def _auto_login_feed(self, feed: Feed, fields: dict) -> None:
    now = datetime.now()
    if now.time() < AUTO_LOGIN_AFTER:
      self.notice = (f"Will log in to {feed.name} automatically at "
                     f"{AUTO_LOGIN_AFTER:%H:%M}.")
      await self.broadcast()
      return
    if self._next_auto_try and now < self._next_auto_try:
      return
    try:
      await feed.login(fields)
    except Exception as e:
      self.auto_status = f"Automatic login failed at {now:%H:%M}: {e}"
      self.notice = self.auto_status + " Retrying in 5 minutes."
      self._next_auto_try = now + timedelta(minutes=5)
      await self.broadcast()
      return
    self._next_auto_try = None
    self.auto_status = f"Logged in automatically at {now:%H:%M}"
    self.notice = ""
    await self.after_login(feed.name, auto=True)

  async def after_login(self, feed_name: str, auto: bool = False) -> None:
    """Common steps after any successful login."""
    self._instrument_cache.pop(feed_name, None)
    await self.restart_feed(feed_name)
    label = FEED_LABELS.get(feed_name, feed_name)
    if not await self.resume_after_login(feed_name):
      await self.show_idle(
          f"{'Logged in to' if auto else 'Connected to'} {label}. Choose "
          "Symbol, Expiry, Strike and CE/PE above, then click Watch.")
      return
    await self.broadcast()

  # ---- computation & broadcast -------------------------------------------
  def engine_cfg(self) -> indicators.EngineConfig:
    return indicators.EngineConfig(window=self.settings.window,
                                   top_volumes=self.settings.top_volumes)

  def _rows(self, e: Entry) -> list[dict]:
    bars = resample(e.candles, self.settings.timeframe)
    return indicators.compute(bars, self.engine_cfg())

  def _summary(self, e: Entry, rows: Optional[list[dict]] = None) -> dict:
    """One watchlist row (cached until the contract gets new data)."""
    if not e.dirty and e.summary:
      return e.summary
    rows = self._rows(e) if rows is None else rows
    s = indicators.summarize(rows, self.engine_cfg()) if rows else {}
    chg = None
    if rows:
      day = rows[-1]["ts"][:10]
      prev = [r for r in rows if r["ts"][:10] < day]
      base = prev[-1]["close"] if prev else next(
          r["open"] for r in rows if r["ts"][:10] == day)
      if base:
        chg = (rows[-1]["close"] - base) / base * 100
    e.summary = {
        "id": e.id, "label": e.instrument.label, "feed": e.feed.name,
        "pinned": e.pinned, "error": e.error, "chg_pct": chg,
        "loaded": e.loaded,
        "oi_seen": any(r["oi"] is not None for r in rows), **s}
    e.dirty = False
    return e.summary

  def build_payload(self, view: Optional[str] = None) -> dict:
    vid = self.selected if view is None else view
    e = self.entries.get(vid) or Entry()
    rows = self._rows(e) if e.feed else []
    cfg = self.engine_cfg()
    if e.feed:
      e.dirty = True  # we have fresh rows anyway
      self._summary(e, rows)
    notice = self.notice
    stream_err = (e.feed.stream_error() if e.feed and
                  hasattr(e.feed, "stream_error") else "")
    if stream_err and not market_open() and not notice:
      notice = ("Live stream is closed outside market hours (Mon–Fri "
                "9:15–15:30). Showing today's data; TradeLive will reconnect "
                "automatically.")
    if not notice and e.feed and hasattr(e.feed, "interruption_notice"):
      notice = e.feed.interruption_notice()
    if (not notice and rows and e.feed and e.feed.name != "replay" and
        all(r["oi"] is None for r in rows)):
      if getattr(e.feed, "history_has_oi", False):
        notice = ("No open interest for this contract (shares/cash have "
                  "none; for options and futures it appears once trading "
                  "starts).")
      else:
        notice = (f"Prices are from {e.feed.label}'s history, which has no "
                  "open interest. OI, OI Δ, Net flow and Signal fill in from "
                  "live data – keep TradeLive running from before 9:15.")
    watch = []
    for x in self.entries.values():
      if x.pinned:
        w = dict(self._summary(x))
        w["selected"] = x.id == e.id
        watch.append(w)
    return {
        "type": "snapshot",
        "id": e.id,
        "instrument": e.instrument.label if e.instrument else "",
        "feed": e.feed.name if e.feed else "",
        "pinned": e.pinned,
        "loaded": e.loaded,
        "settings": {
            "window": self.settings.window,
            "top_volumes": self.settings.top_volumes,
            "timeframe": self.settings.timeframe,
        },
        "summary": indicators.summarize(rows, cfg),
        "rows": rows,
        "watchlist": watch,
        "recent": [{"id": x.id, "label": x.instrument.label}
                   for x in sorted(self.entries.values(),
                                   key=lambda x: x.viewed_at, reverse=True)
                   if not x.pinned and x.id != e.id],
        "max_watch": MAX_WATCH,
        "feeds_ready": {n: f.is_ready() for n, f in self.feeds.items()},
        "notice": notice,
        "error": e.error or (_stream_msg(stream_err, e.feed.name if e.feed
                                         else "") if market_open() else ""),
    }

  async def broadcast(self) -> None:
    self.payload = self.build_payload()
    cache = {self.selected: self.payload}
    dead = []
    for ws in list(self.clients):
      vid = self.view_of(self.ws_client.get(ws, ""))
      if vid not in cache:
        cache[vid] = self.build_payload(vid)
      try:
        await ws.send_json(cache[vid])
      except Exception:  # client went away
        dead.append(ws)
    for ws in dead:
      self.clients.discard(ws)
      self.ws_client.pop(ws, None)

  def request_broadcast(self) -> None:
    """Coalesces bursts of updates (many contracts) into ~3 pushes/second."""
    if self._bcast_pending:
      return
    self._bcast_pending = True

    async def later():
      await asyncio.sleep(0.3)
      self._bcast_pending = False
      await self.broadcast()

    asyncio.create_task(later())

  def mark_all_dirty(self) -> None:
    for e in self.entries.values():
      e.dirty = True

  # ---- contracts ----------------------------------------------------------
  async def instruments(self, feed_name: str) -> list[Instrument]:
    feed = self.feeds[feed_name]
    if feed_name == "replay" or feed_name not in self._instrument_cache:
      self._instrument_cache[feed_name] = await feed.instruments()
    return self._instrument_cache[feed_name]

  async def add_entry(self, feed_name: str, key: str, pinned: bool = False,
                      save: bool = True) -> Entry:
    """Starts tracking a contract (no-op if already tracked)."""
    feed = self.feeds.get(feed_name)
    if feed is None:
      raise HTTPException(404, f"Unknown feed {feed_name}")
    if not feed.is_ready():
      raise HTTPException(400, "Please connect to the broker first.")
    eid = f"{feed_name}|{key}"
    e = self.entries.get(eid)
    if e is None:
      inst = next((i for i in await self.instruments(feed_name)
                   if i.key == key), None)
      if inst is None:
        raise HTTPException(404, "Instrument not found")
      # Another window may have added it while we awaited the list: reuse
      # that entry instead of starting a second (leaked) stream.
      e = self.entries.get(eid)
      if e is None:
        e = Entry(feed, inst)
        self.entries[eid] = e
        e.task = asyncio.create_task(self._run(e))
    if pinned and not e.pinned:
      if sum(x.pinned for x in self.entries.values()) >= MAX_WATCH:
        raise HTTPException(400, f"The watchlist holds at most {MAX_WATCH} "
                                 "contracts. Remove one first.")
      e.pinned = True
      if save:
        self._save_watchlist()
    return e

  async def select(self, feed_name: str, key: str, client: str = "") -> None:
    """Shows a contract in a window's detailed view (tracking it if needed).
    `client` identifies the window; "" = the default/main window."""
    old = self.view_of(client)
    e = await self.add_entry(feed_name, key)
    for other in set(self.ws_client.values()):
      if other != client:
        self.claim_view(other)
    self.selected = e.id
    if client:
      self.views[client] = e.id
    e.viewed_at = datetime.now()
    prev = self.entries.get(old)
    if (prev is not None and prev.id != e.id and not prev.pinned and
        prev.id not in self.on_screen() and
        getattr(prev.feed, "history_has_oi", False)):
      # e.g. Zerodha: history has OI, so no background recording needed.
      await self._stop_entry(prev.id)
    await self._trim()
    self.notice = ""
    inst = e.instrument
    self.settings.last_feed, self.settings.last_instrument = feed_name, key
    self.settings.last_instrument_meta = dict(inst.__dict__)
    self.settings.last_by_feed[feed_name] = {"key": key,
                                             "meta": dict(inst.__dict__)}
    self.settings.save()
    await self.broadcast()

  async def set_pinned(self, eid: str, pinned: bool) -> None:
    e = self.entries.get(eid)
    if e is None:
      raise HTTPException(404, "Not in the watchlist")
    if pinned:
      await self.add_entry(e.feed.name, e.instrument.key, pinned=True)
    else:
      e.pinned = False
      self._save_watchlist()
      await self._trim()
    await self.broadcast()

  def _save_watchlist(self) -> None:
    keep = [w for w in self.settings.watchlist
            if f"{w.get('feed')}|{w.get('key')}" not in self.entries]
    # Entries we track are authoritative; keep saved items for feeds that
    # aren't connected yet (e.g. live contracts before the morning login).
    tracked = [{"feed": e.feed.name, "key": e.instrument.key,
                "meta": dict(e.instrument.__dict__)}
               for e in self.entries.values() if e.pinned]
    untracked = [w for w in keep if not self.feeds.get(
        w.get("feed", ""), None) or not self.feeds[w["feed"]].is_ready()]
    self.settings.watchlist = (tracked + untracked)[:MAX_WATCH]
    self.settings.save()

  async def clear_watchlist(self) -> None:
    """Empties the watchlist (all brokers, also hidden/disabled ones). The
    contract on screen stays open; the rest keep recording as "recent"."""
    for e in self.entries.values():
      e.pinned = False
      e.dirty = True
    self.settings.watchlist = []
    self.settings.save()
    await self._trim()
    await self.broadcast()

  async def clear_recent(self, eid: str = "") -> None:
    """Stops background recording: one contract (eid) or all of them that
    are neither in the watchlist nor on screen."""
    shown = self.on_screen()
    for x in list(self.entries.values()):
      if x.pinned or x.id in shown:
        continue
      if not eid or x.id == eid:
        await self._stop_entry(x.id)
    await self.broadcast()

  async def restart_feed(self, feed_name: str) -> None:
    """Restarts every tracked contract of a feed (after a new login: the old
    token expired at midnight, so reconnect and reload today's history)."""
    await self.drop_recent(feed_name)
    for e in list(self.entries.values()):
      if e.feed.name == feed_name and e.task:
        e.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
          await e.task
        e.candles, e.error, e.dirty = [], "", True
        e.task = asyncio.create_task(self._run(e))

  async def _trim(self) -> None:
    """Keeps at most MAX_TRACKED contracts: drops the least recently viewed
    ones that are neither in the watchlist nor on screen."""
    while len(self.entries) > MAX_TRACKED:
      shown = self.on_screen()
      spare = [x for x in self.entries.values()
               if not x.pinned and x.id not in shown]
      if not spare:
        break
      await self._stop_entry(min(spare, key=lambda x: x.viewed_at).id)

  async def drop_recent(self, feed_name: str) -> None:
    """Stops background-only contracts of a feed (new day / new login)."""
    for x in list(self.entries.values()):
      if (x.feed.name == feed_name and not x.pinned and
          x.id not in self.on_screen()):
        await self._stop_entry(x.id)

  async def _drop_if_unpinned(self, eid: str) -> None:
    e = self.entries.get(eid)
    if e is not None and not e.pinned:
      await self._stop_entry(eid)

  async def _stop_entry(self, eid: str) -> None:
    e = self.entries.pop(eid, None)
    if e and e.task:
      e.task.cancel()
      with contextlib.suppress(asyncio.CancelledError, Exception):
        await e.task

  async def _run(self, e: Entry) -> None:
    feed, inst = e.feed, e.instrument

    async def on_update(candles: list[Candle]) -> None:
      e.candles = candles
      e.loaded = True
      e.error = ""
      e.dirty = True
      if self.notice.startswith("Live stream is closed") and market_open():
        self.notice = ""
      self.request_broadcast()

    delay = 5
    while True:
      try:
        await feed.run(inst, on_update)
        return
      except asyncio.CancelledError:
        raise
      except Exception as ex:  # keep the app alive; show the error in the UI
        log.warning("feed error (%s %s): %s", feed.name, inst.label, ex,
                    exc_info=True)
        if type(ex).__name__ == "HistoryError":
          maint = (f" {feed.label} does maintenance about 7–9 pm and "
                   "7–8:30 am." if feed.name == "mstock" else "")
          e.error = (f"Couldn't load today's data from {feed.label} ({ex})."
                     f"{maint} Retrying in {min(delay, 60)}s…")
          e.dirty = True
          self.request_broadcast()
          await asyncio.sleep(min(delay, 60))
          delay = min(delay * 2, 60)
          continue
        if feed.name == "replay":
          hint = (" If the file is open in Excel, close it (Excel locks open "
                  "files on Windows).") if isinstance(ex, PermissionError) else ""
          e.error = f"Could not play the replay file: {ex}.{hint}"
        elif not market_open():
          e.error = ""
          self.notice = ("Live stream is closed outside market hours "
                         "(Mon–Fri 9:15–15:30). Showing today's data; "
                         "TradeLive will reconnect automatically.")
          delay = 60
        else:
          e.error = f"Live connection problem: {ex}. Retrying in {delay}s…"
        e.dirty = True
        self.request_broadcast()
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)  # back off: 5, 10, 20, 40, 60 s

  async def stop(self) -> None:
    """Stops everything (shutdown / tests)."""
    for eid in list(self.entries):
      await self._stop_entry(eid)
    self.selected = ""
    self.views.clear()


# ---------------------------------------------------------------- HTTP layer
# Plain Starlette (pure Python, no compiled dependencies) so the app installs
# anywhere without building native code.


async def _json_body(request: Request) -> dict:
  try:
    body = await request.json()
  except ValueError as e:
    raise HTTPException(400, "Invalid JSON body") from e
  if not isinstance(body, dict):
    raise HTTPException(400, "Expected a JSON object")
  return body


def _opt_num(body: dict, key: str, kind):
  v = body.get(key)
  if v is None:
    return None
  try:
    return kind(v)
  except (TypeError, ValueError) as e:
    raise HTTPException(400, f"Invalid value for {key}") from e


def create_app() -> Starlette:
  state = App()
  static = _static_dir()

  async def index(request: Request):
    # Stamp asset URLs with their modification time so browsers (notably the
    # Edge app window on Windows) never run a stale app.js after an update.
    with open(os.path.join(static, "index.html"), encoding="utf-8") as f:
      html = f.read()
    for name in ("app.js", "style.css", "lightweight-charts.js"):
      p = os.path.join(static, name)
      ver = int(os.path.getmtime(p)) if os.path.exists(p) else 0
      html = html.replace(f"/static/{name}\"", f"/static/{name}?v={ver}\"")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

  async def get_state(request: Request):
    return JSONResponse({
        "feeds": [{
            "name": n,
            "ready": f.is_ready(),
            "login_fields": f.login_fields,
            "login_help": getattr(f, "login_help", ""),
            "label": FEED_LABELS.get(n, n),
            "saved": dict(state.settings.broker.get(n, {})),
        } for n, f in state.feeds.items()],
        "settings": {
            "window": state.settings.window,
            "top_volumes": state.settings.top_volumes,
            "timeframe": state.settings.timeframe,
            "replay_seconds_per_bar": state.settings.replay_seconds_per_bar,
            "last_feed": state.settings.last_feed,
            "last_instrument": state.settings.last_instrument,
            "last_by_feed": {
                k: {"key": v.get("key"),
                    "meta": {m: (v.get("meta") or {}).get(m) for m in
                             ("symbol", "expiry", "strike", "option_type")}}
                for k, v in state.settings.last_by_feed.items()},
        },
        "data_dir": os.path.join(app_dir(), "data"),
        "version": __version__,
    })

  async def get_instruments(request: Request):
    feed = request.query_params.get("feed", "")
    symbol = request.query_params.get("symbol", "")
    if feed not in state.feeds:
      raise HTTPException(404, "Unknown feed")
    if not state.feeds[feed].is_ready():
      raise HTTPException(400, "Please connect to the broker first.")
    items = await state.instruments(feed)
    if symbol:
      items = [i for i in items if i.symbol == symbol]
      fill = getattr(state.feeds[feed], "fill_ref_prices", None)
      if fill:
        await fill(items)
    return JSONResponse([i.__dict__ for i in items[:20000]])

  async def get_symbols(request: Request):
    """Distinct underlyings for the picker: indices first, then stocks."""
    feed = request.query_params.get("feed", "")
    if feed not in state.feeds:
      raise HTTPException(404, "Unknown feed")
    if not state.feeds[feed].is_ready():
      raise HTTPException(400, "Please connect to the broker first.")
    seen: dict[str, str] = {}
    for i in await state.instruments(feed):
      seen.setdefault(i.symbol, i.kind)
    order = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"]
    syms = sorted(seen.items(), key=lambda kv: (
        kv[1] != "index", order.index(kv[0]) if kv[0] in order else 99, kv[0]))
    return JSONResponse([{"symbol": s, "kind": k} for s, k in syms])

  async def post_select(request: Request):
    body = await _json_body(request)
    feed, key = str(body.get("feed", "")), str(body.get("key", ""))
    if body.get("id"):
      feed, _, key = str(body["id"]).partition("|")
    await state.select(feed, key, str(body.get("client") or ""))
    return JSONResponse({"ok": True})

  async def post_watchlist(request: Request):
    """{"id": ..., "pinned": bool} or {"feed", "key", "pinned": true}, or
    {"clear": true} to empty the whole watchlist."""
    body = await _json_body(request)
    if body.get("clear"):
      await state.clear_watchlist()
      if body.get("and_recent"):
        await state.clear_recent()
      return JSONResponse({"ok": True})
    if body.get("clear_recent"):
      await state.clear_recent(str(body.get("id") or ""))
      return JSONResponse({"ok": True})
    pinned = bool(body.get("pinned", True))
    eid = str(body.get("id") or "")
    if not eid:
      feed, key = str(body.get("feed", "")), str(body.get("key", ""))
      await state.add_entry(feed, key, pinned=pinned)
      await state.broadcast()
    else:
      await state.set_pinned(eid, pinned)
    return JSONResponse({"ok": True})

  async def post_settings(request: Request):
    body = await _json_body(request)
    s = state.settings
    window = _opt_num(body, "window", int)
    top_n = _opt_num(body, "top_volumes", int)
    if top_n is not None:
      s.top_volumes = max(0, min(top_n, 50))
    timeframe = _opt_num(body, "timeframe", int)
    speed = _opt_num(body, "replay_seconds_per_bar", float)
    if window is not None:
      s.window = max(0, window)
    if timeframe is not None:
      s.timeframe = max(1, timeframe)
    if speed is not None:
      s.replay_seconds_per_bar = max(0.1, speed)
      state.feeds["replay"].seconds_per_bar = s.replay_seconds_per_bar
    s.save()
    state.mark_all_dirty()
    await state.broadcast()
    return JSONResponse({"ok": True})

  async def post_login(request: Request):
    body = await _json_body(request)
    feed_name = str(body.get("feed", ""))
    fields = {str(k): str(v) for k, v in (body.get("fields") or {}).items()}
    feed = state.feeds.get(feed_name)
    if feed is None:
      raise HTTPException(404, "Unknown feed")

    def remember() -> None:
      keep = {k: v for k, v in fields.items()
              if k in getattr(feed, "remember_fields", ())}
      state.settings.broker[feed_name] = keep
      state.settings.save()
      if fields.get("remember") == "1":
        creds = {k: v for k, v in fields.items()
                 if k in feed.login_fields and k != "code"}
        # Never save something that isn't a usable TOTP secret (e.g. a code).
        if creds.get("totp_secret") and totp.secret_problem(
            creds["totp_secret"]):
          creds.pop("totp_secret")
        secure_store.save_feed(feed_name, creds)

    try:
      await feed.login(fields)
    except Exception as e:
      url = getattr(e, "url", None)
      if url:  # e.g. Zerodha: continue on the broker's own login page
        remember()
        return JSONResponse({"ok": False, "open_url": url,
                             "message": str(e)})
      raise HTTPException(400, f"Login failed: {e}") from e
    remember()
    state.notice = ""
    await state.after_login(feed_name)
    clock = getattr(feed, "clock_warning", lambda: "")()
    if clock:
      state.notice = clock
      await state.broadcast()
    return JSONResponse({"ok": True})

  async def kite_callback(request: Request):
    """Zerodha redirects here after the user logs in on their site."""
    feed = state.feeds.get("kite")
    q = request.query_params
    log.info("Zerodha callback received (TradeLive %s, pid %d): status=%s, "
             "request_token=%s, state %s", __version__, os.getpid(),
             q.get("status"), "present" if q.get("request_token") else
             "MISSING", "matches" if feed and feed.check_state(
                 q.get("state", "")) else "DOES NOT MATCH")
    page = ("<!doctype html><meta charset=utf-8><title>TradeLive</title>"
            "<body style='font:18px Segoe UI,Arial;padding:40px'>{}</body>")
    if feed is None or q.get("status") != "success" or not q.get(
        "request_token"):
      return HTMLResponse(page.format(
          "❌ Zerodha login was not completed. Close this tab and click "
          "<b>Connect broker</b> in TradeLive to try again."), 400)
    if not feed.check_state(q.get("state", "")):
      return HTMLResponse(page.format(
          "❌ This login link is old or wasn't started by this TradeLive "
          "window. If more than one TradeLive is running, close all of them "
          "(Task Manager → TradeLive.exe → End task), start TradeLive once, "
          "and click <b>Connect broker</b> again."), 400)
    try:
      await feed.complete_login(q["request_token"])
    except Exception as e:
      return HTMLResponse(page.format(
          f"❌ Zerodha login failed: {html.escape(str(e))}. Check the API "
          "secret in TradeLive (Connect broker)."), 400)
    state.notice = ""
    await state.after_login("kite")
    log.info("Zerodha login completed via callback; broadcasting")
    return HTMLResponse(page.format(
        "✅ Connected to Zerodha. You can close this tab and return to "
        "TradeLive.<script>setTimeout(()=>window.close(),1500)</script>"))

  async def credentials(request: Request):
    feed_name = request.query_params.get("feed", "mstock")
    if request.method == "DELETE":
      secure_store.clear_feed(feed_name)
      state.auto_status = ""
      return JSONResponse({"ok": True})
    fields = secure_store.load_feed(feed_name)
    feed = state.feeds.get(feed_name)
    return JSONResponse({
        "saved": bool(fields), "encrypted": secure_store.ENCRYPTED,
        "auto": bool(fields and (fields.get("totp_secret") or getattr(
            feed, "browser_login", False))),
        "browser_login": bool(getattr(feed, "browser_login", False)),
        "status": state.auto_status})

  async def autostart(request: Request):
    if request.method == "POST":
      body = await _json_body(request)
      try:
        desktop.set_autostart(bool(body.get("enabled")))
      except Exception as e:
        raise HTTPException(400, str(e)) from e
    return JSONResponse({"supported": desktop.autostart_supported(),
                         "enabled": desktop.autostart_enabled(), "ok": True})

  async def get_export(request: Request):
    client = request.query_params.get("client", "")
    p = state.build_payload(state.view_of(client))
    if not p.get("rows"):
      raise HTTPException(400, "Nothing to export yet.")
    title = p.get("instrument") or "TradeLive"
    data = export_xlsx(p["rows"], title, state.settings.window,
                       state.settings.top_volumes)
    fname = "".join(ch if ch.isalnum() or ch in " -_." else "_"
                    for ch in title) + ".xlsx"
    return Response(
        data,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})

  async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    state.clients.add(ws)
    client = ""
    try:
      await ws.send_json(state.payload)
      while True:
        msg = await ws.receive_text()  # keepalive pings / window hello
        if msg.startswith("{"):
          with contextlib.suppress(ValueError):
            data = json.loads(msg)
            if data.get("type") == "hello":
              client = str(data.get("client") or "")[:64]
              if client and any(c == client for w, c in
                                state.ws_client.items() if w is not ws):
                # Same id already open in another window (duplicated tab):
                # give this window its own id, starting on the same contract.
                new = client + "-" + secrets.token_hex(3)
                if client in state.views:
                  state.views[new] = state.views[client]
                await ws.send_json({"type": "new_id", "client": new})
                client = new
              state.ws_client[ws] = client
              state.claim_view(client)
              await ws.send_json(state.build_payload(state.view_of(client)))
    except WebSocketDisconnect:
      pass
    finally:
      state.clients.discard(ws)
      state.ws_client.pop(ws, None)
      if client:
        asyncio.create_task(forget_view_later(client))
      if not state.clients and state.on_all_windows_closed:
        asyncio.create_task(check_all_closed())

  async def check_all_closed():
    """If the last TradeLive window was closed (not just reloaded), stop."""
    await asyncio.sleep(state.close_grace_seconds)
    if not state.clients and state.on_all_windows_closed:
      log.info("all windows closed; stopping TradeLive")
      state.on_all_windows_closed()

  async def forget_view_later(client: str, grace: float = 30.0):
    """A closed window releases its contract (a reload within `grace`
    seconds keeps it)."""
    await asyncio.sleep(grace)
    await state.forget_view(client)

  async def http_error(request: Request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

  @contextlib.asynccontextmanager
  async def lifespan(app):
    with contextlib.suppress(Exception):
      if desktop.refresh_autostart():
        log.info("auto-start now points to this TradeLive (%s)", __version__)
    log.info("brokers enabled: %s; switched off: %s",
             ", ".join(FEED_LABELS.get(n, n) for n in state.feeds
                       if n != "replay") or "none",
             ", ".join(FEED_LABELS.get(n, n) for n in
                       state.settings.disabled_feeds) or "none")
    state.auto_task = asyncio.create_task(state.auto_login_loop())
    state.house_task = asyncio.create_task(state.housekeeping_loop())
    s = state.settings
    # Replay needs no login: restore its watchlist entries right away.
    with contextlib.suppress(Exception):
      if any(w.get("feed") == "replay" for w in s.watchlist):
        await state.resume_after_login("replay")
    if s.last_feed == "replay" and not state.selected:
      with contextlib.suppress(Exception):
        items = await state.instruments("replay")
        key = s.last_instrument if any(
            i.key == s.last_instrument for i in items) else (
                items[0].key if items else "")
        if key:
          await state.select("replay", key)
    try:
      yield
    finally:
      for ws in list(state.clients):
        with contextlib.suppress(Exception):
          await ws.send_json({"type": "shutdown"})
          await ws.close()
      state.auto_task.cancel()
      state.house_task.cancel()
      await state.stop()

  routes = [
      Route("/", index),
      Route("/api/state", get_state),
      Route("/api/instruments", get_instruments),
      Route("/api/symbols", get_symbols),
      Route("/api/select", post_select, methods=["POST"]),
      Route("/api/watchlist", post_watchlist, methods=["POST"]),
      Route("/api/settings", post_settings, methods=["POST"]),
      Route("/api/login", post_login, methods=["POST"]),
      Route("/kite/callback", kite_callback),
      Route("/api/credentials", credentials, methods=["GET", "DELETE"]),
      Route("/api/autostart", autostart, methods=["GET", "POST"]),
      Route("/api/export.xlsx", get_export),
      WebSocketRoute("/ws", ws_endpoint),
      Mount("/static", StaticFiles(directory=static), name="static"),
  ]
  api = Starlette(routes=routes, lifespan=lifespan,
                  exception_handlers={HTTPException: http_error})
  api.state.app = state
  return api
