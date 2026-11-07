@echo off
REM Builds dist\TradeLive.exe on a Windows PC that has Python installed.
REM (Alternative: push to GitHub and use the "build-windows-exe" workflow.)
cd /d %~dp0
python -m pip install -r requirements.txt pyinstaller || goto :err
pyinstaller --noconfirm --onefile --name TradeLive ^
  --add-data "tradelive/static;tradelive/static" ^
  --hidden-import tradelive.feeds.kite --hidden-import tradelive.feeds.mstock --hidden-import tradelive.diagnose --hidden-import tradelive.housekeeping ^
  --collect-submodules uvicorn ^
  run.py || goto :err
if not exist dist\data mkdir dist\data
copy /Y "data\*.xlsx" dist\data\ >nul
copy /Y "README_FOR_USER.md" dist\README.txt >nul
echo.
echo Done. Copy the whole "dist" folder (TradeLive.exe + data) to the user's PC.
pause
exit /b 0
:err
echo Build failed.
pause
exit /b 1
