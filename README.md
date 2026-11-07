# TradeLive (developer notes)

A live option-flow dashboard that rebuilds the hand-made Excel sheet from broker data. End-user instructions are in [README_FOR_USER.md](README_FOR_USER.md).

## Run / debug

| OS | Command |
|---|---|
| macOS / Linux | `./TradeLive.command` (the first run creates `.venv`), or double-click it in Finder |
| Windows (source) | `py -m pip install -r requirements.txt` then `py run.py` |
| Windows (end user) | `TradeLive.exe`, built with `build_windows.bat` or the GitHub workflow |

Flags: `--debug` (verbose, includes raw broker WebSocket text frames), `--no-window`, `--port N`.
Logs go to `tradelive.log` in the app folder (rotating: at most about 8 MB in total). Recorded OI and warm-up caches in `data/live` are kept for 10 days and then deleted automatically.

The first live tick of each session is logged at INFO level (`first tick for ...`). Use it to check the OI scale against the m.Stock app.

Tests: `python -m pytest tests`

## Where things are stored

| | Windows | macOS (from source) | macOS (.app) |
|---|---|---|---|
| Settings, recorded OI (`data/live`), log | next to the exe | the repo folder | `~/Library/Application Support/TradeLive` |
| Saved login | `credentials.dat`, encrypted with DPAPI | Keychain item "TradeLive" | Keychain item "TradeLive" |
| Auto-start | Startup folder `TradeLive.bat` | `~/Library/LaunchAgents/com.tradelive.app.plist` | same |

On Linux, the saved login is **not encrypted** (base64 only). The UI warns about this. Use Linux for development only.

## Layout
- `tradelive/indicators.py`: the sheet's formulas (VWAP/Close, ΔOBV, OI on up/down bars, net flow, highlights, signals)
- `tradelive/feeds/`: `replay.py` (xlsx files in `data/`), `mstock.py` (live m.Stock Type A)
- `tradelive/server.py`: Starlette server (pure Python), WebSocket push and the auto-login loop
- `tradelive/static/`: dashboard (plain JS, with a bundled Lightweight Charts)
- `tradelive/export_xlsx.py`: "Download Excel" with the same formulas and conditional formats
