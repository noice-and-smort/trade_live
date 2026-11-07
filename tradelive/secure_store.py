"""Saved broker login, protected by the operating system.

* Windows: `credentials.dat` encrypted with DPAPI (the mechanism browsers use
  for saved passwords). Only the same Windows user on the same PC can decrypt
  it; a copied file is useless elsewhere.
* macOS: stored in the user's login Keychain (service "TradeLive"), visible
  in the Keychain Access app. Nothing is written to disk by us.
* Other systems (development only): `credentials.dat` base64-encoded, i.e.
  NOT encrypted. `ENCRYPTED` is False so the UI can warn.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import subprocess
import sys
from typing import Optional

from tradelive.settings import app_dir

_FILE = "credentials.dat"
_KC_SERVICE = "TradeLive"
_KC_ACCOUNT = "broker-login"


def _path() -> str:
  return os.path.join(app_dir(), _FILE)


# ---------------------------------------------------------------- Windows
if sys.platform == "win32":
  from ctypes import wintypes

  class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]

  def _to_blob(data: bytes):
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

  def _call(fn, data: bytes) -> bytes:
    blob_in, _keep = _to_blob(data)
    blob_out = _Blob()
    # CRYPTPROTECT_UI_FORBIDDEN = 0x1
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0x1,
              ctypes.byref(blob_out)):
      raise ctypes.WinError()
    try:
      return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
      ctypes.windll.kernel32.LocalFree(blob_out.pbData)

  def _protect(data: bytes) -> bytes:
    return _call(ctypes.windll.crypt32.CryptProtectData, data)

  def _unprotect(data: bytes) -> bytes:
    return _call(ctypes.windll.crypt32.CryptUnprotectData, data)

  ENCRYPTED = True
  BACKEND = "windows-dpapi"

# ---------------------------------------------------------------- macOS
elif sys.platform == "darwin" and not os.environ.get("TRADELIVE_HOME"):
  ENCRYPTED = True
  BACKEND = "macos-keychain"

  def _security(*args: str, check: bool = True) -> str:
    r = subprocess.run(["/usr/bin/security", *args], capture_output=True,
                       text=True, check=False)
    if check and r.returncode != 0:
      raise OSError(r.stderr.strip() or f"security exited {r.returncode}")
    return r.stdout

# ---------------------------------------------------------------- fallback
else:

  def _protect(data: bytes) -> bytes:
    return base64.b64encode(data)

  def _unprotect(data: bytes) -> bytes:
    return base64.b64decode(data)

  ENCRYPTED = False
  BACKEND = "plain-file"


def _write(raw: bytes) -> None:
  if BACKEND == "macos-keychain":
    # Hex keeps the value shell/quoting-safe. -U updates an existing item.
    _security("add-generic-password", "-U", "-s", _KC_SERVICE, "-a",
              _KC_ACCOUNT, "-l", "TradeLive broker login", "-w", raw.hex())
    return
  tmp = _path() + ".tmp"
  with open(tmp, "wb") as f:
    f.write(_protect(raw))
  os.replace(tmp, _path())


def _read() -> Optional[dict]:
  try:
    if BACKEND == "macos-keychain":
      out = _security("find-generic-password", "-s", _KC_SERVICE, "-a",
                      _KC_ACCOUNT, "-w")
      return json.loads(bytes.fromhex(out.strip()).decode("utf-8"))
    with open(_path(), "rb") as f:
      return json.loads(_unprotect(f.read()).decode("utf-8"))
  except (OSError, ValueError):
    return None


def load_all() -> dict:
  """{feed_name: fields} for every broker with a saved login."""
  data = _read() or {}
  if "feed" in data and "fields" in data:  # old single-broker format
    return {data["feed"]: data["fields"]}
  return {k: v for k, v in data.get("feeds", {}).items() if v}


def load_feed(feed: str) -> Optional[dict]:
  return load_all().get(feed)


def save_feed(feed: str, fields: dict) -> None:
  allf = load_all()
  allf[feed] = fields
  _write(json.dumps({"feeds": allf}).encode("utf-8"))


def clear_feed(feed: str) -> None:
  allf = load_all()
  allf.pop(feed, None)
  if allf:
    _write(json.dumps({"feeds": allf}).encode("utf-8"))
  else:
    clear()


# ---- compatibility helpers (single "current" login) ----------------------
def save(creds: dict) -> None:
  save_feed(creds["feed"], creds["fields"])


def load() -> Optional[dict]:
  """First saved login as {"feed", "fields"} (m.Stock preferred)."""
  allf = load_all()
  if not allf:
    return None
  feed = "mstock" if "mstock" in allf else next(iter(allf))
  return {"feed": feed, "fields": allf[feed]}


def clear() -> None:
  if BACKEND == "macos-keychain":
    _security("delete-generic-password", "-s", _KC_SERVICE, "-a", _KC_ACCOUNT,
              check=False)
    return
  if os.path.exists(_path()):
    os.remove(_path())
