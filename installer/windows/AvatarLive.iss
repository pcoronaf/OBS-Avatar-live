; Inno Setup 6 script for OBS Avatar Live (Windows x64).
; Built by build_installer.ps1, which passes the /D defines below.
; Installs into the per-machine OBS plugin folder (https://obsproject.com/kb/legacy-plugin-locations):
;   OBS >= 33:  C:\ProgramData\obs-studio\plugins\obs-avatar-live\obs-avatar-live.dll + data\
;   OBS 31-32:  C:\ProgramData\obs-studio\plugins\obs-avatar-live\bin\64bit\obs-avatar-live.dll + data\
; and removes copies left in the legacy Program Files location by earlier versions.
; Does NOT install any camera driver (OBS Virtual Camera is used).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef Variant
  #define Variant "Lite"
#endif
#ifndef RepoDir
  #define RepoDir "..\.."
#endif
#ifndef PluginDll
  #define PluginDll RepoDir + "\build_x64\RelWithDebInfo\obs-avatar-live.dll"
#endif
#ifndef PluginPdb
  #define PluginPdb RepoDir + "\build_x64\RelWithDebInfo\obs-avatar-live.pdb"
#endif
#ifndef EngineDir
  #define EngineDir RepoDir + "\dist\" + Variant + "\avatar-engine"
#endif
#define MinObsMajor 31
#define FlatLayoutObsMajor 33

[Setup]
AppId={{A84942C7-9B41-4B28-8238-FC8C64241156}
AppName=OBS Avatar Live ({#Variant})
AppVersion={#AppVersion}
AppPublisher=OBS Avatar Live contributors
AppPublisherURL=https://github.com/pcoronaf/OBS-Avatar-live
LicenseFile={#RepoDir}\LICENSE
InfoBeforeFile={#RepoDir}\installer\windows\INFO_BEFORE.txt
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
MinVersion=10.0.17763
DefaultDirName={commonappdata}\obs-studio\plugins\obs-avatar-live
DisableDirPage=yes
DirExistsWarning=no
AppendDefaultDirName=no
UsePreviousAppDir=no
DisableProgramGroupPage=yes
OutputBaseFilename=AvatarLive-Setup-{#AppVersion}-{#Variant}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=OBS Avatar Live ({#Variant})
CloseApplications=yes
CloseApplicationsFilter=obs64.exe,avatar-engine.exe

[Languages]
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
es.ObsTooOld=Se requiere OBS Studio %1 o superior. Versión encontrada: %2.
en.ObsTooOld=OBS Studio %1 or later is required. Found version: %2.
es.ObsNotFoundContinue=No se encontró OBS Studio. El plugin se instalará en la carpeta de plugins de OBS (%1) con el formato de OBS 33 o superior.
en.ObsNotFoundContinue=OBS Studio was not found. The plugin will be installed in the OBS plugin folder (%1) using the OBS 33+ layout.
es.DeleteProfiles=¿Desea eliminar también sus perfiles locales, benchmarks y registros de Avatar Live (%1)?%n%nEsta acción no se puede deshacer.
en.DeleteProfiles=Also delete your local Avatar Live profiles, benchmarks and logs (%1)?%n%nThis cannot be undone.

[Files]
; Plugin module: flat layout for OBS >= 33, bin\64bit for OBS 31-32 (see header)
Source: "{#PluginDll}"; DestDir: "{app}"; Flags: ignoreversion; Check: UseFlatLayout
Source: "{#PluginPdb}"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist; Check: UseFlatLayout
Source: "{#PluginDll}"; DestDir: "{app}\bin\64bit"; Flags: ignoreversion; Check: not UseFlatLayout
Source: "{#PluginPdb}"; DestDir: "{app}\bin\64bit"; Flags: ignoreversion skipifsourcedoesntexist; Check: not UseFlatLayout
; Plugin data (locale etc.) — same place in both layouts, so obs_module_file() finds it either way
Source: "{#RepoDir}\data\*"; DestDir: "{app}\data"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist
; Engine (PyInstaller one-folder build)
Source: "{#EngineDir}\*"; DestDir: "{app}\data\engine"; Flags: ignoreversion recursesubdirs createallsubdirs
; Notices
Source: "{#RepoDir}\LICENSE"; DestDir: "{app}\data"; Flags: ignoreversion
Source: "{#RepoDir}\THIRD_PARTY_NOTICES.md"; DestDir: "{app}\data"; Flags: ignoreversion
Source: "{#RepoDir}\models\MODEL_MANIFEST.json"; DestDir: "{app}\data"; Flags: ignoreversion

[InstallDelete]
; Legacy Program Files location used by 0.1.0 (would otherwise be loaded/flagged by OBS)
Type: files; Name: "{code:GetObsDir}\obs-plugins\64bit\obs-avatar-live.dll"
Type: files; Name: "{code:GetObsDir}\obs-plugins\64bit\obs-avatar-live.pdb"
Type: filesandordirs; Name: "{code:GetObsDir}\data\obs-plugins\obs-avatar-live"
; The other layout in this folder (e.g. after upgrading OBS 32 -> 33), so only one copy remains
Type: filesandordirs; Name: "{app}\bin"; Check: UseFlatLayout
Type: files; Name: "{app}\obs-avatar-live.dll"; Check: not UseFlatLayout
Type: files; Name: "{app}\obs-avatar-live.pdb"; Check: not UseFlatLayout
; Old engine files (PyInstaller folder contents change between versions)
Type: filesandordirs; Name: "{app}\data\engine"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
function ObsExe(Dir: String): String;
begin
  Result := AddBackslash(Dir) + 'bin\64bit\obs64.exe';
end;

function GetObsDir(Param: String): String;
var
  Dir: String;
begin
  Result := '';
  if RegQueryStringValue(HKLM64, 'SOFTWARE\OBS Studio', '', Dir) and FileExists(ObsExe(Dir)) then
    Result := Dir
  else if RegQueryStringValue(HKLM32, 'SOFTWARE\OBS Studio', '', Dir) and FileExists(ObsExe(Dir)) then
    Result := Dir
  else
    Result := ExpandConstant('{commonpf64}\obs-studio');
end;

function ObsMajorVersion(Dir: String; var VersionStr: String): Integer;
var
  Dot: Integer;
begin
  Result := -1;
  VersionStr := '?';
  if not GetVersionNumbersString(ObsExe(Dir), VersionStr) then
    exit;
  Dot := Pos('.', VersionStr);
  if Dot > 0 then
    Result := StrToIntDef(Copy(VersionStr, 1, Dot - 1), -1)
  else
    Result := StrToIntDef(VersionStr, -1);
end;

var
  ObsMajor: Integer;

function InitializeSetup(): Boolean;
var
  Ver, PluginsDir: String;
begin
  Result := True;
  if not FileExists(ObsExe(GetObsDir(''))) then
  begin
    ObsMajor := {#FlatLayoutObsMajor};  { unknown: use the current OBS layout }
    PluginsDir := ExpandConstant('{commonappdata}\obs-studio\plugins');
    SuppressibleMsgBox(FmtMessage(CustomMessage('ObsNotFoundContinue'), [PluginsDir]), mbInformation, MB_OK, IDOK);
    exit;
  end;
  ObsMajor := ObsMajorVersion(GetObsDir(''), Ver);
  if ObsMajor < {#MinObsMajor} then
  begin
    MsgBox(FmtMessage(CustomMessage('ObsTooOld'), ['{#MinObsMajor}', Ver]), mbError, MB_OK);
    Result := False;
  end;
end;

function UseFlatLayout(): Boolean;
begin
  Result := ObsMajor >= {#FlatLayoutObsMajor};
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    { Per-user data of the account running the uninstaller. }
    DataDir := ExpandConstant('{localappdata}\AvatarLive');
    if DirExists(DataDir) and not UninstallSilent() then
      if MsgBox(FmtMessage(CustomMessage('DeleteProfiles'), [DataDir]), mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataDir, True, True, True);
  end;
end;
