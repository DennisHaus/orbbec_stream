@echo off
cd /d "%~dp0"
title Orbbec snapshots - every 15 minutes
if not defined R2_ACCESS_KEY_ID (
  echo Keys not found. Double-click setup.bat first.
  pause
  exit /b 1
)
set "PY=%ORBBEC_PYTHON%"
if not defined PY set "PY=python"
echo Taking a snapshot every 15 minutes. Close this window to stop.
"%PY%" snapshotter.py --interval 15
pause
