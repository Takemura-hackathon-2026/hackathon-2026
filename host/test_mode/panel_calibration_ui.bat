@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0\..\.."

where py >nul 2>&1
if not errorlevel 1 (
    py -3 host\test_mode\panel_calibration_ui.py %*
    exit /b !errorlevel!
)

where python >nul 2>&1
if errorlevel 1 (
    echo Python 3 が見つかりません。python.orgのPython 3.10以降をインストールしてください。
    exit /b 1
)
python host\test_mode\panel_calibration_ui.py %*
exit /b !errorlevel!
