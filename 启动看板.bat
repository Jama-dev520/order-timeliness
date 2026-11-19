@echo off
rem 双击启动本机服务并打开看板。看板右上角的「刷新数据」要靠这个服务抓数。
cd /d "%~dp0"

netstat -ano | findstr ":8000 " | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
  echo [ok] 本机服务已在运行，直接打开看板。
) else (
  echo 正在启动本机服务 127.0.0.1:8000 ...
  start "订单看板服务" /min python serve.py --host 127.0.0.1 --port 8000 --no-fetch
  timeout /t 4 >nul
  echo [ok] 已启动，最小化那个窗口不要关。
)

start "" "%~dp0dashboard.html"
exit /b 0
