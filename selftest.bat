@echo off
rem Runs the complete voice loop with ALL network access blocked. Use it in airplane mode to prove offline operation.
cd /d "%~dp0"
".venv\Scripts\python.exe" -m voiceassist.diagnostics
pause
