# Hosting on Windows 11

The code is identical on Windows; only process supervision differs.

Quick start (three console windows, restart by hand): run `deploy\start_windows.bat`.

Always-on services with NSSM (recommended if you stay on Windows):

1. `winget install NSSM.NSSM`
2. From an elevated PowerShell in the repo directory, for each of runner, bot, dashboard:

```
nssm install pm-runner "C:\path\to\repo\.venv\Scripts\python.exe" "-m runner"
nssm set pm-runner AppDirectory "C:\path\to\repo"
nssm set pm-runner AppStdout "C:\path\to\data\logs\runner.out.log"
nssm set pm-runner AppStderr "C:\path\to\data\logs\runner.err.log"
nssm set pm-runner AppRestartDelay 30000
nssm start pm-runner
```

Repeat with `-m bot` for `pm-bot` and `-m streamlit run dashboard\app.py --server.address 0.0.0.0 --server.port 8501 --server.headless true` for `pm-dashboard`.

3. Keep the laptop awake: `powercfg /change standby-timeout-ac 0` and `powercfg /change hibernate-timeout-ac 0`, and set lid close to "Do nothing" in Power Options.
4. Set Active Hours in Windows Update so forced reboots do not land during runs; services restart automatically after a reboot.
5. The services run as the user who logged into Claude Code (`claude` login); the Claude credentials live in that user's profile.
