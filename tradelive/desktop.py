"""Desktop conveniences: open as an app window and start at login.

Windows: Edge/Chrome app window; a launcher in the Startup folder.
macOS:   Chrome/Edge/Brave app window (else Safari); a LaunchAgent.
"""

from __future__ import annotations

import contextlib
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import webbrowser

_WIN_STARTUP_NAME = "TradeLive.bat"
_MAC_AGENT_ID = "com.tradelive.app"
_procs: list[subprocess.Popen] = []


def _launch_command() -> list[str]:
  """Command that starts TradeLive (packaged exe, or python + run.py)."""
  if getattr(sys, "frozen", False):
    return [sys.executable]
  run_py = os.path.join(os.path.dirname(os.path.dirname(
      os.path.abspath(__file__))), "run.py")
  return [sys.executable, run_py]


def _browser_candidates() -> list[str]:
  if sys.platform == "win32":
    out = []
    for base in filter(None, (os.environ.get(k, "") for k in (
        "ProgramFiles(x86)", "ProgramFiles", "LocalAppData"))):
      out += [
          os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
          os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
      ]
    return out
  if sys.platform == "darwin":
    apps = ["Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium"]
    roots = ["/Applications", os.path.expanduser("~/Applications")]
    return [os.path.join(r, f"{a}.app", "Contents", "MacOS", a)
            for r in roots for a in apps]
  return [shutil.which(n) or "" for n in ("google-chrome", "chromium",
                                          "microsoft-edge")]


def _spawn_detached(args: list[str]) -> None:
  """Starts a program completely independent of TradeLive, so closing
  TradeLive (Ctrl+C, closing the terminal/console) never closes it."""
  kw: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
              "stderr": subprocess.DEVNULL, "close_fds": True}
  if sys.platform == "win32":
    kw["creationflags"] = (subprocess.DETACHED_PROCESS |  # type: ignore[attr-defined]
                           subprocess.CREATE_NEW_PROCESS_GROUP)  # type: ignore[attr-defined]
  else:
    kw["start_new_session"] = True  # own session: no Ctrl+C / hang-up
  proc = subprocess.Popen(args, **kw)
  if proc is not None:
    _procs.append(proc)


def _mac_browser_profile() -> str:
  return os.path.expanduser("~/Library/Application Support/TradeLive/browser")


def close_app_window() -> None:
  """Closes the TradeLive app window(s) opened by this process."""
  while _procs:
    proc = _procs.pop()
    with contextlib.suppress(Exception):
      proc.terminate()
  if sys.platform == "darwin":
    lock = os.path.join(_mac_browser_profile(), "SingletonLock")
    with contextlib.suppress(OSError, ValueError):
      target = os.readlink(lock)
      pid = int(target.rsplit("-", 1)[-1])
      if pid > 0:
        os.kill(pid, signal.SIGTERM)


def open_app_window(url: str) -> None:
  """Opens the dashboard in a chromeless browser window (looks like a normal
  desktop app). Falls back to the default browser."""
  app_args = [f"--app={url}", "--window-size=1500,950"]
  if sys.platform == "darwin":
    # Separate profile so the app window opens even if the browser is
    # already running with the user's normal profile, and never touches it.
    prof = os.path.expanduser("~/Library/Application Support/TradeLive/browser")
    for exe in _browser_candidates():
      if os.path.exists(exe):
        app_bundle = exe.split("/Contents/MacOS/")[0]
        try:
          # Launch through macOS ("open -n"): a fully independent new
          # instance that is not a child of TradeLive.
          _spawn_detached(["/usr/bin/open", "-n", "-a", app_bundle, "--args",
                           *app_args, f"--user-data-dir={prof}"])
          return
        except OSError:
          continue
    webbrowser.open(url)
    return
  for exe in _browser_candidates():
    if exe and os.path.exists(exe):
      try:
        _spawn_detached([exe, *app_args])
        return
      except OSError:
        continue
  webbrowser.open(url)


# ---------------------------------------------------------------- autostart
def _win_startup_path() -> str:
  return os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                      "Start Menu", "Programs", "Startup", _WIN_STARTUP_NAME)


def _mac_agent_path() -> str:
  return os.path.expanduser(f"~/Library/LaunchAgents/{_MAC_AGENT_ID}.plist")


def autostart_supported() -> bool:
  return sys.platform in ("win32", "darwin")


def autostart_enabled() -> bool:
  if sys.platform == "win32":
    return os.path.exists(_win_startup_path())
  if sys.platform == "darwin":
    return os.path.exists(_mac_agent_path())
  return False


def autostart_target() -> str:
  """The program the auto-start entry currently launches ('' if none)."""
  try:
    if sys.platform == "win32":
      with open(_win_startup_path(), encoding="utf-8") as f:
        for line in f:
          if line.lower().startswith("if not exist"):
            return line.split('"')[1]
    elif sys.platform == "darwin":
      with open(_mac_agent_path(), "rb") as f:
        return plistlib.load(f)["ProgramArguments"][-1]
  except (OSError, IndexError, KeyError, ValueError):
    pass
  return ""


def refresh_autostart() -> bool:
  """If auto-start is on but points to another (older or deleted) copy,
  re-point it to the TradeLive that is running now. Returns True if fixed."""
  if not autostart_supported() or not autostart_enabled():
    return False
  me = _launch_command()[-1]
  if os.path.normcase(autostart_target()) == os.path.normcase(me):
    return False
  set_autostart(True)
  return True


def set_autostart(enabled: bool) -> None:
  if not autostart_supported():
    raise RuntimeError("Auto-start is available on Windows and macOS only.")
  cmd = _launch_command()
  workdir = os.path.dirname(cmd[-1])
  if sys.platform == "win32":
    path = _win_startup_path()
    if enabled:
      quoted = " ".join(f'"{c}"' for c in cmd)
      # Only start if that exact file still exists (no error popup when an
      # old copy was deleted). TradeLive re-points this file to itself on
      # every start, so it always launches the latest version used.
      with open(path, "w", encoding="utf-8") as f:
        f.write(f'@echo off\r\nrem TradeLive auto-start (managed by TradeLive)\r\n'
                f'if not exist "{cmd[-1]}" exit /b 0\r\n'
                f'cd /d "{workdir}"\r\n'
                f'start "TradeLive" /min {quoted}\r\n')
    elif os.path.exists(path):
      os.remove(path)
    return
  # macOS LaunchAgent: runs at login, in the user's session.
  path = _mac_agent_path()
  if enabled:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # The app writes its own rotating tradelive.log; console output from a
    # login item would otherwise grow forever.
    log = "/dev/null"
    with open(path, "wb") as f:
      plistlib.dump({
          "Label": _MAC_AGENT_ID,
          "ProgramArguments": cmd,
          "WorkingDirectory": workdir,
          "RunAtLoad": True,
          "StandardOutPath": log,
          "StandardErrorPath": log,
      }, f)
  elif os.path.exists(path):
    subprocess.run(["launchctl", "unload", path], capture_output=True,
                   check=False)
    os.remove(path)
