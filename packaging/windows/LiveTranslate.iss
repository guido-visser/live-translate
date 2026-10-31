; Inno Setup script. Build with packaging\windows\build.ps1 (it downloads uv.exe and passes the version).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{8E0F6B8C-6B7A-4E0B-9C55-2D3A0B7E1F11}
AppName=Live Translate
AppVersion={#AppVersion}
AppPublisher=Live Translate
DefaultDirName={autopf}\Live Translate
DefaultGroupName=Live Translate
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=LiveTranslate-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Admin rights are needed for the firewall rule that lets phones connect.
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName=Live Translate
SetupIconFile=LiveTranslate.ico
UninstallDisplayIcon={app}\LiveTranslate.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "dutch"; MessagesFile: "compiler:Languages\Dutch.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "ukrainian"; MessagesFile: "compiler:Languages\Ukrainian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\..\src\*"; DestDir: "{app}\src"; Flags: recursesubdirs ignoreversion; Excludes: "__pycache__,*.pyc"
Source: "..\..\pyproject.toml"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\uv.lock"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\.python-version"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "bin\uv.exe"; DestDir: "{app}\bin"; Flags: ignoreversion
Source: "LiveTranslate.cmd"; DestDir: "{app}"; Flags: ignoreversion
Source: "LiveTranslate.vbs"; DestDir: "{app}"; Flags: ignoreversion
Source: "LiveTranslate.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Live Translate"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\LiveTranslate.vbs"""; WorkingDir: "{app}"; IconFilename: "{app}\LiveTranslate.ico"
Name: "{autodesktop}\Live Translate"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\LiveTranslate.vbs"""; WorkingDir: "{app}"; IconFilename: "{app}\LiveTranslate.ico"; Tasks: desktopicon

[Run]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Live Translate"""; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Live Translate"" dir=in action=allow protocol=TCP localport=8000 profile=any"; Flags: runhidden
Filename: "{sys}\wscript.exe"; Parameters: """{app}\LiveTranslate.vbs"""; Description: "{cm:LaunchProgram,Live Translate}"; Flags: postinstall nowait skipifsilent runasoriginaluser
; An update started from the app runs silently and starts the app again afterwards.
Filename: "{sys}\wscript.exe"; Parameters: """{app}\LiveTranslate.vbs"""; Flags: nowait runasoriginaluser; Check: RelaunchAfterUpdate

[UninstallDelete]
Type: files; Name: "{app}\VERSION"
Type: files; Name: "{app}\LANGUAGE"

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Live Translate"""; Flags: runhidden; RunOnceId: "RemoveFirewallRule"

[Code]
// The app's interface starts in the language chosen for Setup (codes as in languages.py).
function UiLanguage: String;
begin
  Result := 'en';
  if ActiveLanguage = 'dutch' then Result := 'nl'
  else if ActiveLanguage = 'russian' then Result := 'ru'
  else if ActiveLanguage = 'ukrainian' then Result := 'uk';
end;

// Set by the app's updater: /relaunch=1 /waitpid=<pid of the running app>.
function RelaunchAfterUpdate: Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:relaunch|0}') = '1');
end;

// The app closes itself once Setup has started; wait for that before replacing files,
// so the new version doesn't find the old one still running on its port.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Pid: String;
  ResultCode: Integer;
begin
  Result := '';
  Pid := ExpandConstant('{param:waitpid|}');
  if (Pid <> '') and (StrToIntDef(Pid, 0) > 0) then
    Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
      '-NoProfile -NonInteractive -Command "Wait-Process -Id ' + Pid + ' -Timeout 90 -ErrorAction SilentlyContinue"',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    SaveStringToFile(ExpandConstant('{app}\VERSION'), '{#AppVersion}', False);
    SaveStringToFile(ExpandConstant('{app}\LANGUAGE'), UiLanguage, False);
  end;
end;
