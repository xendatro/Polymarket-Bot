@echo off
cd /d %~dp0..
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
start "pm-runner" cmd /k ".venv\Scripts\python.exe -m runner"
start "pm-bot" cmd /k ".venv\Scripts\python.exe -m bot"
start "pm-dashboard" cmd /k ".venv\Scripts\python.exe -m streamlit run dashboard\app.py --server.address 0.0.0.0 --server.port 8501 --server.headless true"
