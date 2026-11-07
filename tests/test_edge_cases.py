"""Edge cases: overnight re-login, port clashes, wrong PC clock."""

import asyncio
import http.server
import os
import socket
import subprocess
import sys
import threading
import time

import httpx
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


@pytest.fixture
def home(tmp_path, monkeypatch):
  monkeypatch.setenv("TRADELIVE_HOME", str(tmp_path))
  (tmp_path / "data").mkdir()
  return tmp_path


def test_relogin_restarts_streams_and_reloads_history(home):
  """Overnight: token expires; after the morning login every watched
  contract must restart (fresh token + today's history), not limp on."""
  from tradelive import server
  from tradelive.feeds.base import Feed
  from tradelive.models import Instrument

  class FakeLive(Feed):
    name = "mstock"
    login_fields = {}

    def __init__(self):
      self.token = "day1"
      self.runs = []

    def is_ready(self):
      return True

    async def instruments(self):
      return [Instrument("NFO:1", "NIFTY", "2099-01-01", 1, "CE"),
              Instrument("NFO:2", "RELIANCE", "2099-01-01", 2, "CE")]

    async def run(self, inst, on_update):
      self.runs.append((inst.key, self.token))
      await asyncio.sleep(3600)

  async def scenario():
    app = server.App()
    f = FakeLive()
    app.feeds["mstock"] = f
    await app.add_entry("mstock", "NFO:1", pinned=True)
    await app.select("mstock", "NFO:2")
    await asyncio.sleep(0.05)
    f.token = "day2"  # new morning login
    await app.restart_feed("mstock")
    await asyncio.sleep(0.05)
    assert sorted(r for r in f.runs if r[1] == "day2") == [
        ("NFO:1", "day2"), ("NFO:2", "day2")]
    assert len(app.entries) == 2  # nothing lost
    await app.stop()

  asyncio.run(scenario())


def test_clock_skew_detection_and_message():
  from tradelive.feeds.mstock import MStockFeed, clock_skew_seconds
  from tradelive.settings import Settings
  from email.utils import formatdate
  resp = httpx.Response(200, headers={"date": formatdate(time.time() - 120,
                                                         usegmt=True)})
  skew = clock_skew_seconds(resp)
  assert 115 < skew < 125  # PC is ~2 minutes fast
  f = MStockFeed(Settings())
  f.clock_skew = skew
  w = f.clock_warning()  # HTTP dates have 1 s resolution: 120 or 121
  assert ("120 seconds fast" in w) or ("121 seconds fast" in w)
  f.clock_skew = 3
  assert f.clock_warning() == ""
  assert clock_skew_seconds(httpx.Response(200)) is None


def test_totp_failure_mentions_clock(monkeypatch):
  from email.utils import formatdate
  from tradelive.feeds.mstock import MStockFeed
  from tradelive.settings import Settings

  def handler(req):
    date = formatdate(time.time() + 300, usegmt=True)  # PC 5 min slow
    if req.url.path.endswith("/connect/login"):
      return httpx.Response(200, headers={"date": date},
                            json={"status": "success", "data": {}})
    return httpx.Response(200, json={"status": "error",
                                     "message": "Invalid TOTP"})

  f = MStockFeed(Settings())
  f._client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                base_url="https://x")
  with pytest.raises(Exception) as ei:
    asyncio.run(f.login({"client_code": "C", "password": "p", "api_key": "k",
                         "totp_secret": "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"}))
  msg = str(ei.value)
  assert "Invalid TOTP" in msg and "slow" in msg and "Set time" in msg


def _free_port():
  with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    return s.getsockname()[1]


def test_port_used_by_other_program_falls_back(home):
  port = _free_port()
  other = http.server.HTTPServer(("127.0.0.1", port),
                                 http.server.SimpleHTTPRequestHandler)
  threading.Thread(target=other.serve_forever, daemon=True).start()
  try:
    env = dict(os.environ, TRADELIVE_HOME=str(home))
    p = subprocess.Popen([sys.executable, "-u", "run.py", "--no-window", "--port",
                          str(port)], cwd=ROOT, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True)
    out = ""
    deadline = time.time() + 20
    while time.time() < deadline:
      line = p.stdout.readline()
      out += line
      if "is running at" in line:
        break
    p.terminate()
    p.wait(timeout=10)
    assert "used by another program" in out
    assert f":{port + 1}/" in out or f":{port + 2}/" in out
  finally:
    other.shutdown()


def test_feed_created_outside_loop_works_inside_another_loop():
  """Regression (Python 3.9 on macOS): objects created before the server's
  event loop starts must not bind to a different loop."""
  from tradelive.feeds.mstock import MStockFeed
  from tradelive.settings import Settings
  f = MStockFeed(Settings())  # created outside any loop, like create_app()

  async def use():
    async with f._hist_lock:
      return True

  assert f._hist_lock_obj is None  # nothing loop-bound created yet
  assert asyncio.run(use())  # created lazily inside the running loop


def test_browser_is_launched_detached(monkeypatch):
  from tradelive import desktop
  calls = []
  monkeypatch.setattr(desktop.subprocess, "Popen",
                      lambda args, **kw: calls.append((args, kw)))
  monkeypatch.setattr(desktop.sys, "platform", "darwin")
  monkeypatch.setattr(desktop.os.path, "exists",
                      lambda p: p.startswith("/Applications/Google Chrome.app"))
  desktop.open_app_window("http://127.0.0.1:8765/")
  args, kw = calls[0]
  assert args[:4] == ["/usr/bin/open", "-n", "-a",
                      "/Applications/Google Chrome.app"]
  assert "--app=http://127.0.0.1:8765/" in args
  assert any(a.startswith("--user-data-dir=") for a in args)
  assert kw.get("start_new_session") is True


def test_stream_url_matches_official_sdk_format():
  from tradelive.feeds.mstock import stream_url
  key = "ay3K9xkB/MAKg@@=="  # API keys contain / @ =
  tok = "eyJhbGciOi.J9-abc_def"
  url = stream_url(key, tok)
  # Same text the official SDK builds: "{root}?ACCESS_TOKEN={t}&API_KEY={k}"
  assert url == f"wss://ws.mstock.trade?ACCESS_TOKEN={tok}&API_KEY={key}"
  assert "%2F" not in url and "%40" not in url
  assert stream_url("a b#c", "t") .endswith("API_KEY=a%20b%23c")  # illegal only


def test_missing_oi_is_explained_on_screen():
  import struct
  from datetime import datetime, timedelta
  from tradelive.feeds.mstock import _Hub, parse_ticks
  h = _Hub()
  h.connected, h.connected_at = True, datetime.now() - timedelta(minutes=2)
  quote = bytearray(44)  # "quote" packet: price + volume, no OI
  struct.pack_into(">IIIIII", quote, 0, 35008, 9860, 0, 0, 1000, 0)
  frame = struct.pack(">HH", 1, 44) + bytes(quote)
  h._note_packets(frame)
  for t in parse_ticks(frame):
    if t.get("oi") is None:
      h.ticks_without_oi += 1
  msg = h.oi_problem()
  assert "WITHOUT open interest" in msg and "44" in msg
  h.ticks_with_oi = 5
  assert h.oi_problem() == ""


def test_stream_failures_back_off_instead_of_hammering(monkeypatch):
  """Regression: with HTTP 502 every contract re-created the connection
  about once a second. Now all share one back-off (5, 10, 20... s)."""
  import types
  from tradelive.feeds import mstock
  from tradelive.models import Instrument
  from tradelive.settings import Settings
  attempts = []

  class Refused:
    async def __aenter__(self):
      attempts.append(1)
      raise RuntimeError("server rejected WebSocket connection: HTTP 502")

    async def __aexit__(self, *a):
      return False

  monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(
      connect=lambda *a, **k: Refused()))

  async def scenario():
    f = mstock.MStockFeed(Settings())
    f.api_key, f.access_token = "k", "t"

    async def none(*a):
      return []
    f._intraday = none
    f._warmup = none
    tasks = [asyncio.create_task(f.run(
        Instrument(f"NFO:{i}", "X", "2099-01-01", i, "CE"), lambda c: none()))
        for i in range(1, 4)]
    await asyncio.sleep(3.0)
    err = f.stream_error()
    for t in tasks:
      t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    return err

  err = asyncio.run(scenario())
  assert len(attempts) == 1  # 3 contracts, 3 s: one attempt (next at 5 s)
  assert "502" in err


def test_502_message_explains_ip():
  from tradelive.server import _stream_msg
  m = _stream_msg("InvalidStatus: server rejected WebSocket connection: HTTP 502")
  assert "Primary IP" in m and "Retrying automatically" in m
  assert _stream_msg("") == ""


def test_closing_last_window_stops_server_and_reload_keeps_it(tmp_path, monkeypatch):
  import time
  from starlette.testclient import TestClient
  from tradelive import server
  monkeypatch.setenv("TRADELIVE_HOME", str(tmp_path))
  (tmp_path / "data").mkdir()
  app = server.create_app()
  st = app.state.app
  st.close_grace_seconds = 0.08
  closed = []
  st.on_all_windows_closed = lambda: closed.append(True)
  with TestClient(app) as c:
    # Two windows open; closing one must not stop the server.
    with c.websocket_connect("/ws") as ws1:
      ws1.receive_json()
      with c.websocket_connect("/ws") as ws2:
        ws2.receive_json()
      time.sleep(0.15)
      assert closed == []
    # All windows closed -> callback fires after the grace period.
    time.sleep(0.15)
    assert closed == [True]


def test_close_app_window_terminates_mac_singleton_lock(tmp_path, monkeypatch):
  import signal
  from tradelive import desktop
  killed = []
  monkeypatch.setattr(desktop.sys, "platform", "darwin")
  monkeypatch.setattr(desktop, "_mac_browser_profile", lambda: str(tmp_path))
  os.symlink("MacBook.local-42424", tmp_path / "SingletonLock")
  monkeypatch.setattr(desktop.os, "kill", lambda pid, sig: killed.append((pid, sig)))
  desktop.close_app_window()
  assert killed == [(42424, signal.SIGTERM)]
