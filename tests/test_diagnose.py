"""The --diagnose report runs end to end and never prints secrets."""

import asyncio
import io
import os
import sys
import types
from contextlib import redirect_stdout

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SECRETS = {"client_code": "C123", "password": "Pa55word!",
           "api_key": "ay3K9xkB/MAKg@@==", "totp_secret": "GEZDGNBVGY3TQOJQ"}
TOKEN = "eyJSECRETJWT.payload.sig"


def test_diagnose_report(monkeypatch, tmp_path):
  import json
  home = os.environ["TRADELIVE_HOME"]
  with open(os.path.join(home, "settings.json"), "w") as f:
    json.dump({"disabled_feeds": []}, f)  # m.Stock explicitly re-enabled
  from tradelive import diagnose, secure_store
  from tradelive.feeds import mstock
  monkeypatch.setattr(secure_store, "load",
                      lambda: {"feed": "mstock", "fields": dict(SECRETS)})
  csv = ("instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,"
         "strike,tick_size,lot_size,instrument_type,segment,exchange\n"
         "111,111,NIFTY26NOVFUT,NIFTY,23000,2026-11-25,0,,65,XX,FUTIDX,NFO\n")

  def handler(req):
    p = req.url.path
    if p.endswith("/scriptmaster"):
      return httpx.Response(200, text=csv)
    if p.endswith("/connect/login"):
      return httpx.Response(200, json={"status": "success", "data": {}})
    if p.endswith("/verifytotp"):
      return httpx.Response(200, json={"status": "success",
                                       "data": {"access_token": TOKEN}})
    return httpx.Response(200, json={"status": "success", "data": {
        "candles": [["2026-11-03T09:15:00+05", 1, 2, 0.5, 1.5, 10]]}})

  orig = mstock.MStockFeed.__init__

  def init(self, s):
    orig(self, s)
    self._client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                     base_url="https://x")
  monkeypatch.setattr(mstock.MStockFeed, "__init__", init)

  class Refused(Exception):
    def __init__(self):
      super().__init__(f"server rejected WebSocket connection: HTTP 502 {TOKEN}")
      self.response = types.SimpleNamespace(
          status_code=502, headers={"Server": "awselb/2.0"},
          body=b"<html>502 Bad Gateway</html>")

  class Conn:
    async def __aenter__(self):
      raise Refused()

    async def __aexit__(self, *a):
      return False

  fake = types.SimpleNamespace(connect=lambda *a, **k: Conn(), __version__="x")
  monkeypatch.setitem(sys.modules, "websockets", fake)
  buf = io.StringIO()
  with redirect_stdout(buf):
    asyncio.run(diagnose.run())
  out = buf.getvalue()
  assert "[1] REST login ... OK" in out and "[3] today's candles ... OK (1 bars" in out
  assert out.count("FAILED") >= 5 and "awselb" in out and "502" in out
  for secret in (SECRETS["password"], SECRETS["api_key"],
                 SECRETS["totp_secret"], TOKEN):
    assert secret not in out, secret


def test_diagnose_never_contacts_mstock_when_switched_off(monkeypatch):
  from tradelive import diagnose
  from tradelive.feeds import mstock
  monkeypatch.setattr(mstock.MStockFeed, "login",
                      lambda *a, **k: (_ for _ in ()).throw(AssertionError(
                          "must not contact m.Stock")))
  buf = io.StringIO()
  with redirect_stdout(buf):
    asyncio.run(diagnose.run())
  assert "switched off" in buf.getvalue()
