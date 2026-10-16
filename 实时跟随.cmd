@echo off
rem ============================================================
rem  Prompt Spider ← DSH 实时喂养器
rem
rem  做什么：持续读取 DSH 的完整事件流，让 Spider UI 跟着
rem          Agent 一直跑，而不是播完那段固定动画就停。
rem
rem  只要这个窗口开着，面板就会一直更新。
rem  按 Ctrl+C 结束（结束时会把会话标记完成，历史保留可回看）。
rem ============================================================
setlocal
cd /d "%~dp0"

set "NODE=C:\Users\24942\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe"

if not exist "%NODE%" (
  echo [ERROR] 找不到 node.exe:
  echo         %NODE%
  pause & exit /b 1
)

echo ============================================
echo   Prompt Spider  ^<-^>  DSH 实时喂养器
echo ============================================
echo.
echo   正在连接 DSH 会话并建立预览链接…
echo   链接会打印在下面，打开它即可看到实时面板。
echo.
echo   保持本窗口开启 = 持续更新
echo   按 Ctrl+C 结束
echo.

"%NODE%" "mcp\live-feed.mjs" --interval 2.5 %*
pause
