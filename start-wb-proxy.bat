@echo off
setlocal EnableExtensions
rem ===========================================================
rem  WorkBuddy international proxy - launcher
rem  Usage: double-click, or  start-wb-proxy.bat [port]
rem
rem  ASCII-only on purpose: .bat files are read using the
rem  console code page, so non-ASCII text breaks the parser.
rem
rem  NOTE: this script puts no %VAR% inside an if( ... ) block,
rem  because a path containing ( or ) breaks the cmd.exe parser.
rem  All checks use if/errorlevel/goto instead.
rem ===========================================================

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8788"
set "HERE=%~dp0"
set "SCRIPT=%HERE%wb_proxy.py"

if not exist "%SCRIPT%" goto no_script

set "PYEXE="

rem ---- 1) bundled runtime (ships with the zip) ----
if not exist "%HERE%python\python.exe" goto try_path
"%HERE%python\python.exe" --version >nul 2>nul
if errorlevel 1 goto try_path
set "PYEXE=%HERE%python\python.exe"
goto run

:try_path
rem ---- 2) python on PATH ----
python --version >nul 2>nul
if errorlevel 1 goto try_py
set "PYEXE=python"
goto run

:try_py
rem ---- 3) py launcher ----
py --version >nul 2>nul
if errorlevel 1 goto try_codex
set "PYEXE=py"
goto run

:try_codex
rem ---- 4) Codex bundled runtimes ----
call :find_codex
if defined PYEXE goto run

echo [ERROR] No usable Python found.
echo.
echo Options:
echo   1. Use the packaged zip, which already contains python\
echo   2. Install Python 3.9+ from https://www.python.org/downloads/
echo      (tick "Add python.exe to PATH" during setup)
echo   3. Edit this file and set PYEXE to a full path, e.g.
echo        set "PYEXE=C:\Python312\python.exe"
echo.
pause
exit /b 1

:no_script
echo [ERROR] wb_proxy.py not found next to this script.
echo         expected: %SCRIPT%
echo.
pause
exit /b 1

:find_codex
if not exist "%USERPROFILE%\.cache\codex-runtimes" goto :eof
for /d %%D in ("%USERPROFILE%\.cache\codex-runtimes\*") do call :probe "%%~D"
goto :eof

:probe
if defined PYEXE goto :eof
for /f "delims=" %%P in ('dir /b /s "%~1\python.exe" 2^>nul') do call :probe_one "%%~P"
goto :eof

:probe_one
if defined PYEXE goto :eof
"%~1" --version >nul 2>nul
if errorlevel 1 goto :eof
set "PYEXE=%~1"
goto :eof

:run
echo ===========================================================
echo   WorkBuddy proxy
echo.
echo   API      : http://127.0.0.1:%PORT%/v1
echo   Dashboard: http://127.0.0.1:%PORT%/
echo.
echo   Python   : %PYEXE%
echo.
echo   Keep this window open. Closing it stops the server.
echo   Press Ctrl+C to stop.
echo ===========================================================
echo.

set "ERRORLOG=%HERE%wb-proxy-errors.log"
set "RESTARTS=0"

:run_server
echo [%DATE% %TIME%] starting proxy >> "%ERRORLOG%"
"%PYEXE%" -u -X faulthandler "%SCRIPT%" --port %PORT% 2>> "%ERRORLOG%"
set "SERVER_EXIT=%ERRORLEVEL%"
echo [%DATE% %TIME%] exit code %SERVER_EXIT% >> "%ERRORLOG%"
echo [server exited: code %SERVER_EXIT%]
echo Error log: %ERRORLOG%
if "%SERVER_EXIT%"=="0" goto finished
type "%ERRORLOG%"
if "%SERVER_EXIT%"=="1" goto finished
if "%SERVER_EXIT%"=="130" goto finished
if "%SERVER_EXIT%"=="-1073741510" goto finished
if %RESTARTS% GEQ 3 goto finished
set /a RESTARTS+=1 >nul
echo Restarting after unexpected exit, attempt %RESTARTS% of 3...
timeout /t 3 /nobreak >nul
goto run_server

:finished
echo.
pause
exit /b %SERVER_EXIT%
