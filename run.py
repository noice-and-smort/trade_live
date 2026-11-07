"""Starts TradeLive and opens it in an app window.

Double-click TradeLive.exe (Windows) / TradeLive.command (macOS), or run
`python run.py`. Debug options:
  --debug       verbose logging (incl. raw broker messages)
  --no-window   don't open a window (e.g. when debugging remotely)
  --port N      use another port (default 8765)
Logs go to the console and to tradelive.log in the app folder.
"""

import argparse
import json
import logging
import logging.handlers
import os
import signal
import socket
import sys
import threading
import urllib.request

import uvicorn

from tradelive import __version__
from tradelive.desktop import close_app_window, open_app_window
from tradelive.server import create_app
from tradelive.settings import app_dir

HOST = "127.0.0.1"


def _port_in_use(port: int) -> bool:
  with socket.socket() as s:
    return s.connect_ex((HOST, port)) == 0


def _running_version(url: str) -> str:
  """Version of the TradeLive already listening on `url` ("" if unknown)."""
  try:
    with urllib.request.urlopen(url + "api/state", timeout=3) as r:
      data = json.load(r)
  except Exception:
    return ""  # not TradeLive (some other program)
  if not isinstance(data, dict) or "feeds" not in data:
    return ""
  return data.get("version") or "older"  # TradeLive before versions existed


def _alert(msg: str) -> None:
  print("\n  " + msg.replace("\n", "\n  ") + "\n")
  if sys.platform == "win32":  # the console may close at once: use a dialog
    try:
      import ctypes
      ctypes.windll.user32.MessageBoxW(None, msg, "TradeLive", 0x30)
    except Exception:
      pass


def _setup_logging(debug: bool) -> str:
  path = os.path.join(app_dir(), "tradelive.log")
  fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
  root = logging.getLogger()
  root.setLevel(logging.DEBUG if debug else logging.INFO)
  console = logging.StreamHandler()
  console.setFormatter(fmt)
  root.addHandler(console)
  try:
    fileh = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000,
                                                 backupCount=3,
                                                 encoding="utf-8")
    fileh.setFormatter(fmt)
    root.addHandler(fileh)
  except OSError:
    pass
  return path


def main() -> None:
  ap = argparse.ArgumentParser(description="TradeLive dashboard")
  ap.add_argument("--debug", action="store_true")
  ap.add_argument("--no-window", action="store_true")
  ap.add_argument("--port", type=int, default=8765)
  ap.add_argument("--diagnose", action="store_true",
                  help="test the m.Stock connection and print a report")
  args = ap.parse_args()
  if args.diagnose:
    from tradelive import diagnose
    diagnose.main()
    if sys.platform == "win32":
      input("\nPress Enter to close.")
    return

  url = f"http://{HOST}:{args.port}/"
  if _port_in_use(args.port):
    running = _running_version(url)
    if not running:
      # Some OTHER program uses this port: use the next free one, unless
      # TradeLive is already running on one of those (then reuse it).
      for port in range(args.port + 1, args.port + 20):
        purl = f"http://{HOST}:{port}/"
        if _port_in_use(port):
          v = _running_version(purl)
          if v == __version__:
            if not args.no_window:
              open_app_window(purl)
            return
          if v:
            _alert(f"An older TradeLive ({v}) is still running. Close it "
                   "(Task Manager → TradeLive.exe → End task) and start "
                   "TradeLive again.")
            return
          continue
        print(f"  Port {args.port} is used by another program; "
              f"using {port} instead.")
        args.port, url = port, purl
        break
    elif running != __version__:
      # An older (or unknown) copy owns the port; opening a window would show
      # the OLD screen. Tell the user instead of silently doing that.
      _alert("TradeLive is already running"
             + (f" (older version {running})" if running else "") +
             f".\n\nThis is version {__version__}. Close the other TradeLive "
             "window (or Task Manager → TradeLive.exe → End task) and start "
             "TradeLive again.")
      return
    else:  # the same version is already running: just open another window
      if not args.no_window:
        open_app_window(url)
      return
  log_path = _setup_logging(args.debug)
  logging.getLogger("tradelive").info("TradeLive %s starting", __version__)
  if not args.no_window:
    threading.Timer(1.5, lambda: open_app_window(url)).start()
  print(f"\n  TradeLive {__version__} is running at {url}\n  Log file: {log_path}\n"
        "  Close the TradeLive window or press Ctrl+C here to stop.\n")
  app = create_app()
  srv = uvicorn.Server(uvicorn.Config(
      app, host=HOST, port=args.port,
      log_level="debug" if args.debug else "warning"))
  app.state.app.on_all_windows_closed = lambda: setattr(srv, "should_exit", True)
  if hasattr(signal, "SIGHUP"):
    signal.signal(signal.SIGHUP, lambda *_: setattr(srv, "should_exit", True))
  try:
    srv.run()
  finally:
    if not args.no_window:
      close_app_window()


if __name__ == "__main__":
  main()
