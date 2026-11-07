"""Test safety: never touch the real app folder or the internet."""

import socket

import pytest


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
  # Default app folder for every test (tests may override with their own).
  monkeypatch.setenv("TRADELIVE_HOME", str(tmp_path / "home"))
  (tmp_path / "home" / "data").mkdir(parents=True, exist_ok=True)
  # Block network access except to this machine.
  real_connect = socket.socket.connect

  def guarded(self, address):
    host = address[0] if isinstance(address, tuple) else address
    if host not in ("127.0.0.1", "localhost", "::1"):
      raise RuntimeError(f"tests must not use the network (tried {host})")
    return real_connect(self, address)

  monkeypatch.setattr(socket.socket, "connect", guarded)
