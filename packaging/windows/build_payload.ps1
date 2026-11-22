# Assembles packaging\windows\payload for the Inno Setup installer. Runs on a GitHub Windows runner.
#   payload\python-x64, payload\python-arm64 : embeddable CPython + site-packages (+ MSVC runtime DLLs)
#   payload\app                              : voiceassist\ and web\
#   payload\models                           : default speech models
param(
  [string]$PyVersion = "3.12.10",
  [string]$SttModel = "whisper-base.en",
  [string]$Voice = "en_US-amy-low"
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Resolve-Path (Join-Path $Here "..\..")
$Payload = Join-Path $Here "payload"
if (Test-Path $Payload) { Remove-Item -Recurse -Force $Payload }
New-Item -ItemType Directory -Force $Payload | Out-Null
$PyTag = "python" + ($PyVersion.Split(".")[0..1] -join "")   # python312

# MSVC runtime DLLs (app-local, so a clean PC doesn't need the VC++ redistributable)
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
$VsPath = & $vswhere -latest -products * -property installationPath
$Redist = Get-ChildItem (Join-Path $VsPath "VC\Redist\MSVC") -Directory | Where-Object { $_.Name -match '^\d' } | Sort-Object Name -Descending | Select-Object -First 1

foreach ($arch in @("amd64", "arm64")) {
  $name = if ($arch -eq "amd64") { "python-x64" } else { "python-arm64" }
  $dest = Join-Path $Payload $name
  Write-Host "== $name"
  $zip = Join-Path $env:RUNNER_TEMP "py-$arch.zip"
  Invoke-WebRequest "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-embed-$arch.zip" -OutFile $zip
  Expand-Archive $zip -DestinationPath $dest
  # Make the embedded interpreter see site-packages and the app folder
  @("$PyTag.zip", ".", "Lib\site-packages", "..\app", "import site") | Set-Content -Encoding ascii (Join-Path $dest "$PyTag._pth")
  $site = Join-Path $dest "Lib\site-packages"
  New-Item -ItemType Directory -Force $site | Out-Null
  $plat = if ($arch -eq "amd64") { "win_amd64" } else { "win_arm64" }
  python -m pip install --disable-pip-version-check --no-compile --only-binary=:all: --platform $plat `
    --python-version ($PyVersion.Split(".")[0..1] -join ".") --implementation cp --target $site -r (Join-Path $Root "requirements.txt")
  if ($LASTEXITCODE -ne 0) { throw "pip install failed for $arch" }
  $crtArch = if ($arch -eq "amd64") { "x64" } else { "arm64" }
  $crt = Get-ChildItem (Join-Path $Redist.FullName $crtArch) -Directory -Filter "Microsoft.VC*.CRT" -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($crt) {
    Copy-Item (Join-Path $crt.FullName "*.dll") $dest -Force
    Write-Host "   MSVC runtime from $($crt.FullName)"
  } else {
    Write-Warning "MSVC runtime for $crtArch not found on the build machine"
  }
}

Write-Host "== app"
$app = Join-Path $Payload "app"
New-Item -ItemType Directory -Force $app | Out-Null
Copy-Item -Recurse (Join-Path $Root "voiceassist") $app
Copy-Item -Recurse (Join-Path $Root "web") $app
Get-ChildItem $app -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force

Write-Host "== speech models"
$env:VOICEASSIST_MODELS = Join-Path $Payload "models"
$env:VOICEASSIST_HOME = Join-Path $env:RUNNER_TEMP "va-build-home"
Push-Location $Root
python -m voiceassist.setup_models --stt $SttModel --voice $Voice
if ($LASTEXITCODE -ne 0) { throw "model download failed" }
Pop-Location

Get-ChildItem $Payload | ForEach-Object {
  $size = (Get-ChildItem $_.FullName -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
  Write-Host ("{0,-14} {1,8:N1} MB" -f $_.Name, $size)
}
