<#
.SYNOPSIS
  Builds the avatar-engine one-folder executable with PyInstaller.
.PARAMETER Variant
  Lite        : onnxruntime (CPU) [+ optional OpenVINO or DirectML EP]. Never CUDA/TensorRT.
  Accelerated : onnxruntime-gpu (CUDA) or DirectML, per -Backend.
.PARAMETER Backend
  Lite:        cpu | openvino | directml      (default cpu)
  Accelerated: cuda | directml                 (default directml)
.EXAMPLE
  installer\windows\build_engine.ps1 -Variant Lite -Backend openvino
#>
[CmdletBinding()]
param(
    [ValidateSet('Lite', 'Accelerated')] [string] $Variant = 'Lite',
    [ValidateSet('cpu', 'openvino', 'directml', 'cuda')] [string] $Backend = '',
    [string] $Python = 'py -3.11'
)
$ErrorActionPreference = 'Stop'
$Repo = Resolve-Path (Join-Path $PSScriptRoot '..\..')
Set-Location $Repo

if (-not $Backend) { $Backend = if ($Variant -eq 'Lite') { 'cpu' } else { 'directml' } }
if ($Variant -eq 'Lite' -and $Backend -eq 'cuda') { throw 'The Lite edition must not include CUDA.' }

$Venv = Join-Path $Repo "build\venv-$Variant-$Backend"
if (-not (Test-Path $Venv)) { Invoke-Expression "$Python -m venv `"$Venv`"" }
$Py = Join-Path $Venv 'Scripts\python.exe'

& $Py -m pip install --upgrade pip wheel
& $Py -m pip install pyinstaller
& $Py -m pip install -e engine

# The onnxruntime distributions are mutually exclusive: replace the CPU one when needed.
switch ($Backend) {
    'openvino' { & $Py -m pip uninstall -y onnxruntime; & $Py -m pip install onnxruntime-openvino }
    'directml' { & $Py -m pip uninstall -y onnxruntime; & $Py -m pip install onnxruntime-directml }
    'cuda'     { & $Py -m pip uninstall -y onnxruntime; & $Py -m pip install onnxruntime-gpu }
}

# Sanity check: ONNX Runtime imports and reports providers.
& $Py -c "import onnxruntime as o; print('ORT', o.__version__, o.get_available_providers())"
if ($LASTEXITCODE -ne 0) { throw 'ONNX Runtime import check failed.' }

$env:AVATAR_LIVE_VARIANT = $Variant
& $Py -m PyInstaller --noconfirm --clean `
    --distpath (Join-Path $Repo "dist\$Variant") `
    --workpath (Join-Path $Repo "build\pyinstaller-$Variant") `
    installer\windows\avatar-engine.spec
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }

$Exe = Join-Path $Repo "dist\$Variant\avatar-engine\avatar-engine.exe"
& $Exe selftest
if ($LASTEXITCODE -ne 0) { throw "selftest failed for $Exe" }
Write-Host "Engine built: $Exe"
