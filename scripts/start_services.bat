@echo off
rem Launch the helper services detached (start /b) so Hermes killing its own
rem child process tree does not take them down. persist_on_release is NOT enough.
rem Everything is resolved from this file's own folder + env vars, no fixed drives:
rem   HERMES_PYTHON      python executable (default: pythonw/python from PATH)
rem   HERMES_DESK        private desktop name      (default HermesDesk)
rem   HERMES_CDP_PORT    Chrome CDP port           (default 18763)
rem   HERMES_STREAM_PORT / HERMES_BROKER_PORT / HERMES_DRIVER_PORT
rem Keep this file ASCII-only: Chinese comments get mangled when run via git-bash,
rem and timeout.exe is shadowed by GNU timeout there, so wait with ping instead.
setlocal
cd /d "%~dp0"

if "%HERMES_PYTHON%"=="" (
  where pythonw.exe >nul 2>&1 && (set "PY=pythonw.exe") || (set "PY=python.exe")
) else (
  set "PY=%HERMES_PYTHON%"
)
if "%HERMES_DESK%"=="" set "HERMES_DESK=HermesDesk"
if "%HERMES_CDP_PORT%"=="" set "HERMES_CDP_PORT=18763"
if "%HERMES_STREAM_PORT%"=="" set "HERMES_STREAM_PORT=8765"
if "%HERMES_BROKER_PORT%"=="" set "HERMES_BROKER_PORT=8899"
if "%HERMES_DRIVER_PORT%"=="" set "HERMES_DRIVER_PORT=8897"

netstat -ano | findstr ":%HERMES_STREAM_PORT% " | findstr LISTENING >nul || start "" /b "%PY%" desk_stream.py --port %HERMES_STREAM_PORT% --desktop %HERMES_DESK% --cdp %HERMES_CDP_PORT%
netstat -ano | findstr ":%HERMES_BROKER_PORT% " | findstr LISTENING >nul || start "" /b "%PY%" vault_broker.py --port %HERMES_BROKER_PORT%
netstat -ano | findstr ":%HERMES_DRIVER_PORT% " | findstr LISTENING >nul || start "" /b "%PY%" browser_daemon.py --cdp %HERMES_CDP_PORT% --port %HERMES_DRIVER_PORT%

ping -n 9 127.0.0.1 >nul

for %%P in (%HERMES_STREAM_PORT% %HERMES_BROKER_PORT% %HERMES_DRIVER_PORT%) do (
  netstat -ano | findstr ":%%P " | findstr LISTENING >nul && echo %%P UP || echo %%P DOWN
)
endlocal
