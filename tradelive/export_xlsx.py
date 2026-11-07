"""Exports the computed table as an .xlsx laid out exactly like the original
sheet (same columns, formulas and colour rules), newest bar on top."""

from __future__ import annotations

import io

import xlsxwriter

RED = {"font_color": "#9C0006", "bg_color": "#FFC7CE"}
GREEN = {"font_color": "#006100", "bg_color": "#C6EFCE"}
YELLOW = {"bg_color": "#FFFF00"}
AMBER = {"bg_color": "#FFD8A8", "font_color": "#7C2D12"}  # top-N volumes


def export_xlsx(rows: list[dict], title: str, window: int,
                top_volumes: int = 5) -> bytes:
  buf = io.BytesIO()
  wb = xlsxwriter.Workbook(buf, {"in_memory": True})
  ws = wb.add_worksheet(title[:31].translate(str.maketrans("[]:*?/\\", "       ")))
  font = {"font_size": 9, "font_name": "Calibri"}
  hdr = wb.add_format({**font, "bold": True, "text_wrap": True})
  dt = wb.add_format({**font, "num_format": "yyyy-mm-dd hh:mm:ss"})
  num = wb.add_format(font)
  two = wb.add_format({**font, "num_format": "0.00"})
  red, green, yellow, amber = (wb.add_format(f)
                               for f in (RED, GREEN, YELLOW, AMBER))

  headers = ["Date", "Open", "High", "Low", "Close", "Volume", None,
             "PSAR (0.02,0.2)", "OI", "On Bal Vol", "VWAP", "VWAP/Close",
             "dOBV", "dOI up-bar", "dOI down-bar",
             f"Net OI flow ({window if window > 0 else 'all rows'})",
             "Signal"]
  ws.set_row(0, 36)
  for j, h in enumerate(headers):
    if h:
      ws.write(0, j, h, hdr)
  ws.set_column(0, 0, 18)
  ws.set_column(1, 16, 10)
  ws.freeze_panes(1, 1)
  ws.autofilter(0, 0, 0, 10)

  from datetime import datetime
  data = list(reversed(rows))  # newest first, like the original sheet
  n = len(data)
  last = n + 1  # last data row in Excel numbering
  for i, r in enumerate(data):
    x = i + 1  # 0-based row index
    e = x + 1  # Excel row number
    ws.write_datetime(x, 0, datetime.strptime(r["ts"], "%Y-%m-%d %H:%M"), dt)
    for j, k in ((1, "open"), (2, "high"), (3, "low"), (4, "close"),
                 (5, "volume"), (7, "psar"), (8, "oi"), (9, "obv"),
                 (10, "vwap")):
      if r[k] is not None:
        ws.write_number(x, j, round(r[k], 2), num)
    ws.write_formula(x, 11, f"=K{e}/E{e}", two, r["vwap_ratio"])
    if e < last:  # the oldest bar has no previous bar to diff against
      ws.write_formula(x, 12, f"=J{e}-J{e+1}", num, r["d_obv"])
      ws.write_formula(x, 13, f'=IF(M{e}>0,I{e}-I{e+1},"")', num,
                       r["oi_up"] if r["oi_up"] is not None else "")
      ws.write_formula(x, 14, f'=IF(M{e}<0,I{e}-I{e+1},"")', num,
                       r["oi_down"] if r["oi_down"] is not None else "")
    ws.write(x, 16, r["signal"], num)

  w = min(window if window > 0 else n, n)
  wend = w + 1
  if n:
    ws.write_formula(1, 15, f"=SUM(N2:N{wend})-SUM(O2:O{wend})", num,
                     data[0]["net_flow"] or 0)

  # Signal summary over the counted rows (live formulas), columns S..V.
  if n:
    rng = f"$Q$2:$Q${wend}"
    ws.write(0, 18, "Signal", hdr)
    ws.write(0, 19, "Bars", hdr)
    ws.write(0, 20, "Σ OI change", hdr)
    ws.write(0, 21, "Σ Volume", hdr)
    sig_fmt = {k: wb.add_format({**font, "bold": True, "font_color": "#FFFFFF",
                                 "bg_color": c})
               for k, c in (("LB", "#15803D"), ("SC", "#65A30D"),
                            ("SB", "#B91C1C"), ("LU", "#EA580C"))}
    # LB/SC happen on up-bars (column N), SB/LU on down-bars (column O).
    for i, (k, col) in enumerate((("LB", "N"), ("SC", "N"), ("SB", "O"),
                                  ("LU", "O")), start=1):
      ws.write(i, 18, k, sig_fmt[k])
      ws.write_formula(i, 19, f'=COUNTIF({rng},"{k}")', num)
      ws.write_formula(i, 20, f'=SUMIF({rng},"{k}",${col}$2:${col}${wend})',
                       num)
      ws.write_formula(i, 21, f'=SUMIF({rng},"{k}",$F$2:$F${wend})', num)
    ws.write(5, 18, "Price-up bars (LB+SC)", hdr)
    ws.write_formula(5, 19, "=T2+T3", num)
    ws.write(6, 18, "Price-down bars (SB+LU)", hdr)
    ws.write_formula(6, 19, "=T4+T5", num)
    ws.set_column(18, 18, 16)
    ws.set_column(19, 21, 12)

  def cf(rng, crit, val, fmt):
    ws.conditional_format(rng, {"type": "cell", "criteria": crit, "value": val,
                                "format": fmt, "stop_if_true": True})

  if n:
    cf(f"L2:L{last}", ">", 1, red)
    cf(f"L2:L{last}", "<", 1, green)
    for col in "MNO":
      cf(f"{col}2:{col}{last}", ">", 0, green)
      cf(f"{col}2:{col}{last}", "<", 0, red)
    ws.conditional_format(f"F2:F{wend}", {
        "type": "formula", "criteria": f"=F2=MAX($F$2:$F${wend})",
        "format": yellow, "stop_if_true": True})
    if top_volumes > 1:  # the rest of the top N volumes in amber
      ws.conditional_format(f"F2:F{wend}", {
          "type": "formula",
          "criteria": (f"=AND(F2>0,F2>=LARGE($F$2:$F${wend},"
                       f"MIN({top_volumes},COUNT($F$2:$F${wend}))))"),
          "format": amber})
    ws.conditional_format(f"I2:I{wend}", {
        "type": "formula", "criteria": f"=I2=MAX($I$2:$I${wend})",
        "format": red, "stop_if_true": True})
    ws.conditional_format(f"I2:I{wend}", {
        "type": "formula", "criteria": f"=I2=MIN($I$2:$I${wend})",
        "format": green})
  wb.close()
  return buf.getvalue()
