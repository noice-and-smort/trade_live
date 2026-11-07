"""Feed adapter interface.

A feed knows how to list instruments, fetch today's 1-minute history and push
live updates. The server only talks to this interface, so adding a broker
means adding one file.
"""

from __future__ import annotations

import abc
from typing import Awaitable, Callable

from tradelive.models import Candle, Instrument

# Callback receiving the full ascending list of 1-minute candles.
OnUpdate = Callable[[list[Candle]], Awaitable[None]]


class Feed(abc.ABC):
  name: str = "base"
  label: str = "Broker"  # shown in messages, e.g. "Zerodha"
  # Fields the UI should ask for on the "Connect" screen (key -> label).
  login_fields: dict[str, str] = {}

  def is_ready(self) -> bool:
    """True when the feed can serve data (e.g. logged in)."""
    return True

  async def login(self, fields: dict[str, str]) -> None:
    """Performs broker login. Raises on failure."""

  @abc.abstractmethod
  async def instruments(self) -> list[Instrument]:
    ...

  @abc.abstractmethod
  async def run(self, instrument: Instrument, on_update: OnUpdate) -> None:
    """Streams data for `instrument` until cancelled."""
