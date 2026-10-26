@echo off
setlocal
cd /d "%~dp0"
title Orbbec snapshot setup
echo.
echo  ==============================================
echo   Orbbec Astra 2 snapshots - one-time setup
echo  ==============================================
echo.

rem ---- find Python: the "orbbec" conda environment first, then Anaconda base, then PATH ----
set "PY="
for %%P in ("%USERPROFILE%\.conda\envs\orbbec\python.exe" "%USERPROFILE%\anaconda3\envs\orbbec\python.exe" "%USERPROFILE%\miniconda3\envs\orbbec\python.exe" "%ProgramData%\anaconda3\envs\orbbec\python.exe" "%USERPROFILE%\anaconda3\python.exe" "%USERPROFILE%\miniconda3\python.exe" "%LOCALAPPDATA%\anaconda3\python.exe" "%ProgramData%\anaconda3\python.exe" "%ProgramData%\miniconda3\python.exe") do (
  if not defined PY if exist "%%~P" set "PY=%%~P"
)
if not defined PY for /f "delims=" %%P in ('where python 2^>nul ^| findstr /v /i "WindowsApps"') do (
  if not defined PY set "PY=%%P"
)
if not defined PY goto nopython
echo  Python found: %PY%
setx ORBBEC_PYTHON "%PY%" >nul

rem ---- bucket details ----
set "R2_ACCOUNT_ID=cda5eaaba44345e253835586b1cc1b29"
set "R2_BUCKET=orbbecstream"
setx R2_ACCOUNT_ID "%R2_ACCOUNT_ID%" >nul
setx R2_BUCKET "%R2_BUCKET%" >nul

echo.
echo  Paste your R2 keys below (right-click pastes in this window).
echo  They are saved only on this PC, for your Windows user.
echo.
set /p "R2_ACCESS_KEY_ID=  Access Key ID:     "
set /p "R2_SECRET_ACCESS_KEY=  Secret Access Key: "
if "%R2_ACCESS_KEY_ID%"=="" goto nokeys
if "%R2_SECRET_ACCESS_KEY%"=="" goto nokeys
setx R2_ACCESS_KEY_ID "%R2_ACCESS_KEY_ID%" >nul
setx R2_SECRET_ACCESS_KEY "%R2_SECRET_ACCESS_KEY%" >nul

rem ---- packages ----
echo.
echo  Installing packages, this can take a minute...
"%PY%" -m pip install --quiet numpy boto3 pillow
if errorlevel 1 goto pipfail
"%PY%" -m pip install --quiet pyorbbecsdk2
if errorlevel 1 (
  echo.
  echo  NOTE: the camera library pyorbbecsdk2 did not install.
  echo  The upload test below still works. Send the message above to get help with the camera part.
)

rem ---- test upload ----
echo.
echo  Testing the upload to your bucket...
echo.
"%PY%" snapshotter.py --mock --test
if errorlevel 1 goto testfail

echo.
echo  ==============================================
echo   All set. Plug in the camera and double-click
echo   run_snapshots.bat to start taking snapshots.
echo  ==============================================
echo.
pause
exit /b 0

:nopython
echo  Could not find Python. Install Anaconda or Python from python.org, then run this again.
pause
exit /b 1

:nokeys
echo.
echo  No key entered. Run setup.bat again and paste both keys.
pause
exit /b 1

:pipfail
echo.
echo  Installing numpy/boto3/pillow failed. Check your internet connection and run setup.bat again.
pause
exit /b 1

:testfail
echo.
echo  The test failed - see the message above. You can run setup.bat again to re-enter the keys.
pause
exit /b 1
