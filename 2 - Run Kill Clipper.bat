@echo off
rem Starts Kill Clipper without a console window (using pythonw / pyw).
rem You can also drag a video onto this file to open Kill Clipper with it.
cd /d "%~dp0"
where pyw >nul 2>nul && (start "" pyw "%~dp0kill_clipper.py" %1 & exit /b)
where pythonw >nul 2>nul && (start "" pythonw "%~dp0kill_clipper.py" %1 & exit /b)

rem Fallback: no windowless Python found, so run it with a console window
where py >nul 2>nul && (set "PY=py") || (set "PY=python")
%PY% "%~dp0kill_clipper.py" %1
if errorlevel 1 pause
