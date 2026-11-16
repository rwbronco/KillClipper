@echo off
title Kill Clipper - setup
cd /d "%~dp0"
echo.
echo Step 1 of 2: installing the add-ons Kill Clipper needs
echo (numpy, imageio-ffmpeg and py7zr). This only needs to be done once.
echo.
where py >nul 2>nul && (set "PY=py") || (set "PY=python")
%PY% -m pip install --upgrade numpy imageio-ffmpeg py7zr
if errorlevel 1 goto failed
echo.
echo Step 2 of 2: getting the video player (libmpv-2.dll, about 30 MB download)
echo.
%PY% "%~dp0kill_clipper.py" --install-player
if errorlevel 1 goto player_failed
echo.
echo All done! Now double-click "2 - Run Kill Clipper.bat".
echo.
pause
exit /b

:player_failed
echo.
echo The add-ons installed, but the video player didn't (see the message above).
echo Kill Clipper will still scan videos and save XML sequences, and you can
echo install the player later from inside Kill Clipper, or run this file again.
echo.
pause
exit /b

:failed
echo.
echo Setup did not finish. The most common cause is that Python isn't installed,
echo or wasn't added to PATH. Install it from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" on the first installer screen, then
echo run this file again.
echo.
pause
