"""PSAR warm-up, mid-minute merge, interruption notices, exchange VWAP."""

import asyncio
import glob
import os
import sys
from datetime import date, datetime, timedelta

import httpx
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tradelive import indicators  # noqa: E402
from tradelive.candles import CandleBuilder  # noqa: E402
from tradelive.models import Candle  # noqa: E402
from tradelive.xlsx_reader import read_xlsx  # noqa: E402


def sample():
  return read_xlsx(glob.glob(os.path.join(ROOT, "data", "*sample*.xlsx"))[0])[1]


def strip_ext(cs):
  for c in cs:
    c.ext_psar = c.ext_obv = c.ext_vwap = None
  return cs


def test_warmup_rows_hidden_but_seed_psar():
  cs = strip_ext(sample())
  today = [c for c in cs if c.ts.day == 2]
  prev = [c for c in cs if c.ts.day != 2]
  for c in prev:
    c.warm = True
  cfg = indicators.EngineConfig()
  with_warm = indicators.compute(prev + today, cfg)
  cold = indicators.compute(strip_ext(sample())[len(prev):], cfg)
  assert len(with_warm) == len(today)  # previous day not shown/counted
  assert with_warm[0]["ts"].startswith("2026-11-02")
  # First bar's ΔOBV uses the previous close (like the sheet), cold has none.
  assert with_warm[0]["d_obv"] is not None and cold[0]["d_obv"] is None
  # Net flow over "All" counts only today's rows.
  exp = sum(r["oi_up"] or 0 for r in with_warm) - sum(
      r["oi_down"] or 0 for r in with_warm)
  assert with_warm[-1]["net_flow"] == exp


def test_exchange_vwap_used_per_bar():
  cs = strip_ext(sample()[-5:])
  cs[-1].ext_vwap = 123.45  # live bar with exchange ATP
  rows = indicators.compute(cs, indicators.EngineConfig())
  assert rows[-1]["vwap"] == 123.45
  assert rows[-2]["vwap"] != 123.45 and rows[-2]["vwap"] > 0  # own calc


def test_builder_takes_sane_atp_only():
  b = CandleBuilder()
  t = datetime(2026, 11, 3, 9, 15, 5)
  b.on_tick(t, 100, 1000, 5, atp=99.5)
  assert b.candles[-1].ext_vwap == 99.5
  b.on_tick(t + timedelta(seconds=1), 101, 1100, 5, atp=0)
  assert b.candles[-1].ext_vwap == 99.5  # zero ignored
  b.on_tick(t + timedelta(minutes=1), 102, 1200, 5, atp=9999)
  assert b.candles[-1].ext_vwap is None  # absurd value ignored


def test_mid_minute_merge():
  from tradelive.feeds.mstock import merge_history_live
  m = datetime(2026, 11, 3, 10, 0)
  hist = [Candle(m - timedelta(minutes=1), 10, 11, 9, 10.5, 500, None),
          Candle(m, 10.5, 12, 10, 11, 300, None)]  # partial current minute
  live = [Candle(m, 11, 11.5, 9.5, 11.2, 200, 7000),
          Candle(m + timedelta(minutes=1), 11.2, 11.3, 11, 11.1, 50, 7100)]
  out = merge_history_live([], hist, live)
  assert [c.ts for c in out] == [hist[0].ts, m, live[1].ts]
  c = out[1]
  assert (c.open, c.high, c.low, c.close, c.volume, c.oi) == (
      10.5, 12, 9.5, 11.2, 500, 7000)
  assert merge_history_live([], hist, []) == hist


def test_interruption_notice(monkeypatch):
  from tradelive.feeds import mstock
  from tradelive.settings import Settings
  f = mstock.MStockFeed(Settings())
  d = date.today()
  f._hub.interruptions = [
      [datetime.combine(d, datetime.min.time()).replace(hour=10, minute=2),
       datetime.combine(d, datetime.min.time()).replace(hour=10, minute=15)],
      [datetime.combine(d, datetime.min.time()).replace(hour=11, minute=0),
       datetime.combine(d, datetime.min.time()).replace(hour=11, minute=0,
                                                        second=20)],  # short
      [datetime.combine(d, datetime.min.time()).replace(hour=20),
       datetime.combine(d, datetime.min.time()).replace(hour=21)],  # closed
  ]
  msg = f.interruption_notice()
  assert "10:02–10:15" in msg and "11:00" not in msg and "20:00" not in msg
  f._hub.interruptions = []
  assert f.interruption_notice() == ""


def test_warmup_fetch_skips_weekend_and_caches(tmp_path, monkeypatch):
  monkeypatch.setenv("TRADELIVE_HOME", str(tmp_path))
  from tradelive.feeds import mstock
  from tradelive.settings import Settings
  calls = []

  def handler(req):
    day = req.url.params["from"][:10]
    calls.append(day)
    # Only the most recent weekday before today has data.
    want = None
    d = date.today()
    for back in range(1, 6):
      x = date.fromordinal(d.toordinal() - back)
      if x.weekday() < 5:
        want = x.isoformat()
        break
    candles = [[f"{day}T15:29:00+05", 1, 2, 0.5, 1.5, 10]] if day == want else []
    return httpx.Response(200, json={"status": "success",
                                     "data": {"candles": candles}})

  f = mstock.MStockFeed(Settings())
  f._client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                base_url="https://x")
  f._last_hist = -100
  bars = asyncio.run(f._warmup("NFO", "123"))
  assert len(bars) == 1 and bars[0].warm
  assert all(date.fromisoformat(c).weekday() < 5 for c in calls)
  n = len(calls)
  bars2 = asyncio.run(f._warmup("NFO", "123"))  # from the disk cache
  assert len(calls) == n and bars2[0].warm and bars2[0].close == 1.5


def test_today_shows_immediately_even_if_warmup_hangs(monkeypatch):
  """Regression: a slow/hanging previous-day request must not keep a
  contract on "loading…"."""
  import types
  from tradelive.feeds import mstock
  from tradelive.models import Instrument
  from tradelive.settings import Settings

  class Conn:  # a live stream that connects but sends nothing (after hours)
    async def __aenter__(self):
      return self

    async def __aexit__(self, *a):
      return False

    async def send(self, m):
      pass

    def __aiter__(self):
      return self

    async def __anext__(self):
      await asyncio.sleep(3600)

  monkeypatch.setitem(sys.modules, "websockets",
                      types.SimpleNamespace(connect=lambda *a, **k: Conn()))

  async def scenario():
    f = mstock.MStockFeed(Settings())
    f.api_key, f.access_token = "k", "t"
    today = datetime.now().replace(second=0, microsecond=0)

    async def intraday(exch, token):
      return [Candle(today, 1, 2, 0.5, 1.5, 10, None)]

    async def hanging_warmup(exch, token):
      await asyncio.sleep(3600)

    f._intraday = intraday
    f._warmup = hanging_warmup
    got = []

    async def on_update(c):
      got.append(len(c))

    t = asyncio.create_task(f.run(
        Instrument("NFO:5", "RELIANCE", "2099-01-01", 1, "CE"), on_update))
    for _ in range(30):
      await asyncio.sleep(0.05)
      if got:
        break
    t.cancel()
    await asyncio.gather(t, return_exceptions=True)
    return got

  got = asyncio.run(scenario())
  assert got and got[0] == 1  # today's bar shown within ~1.5 s


def test_history_failure_after_hours_is_reported_not_loading(monkeypatch):
  """During m.Stock maintenance (after hours) a new contract must show a
  clear error and retry, not "loading…" / "no trades"."""
  from tradelive.feeds import mstock
  from tradelive.models import Instrument
  from tradelive.settings import Settings
  monkeypatch.setattr(mstock, "_market_hours_now", lambda: False)

  async def scenario():
    f = mstock.MStockFeed(Settings())

    async def failing(exch, token):
      raise mstock.MStockError("HTTP 503")
    f._intraday = failing
    with pytest.raises(mstock.HistoryError):
      await f.run(Instrument("NFO:5", "RELIANCE", "2099-01-01", 1, "CE"),
                  lambda c: None)

  asyncio.run(scenario())


def test_server_shows_history_error_message(tmp_path, monkeypatch):
  monkeypatch.setenv("TRADELIVE_HOME", str(tmp_path))
  (tmp_path / "data").mkdir()
  from tradelive import server
  from tradelive.feeds.base import Feed
  from tradelive.feeds.mstock import HistoryError
  from tradelive.models import Instrument

  class Failing(Feed):
    name = "mstock"
    login_fields = {}

    async def instruments(self):
      return [Instrument("NFO:5", "RELIANCE", "2099-01-01", 1, "CE")]

    async def run(self, inst, on_update):
      raise HistoryError("HTTP 503")

  async def scenario():
    app = server.App()
    app.feeds["mstock"] = Failing()
    await app.select("mstock", "NFO:5")
    await asyncio.sleep(0.2)
    p = app.build_payload()
    await app.stop()
    return p

  p = asyncio.run(scenario())
  assert "Couldn't load today's data" in p["error"] and "maintenance" in p["error"]


def test_premarket_null_candles_are_fine():
  """Before 9:15 m.Stock answers {"candles": null}; that's "no bars yet",
  not an error (regression: 'NoneType' object is not iterable)."""
  from tradelive.feeds.mstock import MStockFeed, parse_candles
  from tradelive.settings import Settings
  assert parse_candles({"candles": None}) == []
  assert parse_candles({}) == [] and parse_candles(None) == []
  assert parse_candles([]) == []
  rows = parse_candles({"candles": [["2026-11-03T09:15:00+05", 1, 2, 0.5, 1.5, 10],
                                    ["bad"], None,
                                    ["2026-11-03T09:16:00+05", 1, 2, 1, 1.2, None]]})
  assert [r.ts.minute for r in rows] == [15, 16] and rows[1].volume == 0

  def handler(req):
    return httpx.Response(200, json={"status": "success",
                                     "data": {"candles": None}})

  f = MStockFeed(Settings())
  f._client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                base_url="https://x")
  assert asyncio.run(f._intraday("NFO", "1")) == []
