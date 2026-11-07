"""Tick -> candle aggregation and timeframe resampling."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, Sequence

from tradelive.models import Candle


def floor_time(ts: datetime, minutes: int) -> datetime:
  base = ts.replace(second=0, microsecond=0)
  # Align to the session start (09:15 for NSE) so 3m/5m bars match charts.
  session = base.replace(hour=9, minute=15)
  if base < session:
    return base - timedelta(minutes=base.minute % minutes)
  offset = int((base - session).total_seconds() // 60) % minutes
  return base - timedelta(minutes=offset)


def resample(candles: Sequence[Candle], minutes: int) -> list[Candle]:
  """Merges 1-minute candles into `minutes`-minute candles."""
  if minutes <= 1:
    return list(candles)
  out: list[Candle] = []
  for c in candles:
    bucket = floor_time(c.ts, minutes)
    if out and out[-1].ts == bucket:
      b = out[-1]
      b.high = max(b.high, c.high)
      b.low = min(b.low, c.low)
      b.close = c.close
      b.volume += c.volume
      b.oi = c.oi if c.oi is not None else b.oi
      # Source indicators are point-in-time values: keep the last one.
      b.ext_psar, b.ext_obv, b.ext_vwap = c.ext_psar, c.ext_obv, c.ext_vwap
    else:
      out.append(
          Candle(bucket, c.open, c.high, c.low, c.close, c.volume, c.oi,
                 c.ext_psar, c.ext_obv, c.ext_vwap, c.warm))
  # Resampled PSAR from 1m values is not meaningful; let the engine recompute.
  for b in out:
    b.ext_psar = None
  return out


class CandleBuilder:
  """Builds 1-minute candles from a stream of ticks.

  Brokers send cumulative day volume, so per-bar volume is the difference
  between the cumulative volume at the end and start of the bar.
  """

  def __init__(self):
    self.candles: list[Candle] = []
    self._last_cum_vol: Optional[float] = None

  def on_tick(self, ts: datetime, ltp: float, cum_volume: float,
              oi: Optional[float], atp: Optional[float] = None) -> bool:
    """Returns True if a new bar was started."""
    bucket = ts.replace(second=0, microsecond=0)
    vol = 0.0
    if self._last_cum_vol is not None and cum_volume >= self._last_cum_vol:
      vol = cum_volume - self._last_cum_vol
    self._last_cum_vol = cum_volume
    if self.candles and self.candles[-1].ts == bucket:
      c = self.candles[-1]
      c.high = max(c.high, ltp)
      c.low = min(c.low, ltp)
      c.close = ltp
      c.volume += vol
      c.oi = oi or c.oi
      c.ext_vwap = _sane_atp(atp, ltp) or c.ext_vwap
      return False
    self.candles.append(Candle(bucket, ltp, ltp, ltp, ltp, vol, oi,
                               ext_vwap=_sane_atp(atp, ltp)))
    return True


def _sane_atp(atp: Optional[float], ltp: float) -> Optional[float]:
  """The exchange's average traded price (= official day VWAP), if sane."""
  if not atp or atp <= 0 or not ltp:
    return None
  return atp if 0.5 * ltp <= atp <= 2 * ltp else None
