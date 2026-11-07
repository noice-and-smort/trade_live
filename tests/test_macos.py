"""macOS support: Keychain-backed credentials and LaunchAgent autostart,
tested with the platform and the `security` tool simulated."""

import importlib
import os
import plistlib
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_mac_keychain_backend(monkeypatch):
  monkeypatch.setattr(sys, "platform", "darwin")
  monkeypatch.delenv("TRADELIVE_HOME", raising=False)
  import tradelive.secure_store as ss
  ss = importlib.reload(ss)
  assert ss.BACKEND == "macos-keychain" and ss.ENCRYPTED

  vault = {}

  def fake_security(*args, check=True):
    cmd = args[0]
    if cmd == "add-generic-password":
      vault["v"] = args[args.index("-w") + 1]
      return ""
    if cmd == "find-generic-password":
      if "v" not in vault:
        raise OSError("not found")
      return vault["v"] + "\n"
    if cmd == "delete-generic-password":
      vault.pop("v", None)
      return ""
    raise AssertionError(cmd)

  monkeypatch.setattr(ss, "_security", fake_security)
  assert ss.load() is None
  ss.save({"feed": "mstock", "fields": {"password": "p@ss"}})
  assert "p@ss" not in vault["v"]  # hex-encoded in the keychain item
  assert ss.load()["fields"]["password"] == "p@ss"
  ss.clear()
  assert ss.load() is None
  monkeypatch.undo()
  importlib.reload(ss)


def test_mac_launch_agent(monkeypatch, tmp_path):
  from tradelive import desktop
  monkeypatch.setattr(sys, "platform", "darwin")
  monkeypatch.setenv("HOME", str(tmp_path))
  monkeypatch.setattr(desktop.subprocess, "run", lambda *a, **k: None)
  assert desktop.autostart_supported()
  desktop.set_autostart(True)
  path = tmp_path / "Library" / "LaunchAgents" / "com.tradelive.app.plist"
  plist = plistlib.loads(path.read_bytes())
  assert plist["RunAtLoad"] is True
  assert plist["ProgramArguments"][-1].endswith("run.py")
  assert desktop.autostart_enabled()
  desktop.set_autostart(False)
  assert not path.exists()


def test_autostart_repoints_to_running_copy(monkeypatch, tmp_path):
  """Auto-start always launches the copy that ran last (never an old or
  deleted one)."""
  import plistlib
  from tradelive import desktop
  monkeypatch.setattr(sys, "platform", "darwin")
  monkeypatch.setenv("HOME", str(tmp_path))
  monkeypatch.setattr(desktop.subprocess, "run", lambda *a, **k: None)
  agent = tmp_path / "Library" / "LaunchAgents" / "com.tradelive.app.plist"
  agent.parent.mkdir(parents=True)
  with open(agent, "wb") as f:  # left behind by an old, deleted copy
    plistlib.dump({"Label": "com.tradelive.app",
                   "ProgramArguments": ["/old/python", "/old/TradeLive/run.py"],
                   "RunAtLoad": True}, f)
  assert desktop.refresh_autostart() is True
  args = plistlib.loads(agent.read_bytes())["ProgramArguments"]
  assert args[-1].endswith("run.py") and not args[-1].startswith("/old/")
  assert desktop.refresh_autostart() is False  # already correct


def test_windows_launcher_checks_file_exists(monkeypatch, tmp_path):
  from tradelive import desktop
  monkeypatch.setattr(sys, "platform", "win32")
  monkeypatch.setenv("APPDATA", str(tmp_path))
  startup = tmp_path / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
  startup.mkdir(parents=True)
  monkeypatch.setattr(desktop, "_launch_command",
                      lambda: [r"C:\TradeLive\TradeLive.exe"])
  desktop.set_autostart(True)
  bat = (startup / "TradeLive.bat").read_text()
  assert r'if not exist "C:\TradeLive\TradeLive.exe" exit /b 0' in bat
  assert desktop.autostart_target() == r"C:\TradeLive\TradeLive.exe"
  # An old copy in another folder gets replaced by the running one.
  (startup / "TradeLive.bat").write_text(
      '@echo off\r\nif not exist "C:\\Old\\TradeLive.exe" exit /b 0\r\n')
  assert desktop.refresh_autostart() is True
  assert desktop.autostart_target() == r"C:\TradeLive\TradeLive.exe"
