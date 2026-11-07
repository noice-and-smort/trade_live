TradeLive – how to use
======================

START
  1. Double-click TradeLive.exe. A black window opens (leave it open) and the
     dashboard opens in your web browser.
  2. To stop, close the black window.
  Tip: right-click TradeLive.exe > "Send to" > "Desktop (create shortcut)".

TRY IT WITHOUT THE BROKER (Replay)
  Source = "Replay (saved files)" plays any chart export .xlsx kept in the
  "data" folder as if it were live. Good for practice or after market hours.

LIVE DATA FROM m.STOCK – CURRENTLY SWITCHED OFF
  (m.Stock's live stream is down. To switch it back on, set
  "disabled_feeds": [] in settings.json next to TradeLive and restart.)

m.STOCK – ONE-TIME SETUP (about 5 minutes, with help)
  1. On trade.mstock.com > menu > Trading APIs: create an API key and click
     "Enable TOTP". Copy the TOTP SECRET KEY (the text under the QR code).
  2. In TradeLive: Source = "m.Stock (live)" > "Connect broker". Fill in
     client code, password, API key and the TOTP secret key. Keep
     "Remember on this computer..." ticked and click Connect.
  3. Pick Index, Expiry, Strike and CE/PE, then click "Watch".
  4. Tick "Start with Windows" in the top bar.

EVERY DAY AFTER THAT
  Nothing. Switch on the computer. TradeLive opens by itself, logs in at
  08:35 and shows the same contract. When a weekly contract expires it moves
  to the same strike on the next expiry automatically.
  Keep the computer on from before 9:15 so OI is recorded for the whole day.

  The saved login is encrypted with Windows and works only for this Windows
  user on this computer. To remove it: Connect broker > "Forget saved login".
  No TOTP secret? Leave it empty: the login is still remembered, and each
  morning you only type the 6-digit code (or SMS OTP) and click Connect.

READING THE SCREEN
  Top cards  : Net OI flow (your P2 number), price vs VWAP, PSAR, the latest
               signal and how many bars in a row, bars since the biggest volume.
  Table      : same columns and colours as your Excel sheet, newest on top.
               Light-blue rows are the rows being counted ("Rows to count").
  Signal     : LB Long build-up, SC Short covering, SB Short build-up,
               LU Long unwinding.
  Candle     : 1m / 3m / 5m.   Rows to count: 5 ... All.
  Download Excel : saves your usual sheet (same formulas and colours).
  Sound alerts   : beeps when a signal repeats 3 bars in a row or price
                   crosses VWAP.

WATCHLIST (several contracts at once)
  Open a contract (Watch / Play), then click "☆ Add to watchlist" next to its
  name. Up to 20 contracts appear in the Watchlist panel at the top, each with
  price, % change, VWAP, PSAR, latest signal, net OI flow and volume spike.
  Click a row to open its full table below. ✕ removes it. "Sort" puts the
  most active ones on top. The watchlist is remembered and restored every
  morning (weekly contracts roll to the next expiry automatically).
  With "Sound alerts" on, any watched contract beeps when a signal repeats
  3 bars in a row or its price crosses VWAP.

ZERODHA (KITE CONNECT) – ONE-TIME SETUP
  1. Go to developers.kite.trade, log in with the Zerodha account and create
     an app ("Connect" plan, ₹500/month – credited back in months with
     ₹2,000+ brokerage).
  2. In the app settings set:
       Redirect URL:  http://127.0.0.1:8765/kite/callback
     (Postback URL can stay empty.) Note the API key and API secret.
  3. In TradeLive: Source = "Zerodha (live)" > Connect broker > enter API key
     and API secret, keep "Remember" ticked > Connect.
  4. Zerodha's login page opens: log in there. You are sent back and
     TradeLive continues by itself.

EVERY DAY WITH ZERODHA
  Zerodha requires its own login once a day (automated logins aren't
  allowed). At 8:35 TradeLive opens the Zerodha login page by itself – just
  log in there. Zerodha's history includes OI, so even if TradeLive starts
  late, the whole day's OI is shown.
