@echo off
cd /d "%~dp0"
set PY=python
where python >nul 2>nul || set PY=py

echo ============================================
echo  Update OC login cookie (hk-sit)
echo ============================================
echo.
echo A Chrome window will open.
echo Enter your account / password / 4-digit code.
echo After login, data will be refreshed automatically.
echo.
echo If the cookie is still valid, it exits in seconds.
echo.

%PY% login_browser.py --refresh
set RC=%errorlevel%

echo.
if not "%RC%"=="0" (
  echo [FAILED] exit code %RC%
  echo Try running in PowerShell instead:
  echo   cd /d "%~dp0"
  echo   python login_browser.py --refresh
) else (
  echo [DONE]
)
echo.
pause
