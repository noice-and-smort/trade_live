"""Indicator engine: reproduces (and cleans up) the derived columns of the
original Excel sheet.

Sheet column -> field here
  H  PSAR (0.02, 0.2)          -> psar
  J  On-Balance Volume         -> obv
  K  VWAP                      -> vwap
  L  =K/E                      -> vwap_ratio      (<1 green: price above VWAP)
  M  =J(t)-J(t-1)              -> d_obv           (signed volume)
  N  =IF(M>0, dOI, "")         -> oi_up           (OI change on up-bars)
  O  =IF(M>0, "", dOI)         -> oi_down         (OI change on down-bars)
  P2 =SUM(N win)-SUM(O win)    -> net_flow        (computed for every bar)
  CF max volume in window      -> vol_max
  CF max / min OI in window    -> oi_max / oi_min

Additions: `signal` (LB/SC/SB/LU build-up classification) and `psar_bias`.

Fixes vs the sheet: bars with no price change (d_obv == 0) are neutral instead
of being counted as down-bars; the first bar has no deltas instead of
referencing an empty row; the rolling window is a true N-bar window (or all
rows) rather than hard-coded cell references.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from tradelive.models import Candle

SIGNALS = {
    (1, 1): ("LB", "Long build-up"),
    (1, -1): ("SC", "Short covering"),
    (-1, 1): ("SB", "Short build-up"),
    (-1, -1): ("LU", "Long unwinding"),
}


@dataclass
class EngineConfig:
  window: int = 0  # rolling window in bars; 0 (default) means "all rows"
  top_volumes: int = 5  # highlight the N biggest volumes in the window
  psar_start: float = 0.02
  psar_step: float = 0.02
  psar_max: float = 0.2
  prefer_source_indicators: bool = True  # use ext_* values when provided


def _sign(x: float) -> int:
  return (x > 0) - (x < 0)


def compute_psar(candles: Sequence[Candle], start: float, step: float,
                 max_af: float) -> list[Optional[float]]:
  """Wilder's Parabolic SAR."""
  n = len(candles)
  out: list[Optional[float]] = [None] * n
  if n < 2:
    return out
  up = candles[1].close >= candles[0].close
  sar = candles[0].low if up else candles[0].high
  ep = candles[0].high if up else candles[0].low
  af = start
  out[0] = sar
  for i in range(1, n):
    c = candles[i]
    sar = sar + af * (ep - sar)
    if up:
      sar = min(sar, candles[i - 1].low, candles[max(i - 2, 0)].low)
      if c.low < sar:
        up, sar, ep, af = False, ep, c.low, start
      elif c.high > ep:
        ep, af = c.high, min(af + step, max_af)
    else:
      sar = max(sar, candles[i - 1].high, candles[max(i - 2, 0)].high)
      if c.high > sar:
        up, sar, ep, af = True, ep, c.high, start
      elif c.low < ep:
        ep, af = c.low, min(af + step, max_af)
    out[i] = sar
  return out


def compute_vwap(candles: Sequence[Candle]) -> list[Optional[float]]:
  """Session VWAP on typical price, reset at each new trading day."""
  out: list[Optional[float]] = []
  pv = vol = 0.0
  day = None
  for c in candles:
    if c.ts.date() != day:
      day, pv, vol = c.ts.date(), 0.0, 0.0
    pv += (c.high + c.low + c.close) / 3.0 * c.volume
    vol += c.volume
    out.append(pv / vol if vol else c.close)
  return out


def compute_obv(candles: Sequence[Candle]) -> list[float]:
  out: list[float] = []
  obv = 0.0
  for i, c in enumerate(candles):
    if i:
      obv += _sign(c.close - candles[i - 1].close) * c.volume
    out.append(obv)
  return out


def compute(candles: Sequence[Candle], cfg: EngineConfig) -> list[dict]:
  """Returns one dict per candle (ascending time) with all derived fields."""
  n = len(candles)
  if not n:
    return []

  def pick(attr: str, fallback: list) -> list:
    if cfg.prefer_source_indicators and all(
        getattr(c, attr) is not None for c in candles):
      return [getattr(c, attr) for c in candles]
    return fallback

  psar = pick("ext_psar", compute_psar(candles, cfg.psar_start, cfg.psar_step,
                                       cfg.psar_max))
  obv = pick("ext_obv", compute_obv(candles))
  # VWAP per bar: the exchange's value when the feed provides it (live
  # ticks), otherwise our own calculation (history, files without VWAP).
  own_vwap = compute_vwap(candles)
  vwap = [c.ext_vwap if (cfg.prefer_source_indicators and
                         c.ext_vwap is not None) else own_vwap[i]
          for i, c in enumerate(candles)]

  rows: list[dict] = []
  for i, c in enumerate(candles):
    d_obv = d_oi = None
    if i:
      d_obv = obv[i] - obv[i - 1]
      if c.oi is not None and candles[i - 1].oi is not None:
        d_oi = c.oi - candles[i - 1].oi
    s_obv = _sign(d_obv) if d_obv is not None else 0
    oi_up = d_oi if s_obv > 0 else None
    oi_down = d_oi if s_obv < 0 else None
    sig = SIGNALS.get((s_obv, _sign(d_oi))) if d_oi is not None else None
    rows.append({
        "ts": c.ts.strftime("%Y-%m-%d %H:%M"),
        "open": c.open,
        "high": c.high,
        "low": c.low,
        "close": c.close,
        "volume": c.volume,
        "psar": psar[i],
        "oi": c.oi,
        "obv": obv[i],
        "vwap": vwap[i],
        "vwap_ratio": (vwap[i] / c.close) if c.close else None,
        "d_obv": d_obv,
        "d_oi": d_oi,
        "oi_up": oi_up,
        "oi_down": oi_down,
        "signal": sig[0] if sig else "",
        "signal_name": sig[1] if sig else "",
        "psar_bias": ("BEAR" if psar[i] is not None and psar[i] > c.close else
                      "BULL"),
        "vol_max": False,
        "oi_max": False,
        "oi_min": False,
    })

  # Warm-up bars (previous day) only seed PSAR/OBV/ΔOBV: hide them.
  rows = [r for r, c in zip(rows, candles) if not c.warm]
  n = len(rows)
  if not n:
    return []

  # Rolling net OI flow for every bar (None while the window has no OI data,
  # e.g. broker history without OI, so the UI shows a blank, not a fake 0).
  w = cfg.window if cfg.window and cfg.window > 0 else n
  up_sum = down_sum = 0.0
  have = 0  # bars in the window that have an OI change
  for i, r in enumerate(rows):
    up_sum += r["oi_up"] or 0.0
    down_sum += r["oi_down"] or 0.0
    have += r["d_oi"] is not None
    if i >= w:
      old = rows[i - w]
      up_sum -= old["oi_up"] or 0.0
      down_sum -= old["oi_down"] or 0.0
      have -= old["d_oi"] is not None
    r["net_flow"] = (up_sum - down_sum) if have else None

  # Highlights apply to the latest window (like the sheet's CF on rows 2..18).
  latest = rows[-w:]
  vmax = max(r["volume"] for r in latest)
  ois = [r["oi"] for r in latest if r["oi"] is not None]
  oimax = max(ois) if ois else None
  oimin = min(ois) if ois else None
  for r in latest:
    r["vol_max"] = r["volume"] == vmax
    r["oi_max"] = r["oi"] is not None and r["oi"] == oimax
    r["oi_min"] = r["oi"] is not None and r["oi"] == oimin
  # Rank of each volume among the top N in the window (1 = biggest; equal
  # volumes share a rank). 0 = not in the top N.
  for r in rows:
    r["vol_rank"] = 0
  if cfg.top_volumes > 0:
    distinct = sorted({r["volume"] for r in latest if r["volume"] > 0},
                      reverse=True)[:cfg.top_volumes]
    rank = {v: i + 1 for i, v in enumerate(distinct)}
    for r in latest:
      r["vol_rank"] = rank.get(r["volume"], 0)
  return rows


def signal_stats(rows: list[dict]) -> dict:
  """Per signal type over `rows`: bars, total OI change, total volume; plus
  a bullish (LB+SC) vs bearish (SB+LU) split."""
  stats = {k: {"bars": 0, "oi": 0.0, "volume": 0.0}
           for k in ("LB", "SC", "SB", "LU")}
  for r in rows:
    st = stats.get(r["signal"])
    if st is None:
      continue
    st["bars"] += 1
    st["oi"] += r["d_oi"] or 0.0
    st["volume"] += r["volume"] or 0.0
  bull = stats["LB"]["bars"] + stats["SC"]["bars"]
  bear = stats["SB"]["bars"] + stats["LU"]["bars"]
  # OI-weighted "who is in control": new longs + shorts covering vs new
  # shorts + longs exiting (absolute OI moved by each side).
  bull_oi = abs(stats["LB"]["oi"]) + abs(stats["SC"]["oi"])
  bear_oi = abs(stats["SB"]["oi"]) + abs(stats["LU"]["oi"])
  total = bull_oi + bear_oi
  return {**stats, "bull_bars": bull, "bear_bars": bear,
          "bull_oi": bull_oi, "bear_oi": bear_oi,
          "bull_share": (bull_oi / total) if total else None,
          "with_signal": bull + bear, "rows": len(rows)}


def summarize(rows: list[dict], cfg: EngineConfig) -> dict:
  """Headline numbers for the KPI strip (based on the latest bar)."""
  if not rows:
    return {}
  last = rows[-1]
  w = cfg.window if cfg.window and cfg.window > 0 else len(rows)
  latest = rows[-w:]
  spike_idx = max(range(len(latest)), key=lambda k: latest[k]["volume"])
  counts: dict[str, int] = {}
  for r in latest:
    if r["signal"]:
      counts[r["signal"]] = counts.get(r["signal"], 0) + 1
  # Consecutive same-signal streak ending at the latest bar.
  streak = 0
  for r in reversed(rows):
    if r["signal"] and r["signal"] == last["signal"]:
      streak += 1
    else:
      break
  d_ois = [r["d_oi"] for r in latest if r["d_oi"] is not None]
  oi_change = sum(d_ois) if d_ois else None
  base_oi = (last["oi"] - oi_change
             if last["oi"] is not None and oi_change is not None else None)
  oi_change_pct = (oi_change / base_oi * 100.0
                   if base_oi and base_oi > 0 else None)
  return {
      "ts": last["ts"],
      "close": last["close"],
      "oi": last["oi"],
      "d_oi": last["d_oi"],
      "oi_max": last["oi_max"],
      "oi_min": last["oi_min"],
      "oi_change": oi_change,
      "oi_change_pct": oi_change_pct,
      "net_flow": last["net_flow"],
      "vwap_ratio": last["vwap_ratio"],
      "above_vwap": (last["vwap_ratio"] or 1) < 1,
      "psar_bias": last["psar_bias"],
      "signal": last["signal"],
      "signal_name": last["signal_name"],
      "streak": streak,
      "bars_since_vol_spike": len(latest) - 1 - spike_idx,
      "signal_counts": counts,
      "signal_stats": signal_stats(latest),
      "window": w,
  }
