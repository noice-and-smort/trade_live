"""Long-running use must not grow memory or disk without bound."""

import os
import sys
import time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def test_old_recordings_deleted_recent_kept(tmp_path, monkeypatch):
  monkeypatch.setenv("TRADELIVE_HOME", str(tmp_path))
  from tradelive import housekeeping
  live = tmp_path / "data" / "live"
  live.mkdir(parents=True)
  old, new = live / "2026-10-01_123.json", live / "2026-11-02_123.json"
  old.write_text("{}"); new.write_text("{}")
  t = time.time() - 30 * 86400
  os.utime(old, (t, t))
  assert housekeeping.clean_recorded_data(keep_days=10) == 1
  assert not old.exists() and new.exists()
  assert housekeeping.clean_stale_pyinstaller_dirs() == 0  # not frozen


def test_live_bars_reset_on_new_day():
  from tradelive.feeds.mstock import OiStore, _Sub
  sub = _Sub("X", OiStore.__new__(OiStore))
  sub.store.oi, sub.store._dirty, sub.store.path = {}, False, os.devnull
  d1 = datetime(2026, 11, 2, 15, 29)
  sub.builder.on_tick(d1, 100, 10, 1)
  assert len(sub.builder.candles) == 1

  class Later(datetime):
    @classmethod
    def now(cls, tz=None):
      return datetime(2026, 11, 3, 9, 15, 2)

  import tradelive.feeds.mstock as m
  orig = m.datetime
  m.datetime = Later
  try:
    sub.on_tick({"ltp": 101, "volume": 5, "oi": 2})
  finally:
    m.datetime = orig
  assert [c.ts.date() for c in sub.builder.candles] == [datetime(2026, 11, 3).date()]


def test_interruptions_pruned_to_today():
  from tradelive.feeds.mstock import _Hub
  h = _Hub()
  y = datetime.now() - timedelta(days=1)
  h.interruptions = [[y, y + timedelta(minutes=5)]] * 100
  h._record_gap(datetime.now() - timedelta(minutes=2), datetime.now())
  assert len(h.interruptions) == 1


def test_contract_list_strings_are_shared():
  from tradelive.feeds.mstock import parse_scrip_master
  hdr = ("instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,"
         "strike,tick_size,lot_size,instrument_type,segment,exchange\n")
  rows = "".join(f"{i},{i},NIFTYX{i}CE,NIFTY,1,2026-11-03,{i},,65,CE,OPTIDX,NFO\n"
                 for i in range(1, 50))
  items = parse_scrip_master(hdr + rows)
  assert all(x.symbol is items[0].symbol and x.expiry is items[0].expiry
             for x in items)
