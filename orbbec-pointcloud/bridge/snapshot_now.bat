@echo off
cd /d "%~dp0"
title Orbbec snapshot now
if not defined R2_ACCESS_KEY_ID (
  echo Keys not found. Double-click setup.bat first.
  pause
  exit /b 1
)
set "PY=%ORBBEC_PYTHON%"
if not defined PY set "PY=python"
"%PY%" snapshotter.py --once
timeout /t 5
