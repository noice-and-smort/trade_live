"""Persistent user settings (stored next to the app, never uploaded)."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field


def app_dir() -> str:
  """Folder that holds settings and the `data` folder.

  For the packaged .exe this is the folder containing the .exe, so the user
  can see and back up everything in one place.
  """
  if os.environ.get("TRADELIVE_HOME"):  # used by tests
    return os.environ["TRADELIVE_HOME"]
  if getattr(sys, "frozen", False) and sys.platform == "darwin":
    # Inside a .app bundle the exe folder is read-only-ish; use the standard
    # per-user location instead.
    d = os.path.expanduser("~/Library/Application Support/TradeLive")
    os.makedirs(d, exist_ok=True)
    return d
  if getattr(sys, "frozen", False):
    return os.path.dirname(sys.executable)
  return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass
class Settings:
  window: int = 0  # rows counted for net OI flow / highlights; 0 = all rows
  top_volumes: int = 5  # how many of the biggest volumes to highlight
  timeframe: int = 1  # minutes
  replay_seconds_per_bar: float = 2.0
  last_feed: str = "replay"
  last_instrument: str = ""
  # symbol/expiry/strike/type of the last contract, used to roll to the next
  # expiry automatically when the weekly contract has expired.
  last_instrument_meta: dict = field(default_factory=dict)
  # Last contract per feed ({"mstock": {"key": ..., "meta": {...}}}), so
  # looking at a replay file doesn't make him lose his live contract.
  last_by_feed: dict = field(default_factory=dict)
  # Brokers hidden from the app. m.Stock is off while its live stream
  # (ws.mstock.trade) is down; set to [] in settings.json to re-enable.
  disabled_feeds: list = field(default_factory=lambda: ["mstock"])
  # Watchlist: [{"feed": "mstock", "key": "NFO:53940", "meta": {...}}, ...]
  watchlist: list = field(default_factory=list)
  # Per-broker saved login fields (e.g. client code, API key). Secrets that
  # change daily (OTP) are never saved.
  broker: dict = field(default_factory=dict)

  @staticmethod
  def path() -> str:
    return os.path.join(app_dir(), "settings.json")

  @classmethod
  def load(cls) -> "Settings":
    try:
      with open(cls.path(), encoding="utf-8") as f:
        raw = json.load(f)
      known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
      return cls(**known)
    except (OSError, ValueError, TypeError):
      return cls()

  def save(self) -> None:
    tmp = self.path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
      json.dump(asdict(self), f, indent=2)
    os.replace(tmp, self.path())
