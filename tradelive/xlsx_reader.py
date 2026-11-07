"""Reads chart-export style .xlsx files (like the original sheet) using only
the standard library, so the packaged app stays small."""

from __future__ import annotations

import re
import zipfile
from datetime import datetime, timedelta
from typing import Optional
from xml.etree import ElementTree as ET

from tradelive.models import Candle

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_EPOCH = datetime(1899, 12, 30)


def _col(ref: str) -> str:
  return re.match(r"[A-Z]+", ref).group(0)


def _header_key(text: str) -> Optional[str]:
  t = re.sub(r"[^a-z ]", "", text.lower())
  for key, pats in (
      ("ts", ("date", "time")),
      ("open", ("open",)),
      ("high", ("high",)),
      ("low", ("low",)),
      ("close", ("close",)),
      ("volume", ("volume",)),
      ("psar", ("psar", "sar")),
      ("obv", ("bal vol", "obv")),
      ("vwap", ("vwap",)),
      ("oi", ("oi", "open interest")),
  ):
    if any(t.strip() == p or (len(p) > 3 and p in t) for p in pats):
      return key
  return None


def read_xlsx(path: str) -> tuple[str, list[Candle]]:
  """Returns (sheet name, candles ascending by time)."""
  with zipfile.ZipFile(path) as z:
    strings: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
      root = ET.fromstring(z.read("xl/sharedStrings.xml"))
      for si in root.findall("m:si", _NS):
        strings.append("".join(t.text or "" for t in si.iter(
            "{%s}t" % _NS["m"])))
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    sheet_name = wb.find("m:sheets/m:sheet", _NS).get("name")
    sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))

  table: list[dict[str, str]] = []
  for row in sheet.iter("{%s}row" % _NS["m"]):
    vals: dict[str, str] = {}
    for c in row.findall("m:c", _NS):
      v = c.find("m:v", _NS)
      if v is None or v.text is None:
        continue
      vals[_col(c.get("r"))] = (strings[int(v.text)]
                                if c.get("t") == "s" else v.text)
    table.append(vals)
  if not table:
    return sheet_name, []

  cols: dict[str, str] = {}
  for col, text in table[0].items():
    key = _header_key(text)
    if key and key not in cols:
      cols[key] = col
  missing = {"ts", "open", "high", "low", "close", "volume"} - cols.keys()
  if missing:
    raise ValueError(f"Missing columns in {path}: {sorted(missing)}")

  def num(r: dict, key: str) -> Optional[float]:
    col = cols.get(key)
    if col and col in r:
      try:
        return float(r[col])
      except ValueError:
        return None
    return None

  candles = []
  for r in table[1:]:
    ts = num(r, "ts")
    if ts is None:
      continue
    candles.append(
        Candle(
            ts=_EPOCH + timedelta(days=ts),
            open=num(r, "open"),
            high=num(r, "high"),
            low=num(r, "low"),
            close=num(r, "close"),
            volume=num(r, "volume") or 0.0,
            oi=num(r, "oi") or 0.0,
            ext_psar=num(r, "psar"),
            ext_obv=num(r, "obv"),
            ext_vwap=num(r, "vwap"),
        ))
  for c in candles:  # round float day-fractions to the nearest second
    c.ts = (c.ts + timedelta(milliseconds=500)).replace(microsecond=0)
  candles.sort(key=lambda c: c.ts)
  return sheet_name, candles
