@echo off
setlocal

rem Start the verified Windows local runner. Keep this window open.
set "RUNNER_DIR=%WB_RUNNER_DIR%"
if not defined RUNNER_DIR set "RUNNER_DIR=C:\Users\gnen0\WorkBuddy\2026-09-03-12-12-43\.workbuddy\tmp_wb\runner"
set "PY=C:\Users\gnen0\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not exist "%PY%" set "PY=python"

if not exist "%RUNNER_DIR%\local_runner.py" (
  echo local_runner.py was not found: "%RUNNER_DIR%\local_runner.py"
  exit /b 1
)

pushd "%RUNNER_DIR%"
"%PY%" "%RUNNER_DIR%\local_runner.py" --port 8790
set "EXIT_CODE=%errorlevel%"
popd
exit /b %EXIT_CODE%
