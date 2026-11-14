; Whisper Unchained Server: the Windows .exe installer (Inno Setup 6).
; Build it with installer\build_exe.ps1, which stages the files and runs ISCC.
;
; What the .exe does: copies the app into the user's own folder (no admin
; needed, and Settings can write .env there), adds Start-menu entries, then
; offers to run Setup.bat, which downloads Python + packages and opens the
; setup wizard (that wizard asks for admin once, for the background service).
; Uninstalling runs installer\uninstall_app.py on the app's own Python (Smart
; App Control blocks the unsigned unins000.exe). unins000.exe stays as the
; fallback: it removes the service, login shortcut and right-click entry
; (installer\uninstall.ps1), then asks before deleting the user's own data.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef StageDir
  #define StageDir "build\stage"
#endif

[Setup]
; Never change AppId: Windows uses it to recognise upgrades of the same app.
AppId={{8C1F5B2E-4D7A-4E7B-9C2A-6B0F3E1D9A57}
AppName=Whisper Unchained Server
AppVersion={#AppVersion}
AppVerName=Whisper Unchained Server {#AppVersion}
AppPublisher=Whisper Unchained (open source)
AppPublisherURL=https://github.com/Dominic-Shirazi/Whisper-Unchained-Server
AppSupportURL=https://github.com/Dominic-Shirazi/Whisper-Unchained-Server/issues
DefaultDirName={localappdata}\Programs\Whisper Unchained
DefaultGroupName=Whisper Unchained
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
LicenseFile={#StageDir}\LICENSE
SetupIconFile={#StageDir}\client\assets\app_icon.ico
UninstallDisplayIcon={app}\client\assets\app_icon.ico
UninstallDisplayName=Whisper Unchained Server
OutputDir=dist
OutputBaseFilename=WhisperUnchainedServer-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Files are plain scripts; nothing here needs a reboot or closing apps.
CloseApplications=no
RestartIfNeededByRun=no
; Inno Setup 6.7 passes its RedirectionGuard on to the "Finish setup" step it
; starts, and there it makes Windows refuse the folder link uv creates for its
; private Python ("untrusted mount point", os error 448). This installer runs
; without admin rights, which is what the guard protects, so it's off.
RedirectionGuard=no

[Files]
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\Whisper Unchained"; Filename: "{app}\.venv\Scripts\pythonw.exe"; \
  Parameters: """{app}\client\listener_watchdog.py"""; WorkingDir: "{app}"; \
  IconFilename: "{app}\client\assets\app_icon.ico"; Comment: "Start the dictation hotkey and tray icon"
Name: "{group}\Whisper Unchained Settings"; Filename: "{app}\.venv\Scripts\pythonw.exe"; \
  Parameters: """{app}\client\settings_gui.py"""; WorkingDir: "{app}"; \
  IconFilename: "{app}\client\assets\app_icon.ico"
Name: "{group}\Repair or change setup"; Filename: "{app}\Setup.bat"; WorkingDir: "{app}"; \
  IconFilename: "{app}\client\assets\app_icon.ico"
; Uninstall: the app's own Python runs installer\uninstall_app.py, because Smart
; App Control blocks the unsigned unins000.exe (see that file). Before Finish
; setup has made the .venv, unins000.exe is all there is; bootstrap.ps1 then
; re-points this shortcut (uninstall_app.py --register).
Name: "{group}\Uninstall Whisper Unchained Server"; Filename: "{app}\.venv\Scripts\pythonw.exe"; \
  Parameters: """{app}\installer\uninstall_app.py"""; WorkingDir: "{localappdata}"; \
  IconFilename: "{app}\client\assets\app_icon.ico"; Check: VenvReady
Name: "{group}\Uninstall Whisper Unchained Server"; Filename: "{uninstallexe}"; Check: not VenvReady

[Run]
Filename: "{app}\Setup.bat"; WorkingDir: "{app}"; \
  Description: "Finish setup now: downloads its own Python, speech engine and model into this folder (a few minutes)"; \
  Flags: postinstall shellexec skipifsilent

[Messages]
FinishedLabel=The app is copied to your PC. One step is left: Finish setup.%n%nIt downloads, into this folder only:%n   - its own private Python%n   - the speech engine (plus NVIDIA's libraries if you have an NVIDIA card)%n   - a speech model%n%nNothing goes into a system Python, PATH or your other projects, so it can't conflict with them. Uninstalling removes it all.

[UninstallRun]
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\installer\uninstall.ps1"" -AppDir ""{app}"""; \
  Flags: runhidden waituntilterminated; RunOnceId: "RemoveServiceShortcutsMenu"

[UninstallDelete]
; Made by Setup.bat, not by this installer, and safe to rebuild.
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\installer\.tools"
Type: filesandordirs; Name: "{app}\installer\logs"
Type: filesandordirs; Name: "{app}\api\__pycache__"
Type: filesandordirs; Name: "{app}\client\__pycache__"
Type: filesandordirs; Name: "{app}\installer\__pycache__"
Type: filesandordirs; Name: "{app}\scripts\__pycache__"

[Code]
const
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{8C1F5B2E-4D7A-4E7B-9C2A-6B0F3E1D9A57}_is1';

function VenvReady: Boolean;
begin
  Result := FileExists(ExpandConstant('{app}\.venv\Scripts\pythonw.exe'));
end;

// Windows' Settings > Apps > Uninstall runs UninstallString. Smart App Control
// blocks unins000.exe (unsigned, and Microsoft's cloud has never seen it), so
// point it at the app's own Python, which SAC already allows. This runs after
// Setup has written the entry, so it also survives an upgrade. A first install
// has no .venv yet: bootstrap.ps1 does the same once it has made one
// (installer\uninstall_app.py --register, which writes the same command).
procedure CurStepChanged(CurStep: TSetupStep);
var
  Cmd: String;
begin
  if (CurStep <> ssPostInstall) or not VenvReady then
    exit;
  Cmd := '"' + ExpandConstant('{app}\.venv\Scripts\pythonw.exe') + '" "' +
         ExpandConstant('{app}\installer\uninstall_app.py') + '"';
  RegWriteStringValue(HKA, UninstallKey, 'UninstallString', Cmd);
  RegWriteStringValue(HKA, UninstallKey, 'QuietUninstallString', Cmd + ' --quiet');
end;

// What's left after uninstall is the user's own data: .env (settings and
// keys), word lists, transcripts, recordings and downloaded speech models.
// Keep it unless they say otherwise, so a reinstall picks up where they were.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  AppDir: String;
begin
  if CurUninstallStep <> usPostUninstall then
    exit;
  AppDir := ExpandConstant('{app}');
  if UninstallSilent or not DirExists(AppDir) then
    exit;
  if MsgBox('Also delete your settings, word lists, transcripts, recordings and downloaded speech models?' + #13#10#13#10 +
            'Choose No to keep them in' + #13#10 + AppDir + #13#10 + 'so a reinstall picks up where you left off.',
            mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    DelTree(AppDir, True, True, True);
end;
