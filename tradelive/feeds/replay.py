"""Replay feed: plays a saved chart export (.xlsx) as if it were live.

Useful for demos, testing after market hours, and reviewing old sessions.
Every `.xlsx` in the data folder shows up as an instrument.
"""

from __future__ import annotations

import asyncio
import os

from tradelive.feeds.base import Feed, OnUpdate
from tradelive.models import Instrument
from tradelive.xlsx_reader import read_xlsx


class ReplayFeed(Feed):
  name = "replay"

  def __init__(self, data_dir: str, seconds_per_bar: float = 2.0,
               warmup_bars: int = 30):
    self.data_dir = data_dir
    self.seconds_per_bar = seconds_per_bar
    self.warmup_bars = warmup_bars

  async def instruments(self) -> list[Instrument]:
    out = []
    if os.path.isdir(self.data_dir):
      for f in sorted(os.listdir(self.data_dir)):
        if f.lower().endswith(".xlsx") and not f.startswith("~$"):
          out.append(
              Instrument(key=os.path.join(self.data_dir, f), symbol="REPLAY",
                         expiry="", strike=0, option_type="FILE",
                         label=f"Replay: {os.path.splitext(f)[0]}"))
    return out

  async def run(self, instrument: Instrument, on_update: OnUpdate) -> None:
    _, candles = await asyncio.to_thread(read_xlsx, instrument.key)
    n = min(self.warmup_bars, len(candles))
    await on_update(candles[:n])
    while n < len(candles):
      await asyncio.sleep(self.seconds_per_bar)
      n += 1
      await on_update(candles[:n])
    # Hold the final state until the UI switches instrument.
    while True:
      await asyncio.sleep(3600)
