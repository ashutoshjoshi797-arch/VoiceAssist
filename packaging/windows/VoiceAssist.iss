; VoiceAssist Windows installer (Inno Setup 6).
; Built by .github/workflows/build.yml from the payload that build_payload.ps1 assembles:
;   payload\python-x64\   embedded Python 3.12 (x64)   + packages
;   payload\python-arm64\ embedded Python 3.12 (ARM64) + packages
;   payload\app\          voiceassist\ + web\
;   payload\models\       speech models (Whisper, Piper voice, Silero VAD)
; Per-user install, no admin rights. The right Python is chosen for the PC (ARM64 or x64).

#ifndef AppVer
  #define AppVer "1.1.0"
#endif

[Setup]
AppId={{6C4B8E5A-3F1D-4C2B-9A7E-5D0F2B8C1A94}
AppName=VoiceAssist
AppVersion={#AppVer}
AppVerName=VoiceAssist {#AppVer}
AppPublisher=VoiceAssist
DefaultDirName={localappdata}\Programs\VoiceAssist
DefaultGroupName=VoiceAssist
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible or arm64
ArchitecturesInstallIn64BitMode=x64compatible or arm64
OutputDir=..\..\dist
OutputBaseFilename=VoiceAssist-Setup-{#AppVer}
SetupIconFile=voiceassist.ico
UninstallDisplayIcon={app}\voiceassist.ico
UninstallDisplayName=VoiceAssist
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
RestartApplications=no
MinVersion=10.0.17763

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "payload\python-x64\*";   DestDir: "{app}\python"; Check: not IsArm64; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "payload\python-arm64\*"; DestDir: "{app}\python"; Check: IsArm64;     Flags: recursesubdirs createallsubdirs ignoreversion
Source: "payload\app\*";          DestDir: "{app}\app";    Flags: recursesubdirs createallsubdirs ignoreversion
; speech models go where the app looks for them; existing (possibly newer) models are kept
Source: "payload\models\*";       DestDir: "{localappdata}\VoiceAssist\models"; Flags: recursesubdirs createallsubdirs onlyifdoesntexist uninsneveruninstall
Source: "voiceassist.ico";        DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
; remove code from a previous version so no stale files remain
Type: filesandordirs; Name: "{app}\app"

[Icons]
Name: "{autoprograms}\VoiceAssist"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m voiceassist"; WorkingDir: "{app}\app"; IconFilename: "{app}\voiceassist.ico"; Comment: "AI voice assistant (works offline)"
Name: "{autodesktop}\VoiceAssist";  Filename: "{app}\python\pythonw.exe"; Parameters: "-m voiceassist"; WorkingDir: "{app}\app"; IconFilename: "{app}\voiceassist.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: "-m voiceassist"; WorkingDir: "{app}\app"; Description: "Start VoiceAssist now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-NoProfile -NonInteractive -WindowStyle Hidden -Command ""Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*-m voiceassist*' } | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }"""; Flags: runhidden; RunOnceId: "StopVoiceAssist"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\app"

[Code]
procedure StopRunningApp();
var
  ResultCode: Integer;
begin
  { stop a running VoiceAssist engine so its files can be replaced }
  Exec('powershell.exe',
    '-NoProfile -NonInteractive -WindowStyle Hidden -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like ''*-m voiceassist*'' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopRunningApp();
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and (not UninstallSilent) then
  begin
    if MsgBox('Also delete your VoiceAssist accounts, conversation history and downloaded speech models from this computer?' + #13#10#13#10 +
              'Choose No to keep them for a future installation.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(ExpandConstant('{localappdata}\VoiceAssist'), True, True, True);
  end;
end;
