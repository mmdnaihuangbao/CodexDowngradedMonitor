@echo off
setlocal
cd /d "%~dp0"

rem ---------------------------------------------------------------------
rem  Launcher for "Codex model downgrade monitor".
rem
rem  KEEP THIS FILE ASCII-ONLY.
rem  cmd.exe parses .bat with the local code page (GBK on Chinese
rem  Windows). Any non-ASCII byte can swallow the following character and
rem  break redirection -- typical symptom is "'nul' is not recognized as
rem  an internal or external command". All real logic lives in start.py,
rem  which handles its own encoding correctly.
rem
rem  Usage:  start.bat              start (opens the panel)
rem          start.bat --stop       stop the service
rem          start.bat --status     show status
rem          start.bat --workers 8  pass-through flags to start.py
rem ---------------------------------------------------------------------

call :pick_python
if not defined PY (
  echo [ERROR] Python 3.8+ not found.
  echo         Install Python, or add it to PATH, then retry.
  echo.
  pause
  exit /b 1
)

for %%A in (%*) do (
  if /i "%%~A"=="--stop" goto :sync
  if /i "%%~A"=="--status" goto :sync
)

start "" "%PY%" "%~dp0start.py" %*
exit /b 0

:sync
rem stop/status need a visible console, so use python.exe instead of pythonw.exe
set "PYC=%PY:pythonw.exe=python.exe%"
if not exist "%PYC%" set "PYC=%PY%"
"%PYC%" "%~dp0start.py" %*
echo.
pause
exit /b 0

:pick_python
set "PY="
if exist "%LOCALAPPDATA%\Programs\Python\Python314\pythonw.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python314\pythonw.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python310\pythonw.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python310\pythonw.exe"
if not defined PY if exist "C:\Python314\pythonw.exe" set "PY=C:\Python314\pythonw.exe"
if not defined PY if exist "C:\Python313\pythonw.exe" set "PY=C:\Python313\pythonw.exe"
if not defined PY if exist "C:\Python312\pythonw.exe" set "PY=C:\Python312\pythonw.exe"
if not defined PY if exist "C:\Python311\pythonw.exe" set "PY=C:\Python311\pythonw.exe"
if not defined PY if exist "C:\Python310\pythonw.exe" set "PY=C:\Python310\pythonw.exe"
if not defined PY for %%P in (pythonw.exe) do set "PY=%%~$PATH:P"
if not defined PY for %%P in (python.exe)  do set "PY=%%~$PATH:P"
goto :eof
