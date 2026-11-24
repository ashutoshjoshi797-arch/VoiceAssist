; VoiceAssist Windows installer (NSIS 3) - an alternative to VoiceAssist.iss that can also be built on Linux:
;   NSISDIR=<nsis> makensis -DAPPVER=1.1.1 -DPAYLOAD=<payload dir> -DOUTFILE=<exe> VoiceAssist.nsi
; Payload layout (same as for the Inno Setup build):
;   python-x64\  python-arm64\   relocatable CPython 3.12 + packages
;   app\                          voiceassist\ + web\
;   models\                       speech models (Whisper, Piper voice, Silero VAD)
; Per-user install, no admin rights. The right Python is chosen for the PC (ARM64 or x64).
Unicode true
ManifestDPIAware true
SetCompressor /SOLID lzma
SetCompressorDictSize 64
RequestExecutionLevel user

!ifndef APPVER
  !define APPVER "1.1.1"
!endif
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\VoiceAssist"
!define STOP_APP `powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -Command "Get-CimInstance Win32_Process | Where-Object { $$_.CommandLine -like '*-m voiceassist*' } | ForEach-Object { Stop-Process -Id $$_.ProcessId -Force }"`

!include "MUI2.nsh"
!include "x64.nsh"
!include "LogicLib.nsh"

Name "VoiceAssist"
Caption "VoiceAssist ${APPVER} Setup"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\Programs\VoiceAssist"
BrandingText "VoiceAssist ${APPVER}"
VIProductVersion "${APPVER}.0"
VIAddVersionKey "ProductName" "VoiceAssist"
VIAddVersionKey "FileDescription" "VoiceAssist Setup"
VIAddVersionKey "FileVersion" "${APPVER}"
VIAddVersionKey "ProductVersion" "${APPVER}"
VIAddVersionKey "LegalCopyright" "VoiceAssist"

!define MUI_ICON "voiceassist.ico"
!define MUI_UNICON "voiceassist.ico"
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TITLE "Welcome to VoiceAssist"
!define MUI_WELCOMEPAGE_TEXT "VoiceAssist lets you talk to an AI and hear it answer - online with Claude, OpenAI or Gemini, and offline (even in airplane mode) with AI that runs on this computer.$\r$\n$\r$\nSetup installs it for your Windows account only. No administrator rights are needed.$\r$\n$\r$\nClick Install to continue."
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "Start VoiceAssist now"
!define MUI_FINISHPAGE_RUN_FUNCTION StartApp
!define MUI_FINISHPAGE_TEXT "VoiceAssist is installed. You'll find it in the Start menu and on your desktop.$\r$\n$\r$\nFirst start: create your account, then click 'Set up offline AI' once while you're online."

!define MUI_PAGE_CUSTOMFUNCTION_SHOW WelcomeShow
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Function WelcomeShow
  ; the Welcome page's Next button installs straight away
  GetDlgItem $0 $HWNDPARENT 1
  SendMessage $0 ${WM_SETTEXT} 0 "STR:&Install"
FunctionEnd

Function .onInit
  ${IfNot} ${RunningX64}
  ${AndIfNot} ${IsNativeARM64}
    MessageBox MB_ICONSTOP "VoiceAssist needs 64-bit Windows 10 or 11 (x64 or ARM64)."
    Abort
  ${EndIf}
FunctionEnd

Function StartApp
  SetOutPath "$INSTDIR\app"
  Exec '"$INSTDIR\python\pythonw.exe" -m voiceassist'
FunctionEnd

Section "Install"
  DetailPrint "Closing VoiceAssist if it is running..."
  nsExec::Exec '${STOP_APP}'
  Pop $0
  RMDir /r "$INSTDIR\app"
  RMDir /r "$INSTDIR\python"

  SetOutPath "$INSTDIR\python"
  ${If} ${IsNativeARM64}
    DetailPrint "Installing the ARM64 engine..."
    File /r "${PAYLOAD}\python-arm64\*.*"
  ${Else}
    DetailPrint "Installing the x64 engine..."
    File /r "${PAYLOAD}\python-x64\*.*"
  ${EndIf}

  SetOutPath "$INSTDIR\app"
  File /r "${PAYLOAD}\app\*.*"
  SetOutPath "$INSTDIR"
  File "voiceassist.ico"

  ; speech models go where the app looks for them; existing (possibly newer) models are kept
  DetailPrint "Installing the on-device speech models..."
  SetOverwrite off
  SetOutPath "$LOCALAPPDATA\VoiceAssist\models"
  File /r "${PAYLOAD}\models\*.*"
  SetOverwrite on

  SetShellVarContext current
  SetOutPath "$INSTDIR\app"
  CreateShortcut "$SMPROGRAMS\VoiceAssist.lnk" "$INSTDIR\python\pythonw.exe" "-m voiceassist" "$INSTDIR\voiceassist.ico" 0 SW_SHOWNORMAL "" "AI voice assistant (works offline)"
  CreateShortcut "$DESKTOP\VoiceAssist.lnk" "$INSTDIR\python\pythonw.exe" "-m voiceassist" "$INSTDIR\voiceassist.ico" 0 SW_SHOWNORMAL "" "AI voice assistant (works offline)"

  WriteUninstaller "$INSTDIR\Uninstall VoiceAssist.exe"
  SetRegView 64
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "VoiceAssist"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${APPVER}"
  WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "VoiceAssist"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\voiceassist.ico"
  WriteRegStr HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\Uninstall VoiceAssist.exe"'
  WriteRegStr HKCU "${UNINST_KEY}" "QuietUninstallString" '"$INSTDIR\Uninstall VoiceAssist.exe" /S'
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1
SectionEnd

Section "Uninstall"
  nsExec::Exec '${STOP_APP}'
  Pop $0
  SetShellVarContext current
  Delete "$SMPROGRAMS\VoiceAssist.lnk"
  Delete "$DESKTOP\VoiceAssist.lnk"
  RMDir /r "$INSTDIR\python"
  RMDir /r "$INSTDIR\app"
  Delete "$INSTDIR\voiceassist.ico"
  Delete "$INSTDIR\Uninstall VoiceAssist.exe"
  RMDir "$INSTDIR"
  SetRegView 64
  DeleteRegKey HKCU "${UNINST_KEY}"
  IfSilent done
  MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 "Also delete your VoiceAssist accounts, conversation history and downloaded models from this computer?$\r$\n$\r$\nChoose No to keep them for a future installation." IDNO done
  RMDir /r "$LOCALAPPDATA\VoiceAssist"
  done:
SectionEnd
