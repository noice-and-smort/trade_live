"""Parallel use: several windows, each showing its own contract, sharing
one login, one live connection, the watchlist and settings."""

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tradelive import server  # noqa: E402
from tradelive.feeds.base import Feed  # noqa: E402
from tradelive.models import Candle, Instrument  # noqa: E402
from datetime import datetime  # noqa: E402


class FakeKite(Feed):
  """Zerodha-like: history has OI; counts stream starts per contract."""
  name = "kite"
  login_fields = {}
  history_has_oi = True

  def __init__(self):
    self.runs = []

  async def instruments(self):
    await asyncio.sleep(0.01)  # yield, like a real network call
    return [Instrument(f"NFO:{i}", "NIFTY", "2099-01-01", i, "CE",
                       label=f"C{i}") for i in range(1, 8)]

  async def run(self, inst, on_update):
    self.runs.append(inst.key)
    await on_update([Candle(datetime(2026, 11, 5, 9, 15), 1, 1, 1, 1, 1, 1)])
    await asyncio.sleep(3600)


def make_app():
  app = server.App()
  app.feeds["kite"] = FakeKite()
  return app


def shown(app, client):
  return app.build_payload(app.view_of(client))["instrument"]


def run(coro):
  return asyncio.run(coro)


def test_same_contract_in_two_windows_then_one_leaves():
  async def s():
    app = make_app()
    await app.select("kite", "NFO:1", "A")
    await app.select("kite", "NFO:1", "B")
    await asyncio.sleep(0.05)
    assert app.feeds["kite"].runs == ["NFO:1"]  # one stream, shared
    await app.select("kite", "NFO:2", "B")
    assert "kite|NFO:1" in app.entries  # A still shows it: not stopped
    assert shown(app, "A") == "C1" and shown(app, "B") == "C2"
    await app.stop()
  run(s())


def test_leaving_a_contract_shown_elsewhere_does_not_stop_it():
  async def s():
    app = make_app()
    await app.select("kite", "NFO:1", "A")
    await app.select("kite", "NFO:2", "B")
    await app.select("kite", "NFO:1", "B")   # B also on 1 now
    await app.select("kite", "NFO:3", "A")   # A leaves 1 -> B still has it
    assert "kite|NFO:1" in app.entries and shown(app, "B") == "C1"
    assert "kite|NFO:2" not in app.entries   # nobody shows 2 any more
    await app.stop()
  run(s())


def test_unpin_or_stop_all_never_stops_another_windows_contract():
  async def s():
    app = make_app()
    await app.add_entry("kite", "NFO:1", pinned=True)
    await app.select("kite", "NFO:1", "A")
    await app.select("kite", "NFO:2", "B")
    await app.set_pinned("kite|NFO:1", False)       # ✕ in the watchlist
    await app.clear_recent()                        # "Stop all"
    await app.clear_watchlist()                     # "Clear all"
    assert {"kite|NFO:1", "kite|NFO:2"} <= set(app.entries)
    assert shown(app, "A") == "C1" and shown(app, "B") == "C2"
    await app.stop()
  run(s())


def test_closed_window_releases_contract_unless_shown_elsewhere():
  async def s():
    app = make_app()
    await app.select("kite", "NFO:1", "A")
    await app.select("kite", "NFO:2", "B")
    await app.select("kite", "NFO:3", "C")
    await app.select("kite", "NFO:3", "D")
    await app.forget_view("B")             # B closed: 2 is released
    assert "kite|NFO:2" not in app.entries
    await app.forget_view("C")             # C closed but D shows 3: kept
    assert "kite|NFO:3" in app.entries and shown(app, "D") == "C3"
    assert shown(app, "A") == "C1"
    await app.stop()
  run(s())


def test_reload_within_grace_keeps_the_windows_contract():
  async def s():
    app = make_app()
    await app.select("kite", "NFO:1", "A")
    await app.select("kite", "NFO:2", "B")
    app.ws_client[object()] = "B"          # B reconnected (page reload)
    await app.forget_view("B")
    assert shown(app, "B") == "C2" and "kite|NFO:2" in app.entries
    await app.stop()
  run(s())


def test_two_windows_opening_the_same_new_contract_at_once():
  """Regression: concurrent selects created the contract twice and leaked
  the first stream."""
  async def s():
    app = make_app()
    await asyncio.gather(app.select("kite", "NFO:5", "A"),
                         app.select("kite", "NFO:5", "B"),
                         app.add_entry("kite", "NFO:5", pinned=True))
    await asyncio.sleep(0.05)
    assert app.feeds["kite"].runs.count("NFO:5") == 1
    assert sum(1 for k in app.entries if k == "kite|NFO:5") == 1
    await app.stop()
  run(s())


def test_relogin_keeps_each_windows_contract():
  async def s():
    app = make_app()
    await app.select("kite", "NFO:1", "A")
    await app.select("kite", "NFO:2", "B")
    await asyncio.sleep(0.05)
    await app.restart_feed("kite")          # new morning login
    await app.resume_after_login("kite")
    await asyncio.sleep(0.05)
    assert shown(app, "A") == "C1" and shown(app, "B") == "C2"
    assert app.feeds["kite"].runs.count("NFO:1") == 2  # restarted once
    await app.stop()
  run(s())


def test_many_windows_beyond_the_tracking_limit(monkeypatch):
  monkeypatch.setattr(server, "MAX_TRACKED", 2)

  async def s():
    app = make_app()
    for i, w in enumerate("ABCD", start=1):
      await app.select("kite", f"NFO:{i}", w)
    # All four are on screen: none is dropped despite the limit of 2.
    assert all(shown(app, w) == f"C{i}" for i, w in enumerate("ABCD", 1))
    assert len(app.entries) == 4
    await app.stop()
  run(s())


def test_settings_and_watchlist_are_shared_contracts_are_not():
  async def s():
    app = make_app()
    await app.select("kite", "NFO:1", "A")
    await app.select("kite", "NFO:2", "B")
    await app.add_entry("kite", "NFO:3", pinned=True)
    app.settings.timeframe = 5
    pa, pb = (app.build_payload(app.view_of(c)) for c in "AB")
    assert pa["settings"] == pb["settings"] and pa["settings"]["timeframe"] == 5
    assert [w["label"] for w in pa["watchlist"]] == [w["label"] for w in
                                                     pb["watchlist"]] == ["C3"]
    assert pa["id"] != pb["id"]
    # Watchlist highlight follows each window's own contract.
    await app.select("kite", "NFO:3", "B")
    pa, pb = (app.build_payload(app.view_of(c)) for c in "AB")
    assert not pa["watchlist"][0]["selected"] and pb["watchlist"][0]["selected"]
    await app.stop()
  run(s())


def test_duplicated_tab_gets_its_own_id_over_websocket(tmp_path, monkeypatch):
  monkeypatch.setenv("TRADELIVE_HOME", str(tmp_path))
  (tmp_path / "data").mkdir()
  from starlette.testclient import TestClient
  app = server.create_app()
  st = app.state.app
  st.feeds["kite"] = FakeKite()
  with TestClient(app) as c:
    with c.websocket_connect("/ws") as a, c.websocket_connect("/ws") as b:
      a.receive_json(); b.receive_json()
      c.post("/api/select", json={"feed": "kite", "key": "NFO:1", "client": "X"})
      a.send_text(json.dumps({"type": "hello", "client": "X"}))
      b.send_text(json.dumps({"type": "hello", "client": "X"}))  # duplicate

      def until(ws, pred):
        for _ in range(100):
          m = ws.receive_json()
          if pred(m):
            return m
        raise AssertionError("not received")

      new = until(b, lambda m: m.get("type") == "new_id")["client"]
      assert new != "X" and new.startswith("X-")
      # Starts on the same contract, then is independent.
      c.post("/api/select", json={"feed": "kite", "key": "NFO:2", "client": new})
      assert shown(st, "X") == "C1" and shown(st, new) == "C2"


def test_window_that_never_chose_keeps_its_contract():
  """Regression: a window showing an auto-restored contract (or one it never
  picked itself) followed choices made in the OTHER window."""
  async def s():
    app = make_app()
    await app.select("kite", "NFO:1")      # restored after login (no window)
    for ws, cid in ((object(), "A"), (object(), "B")):
      app.ws_client[ws] = cid              # both windows connect
      app.claim_view(cid)
    await app.select("kite", "NFO:2", "B")  # pick only in window B
    assert shown(app, "A") == "C1" and shown(app, "B") == "C2"
    assert "kite|NFO:1" in app.entries      # A's contract keeps running
    await app.stop()
  run(s())


def test_connected_window_without_hello_claim_is_frozen_on_switch():
  async def s():
    app = make_app()
    await app.select("kite", "NFO:1")
    app.ws_client[object()] = "A"          # connected, never claimed
    await app.select("kite", "NFO:3", "B")
    assert shown(app, "A") == "C1" and shown(app, "B") == "C3"
    await app.stop()
  run(s())
