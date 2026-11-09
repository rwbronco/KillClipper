@echo off
title Kill Clipper - setup
echo.
echo Installing the two add-ons Kill Clipper needs: numpy and imageio-ffmpeg
echo This only needs to be done once.
echo.
where py >nul 2>nul && (set "PY=py") || (set "PY=python")
%PY% -m pip install --upgrade numpy imageio-ffmpeg
if errorlevel 1 goto failed
echo.
echo All done! Now double-click "2 - Run Kill Clipper.bat".
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
