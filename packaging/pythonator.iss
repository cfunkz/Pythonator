; Windows installer, built by packaging/build.py:  ISCC /DVersion=x.y.z /DSource=<staged folder> /DOut=<folder> pythonator.iss
; For your user only: no administrator rights, nothing outside your profile. Settings > Apps lists it with Uninstall.

#ifndef Version
  #error Pass /DVersion=x.y.z
#endif

[Setup]
AppId=Pythonator
AppName=Pythonator
AppVersion={#Version}
AppVerName=Pythonator {#Version}
AppPublisher=cfunkz
AppPublisherURL=https://cfunkz.github.io/Pythonator/
AppSupportURL=https://github.com/cfunkz/Pythonator/issues
AppUpdatesURL=https://github.com/cfunkz/Pythonator/releases
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\Pythonator
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
ChangesEnvironment=yes
CloseApplications=yes
RestartApplications=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
LicenseFile={#Source}\LICENSE
SetupIconFile={#SourcePath}\..\pythonator\resources\icon.ico
UninstallDisplayIcon={app}\pythonator.ico
UninstallDisplayName=Pythonator
OutputDir={#Out}
OutputBaseFilename=Pythonator-{#Version}-windows-x64-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

[Files]
Source: "{#Source}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; The same name and place the uninstaller in pythonatorctl looks for.
Name: "{userprograms}\Pythonator"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m pythonator"; WorkingDir: "{app}"; IconFilename: "{app}\pythonator.ico"; Comment: "Run and supervise services in isolated environments"

[Registry]
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}\bin"; Check: NotOnPath(ExpandConstant('{app}\bin'))

[Run]
; The background service starts at logon, as you (a scheduled task with limited rights).
Filename: "{app}\python\python.exe"; Parameters: "-m pythonator.ctl.main service install"; Flags: runhidden waituntilterminated; StatusMsg: "Starting the background service..."
Filename: "{app}\python\pythonw.exe"; Parameters: "-m pythonator"; WorkingDir: "{app}"; Description: "Open Pythonator"; Flags: postinstall nowait skipifsilent

[UninstallRun]
; Stop every service and take back every folder permission first, then remove the logon task.
Filename: "{app}\python\python.exe"; Parameters: "-m pythonator.ctl.main shutdown --uninstall"; Flags: runhidden waituntilterminated; RunOnceId: "Shutdown"
Filename: "{app}\python\python.exe"; Parameters: "-m pythonator.ctl.main service remove"; Flags: runhidden waituntilterminated; RunOnceId: "ServiceRemove"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
function NotOnPath(Dir: string): Boolean;
var
  Paths: string;
begin
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', Paths) then
    Result := True
  else
    Result := Pos(';' + Uppercase(Dir) + ';', ';' + Uppercase(Paths) + ';') = 0;
end;

procedure RemoveFromPath(Dir: string);
var
  Paths: string;
  P: Integer;
begin
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', Paths) then
    exit;
  P := Pos(';' + Uppercase(Dir) + ';', ';' + Uppercase(Paths) + ';');
  if P = 0 then
    exit;
  { Dir starts at Paths[P]: drop it with the separator before it, or after it when it comes first. }
  if P > 1 then
    Delete(Paths, P - 1, Length(Dir) + 1)
  else
    Delete(Paths, 1, Length(Dir) + 1);
  RegWriteExpandStringValue(HKCU, 'Environment', 'Path', Paths);
end;

{ An update: stop the running version (not its services' grants) so its files can be replaced. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Py: string;
  Code: Integer;
begin
  Py := ExpandConstant('{app}\python\python.exe');
  if FileExists(Py) then
    Exec(Py, '-m pythonator.ctl.main shutdown', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RemoveFromPath(ExpandConstant('{app}\bin'));
end;
