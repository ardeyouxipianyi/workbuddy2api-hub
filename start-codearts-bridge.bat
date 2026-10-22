@echo off
setlocal
cd /d "%~dp0"
if exist "python\python.exe" (
  "python\python.exe" start-codearts-bridge.py %*
) else (
  python start-codearts-bridge.py %*
)
endlocal
