; Inno Setup 6 script for OBS Avatar Live (Windows x64).
; Built by build_installer.ps1, which passes the /D defines below.
; Installs: plugin DLL into OBS, engine + locale into OBS plugin data dir.
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
DefaultDirName={code:GetObsDir}
DirExistsWarning=no
AppendDefaultDirName=no
UsePreviousAppDir=yes
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
es.ObsNotFound=No se encontró OBS Studio. Seleccione manualmente la carpeta de instalación de OBS (la que contiene bin\64bit\obs64.exe).
en.ObsNotFound=OBS Studio was not found. Please select the OBS installation folder (the one containing bin\64bit\obs64.exe).
es.ObsTooOld=Se requiere OBS Studio %1 o superior. Versión encontrada: %2.
en.ObsTooOld=OBS Studio %1 or later is required. Found version: %2.
es.NotObsDir=La carpeta seleccionada no contiene bin\64bit\obs64.exe.
en.NotObsDir=The selected folder does not contain bin\64bit\obs64.exe.
es.DeleteProfiles=¿Desea eliminar también sus perfiles locales, benchmarks y registros de Avatar Live (%1)?%n%nEsta acción no se puede deshacer.
en.DeleteProfiles=Also delete your local Avatar Live profiles, benchmarks and logs (%1)?%n%nThis cannot be undone.

[Files]
; Plugin module
Source: "{#PluginDll}"; DestDir: "{app}\obs-plugins\64bit"; Flags: ignoreversion
Source: "{#PluginPdb}"; DestDir: "{app}\obs-plugins\64bit"; Flags: ignoreversion skipifsourcedoesntexist
; Plugin data (locale etc.)
Source: "{#RepoDir}\data\*"; DestDir: "{app}\data\obs-plugins\obs-avatar-live"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist
; Engine (PyInstaller one-folder build)
Source: "{#EngineDir}\*"; DestDir: "{app}\data\obs-plugins\obs-avatar-live\engine"; Flags: ignoreversion recursesubdirs createallsubdirs
; Notices
Source: "{#RepoDir}\LICENSE"; DestDir: "{app}\data\obs-plugins\obs-avatar-live"; Flags: ignoreversion
Source: "{#RepoDir}\THIRD_PARTY_NOTICES.md"; DestDir: "{app}\data\obs-plugins\obs-avatar-live"; Flags: ignoreversion
Source: "{#RepoDir}\models\MODEL_MANIFEST.json"; DestDir: "{app}\data\obs-plugins\obs-avatar-live"; Flags: ignoreversion

[UninstallDelete]
Type: filesandordirs; Name: "{app}\data\obs-plugins\obs-avatar-live"

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
  else if FileExists(ObsExe(ExpandConstant('{commonpf64}\obs-studio'))) then
    Result := ExpandConstant('{commonpf64}\obs-studio')
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

function InitializeSetup(): Boolean;
begin
  Result := True;
  if not FileExists(ObsExe(GetObsDir(''))) then
    MsgBox(CustomMessage('ObsNotFound'), mbInformation, MB_OK);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Ver: String;
  Major: Integer;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    if not FileExists(ObsExe(WizardDirValue())) then
    begin
      MsgBox(CustomMessage('NotObsDir'), mbError, MB_OK);
      Result := False;
      exit;
    end;
    Major := ObsMajorVersion(WizardDirValue(), Ver);
    if Major < {#MinObsMajor} then
    begin
      MsgBox(FmtMessage(CustomMessage('ObsTooOld'), ['{#MinObsMajor}', Ver]), mbError, MB_OK);
      Result := False;
    end;
  end;
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
