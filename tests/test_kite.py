"""Zerodha (Kite Connect) integration, fully offline (mocked HTTP/stream)."""

import asyncio
import json
import os
import struct
import sys
import types
from datetime import date, datetime
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tradelive.feeds import kite  # noqa: E402
from tradelive.settings import Settings  # noqa: E402

KITE_CSV_HDR = ("instrument_token,exchange_token,tradingsymbol,name,last_price,"
                "expiry,strike,tick_size,lot_size,instrument_type,segment,"
                "exchange\n")
NFO = KITE_CSV_HDR + (
    '12345,48,NIFTY26N0323300CE,"NIFTY",0,2026-11-03,23300,0.05,65,CE,NFO-OPT,NFO\n'
    '12346,49,NIFTY26NOVFUT,"NIFTY",0,2026-11-25,0,0.05,65,FUT,NFO-FUT,NFO\n'
    '22222,87,RELIANCE26NOV1180CE,"RELIANCE",0,2026-11-25,1180,0.05,500,CE,NFO-OPT,NFO\n')
BFO = KITE_CSV_HDR + (
    '33333,1,SENSEX26N0580000CE,"SENSEX",0,2026-11-05,80000,0.05,20,CE,BFO-OPT,BFO\n'
    '44444,2,RELIANCE26NOV1180CE,"RELIANCE",0,2026-11-25,1180,0.05,500,CE,BFO-OPT,BFO\n')
NSE = KITE_CSV_HDR + (
    '738561,2885,RELIANCE,"RELIANCE INDUSTRIES",0,,0,0.05,1,EQ,NSE,NSE\n'
    '256265,1001,NIFTY 50,"NIFTY 50",0,,0,0,0,EQ,INDICES,NSE\n'
    '999,9,SMALLCO,"SMALL CO",0,,0,0.05,1,EQ,NSE,NSE\n')


def make_feed(handler):
  f = kite.KiteFeed(Settings())
  f._client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                base_url=kite.API, headers=kite.HDR)
  return f


def test_parse_instruments_index_stock_cash():
  items = kite.parse_kite_instruments(NFO + BFO[len(KITE_CSV_HDR):] +
                                      NSE[len(KITE_CSV_HDR):])
  by = {(i.symbol, i.option_type, i.key.split(":")[0]): i for i in items}
  assert by[("NIFTY", "CE", "NFO")].kind == "index"
  assert by[("NIFTY", "FUT", "NFO")].key == "NFO:12346"
  assert by[("SENSEX", "CE", "BFO")].kind == "index"
  assert ("RELIANCE", "CE", "BFO") not in by  # BSE stock options skipped
  assert by[("RELIANCE", "EQ", "NSE")].key == "NSE:738561"
  assert not any(i.symbol in ("SMALLCO", "NIFTY 50") for i in items)


def test_login_redirect_and_token_exchange():
  seen = {}

  def handler(req):
    seen["path"], seen["form"] = req.url.path, parse_qs(req.content.decode())
    seen["ver"] = req.headers.get("X-Kite-Version")
    return httpx.Response(200, json={"status": "success", "data": {
        "access_token": "ACC", "user_id": "AB1234"}})

  f = make_feed(handler)
  with pytest.raises(kite.NeedsBrowserLogin) as ei:
    asyncio.run(f.login({"api_key": "key1", "api_secret": "sec1"}))
  q = parse_qs(urlparse(ei.value.url).query)
  assert q["api_key"] == ["key1"] and q["v"] == ["3"]
  state = parse_qs(q["redirect_params"][0])["state"][0]
  assert f.check_state(state) and not f.check_state("forged")
  asyncio.run(f.complete_login("REQTOK"))
  assert seen["path"] == "/session/token" and seen["ver"] == "3"
  assert seen["form"]["checksum"] == [kite.checksum("key1", "REQTOK", "sec1")]
  assert f.is_ready() and f.user_id == "AB1234"
  assert f._auth() == {"Authorization": "token key1:ACC"}


def test_history_includes_oi_and_is_kept(monkeypatch):
  def handler(req):
    assert req.url.params["oi"] == "1"
    return httpx.Response(200, json={"status": "success", "data": {"candles": [
        ["2026-11-03T09:15:00+0530", 100, 101, 99, 100.5, 1000, 5000],
        ["2026-11-03T09:16:00+0530", 100.5, 102, 100, 101, 800, 5100]]}})

  f = make_feed(handler)
  monkeypatch.setattr(kite, "date", types.SimpleNamespace(
      today=lambda: date(2026, 11, 3)))
  bars = asyncio.run(f._candles("12345", date(2026, 11, 3), "15:59:00"))
  assert [c.oi for c in bars] == [5000, 5100]


def test_stream_url_and_no_login_message(monkeypatch):
  sent, urls = [], []

  def frame(token, ltp, vol, oi):
    pkt = bytearray(184)
    struct.pack_into(">IIIIII", pkt, 0, token, int(ltp * 100), 0, 0, vol, 0)
    struct.pack_into(">I", pkt, 48, oi)
    return struct.pack(">HH", 1, len(pkt)) + bytes(pkt)

  class WS:
    def __init__(self):
      self.q = asyncio.Queue()

    async def send(self, m):
      sent.append(m)
      if m.startswith("{") and json.loads(m)["a"] == "mode":
        await self.q.put(frame(12345, 98.6, 1000, 15872025))

    def __aiter__(self):
      return self

    async def __anext__(self):
      return await self.q.get()

  class Conn:
    def __init__(self, url):
      urls.append(url)

    async def __aenter__(self):
      return WS()

    async def __aexit__(self, *a):
      return False

  monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(
      connect=lambda url, **k: Conn(url)))

  async def scenario():
    f = kite.KiteFeed(Settings())
    f.api_key, f.access_token = "key1", "ACC"

    async def none(*a):
      return []
    f._intraday = none
    f._warmup = none
    got = []

    async def on_update(c):
      if c:
        got.append((c[-1].close, c[-1].oi))

    from tradelive.models import Instrument
    t = asyncio.create_task(f.run(
        Instrument("NFO:12345", "NIFTY", "2026-11-03", 23300, "CE"), on_update))
    for _ in range(30):
      await asyncio.sleep(0.1)
      if got:
        break
    t.cancel()
    await asyncio.gather(t, return_exceptions=True)
    return got

  got = asyncio.run(scenario())
  assert urls == ["wss://ws.kite.trade?api_key=key1&access_token=ACC"]
  assert not any(isinstance(m, str) and m.startswith("LOGIN") for m in sent)
  assert got and got[0] == (98.6, 15872025)


def test_secure_store_keeps_both_brokers(tmp_path, monkeypatch):
  monkeypatch.setenv("TRADELIVE_HOME", str(tmp_path))
  from tradelive import secure_store as ss
  ss.save_feed("mstock", {"password": "a"})
  ss.save_feed("kite", {"api_secret": "b"})
  assert ss.load_feed("mstock") == {"password": "a"}
  assert ss.load_feed("kite") == {"api_secret": "b"}
  ss.clear_feed("mstock")
  assert ss.load_feed("mstock") is None and ss.load_feed("kite")
  # Old single-broker files still load.
  ss.clear()
  ss._write(json.dumps({"feed": "mstock", "fields": {"x": 1}}).encode())
  assert ss.load_all() == {"mstock": {"x": 1}}


def test_server_login_handoff_and_callback(tmp_path, monkeypatch):
  monkeypatch.setenv("TRADELIVE_HOME", str(tmp_path))
  (tmp_path / "data").mkdir()
  from starlette.testclient import TestClient
  from tradelive import secure_store, server

  def handler(req):
    p = req.url.path
    if p == "/session/token":
      return httpx.Response(200, json={"status": "success", "data": {
          "access_token": "ACC", "user_id": "AB1"}})
    if p.startswith("/instruments/"):
      exch = p.rsplit("/", 1)[1]
      return httpx.Response(200, text={"NFO": NFO, "BFO": BFO, "NSE": NSE}[exch])
    if p == "/quote/ltp":
      return httpx.Response(200, json={"status": "success", "data": {
          "NFO:NIFTY26NOVFUT": {"last_price": 23312.5}}})
    return httpx.Response(200, json={"status": "success",
                                     "data": {"candles": []}})

  app = server.create_app()
  st = app.state.app
  st.feeds["kite"]._client = httpx.AsyncClient(
      transport=httpx.MockTransport(handler), base_url=kite.API)
  with TestClient(app) as c:
    feeds = {f["name"]: f for f in c.get("/api/state").json()["feeds"]}
    assert "kite" in feeds and "Redirect URL" in feeds["kite"]["login_help"]
    r = c.post("/api/login", json={"feed": "kite", "fields": {
        "api_key": "key1", "api_secret": "sec1", "remember": "1"}}).json()
    assert r["ok"] is False and r["open_url"].startswith(kite.LOGIN_URL)
    assert secure_store.load_feed("kite") == {"api_key": "key1",
                                              "api_secret": "sec1"}
    state = parse_qs(parse_qs(urlparse(r["open_url"]).query)
                     ["redirect_params"][0])["state"][0]
    bad = c.get("/kite/callback?status=success&request_token=T&state=nope")
    assert bad.status_code == 400 and not st.feeds["kite"].is_ready()
    ok = c.get(f"/kite/callback?status=success&request_token=T&state={state}")
    assert ok.status_code == 200 and "Connected to Zerodha" in ok.text
    assert st.feeds["kite"].is_ready()
    kf = {f["name"]: f for f in c.get("/api/state").json()["feeds"]}["kite"]
    assert kf["ready"] is True
    items = c.get("/api/instruments?feed=kite&symbol=NIFTY").json()
    fut = [i for i in items if i["option_type"] == "FUT"][0]
    assert fut["ref_price"] == 23312.5  # ATM strike preselection works
    syms = [s["symbol"] for s in c.get("/api/symbols?feed=kite").json()]
    assert syms[:2] == ["NIFTY", "SENSEX"] and "RELIANCE" in syms
