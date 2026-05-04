@echo off
:: start.bat
:: Opens two separate Command Prompt windows:
::   1. uvicorn server:app --reload
::   2. python3 run_all_cameras.py (which opens one window per camera)

start "uvicorn" cmd /k "cd /d %~dp0 && uvicorn server:app --reload"
start "cameras" cmd /k "cd /d %~dp0 && python run_all_cameras.py"
