; Inno Setup script, saved as UTF-8 with BOM for the Cyrillic below. Build with packaging\windows\build.ps1 (it downloads uv.exe and passes the version).

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
Source: "LiveTranslateSetup.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "LiveTranslate.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "bin\vc_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: VcRuntimeOutdated

[Icons]
Name: "{group}\Live Translate"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\LiveTranslate.vbs"""; WorkingDir: "{app}"; IconFilename: "{app}\LiveTranslate.ico"
Name: "{autodesktop}\Live Translate"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\LiveTranslate.vbs"""; WorkingDir: "{app}"; IconFilename: "{app}\LiveTranslate.ico"; Tasks: desktopicon

[Run]
; onnxruntime and sentencepiece crash on import with a Visual C++ runtime older than 14.40.
Filename: "{tmp}\vc_redist.x64.exe"; Parameters: "/install /quiet /norestart"; StatusMsg: "Installing the Microsoft Visual C++ runtime..."; Flags: waituntilterminated; Check: VcRuntimeOutdated
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Live Translate"""; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Live Translate"" dir=in action=allow protocol=TCP localport=8000 profile=any"; Flags: runhidden
; Through explorer: a program started by Setup can inherit its RedirectionGuard, and uv then refuses
; the Python folder link it made itself ("untrusted mount point", os error 448).
Filename: "{win}\explorer.exe"; Parameters: """{app}\LiveTranslate.vbs"""; Description: "{cm:LaunchProgram,Live Translate}"; Flags: postinstall nowait skipifsilent runasoriginaluser
; An update started from the app runs silently and starts the app again afterwards.
Filename: "{win}\explorer.exe"; Parameters: """{app}\LiveTranslate.vbs"""; Flags: nowait runasoriginaluser; Check: RelaunchAfterUpdate

[Registry]
; Written by the app (Settings → Startup); only here so that uninstalling removes it.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "LiveTranslate"; Flags: dontcreatekey uninsdeletevalue
; A crash in native code (e.g. a GPU driver) leaves a small dump in %LOCALAPPDATA%\CrashDumps of the
; user that ran the app, so the next one can be looked into (13 Nov 2026 left nothing to look at).
Root: HKLM64; Subkey: "SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps\pythonw.exe"; ValueType: dword; ValueName: "DumpType"; ValueData: 1; Flags: uninsdeletekey
Root: HKLM64; Subkey: "SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps\pythonw.exe"; ValueType: dword; ValueName: "DumpCount"; ValueData: 5

[UninstallDelete]
Type: files; Name: "{app}\VERSION"
Type: files; Name: "{app}\LANGUAGE"

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Live Translate"""; Flags: runhidden; RunOnceId: "RemoveFirewallRule"

[CustomMessages]
english.RemoveDataTitle=Uninstall Live Translate
english.RemoveDataIntro=Your data normally stays on this PC, so that a new installation carries on where you left off.
english.RemoveDataCheck=Also remove all data
english.RemoveDataWarning=Warning: this throws away all of your settings, corrections and church profiles, along with the service logs and downloaded models. This can't be undone.
english.RemoveDataFolders=These folders will be deleted completely:
english.RemoveDataBusy=Removing all data...
english.RemoveDataLeft=These folders could not be removed completely. You can delete them yourself:
english.RemoveDataContinue=Continue
dutch.RemoveDataTitle=Live Translate verwijderen
dutch.RemoveDataIntro=Je gegevens blijven normaal op deze pc staan, zodat een nieuwe installatie verdergaat waar je gebleven was.
dutch.RemoveDataCheck=Ook alle gegevens verwijderen
dutch.RemoveDataWarning=Let op: hiermee gooi je al je instellingen, correcties en kerkprofielen weg, net als de dienstlogs en gedownloade modellen. Dit kan niet ongedaan worden gemaakt.
dutch.RemoveDataFolders=Deze mappen worden helemaal verwijderd:
dutch.RemoveDataBusy=Alle gegevens verwijderen...
dutch.RemoveDataLeft=Deze mappen konden niet helemaal worden verwijderd. Je kunt ze zelf weggooien:
dutch.RemoveDataContinue=Doorgaan
russian.RemoveDataTitle=Удаление Live Translate
russian.RemoveDataIntro=Обычно ваши данные остаются на этом компьютере, чтобы новая установка продолжила работу с того же места.
russian.RemoveDataCheck=Также удалить все данные
russian.RemoveDataWarning=Внимание: будут безвозвратно удалены все ваши настройки, исправления и профили церкви, а также журналы служб и загруженные модели. Отменить это невозможно.
russian.RemoveDataFolders=Эти папки будут удалены полностью:
russian.RemoveDataBusy=Удаление всех данных...
russian.RemoveDataLeft=Не удалось полностью удалить эти папки. Вы можете удалить их вручную:
russian.RemoveDataContinue=Продолжить
ukrainian.RemoveDataTitle=Видалення Live Translate
ukrainian.RemoveDataIntro=Зазвичай ваші дані залишаються на цьому комп'ютері, щоб нове встановлення продовжило роботу з того самого місця.
ukrainian.RemoveDataCheck=Також видалити всі дані
ukrainian.RemoveDataWarning=Увага: буде безповоротно видалено всі ваші налаштування, виправлення та профілі церкви, а також журнали служінь і завантажені моделі. Скасувати це неможливо.
ukrainian.RemoveDataFolders=Ці папки буде видалено повністю:
ukrainian.RemoveDataBusy=Видалення всіх даних...
ukrainian.RemoveDataLeft=Не вдалося повністю видалити ці папки. Ви можете видалити їх вручну:
ukrainian.RemoveDataContinue=Продовжити

[Code]
// The app's interface starts in the language chosen for Setup (codes as in languages.py).
function UiLanguage: String;
begin
  Result := 'en';
  if ActiveLanguage = 'dutch' then Result := 'nl'
  else if ActiveLanguage = 'russian' then Result := 'ru'
  else if ActiveLanguage = 'ukrainian' then Result := 'uk';
end;

// True when msvcp140.dll in System32 is missing or older than 14.40.
function VcRuntimeOutdated: Boolean;
var
  MS, LS: Cardinal;
begin
  if GetVersionNumbers(ExpandConstant('{sys}\msvcp140.dll'), MS, LS) then
    Result := ((MS shr 16) < 14) or (((MS shr 16) = 14) and ((MS and $FFFF) < 40))
  else
    Result := True;
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

// -- removing all data when uninstalling ----------------------------------------------------------

var
  DataFolders: TStringList;
  RemoveData: Boolean;

procedure AddDataFolder(Folder: String);
var
  I: Integer;
begin
  if not DirExists(Folder) then Exit;
  for I := 0 to DataFolders.Count - 1 do
    if CompareText(DataFolders[I], Folder) = 0 then Exit;
  DataFolders.Add(Folder);
end;

// data-location.txt is UTF-16, because LiveTranslate.vbs can't read UTF-8.
function ReadDataLocation(FileName: String): String;
var
  Bytes: AnsiString;
  I: Integer;
begin
  Result := '';
  if LoadStringFromFile(FileName, Bytes) then
  begin
    I := 3;  // after the byte order mark
    while I < Length(Bytes) do
    begin
      Result := Result + Chr(Ord(Bytes[I]) + Ord(Bytes[I + 1]) * 256);
      I := I + 2;
    end;
  end;
  Result := Trim(Result);
end;

// The setup window only ever makes a folder of our own, so anything else in that note is left alone.
function IsOurDataFolder(Folder: String): Boolean;
begin
  Result := (Length(Folder) > 3) and (Folder[2] = ':') and (Folder[3] = '\') and
    (CompareText(ExtractFileName(RemoveBackslash(Folder)), 'LiveTranslate') = 0);
end;

procedure AddProfileData(Base, Home: String);
var
  Note, Location: String;
begin
  if not DirExists(Base) then Exit;
  AddDataFolder(Base);
  Note := Base + '\data-location.txt';
  if FileExists(Note) then
  begin
    Location := ReadDataLocation(Note);
    if IsOurDataFolder(Location) then AddDataFolder(RemoveBackslash(Location));
  end
  else
  begin
    // Set up before a folder could be picked: LibreTranslate kept its models in the home folder; see config.py.
    AddDataFolder(Home + '\.local\share\argos-translate');
    AddDataFolder(Home + '\.local\cache\argos-translate');
    AddDataFolder(Home + '\.config\argos-translate');
  end;
end;

// Every account: with a standard account and someone else's admin password, the uninstaller runs
// as that other account, while the data belongs to the one that uses the app.
procedure FindDataFolders;
var
  Profiles: TArrayOfString;
  Home: String;
  I: Integer;
begin
  AddProfileData(ExpandConstant('{localappdata}\LiveTranslate'), ExpandConstant('{%USERPROFILE}'));
  if RegGetSubkeyNames(HKLM, 'SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList', Profiles) then
    for I := 0 to GetArrayLength(Profiles) - 1 do
      if RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\' + Profiles[I], 'ProfileImagePath', Home) then
        AddProfileData(Home + '\AppData\Local\LiveTranslate', Home);
end;

function AddLabel(Form: TSetupForm; Caption: String; Top: Integer): TNewStaticText;
begin
  Result := TNewStaticText.Create(Form);
  Result.Parent := Form;
  Result.AutoSize := False;
  Result.WordWrap := True;
  Result.Left := ScaleX(20);
  Result.Top := Top;
  Result.Width := Form.ClientWidth - ScaleX(40);
  Result.Caption := Caption;
  Result.AdjustHeight;
end;

// False: cancelled, and nothing happens at all.
function AskRemoveData: Boolean;
var
  Form: TSetupForm;
  Text: TNewStaticText;
  Check: TNewCheckBox;
  ContinueButton, CancelButton: TNewButton;
  Top: Integer;
begin
  Form := CreateCustomForm(ScaleX(520), ScaleY(300), False, False);
  try
    Form.Caption := CustomMessage('RemoveDataTitle');
    Text := AddLabel(Form, CustomMessage('RemoveDataIntro'), ScaleY(18));
    Top := Text.Top + Text.Height + ScaleY(14);

    Check := TNewCheckBox.Create(Form);
    Check.Parent := Form;
    Check.Left := ScaleX(20);
    Check.Top := Top;
    Check.Width := Form.ClientWidth - ScaleX(40);
    Check.Height := ScaleY(20);
    Check.Caption := CustomMessage('RemoveDataCheck');
    Check.Font.Style := [fsBold];
    Check.Checked := False;
    Top := Top + Check.Height + ScaleY(10);

    Text := AddLabel(Form, CustomMessage('RemoveDataWarning'), Top);
    Text.Font.Color := $0000C0;
    Text.Font.Style := [fsBold];
    Text.AdjustHeight;
    Top := Top + Text.Height + ScaleY(14);

    Text := AddLabel(Form, CustomMessage('RemoveDataFolders') + #13#10 + DataFolders.Text, Top);
    Top := Top + Text.Height + ScaleY(14);

    ContinueButton := TNewButton.Create(Form);
    ContinueButton.Parent := Form;
    ContinueButton.Width := ScaleX(100);
    ContinueButton.Height := ScaleY(26);
    ContinueButton.Left := Form.ClientWidth - ScaleX(20) - 2 * ContinueButton.Width - ScaleX(8);
    ContinueButton.Top := Top;
    ContinueButton.Caption := CustomMessage('RemoveDataContinue');
    ContinueButton.ModalResult := mrOk;
    ContinueButton.Default := True;

    CancelButton := TNewButton.Create(Form);
    CancelButton.Parent := Form;
    CancelButton.Width := ContinueButton.Width;
    CancelButton.Height := ContinueButton.Height;
    CancelButton.Left := Form.ClientWidth - ScaleX(20) - CancelButton.Width;
    CancelButton.Top := Top;
    CancelButton.Caption := SetupMessage(msgButtonCancel);
    CancelButton.ModalResult := mrCancel;
    CancelButton.Cancel := True;

    Form.ClientHeight := Top + ContinueButton.Height + ScaleY(16);
    Form.Position := poScreenCenter;
    Result := Form.ShowModal = mrOk;
    RemoveData := Result and Check.Checked;
  finally
    Form.Free;
  end;
end;

// Runs before Windows' own "Are you sure?", which so stays the last question.
function InitializeUninstall: Boolean;
begin
  Result := True;
  DataFolders := TStringList.Create;
  // A silent uninstall has nobody to warn, so it never removes data.
  if UninstallSilent then Exit;
  FindDataFolders;
  if DataFolders.Count > 0 then Result := AskRemoveData;
end;

// Open files can't be deleted, so the app and its LibreTranslate have to go first.
procedure StopProgramsIn(Folder: String);
var
  ResultCode: Integer;
  Prefix: String;
begin
  Prefix := Folder + '\';
  StringChangeEx(Prefix, '''', '''''', True);
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -NonInteractive -Command "Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith(''' + Prefix + ''', [StringComparison]::OrdinalIgnoreCase) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

// With rmdir: unlike a plain delete it never follows a folder link, and uv makes those.
procedure DeleteDataFolders;
var
  Left: String;
  I, Attempt, ResultCode: Integer;
begin
  Left := '';
  for I := 0 to DataFolders.Count - 1 do
  begin
    // Something that just ended can hold on to its files for a moment.
    for Attempt := 1 to 5 do
      if DirExists(DataFolders[I]) then
      begin
        if Attempt > 1 then Sleep(1000);
        Exec(ExpandConstant('{cmd}'), '/c rmdir /s /q "' + DataFolders[I] + '"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      end;
    if DirExists(DataFolders[I]) then Left := Left + #13#10 + DataFolders[I];
  end;
  if Left <> '' then
    MsgBox(CustomMessage('RemoveDataLeft') + #13#10 + Left, mbError, MB_OK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  I: Integer;
begin
  if RemoveData and (CurUninstallStep = usUninstall) then
  begin
    UninstallProgressForm.StatusLabel.Caption := CustomMessage('RemoveDataBusy');
    for I := 0 to DataFolders.Count - 1 do StopProgramsIn(DataFolders[I]);
    DeleteDataFolders;
  end;
end;
