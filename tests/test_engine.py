"""Parity tests: the engine must reproduce the original sheet's columns."""

import glob
import io
import os
import struct
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tradelive import indicators  # noqa: E402
from tradelive.candles import CandleBuilder, resample  # noqa: E402
from tradelive.export_xlsx import export_xlsx  # noqa: E402
from tradelive.feeds.mstock import parse_ticks  # noqa: E402
from tradelive.xlsx_reader import read_xlsx  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = glob.glob(os.path.join(ROOT, "data", "*sample*.xlsx"))[0]


def load():
  _, candles = read_xlsx(SAMPLE)
  return candles


def test_reader():
  c = load()
  assert len(c) == 148
  assert c[-1].ts.strftime("%Y-%m-%d %H:%M") == "2026-11-02 11:23"
  assert c[-1].close == 98.6 and c[-1].oi == 15872025


def test_sheet_columns_match():
  """L, M, N, O and P2 for the newest bars match the values Excel cached."""
  rows = indicators.compute(load(), indicators.EngineConfig(window=17))
  top = rows[-1]
  assert abs(top["vwap_ratio"] - 0.9640973630831644) < 1e-12  # L2
  assert top["d_obv"] == 1875445  # M2
  assert top["oi_up"] == 188825 and top["oi_down"] is None  # N2 / O2
  assert top["net_flow"] == 925080  # P2
  r4 = rows[-3]  # sheet row 4
  assert r4["d_obv"] == -1762605 and r4["oi_down"] == -193765
  assert r4["signal"] == "LU"
  assert top["signal"] == "LB"


def test_own_indicators_match_chart():
  c = load()
  vwap = indicators.compute_vwap(c)
  today = [i for i, x in enumerate(c) if x.ts.day == 2]
  assert max(abs(vwap[i] - c[i].ext_vwap) for i in today) < 0.01
  psar = indicators.compute_psar(c, 0.02, 0.02, 0.2)
  assert max(abs(psar[i] - c[i].ext_psar) for i in range(-60, 0)) < 0.01


def test_window_all_and_highlights():
  assert indicators.EngineConfig().window == 0  # "All rows" is the default
  rows = indicators.compute(load(), indicators.EngineConfig(window=0))
  assert sum(r["vol_max"] for r in rows) >= 1
  # With all rows, net flow equals the sum over the whole table.
  assert rows[-1]["net_flow"] == sum(r["oi_up"] or 0 for r in rows) - sum(
      r["oi_down"] or 0 for r in rows)
  data = export_xlsx(rows, "t", 0)
  with zipfile.ZipFile(io.BytesIO(data)) as z:
    xml = z.read("xl/worksheets/sheet1.xml").decode()
  assert "SUM(N2:N149)-SUM(O2:O149)" in xml
  rows17 = indicators.compute(load(), indicators.EngineConfig(window=17))
  assert not any(r["vol_max"] for r in rows17[:-17])
  s = indicators.summarize(rows17, indicators.EngineConfig(window=17))
  assert s["window"] == 17 and s["above_vwap"]
  assert s["oi"] == rows17[-1]["oi"] == 15872025
  assert s["d_oi"] == rows17[-1]["d_oi"] == 188825
  assert s["oi_change"] == sum(r["d_oi"] for r in rows17[-17:])
  assert s["oi_change"] == rows17[-1]["oi"] - rows17[-18]["oi"]


def test_resample_and_export():
  bars = resample(load(), 5)
  assert all(b.ts.minute % 5 == 0 for b in bars if b.ts.hour > 9)
  rows = indicators.compute(load(), indicators.EngineConfig())
  data = export_xlsx(rows, "NIFTY 03-Nov 23300 CE", 17)
  with zipfile.ZipFile(io.BytesIO(data)) as z:
    xml = z.read("xl/worksheets/sheet1.xml").decode()
  assert "SUM(N2:N18)-SUM(O2:O18)" in xml and "conditionalFormatting" in xml


def test_candle_builder_and_tick_parser():
  from datetime import datetime
  b = CandleBuilder()
  b.on_tick(datetime(2026, 11, 2, 9, 15, 1), 100, 1000, 5000)
  b.on_tick(datetime(2026, 11, 2, 9, 15, 30), 103, 1500, 5100)
  b.on_tick(datetime(2026, 11, 2, 9, 16, 2), 101, 1800, 5200)
  assert len(b.candles) == 2
  assert b.candles[0].high == 103 and b.candles[0].volume == 500
  assert b.candles[1].volume == 300 and b.candles[1].oi == 5200
  pkt = bytearray(184)
  struct.pack_into(">IIIIII", pkt, 0, 35008, 9860, 0, 0, 123456, 0)
  struct.pack_into(">I", pkt, 48, 15872025)
  frame = struct.pack(">HH", 1, len(pkt)) + bytes(pkt)
  t = parse_ticks(frame)[0]
  assert t["token"] == 35008 and t["ltp"] == 98.6
  assert t["volume"] == 123456 and t["oi"] == 15872025


def test_history_without_oi_shows_blank_flow():
  """m.Stock history has no OI: flow must be blank (None), not a fake 0."""
  c = load()[-20:]
  for x in c:
    x.oi = None
  rows = indicators.compute(c, indicators.EngineConfig())
  assert all(r["net_flow"] is None and r["signal"] == "" for r in rows)
  assert rows[-1]["d_obv"] is not None  # price/volume columns still work
  export_xlsx(rows, "no oi", 0)  # must not crash
  # Once live OI arrives for the latest bars, flow appears.
  c[-1].oi, c[-2].oi = 1000, 900
  rows = indicators.compute(c, indicators.EngineConfig())
  assert rows[-1]["net_flow"] is not None


def test_market_open_hours():
  from datetime import datetime
  from tradelive.server import market_open
  assert market_open(datetime(2026, 11, 2, 10, 0))  # Monday 10:00
  assert not market_open(datetime(2026, 11, 2, 18, 30))  # evening
  assert not market_open(datetime(2026, 11, 1, 10, 0))  # Sunday


def test_top_volume_ranks_and_excel_rule():
  rows = indicators.compute(load(), indicators.EngineConfig(window=17,
                                                            top_volumes=5))
  latest = rows[-17:]
  ranked = sorted((r for r in latest if r["vol_rank"]), key=lambda r: r["vol_rank"])
  assert [r["vol_rank"] for r in ranked][:5] == [1, 2, 3, 4, 5]
  top5 = sorted((r["volume"] for r in latest), reverse=True)[:5]
  assert sorted((r["volume"] for r in ranked), reverse=True) == top5
  assert ranked[0]["vol_max"]  # #1 is the yellow max
  assert not any(r["vol_rank"] for r in rows[:-17])  # only counted rows
  assert not any(r["vol_rank"] for r in indicators.compute(
      load(), indicators.EngineConfig(top_volumes=0)))  # "Off"
  data = export_xlsx(rows, "t", 17, 5)
  with zipfile.ZipFile(io.BytesIO(data)) as z:
    xml = z.read("xl/worksheets/sheet1.xml").decode()
  assert "LARGE($F$2:$F$18,MIN(5,COUNT($F$2:$F$18)))" in xml


def test_scrip_master_includes_stocks():
  from tradelive.feeds.mstock import parse_scrip_master
  csv_text = (
      "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,"
      "strike,tick_size,lot_size,instrument_type,segment,exchange\n"
      "2885,2885,RELIANCE,RELIANCE INDUSTRIES LTD,,,,0.1,1,EQ,EQ,NSE\n"
      "1594,1594,INFY,INFOSYS LTD,,,,0.1,1,EQ,EQ,NSE\n"
      "9999,9999,NOTFNO,SOME SMALL CO,,,,0.1,1,EQ,EQ,NSE\n"
      "36346,36346,RELIANCE26NOV1180CE,RELIANCE,40.5,2026-11-23,1180,,500,CE,OPTSTK,NFO\n"
      "59097,59097,RELIANCE26NOVFUT,RELIANCE,1178.4,2026-11-23,0,,500,XX,FUTSTK,NFO\n"
      "53940,53940,NIFTY26N0323300CE,NIFTY,98.6,2026-11-03,23300,,65,CE,OPTIDX,NFO\n"
      "70001,70001,RELIANCE26NOV1180CE,RELIANCE,1,2026-11-23,1180,,250,CE,OPTSTK,BFO\n"
      "80001,80001,USDINR,USDINR,1,2026-11-23,85,,1,CE,OPTCUR,CDS\n")
  items = parse_scrip_master(csv_text)
  by = {(i.symbol, i.option_type): i for i in items}
  assert by[("NIFTY", "CE")].kind == "index" and items[0].symbol == "NIFTY"
  assert by[("RELIANCE", "CE")].key == "NFO:36346"  # NSE, not BSE
  assert by[("RELIANCE", "FUT")].ref_price == 1178.4
  assert by[("RELIANCE", "EQ")].key == "NSE:2885"  # cash share of F&O stock
  assert ("INFY", "EQ") not in by and ("NOTFNO", "EQ") not in by
  assert not any(i.symbol == "USDINR" for i in items)


def test_signal_stats_match_sheet_net_flow():
  rows = indicators.compute(load(), indicators.EngineConfig(window=17))
  st = indicators.summarize(rows, indicators.EngineConfig(window=17))["signal_stats"]
  assert st["rows"] == 17 and st["with_signal"] == sum(
      st[k]["bars"] for k in ("LB", "SC", "SB", "LU"))
  # LB+SC OI minus SB+LU OI is exactly the sheet's P2 (net OI flow).
  net = st["LB"]["oi"] + st["SC"]["oi"] - st["SB"]["oi"] - st["LU"]["oi"]
  assert net == rows[-1]["net_flow"] == 925080
  assert 0 <= st["bull_share"] <= 1
  data = export_xlsx(rows, "t", 17)
  with zipfile.ZipFile(io.BytesIO(data)) as z:
    xml = z.read("xl/worksheets/sheet1.xml").decode()
  assert 'COUNTIF($Q$2:$Q$18,"LB")' in xml
  assert 'SUMIF($Q$2:$Q$18,"SB",$O$2:$O$18)' in xml
