@echo off
chcp 65001 >nul
rem ============================================================
rem   Prompt Spider <- DSH   实时跟随（一键）
rem
rem   一句话：双击这个文件，Agent 的每一步就会画到蜘蛛上。
rem
rem   它做三件事：
rem     1. 需要的话，启动 4317 主服务（稳定入口 /live 靠它）
rem     2. 启动喂养器：跟随 DSH 会话日志，把每次工具调用变成画布上的点
rem     3. 打开 http://127.0.0.1:4317/live
rem
rem   ⚠️ 关键点：**不需要 Agent 配合**。
rem      喂养器是「读日志」的，不是「等 Agent 上报」的 ——
rem      Agent 只管干活，日志自动写，画布自动跟。
rem
rem   保持本窗口开启 = 持续跟随。按 Ctrl+C 结束。
rem ============================================================
setlocal
cd /d "%~dp0"

set "NODE=C:\Users\24942\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe"
set "PORT=4317"

if not exist "%NODE%" (
  echo [ERROR] 找不到 node.exe:
  echo         %NODE%
  pause & exit /b 1
)

echo ============================================
echo   Prompt Spider  ^<-^>  DSH   实时跟随
echo ============================================
echo.

rem ── 第 1 步：需要的话启动 4317 主服务 ──────────────────────
rem 用 netstat 检查端口，避免重复启动（重复启动会 EADDRINUSE）
netstat -ano | findstr /R /C:":%PORT% .*LISTENING" >nul 2>&1
if errorlevel 1 (
  echo   [1/2] 启动主服务 %PORT% ...
  start "Prompt Spider 主服务" /MIN "%NODE%" "server.mjs"
  set "READY="
  for /L %%i in (1,1,20) do (
    if not defined READY (
      netstat -ano | findstr /R /C:":%PORT% .*LISTENING" >nul 2>&1
      if not errorlevel 1 set "READY=1"
    )
  )
  echo         已就绪
) else (
  echo   [1/2] 主服务 %PORT% 已在运行
)

rem ── 第 2 步：打开稳定入口页 ──────────────────────────────
start "" "http://127.0.0.1:%PORT%/live"
echo   [2/2] 打开 http://127.0.0.1:%PORT%/live
echo.
echo   正在连接 DSH 会话并建立预览链接...
echo   下面会打印预览地址。保持本窗口开启 = 持续跟随。
echo   按 Ctrl+C 结束（会把会话标记完成，历史可回看）。
echo.

rem ── 第 3 步：前台运行喂养器 ──────────────────────────────
"%NODE%" "mcp\live-feed.mjs" --interval 2.5 %*
pause
