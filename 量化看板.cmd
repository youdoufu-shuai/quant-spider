@echo off
rem ============================================================
rem  量化看板  --  一键启动
rem
rem  启动后浏览器打开 http://127.0.0.1:4400/
rem  保持本窗口开启 = 服务持续运行
rem ============================================================
setlocal
cd /d "%~dp0"

set "NODE=C:\Users\24942\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe"
set "QUANT_PYTHON=C:\Users\24942\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
set "QUANT_PORT=4400"

if not exist "%NODE%" (
  echo [ERROR] 找不到 node.exe: %NODE%
  pause & exit /b 1
)
if not exist "%QUANT_PYTHON%" (
  echo [ERROR] 找不到 python.exe: %QUANT_PYTHON%
  pause & exit /b 1
)

echo ============================================
echo   量化看板  QUANT BOARD
echo ============================================
echo.
echo   量化看板  http://127.0.0.1:%QUANT_PORT%/
echo   Agent 实时 http://127.0.0.1:4317/live
echo   蜘蛛美术  http://127.0.0.1:4317/spider-viewer.html
echo.
echo   数据    真实行情（Binance 镜像 / 新浪财经）
echo   边界    只读行情，无交易接口，不产生任何真实委托
echo.
echo   首次加载需 0.4-2 秒（含真实网络请求），之后走缓存
echo   页面默认每 30 秒自动刷新（可在顶部切换）
echo   按 Ctrl+C 停止
echo.

start "" /B cmd /c "timeout /t 3 /nobreak >nul & start http://127.0.0.1:%QUANT_PORT%/"
"%NODE%" "quant\server.mjs"
pause
