@echo off
rem Starts VoiceAssist with a console window (useful for seeing logs). The Start-menu shortcut runs it without a console.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo VoiceAssist is not installed yet. Run:  powershell -ExecutionPolicy Bypass -File install.ps1
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m voiceassist %*
