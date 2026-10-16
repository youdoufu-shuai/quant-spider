@echo off
rem ============================================================
rem  Prompt Spider UI  --  本地启动
rem
rem  为什么不用项目自带的 启动.cmd：
rem  那份脚本直接调用 `node server.mjs`，依赖 node 在 PATH 里。
rem  本机 Node 是精简发行版（只有 node.exe，没进 PATH），
rem  所以这里把 node.exe 的绝对路径写进来。
rem ============================================================
setlocal
cd /d "%~dp0"

set "NODE=C:\Users\24942\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe"
set "PORT=4317"

if not exist "%NODE%" (
  echo [ERROR] 找不到 node.exe:
  echo         %NODE%
  echo         请修改本文件里的 NODE 路径，或把 node 加入 PATH。
  pause & exit /b 1
)

echo ============================================
echo   Prompt Spider UI
echo   机械蜘蛛 + 蛛网分流的提示词可视化
echo ============================================
echo.
echo   本地地址: http://127.0.0.1:%PORT%
echo   按 Ctrl+C 停止服务
echo.

start "" /B cmd /c "timeout /t 2 /nobreak >nul & start http://127.0.0.1:%PORT%/"

set "PORT=%PORT%"
"%NODE%" server.mjs
pause
