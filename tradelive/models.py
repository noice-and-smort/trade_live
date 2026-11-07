"""Core data types shared by feeds, the indicator engine and the UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Candle:
  """One OHLCV + OI bar.

  `ts` is the bar *start* time (local exchange time, naive datetime).
  Optional `ext_*` fields carry indicator values supplied by the source
  (e.g. a chart export); the engine computes its own when they are absent.
  """

  ts: datetime
  open: float
  high: float
  low: float
  close: float
  volume: float
  oi: Optional[float]  # None when the source has no OI for this bar
  ext_psar: Optional[float] = None
  ext_obv: Optional[float] = None
  ext_vwap: Optional[float] = None
  # True for previous-day bars loaded only to warm up PSAR/OBV: used in the
  # calculation but not shown or counted.
  warm: bool = False


@dataclass
class Instrument:
  """A tradable contract the user can pick in the UI."""

  key: str  # feed-specific unique id (e.g. exchange token)
  symbol: str  # e.g. "NIFTY", "RELIANCE"
  expiry: str  # ISO date "2026-11-03" ("" for non-derivatives)
  strike: float  # 0 for non-options
  option_type: str  # "CE" | "PE" | "FUT" | "EQ"
  label: str = field(default="")
  kind: str = "index"  # "index" | "stock" (for grouping in the picker)
  ref_price: float = 0.0  # last price from the instrument list (for ATM)

  def __post_init__(self):
    if not self.label:
      if self.option_type in ("CE", "PE"):
        self.label = f"{self.symbol} {self.expiry} {self.strike:g} {self.option_type}"
      else:
        self.label = f"{self.symbol} {self.expiry} {self.option_type}".strip()
