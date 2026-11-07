# Windows installer

Two-step build (run from the repository root in PowerShell 7 on Windows x64):

```powershell
# 1. Plugin (obs-plugintemplate presets)
cmake --preset windows-x64
cmake --build --preset windows-x64 --config RelWithDebInfo

# 2. Engine (PyInstaller one-folder build, runs `avatar-engine selftest` afterwards)
installer\windows\build_engine.ps1 -Variant Lite                 # CPU only
installer\windows\build_engine.ps1 -Variant Lite -Backend openvino
installer\windows\build_engine.ps1 -Variant Accelerated -Backend directml

# 3. Installer (Inno Setup 6)
installer\windows\build_installer.ps1 -Variant Lite -PluginBuildDir build_x64\RelWithDebInfo
```

Output: `release\AvatarLive-Setup-<version>-<Variant>.exe` (version from `buildspec.json`).

| File | Purpose |
|---|---|
| `avatar-engine.spec` | PyInstaller spec; collects ONNX Runtime, OpenCV and PortAudio binaries. The Lite variant filters out any CUDA/TensorRT DLLs. |
| `build_engine.ps1` | Creates a per-variant venv, installs the engine and the matching ONNX Runtime distribution (mutually exclusive packages), runs PyInstaller and `selftest`. |
| `AvatarLive.iss` | Inno Setup script. x64 only; finds OBS through `HKLM\SOFTWARE\OBS Studio`; requires `obs64.exe` ≥ 31; installs the DLL into `obs-plugins\64bit` and engine + locale into `data\obs-plugins\obs-avatar-live`; no camera driver; uninstall removes the plugin and optionally the user's `%LOCALAPPDATA%\AvatarLive`. |
| `build_installer.ps1` | Invokes `ISCC.exe` with the right defines. |
| `INFO_BEFORE.txt` | Synthetic-content and consent notice shown before installation. |

Install layout:

```
<OBS>\obs-plugins\64bit\obs-avatar-live.dll
<OBS>\data\obs-plugins\obs-avatar-live\locale\*.ini
<OBS>\data\obs-plugins\obs-avatar-live\engine\avatar-engine.exe (+ runtime)
<OBS>\data\obs-plugins\obs-avatar-live\{LICENSE, THIRD_PARTY_NOTICES.md, MODEL_MANIFEST.json}
```

Model weights are never packaged. Code signing (`signtool`) is not wired in yet; sign the DLL, the engine
executable and the installer before any public release. Licensing review must be completed first
(`docs/LICENSE_REVIEW.md`).
