@echo off
rem Double-click to open the VPU PCM dashboard (tools\dashboard\vpu_pcm_dashboard.py).
rem It finds the board by itself. Arguments are passed on, e.g. dashboard.bat --port SIM
rem The first run installs the Python packages it needs (requirements.txt).
cd /d "%~dp0"

set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
"%PY%" --version >nul 2>&1
if errorlevel 1 (
    echo Python was not found. Install Python 3 from python.org, ticking "Add python.exe to PATH".
    goto failed
)

"%PY%" -c "import pyqtgraph, PyQt6, serial, scipy, acquisition, everyday_sensing" >nul 2>&1
if errorlevel 1 (
    echo Installing the dashboard's Python packages, which happens once...
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 goto failed
)

echo Starting the dashboard; the window can take half a minute to appear.
"%PY%" tools\dashboard\vpu_pcm_dashboard.py %*
if errorlevel 1 goto failed
exit /b 0

:failed
echo.
echo The dashboard did not start: the messages above say why.
pause
exit /b 1
