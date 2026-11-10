# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for avatar-engine.exe (one-folder build).
# Usage (from repo root, inside the build venv):
#   pyinstaller --noconfirm --distpath dist --workpath build\pyinstaller installer\windows\avatar-engine.spec
import os
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

REPO = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
ENGINE = os.path.join(REPO, "engine")
VARIANT = os.environ.get("AVATAR_LIVE_VARIANT", "Lite")

datas, binaries, hiddenimports = [], [], []

# ONNX Runtime: whichever distribution is installed (onnxruntime, -openvino, -directml, -gpu)
# ships under the "onnxruntime" import name.
for pkg in ("onnxruntime", "cv2", "sounddevice", "_sounddevice_data", "cv2_enumerate_cameras"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

if VARIANT == "Lite":
    # Never ship CUDA/TensorRT in the Lite edition.
    def _not_cuda(entry):
        name = os.path.basename(entry[0]).lower()
        return not any(k in name for k in ("cuda", "cudnn", "cublas", "tensorrt", "nvinfer"))
    binaries = [b for b in binaries if _not_cuda(b)]

hiddenimports += collect_submodules("avatar_engine")
# Package data (avatar_engine/config.schema.json, read via importlib.resources).
datas += collect_data_files("avatar_engine")

# Contracts and manifest are needed at runtime for validation.
datas += [
    (os.path.join(REPO, "contracts", "config.schema.json"), "contracts"),
    (os.path.join(REPO, "models", "MODEL_MANIFEST.json"), "models"),
    # Redistributable models only (YuNet face detector, MIT) + their license texts.
    (os.path.join(REPO, "models", "bundled"), os.path.join("models", "bundled")),
]

a = Analysis(
    [os.path.join(ENGINE, "avatar_engine", "__main__.py")],
    pathex=[ENGINE],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "IPython", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="avatar-engine",
    console=True,          # stdout/stderr are captured by the plugin launcher; window is hidden by CREATE_NO_WINDOW
    disable_windowed_traceback=False,
    target_arch="x86_64",
    upx=False,
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="avatar-engine")
