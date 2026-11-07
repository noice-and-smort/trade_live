"""Watchlist (several contracts at once) and the shared m.Stock stream."""

import asyncio
import json
import os
import shutil
import struct
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "data")


@pytest.fixture
def home(tmp_path, monkeypatch):
  monkeypatch.setenv("TRADELIVE_HOME", str(tmp_path))
  (tmp_path / "data").mkdir()
  src = [f for f in os.listdir(DATA) if f.endswith(".xlsx")][0]
  for name in ("A.xlsx", "B.xlsx", "C.xlsx"):
    shutil.copy(os.path.join(DATA, src), tmp_path / "data" / name)
  return tmp_path


def test_watchlist_pin_select_and_restore(home):
  from tradelive import server

  async def scenario():
    app = server.App()
    app.feeds["replay"].seconds_per_bar = 0.05
    items = {os.path.basename(i.key): i.key
             for i in await app.instruments("replay")}
    # Pin A and B; view C (not pinned).
    await app.add_entry("replay", items["A.xlsx"], pinned=True)
    await app.add_entry("replay", items["B.xlsx"], pinned=True)
    await app.select("replay", items["C.xlsx"])
    await asyncio.sleep(0.3)
    p = app.build_payload()
    assert [w["label"] for w in p["watchlist"]] == ["Replay: A", "Replay: B"]
    assert p["instrument"] == "Replay: C" and p["rows"]
    assert all(w.get("ts") for w in p["watchlist"])  # summaries computed
    # Switching to A keeps C recording in the background (no OI gaps when
    # he comes back); A and B keep running too.
    await app.select("replay", items["A.xlsx"])
    c_id = "replay|" + items["C.xlsx"]
    assert c_id in app.entries and len(app.entries) == 3
    p = app.build_payload()
    assert p["watchlist"][0]["selected"]
    assert [r["label"] for r in p["recent"]] == ["Replay: C"]
    n_before = len(app.entries[c_id].candles)
    await asyncio.sleep(0.3)
    assert len(app.entries[c_id].candles) > n_before  # still recording
    # Switching back to C is instant: same running entry, rows kept.
    task = app.entries[c_id].task
    await app.select("replay", items["C.xlsx"])
    assert app.entries[c_id].task is task
    assert len(app.build_payload()["rows"]) >= n_before
    await app.select("replay", items["A.xlsx"])
    # Unpin B -> leaves the watchlist but keeps recording as "recent".
    await app.set_pinned("replay|" + items["B.xlsx"], False)
    saved = [w["key"] for w in app.settings.watchlist]
    assert saved == [items["A.xlsx"]]
    assert "Replay: B" in [r["label"] for r in app.build_payload()["recent"]]
    await app.stop()

    # A fresh app restores the watchlist + last view.
    app2 = server.App()
    assert await app2.resume_after_login("replay")
    assert app2.session.instrument.key == items["A.xlsx"]
    assert app2.entries["replay|" + items["A.xlsx"]].pinned
    await app2.stop()

  asyncio.run(scenario())


def test_background_recording_is_capped(home, monkeypatch):
  """At most MAX_TRACKED contracts; the least recently viewed recent one is
  dropped first, never a watchlist one or the one on screen."""
  from tradelive import server
  monkeypatch.setattr(server, "MAX_TRACKED", 2)

  async def scenario():
    app = server.App()
    keys = sorted(i.key for i in await app.instruments("replay"))
    await app.add_entry("replay", keys[0], pinned=True)   # A: watchlist
    await app.select("replay", keys[1])                    # B viewed
    await asyncio.sleep(0.01)
    await app.select("replay", keys[2])                    # C viewed
    ids = set(app.entries)
    assert "replay|" + keys[0] in ids and "replay|" + keys[2] in ids
    assert "replay|" + keys[1] not in ids  # oldest recent dropped
    await app.stop()

  asyncio.run(scenario())


def test_watchlist_max(home, monkeypatch):
  from tradelive import server
  from starlette.exceptions import HTTPException
  monkeypatch.setattr(server, "MAX_WATCH", 2)

  async def scenario():
    app = server.App()
    keys = [i.key for i in await app.instruments("replay")]
    await app.add_entry("replay", keys[0], pinned=True)
    await app.add_entry("replay", keys[1], pinned=True)
    with pytest.raises(HTTPException) as ei:
      await app.add_entry("replay", keys[2], pinned=True)
    assert "at most 2" in ei.value.detail
    await app.stop()

  asyncio.run(scenario())


def test_shared_stream_routes_ticks_to_each_contract(home, monkeypatch):
  """Two contracts -> ONE WebSocket; each gets only its own ticks."""
  from tradelive.feeds import mstock
  from tradelive.models import Instrument
  from tradelive.settings import Settings

  connects, sent = [], []

  def frame(token, ltp, vol, oi):
    pkt = bytearray(184)
    struct.pack_into(">IIIIII", pkt, 0, token, int(ltp * 100), 0, 0, vol, 0)
    struct.pack_into(">I", pkt, 48, oi)
    return struct.pack(">HH", 1, len(pkt)) + bytes(pkt)

  class FakeWS:
    def __init__(self):
      self.q = asyncio.Queue()

    async def send(self, m):
      sent.append(m)
      if isinstance(m, str) and m.startswith("{"):
        msg = json.loads(m)
        if msg["a"] == "mode":
          for tok in msg["v"][1]:
            await self.q.put(frame(tok, tok / 1000, 100 + tok % 7, tok * 10))

    def __aiter__(self):
      return self

    async def __anext__(self):
      return await self.q.get()

  class Conn:
    async def __aenter__(self):
      connects.append(1)
      return FakeWS()

    async def __aexit__(self, *a):
      return False

  monkeypatch.setitem(sys.modules, "websockets",
                      types.SimpleNamespace(connect=lambda *a, **k: Conn()))

  async def scenario():
    f = mstock.MStockFeed(Settings())
    f.api_key, f.access_token = "k", "t"

    async def no_history(exch, token):
      return []
    f._intraday = no_history
    f._warmup = lambda exch, token: no_history(exch, token)  # no network
    got = {}

    def cb(label):
      async def on_update(c):
        if c:
          got[label] = (c[-1].close, c[-1].oi)
      return on_update

    a = Instrument("NFO:1000", "NIFTY", "2099-01-01", 1, "CE", label="A")
    b = Instrument("NFO:2000", "RELIANCE", "2099-01-01", 2, "CE", label="B")
    ta = asyncio.create_task(f.run(a, cb("A")))
    tb = asyncio.create_task(f.run(b, cb("B")))
    for _ in range(40):
      await asyncio.sleep(0.1)
      if len(got) == 2:
        break
    ta.cancel(); tb.cancel()
    await asyncio.gather(ta, tb, return_exceptions=True)
    return got

  got = asyncio.run(scenario())
  assert len(connects) == 1  # one shared connection
  assert got["A"] == (1.0, 10000) and got["B"] == (2.0, 20000)
  assert sum(1 for m in sent if isinstance(m, str) and m.startswith("LOGIN")) == 1


def test_clear_watchlist(home):
  from tradelive import server

  async def scenario():
    app = server.App()
    keys = [i.key for i in await app.instruments("replay")]
    await app.add_entry("replay", keys[0], pinned=True)
    await app.add_entry("replay", keys[1], pinned=True)
    await app.select("replay", keys[2])
    app.settings.watchlist.append({"feed": "mstock", "key": "NFO:1", "meta": {}})
    await app.clear_watchlist()
    p = app.build_payload()
    assert p["watchlist"] == [] and app.settings.watchlist == []
    assert p["instrument"]  # the contract on screen stays open
    await app.stop()

  asyncio.run(scenario())


def test_clear_background_recording(home):
  from tradelive import server

  async def scenario():
    app = server.App()
    keys = sorted(i.key for i in await app.instruments("replay"))
    await app.add_entry("replay", keys[0], pinned=True)
    await app.select("replay", keys[1])
    await app.select("replay", keys[2])  # keys[1] now recording in background
    assert len(app.build_payload()["recent"]) == 1
    await app.clear_recent("replay|" + keys[1])
    assert app.build_payload()["recent"] == []
    await app.select("replay", keys[1])
    await app.select("replay", keys[0])  # keys[1] and keys[2] recent
    await app.clear_recent()
    assert app.build_payload()["recent"] == []
    assert set(app.entries) == {"replay|" + keys[0]}  # watchlist + on screen
    await app.stop()

  asyncio.run(scenario())


def test_no_background_recording_when_history_has_oi(home):
  from tradelive import server
  from tradelive.feeds.base import Feed
  from tradelive.models import Instrument

  class Zerodhaish(Feed):
    name = "kite"
    login_fields = {}
    history_has_oi = True

    async def instruments(self):
      return [Instrument(f"NFO:{i}", "NIFTY", "2099-01-01", i, "CE")
              for i in (1, 2, 3)]

    async def run(self, inst, on_update):
      await asyncio.sleep(3600)

  async def scenario():
    app = server.App()
    app.feeds["kite"] = Zerodhaish()
    await app.add_entry("kite", "NFO:1", pinned=True)
    await app.select("kite", "NFO:2")
    await app.select("kite", "NFO:3")  # NFO:2 is not kept recording
    assert set(app.entries) == {"kite|NFO:1", "kite|NFO:3"}
    assert app.build_payload()["recent"] == []
    await app.select("kite", "NFO:1")  # starred one: keeps running
    assert "kite|NFO:1" in app.entries
    await app.stop()

  asyncio.run(scenario())


def test_two_windows_show_different_contracts(home, monkeypatch):
  from tradelive import server
  from tradelive.feeds.base import Feed
  from tradelive.models import Instrument

  class Zerodhaish(Feed):
    name = "kite"
    login_fields = {}
    history_has_oi = True

    async def instruments(self):
      return [Instrument(f"NFO:{i}", "NIFTY", "2099-01-01", i, "CE",
                         label=f"C{i}") for i in (1, 2, 3)]

    async def run(self, inst, on_update):
      await asyncio.sleep(3600)

  monkeypatch.setattr(server, "MAX_TRACKED", 1)  # stress the protection

  async def scenario():
    app = server.App()
    app.feeds["kite"] = Zerodhaish()
    await app.select("kite", "NFO:1", client="winA")
    await app.select("kite", "NFO:2", client="winB")
    # Both stay tracked although only 1 slot is allowed: both are on screen.
    assert {"kite|NFO:1", "kite|NFO:2"} <= set(app.entries)
    assert app.build_payload(app.view_of("winA"))["instrument"] == "C1"
    assert app.build_payload(app.view_of("winB"))["instrument"] == "C2"
    # Window B switches: its old contract goes (Zerodha), A is untouched.
    await app.select("kite", "NFO:3", client="winB")
    assert "kite|NFO:2" not in app.entries
    assert app.build_payload(app.view_of("winA"))["instrument"] == "C1"
    # A new window without a choice mirrors the last used contract.
    assert app.build_payload(app.view_of("winNew"))["instrument"] == "C3"
    await app.stop()

  asyncio.run(scenario())


def test_two_windows_over_websocket_and_export(home):
  from starlette.testclient import TestClient
  from tradelive import server

  app = server.create_app()
  st = app.state.app
  st.feeds["replay"].seconds_per_bar = 0.05
  with TestClient(app) as c:
    keys = sorted(i["key"] for i in c.get("/api/instruments?feed=replay").json())
    with c.websocket_connect("/ws") as a, c.websocket_connect("/ws") as b:
      a.receive_json(); b.receive_json()
      a.send_text('{"type": "hello", "client": "A"}')
      b.send_text('{"type": "hello", "client": "B"}')
      c.post("/api/select", json={"feed": "replay", "key": keys[0], "client": "A"})
      c.post("/api/select", json={"feed": "replay", "key": keys[1], "client": "B"})

      def latest(ws, want):
        for _ in range(200):
          m = ws.receive_json()
          if m.get("instrument", "").endswith(want):
            return m
        raise AssertionError(want)

      la = latest(a, os.path.basename(keys[0])[:-5])
      lb = latest(b, os.path.basename(keys[1])[:-5])
      assert la["id"] != lb["id"]
    r = c.get("/api/export.xlsx?client=B")
    assert r.status_code == 200
    assert os.path.basename(keys[1])[:-5].replace(" ", "") in \
        r.headers["content-disposition"].replace(" ", "")
