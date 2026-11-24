<#
.SYNOPSIS
  Installs VoiceAssist and everything it needs to work OFFLINE (run this while you have internet).

.DESCRIPTION
  1. Finds (or installs) Python 3.12/3.13 - native ARM64 on Windows-on-ARM, x64 on Intel/AMD.
  2. Creates .venv and installs the Python packages.
  3. Downloads the on-device speech models (Whisper speech recognition, Piper voice, Silero VAD).
  4. Installs Ollama if missing and pulls the local AI model.
  5. Verifies everything and runs the offline self-test.
  6. Creates Start-menu and Desktop shortcuts.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\install.ps1
  powershell -ExecutionPolicy Bypass -File .\install.ps1 -LowRam
  powershell -ExecutionPolicy Bypass -File .\install.ps1 -LocalModel gemma3:4b -SttModel whisper-small.en -Voice en_US-lessac-medium
#>
[CmdletBinding()]
param(
  [string]$LocalModel = "llama3.2:3b",
  [string]$SttModel = "whisper-base.en",
  [string]$Voice = "en_US-amy-low",
  [switch]$LowRam,          # smaller models for 4-8 GB RAM computers
  [switch]$SkipOllama,
  [switch]$NoShortcuts
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "    [ok] $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "    [!]  $msg" -ForegroundColor Yellow }

if ($LowRam) {
  if ($LocalModel -eq "llama3.2:3b") { $LocalModel = "llama3.2:1b" }
  if ($SttModel -eq "whisper-base.en") { $SttModel = "whisper-tiny.en" }
}

$IsArm = ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") -or ($env:PROCESSOR_ARCHITEW6432 -eq "ARM64")
$Arch = if ($IsArm) { "arm64" } else { "x64" }
Write-Host "VoiceAssist installer - Windows $Arch" -ForegroundColor White

# ------------------------------------------------------------------ 1. Python
Step "Looking for Python 3.12 or 3.13"
function Find-Python {
  foreach ($v in @("3.13", "3.12")) {
    try {
      $exe = & py "-$v" -c "import sys; print(sys.executable)" 2>$null
      if ($LASTEXITCODE -eq 0 -and $exe) { return $exe.Trim() }
    } catch {}
  }
  foreach ($cmd in @("python", "python3")) {
    $c = Get-Command $cmd -ErrorAction SilentlyContinue
    if ($c -and $c.Source -notlike "*WindowsApps*") {
      $ver = & $c.Source -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
      if ($ver -in @("3.12", "3.13")) { return $c.Source }
    }
  }
  return $null
}
$Py = Find-Python
if (-not $Py) {
  Warn "Python 3.12/3.13 not found."
  if (Get-Command winget -ErrorAction SilentlyContinue) {
    $ans = Read-Host "    Install Python 3.12 ($Arch) with winget now? [Y/n]"
    if ($ans -eq "" -or $ans -match "^[Yy]") {
      winget install -e --id Python.Python.3.12 --architecture $Arch --accept-package-agreements --accept-source-agreements
      $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
      $Py = Find-Python
    }
  }
  if (-not $Py) { throw "Please install Python 3.12 from https://www.python.org/downloads/windows/ (choose the ARM64 installer on ARM PCs) and run this script again." }
}
$PyArch = & $Py -c "import platform; print(platform.machine())"
Ok "Python: $Py ($PyArch)"
if ($IsArm -and $PyArch -ne "ARM64") {
  Warn "This is an ARM64 PC but Python is $PyArch (runs under emulation, ~2x slower). Native ARM64 Python is recommended."
}

# ------------------------------------------------------------------ 2. venv + packages
Step "Creating virtual environment and installing packages"
if (-not (Test-Path ".venv\Scripts\python.exe")) { & $Py -m venv .venv }
$VPy = Join-Path $Root ".venv\Scripts\python.exe"
& $VPy -m pip install --upgrade pip --disable-pip-version-check -q
& $VPy -m pip install -r requirements.txt --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
& $VPy -c "import sherpa_onnx, numpy, psutil; print('sherpa-onnx', sherpa_onnx.__version__)"
Ok "Python packages installed"

# ------------------------------------------------------------------ 3. speech models
Step "Downloading on-device speech models ($SttModel, voice $Voice, Silero VAD)"
& $VPy -m voiceassist.setup_models --stt $SttModel --voice $Voice
if ($LASTEXITCODE -ne 0) { throw "model download failed" }

# Save the chosen models as the app defaults
& $VPy -m voiceassist.setup_models --save-defaults --stt $SttModel --voice $Voice --local-model $LocalModel
if ($LASTEXITCODE -ne 0) { throw "could not save settings" }

# ------------------------------------------------------------------ 4. Ollama + local model
if (-not $SkipOllama) {
  Step "Local AI engine (Ollama) and model $LocalModel"
  function Find-Ollama {
    $c = Get-Command ollama -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    $p = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
    if (Test-Path $p) { return $p }
    return $null
  }
  $Ollama = Find-Ollama
  if (-not $Ollama -and (Get-Command winget -ErrorAction SilentlyContinue)) {
    $ans = Read-Host "    Ollama is not installed. Install it with winget now? [Y/n]"
    if ($ans -eq "" -or $ans -match "^[Yy]") {
      winget install -e --id Ollama.Ollama --accept-package-agreements --accept-source-agreements
      Start-Sleep -Seconds 3
      $Ollama = Find-Ollama
    }
  }
  if (-not $Ollama) {
    Warn "Ollama not found. Install it from https://ollama.com/download, then run:  ollama pull $LocalModel"
  } else {
    Ok "Ollama: $Ollama"
    try { Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/version" -TimeoutSec 2 | Out-Null }
    catch { Start-Process -FilePath $Ollama -ArgumentList "serve" -WindowStyle Hidden; Start-Sleep -Seconds 4 }
    & $Ollama pull $LocalModel
    if ($LASTEXITCODE -ne 0) { Warn "ollama pull failed - check the model name at https://ollama.com/library" }
  }
}

# ------------------------------------------------------------------ 5. verify
Step "Verifying offline readiness"
& $VPy -m voiceassist.setup_models --verify
Step "Running the offline self-test (all network access blocked inside the test)"
& $VPy -m voiceassist.diagnostics
if ($LASTEXITCODE -eq 0) { Ok "Offline voice loop works on this computer" } else { Warn "Self-test reported problems (see above)" }

# ------------------------------------------------------------------ 6. shortcuts
if (-not $NoShortcuts) {
  Step "Creating shortcuts"
  $shell = New-Object -ComObject WScript.Shell
  $pyw = Join-Path $Root ".venv\Scripts\pythonw.exe"
  foreach ($dir in @([Environment]::GetFolderPath("Programs"), [Environment]::GetFolderPath("Desktop"))) {
    $lnk = $shell.CreateShortcut((Join-Path $dir "VoiceAssist.lnk"))
    $lnk.TargetPath = $pyw
    $lnk.Arguments = "-m voiceassist"
    $lnk.WorkingDirectory = $Root
    $lnk.Description = "AI voice assistant (works offline)"
    $lnk.IconLocation = "$env:SystemRoot\System32\SndVol.exe,0"
    $lnk.Save()
  }
  Ok "Start menu + Desktop shortcut 'VoiceAssist' created"
}

Write-Host "`nDone. Start VoiceAssist from the Start menu / Desktop, or run .\run.bat" -ForegroundColor Green
Write-Host "Online answers come from VoiceAssist Cloud - just sign in with your email in the app. No API key needed." -ForegroundColor Gray
