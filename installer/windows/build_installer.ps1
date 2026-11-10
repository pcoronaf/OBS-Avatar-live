<#
.SYNOPSIS
  Builds AvatarLive-Setup-<version>-<Variant>.exe with Inno Setup 6.
.EXAMPLE
  installer\windows\build_installer.ps1 -Variant Lite -PluginBuildDir build_x64\RelWithDebInfo
#>
[CmdletBinding()]
param(
    [ValidateSet('Lite', 'Accelerated')] [string] $Variant = 'Lite',
    [string] $PluginBuildDir = 'build_x64\RelWithDebInfo',
    [string] $Iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
)
$ErrorActionPreference = 'Stop'
$Repo = Resolve-Path (Join-Path $PSScriptRoot '..\..')
Set-Location $Repo

$Version = (Get-Content buildspec.json -Raw | ConvertFrom-Json).version
$Dll = Join-Path $Repo "$PluginBuildDir\obs-avatar-live.dll"
$Engine = Join-Path $Repo "dist\$Variant\avatar-engine"

if (-not (Test-Path $Dll)) { throw "Plugin DLL not found: $Dll (build with: cmake --build --preset windows-x64)" }
if (-not (Test-Path (Join-Path $Engine 'avatar-engine.exe'))) { throw "Engine not found: $Engine (run build_engine.ps1 -Variant $Variant)" }
if (-not (Test-Path $Iscc)) { throw "Inno Setup compiler not found: $Iscc" }

& $Iscc `
    "/DAppVersion=$Version" `
    "/DVariant=$Variant" `
    "/DPluginDll=$Dll" `
    "/DPluginPdb=$([IO.Path]::ChangeExtension($Dll, '.pdb'))" `
    "/DEngineDir=$Engine" `
    "/DRepoDir=$Repo" `
    "/O$(Join-Path $Repo 'release')" `
    installer\windows\AvatarLive.iss
if ($LASTEXITCODE -ne 0) { throw 'ISCC failed.' }
Write-Host "Installer written to $(Join-Path $Repo 'release')"
