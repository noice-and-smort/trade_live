"""Tests for automatic daily login: TOTP, saved credentials, auto-login loop
and rolling to the next weekly expiry."""

import asyncio
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tradelive import totp  # noqa: E402


def test_totp_rfc6238_vector():
  # RFC 6238 test secret "12345678901234567890" (SHA1), T=59s -> 94287082.
  secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
  assert totp.totp(secret, at=59, digits=8) == "94287082"
  assert totp.totp(secret.lower(), at=59) == "287082"
  uri = f"otpauth://totp/mStock:ABC?secret={secret}&issuer=mStock"
  assert totp.totp(uri, at=59) == "287082"


def test_secret_problem_messages():
  assert totp.secret_problem("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ") is None
  assert totp.secret_problem(" gezd gnbv gy3t qojq ") is None
  assert "0, 1, 8 or 9" in totp.secret_problem("482913")
  assert "invalid characters" in totp.secret_problem("ABCD!EFGHIJKLMNOP")
  assert "too short" in totp.secret_problem("ABCDEF")


def test_code_in_secret_box_is_used_as_code(monkeypatch):
  import httpx
  from tradelive.feeds.mstock import MStockFeed
  from tradelive.settings import Settings
  calls = []

  def handler(req):
    calls.append((req.url.path, req.content.decode()))
    if req.url.path.endswith("/connect/login"):
      return httpx.Response(200, json={"status": "success", "data": {}})
    return httpx.Response(200, json={"status": "success",
                                     "data": {"access_token": "T"}})

  f = MStockFeed(Settings())
  f._client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                base_url="https://x")
  asyncio.run(f.login({"client_code": "C", "password": "p", "api_key": "k",
                       "totp_secret": "482913", "code": ""}))
  assert calls[-1][0].endswith("/session/verifytotp")
  assert "totp=482913" in calls[-1][1]
  assert f.is_ready()


@pytest.fixture
def home(tmp_path, monkeypatch):
  monkeypatch.setenv("TRADELIVE_HOME", str(tmp_path))
  (tmp_path / "data").mkdir()
  return tmp_path


def test_secure_store_roundtrip(home):
  from tradelive import secure_store
  assert secure_store.load() is None
  secure_store.save({"feed": "mstock", "fields": {"password": "p@ss"}})
  assert b"p@ss" not in (home / "credentials.dat").read_bytes()
  assert secure_store.load()["fields"]["password"] == "p@ss"
  secure_store.clear()
  assert secure_store.load() is None


def test_auto_login_resumes_and_rolls_expiry(home, monkeypatch):
  from tradelive import secure_store, server
  from tradelive.feeds.base import Feed
  from tradelive.models import Instrument

  today = date.today()
  nxt = (today + timedelta(days=7)).isoformat()

  class FakeFeed(Feed):
    name = "mstock"
    login_fields = {"client_code": "", "password": "", "api_key": "",
                    "totp_secret": "", "code": ""}

    def __init__(self):
      self.logged_in_with = None

    def is_ready(self):
      return self.logged_in_with is not None

    async def login(self, fields):
      self.logged_in_with = dict(fields)

    async def instruments(self):
      return [Instrument("NFO:2", "NIFTY", nxt, 23300, "CE"),
              Instrument("NFO:3", "NIFTY", nxt, 23400, "CE")]

    async def run(self, instrument, on_update):
      await asyncio.sleep(3600)

  monkeypatch.setattr(server, "AUTO_LOGIN_AFTER", server.dtime(0, 0))

  async def scenario():
    app = server.App()
    fake = FakeFeed()
    app.feeds["mstock"] = fake
    # Yesterday he watched an (now expired) 23300 CE.
    app.settings.last_feed = "mstock"
    app.settings.last_instrument = "NFO:1"
    app.settings.last_instrument_meta = {"symbol": "NIFTY", "strike": 23300.0,
                                         "option_type": "CE",
                                         "expiry": "2000-01-01"}
    secure_store.save({"feed": "mstock",
                       "fields": {"client_code": "C1", "password": "x",
                                  "api_key": "k",
                                  "totp_secret": "GEZDGNBVGY3TQOJQ"}})
    await app._auto_login_once()
    assert fake.logged_in_with["totp_secret"] == "GEZDGNBVGY3TQOJQ"
    assert app.session.instrument.key == "NFO:2"  # rolled to next expiry
    assert "automatically" in app.auto_status
    await app.stop()

  asyncio.run(scenario())


def test_no_auto_login_without_secret(home):
  from tradelive import secure_store, server

  async def scenario():
    from tradelive.feeds.mstock import MStockFeed
    app = server.App()
    app.feeds["mstock"] = MStockFeed(app.settings)
    secure_store.save({"feed": "mstock", "fields": {"password": "x"}})
    await app._auto_login_once()  # must not raise or log in
    assert not app.feeds["mstock"].is_ready()

  asyncio.run(scenario())


def test_login_stops_replay_when_no_previous_live_contract(home):
  """Regression: after logging in, the replay must not keep running and look
  like live data. With a previous live contract it resumes that instead."""
  import shutil
  from tradelive import server
  from tradelive.feeds.base import Feed
  from tradelive.models import Instrument

  sample = [f for f in os.listdir(os.path.join(os.path.dirname(__file__), "..",
                                               "data")) if f.endswith(".xlsx")]
  shutil.copy(os.path.join(os.path.dirname(__file__), "..", "data", sample[0]),
              home / "data" / sample[0])

  class FakeLive(Feed):
    name = "mstock"
    login_fields = {"client_code": ""}

    async def instruments(self):
      return [Instrument("NFO:7", "NIFTY", "2099-01-01", 23300, "CE")]

    async def run(self, instrument, on_update):
      await asyncio.sleep(3600)

  async def scenario():
    app = server.App()
    app.feeds["mstock"] = FakeLive()
    items = await app.instruments("replay")
    await app.select("replay", items[0].key)
    assert app.session.feed.name == "replay"
    # First login on this PC: nothing to resume -> replay stops, hint shown.
    assert not await app.resume_after_login("mstock")
    await app.show_idle("Connected to m.Stock. Choose ...")
    assert app.session.feed is None and app.session.task is None
    assert "Watch" in app.build_payload()["notice"] or app.notice
    # He picks a live contract, then looks at a replay file again...
    await app.select("mstock", "NFO:7")
    await app.select("replay", items[0].key)
    # ...next login resumes the live contract, not the replay.
    assert await app.resume_after_login("mstock")
    assert app.session.instrument.key == "NFO:7"
    await app.stop()

  asyncio.run(scenario())



def test_mstock_disabled_by_default_and_can_be_reenabled(home):
  import json
  from tradelive import server
  app = server.App()
  assert "mstock" not in app.feeds and "kite" in app.feeds
  (home / "settings.json").write_text(json.dumps({"disabled_feeds": []}))
  app2 = server.App()
  assert "mstock" in app2.feeds and "kite" in app2.feeds



def test_saved_mstock_login_is_ignored_when_switched_off(home, monkeypatch):
  """Regression: nothing may log in to / connect to m.Stock by default,
  even with an old saved m.Stock login and after 8:35."""
  from tradelive import secure_store, server
  from tradelive.feeds import mstock
  monkeypatch.setattr(server, "AUTO_LOGIN_AFTER", server.dtime(0, 0))
  called = []
  monkeypatch.setattr(mstock.MStockFeed, "login",
                      lambda self, f: called.append(1))
  secure_store.save_feed("mstock", {"client_code": "C", "password": "p",
                                    "api_key": "k",
                                    "totp_secret": "GEZDGNBVGY3TQOJQ"})

  async def scenario():
    app = server.App()
    assert "mstock" not in app.feeds
    await app._auto_login_once()
    await app.resume_after_login("mstock")

  asyncio.run(scenario())
  assert called == []
