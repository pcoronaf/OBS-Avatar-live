"""Hardware/driver facts used for provider selection and benchmark cache keys."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from functools import lru_cache


def _cpu_name() -> str:
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                return winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
        except OSError:
            return platform.processor()
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def _cpu_flags() -> list[str]:
    flags = []
    if sys.platform == "win32":
        try:
            import ctypes

            f = ctypes.windll.kernel32.IsProcessorFeaturePresent
            for name, pf in (("avx", 39), ("avx2", 40), ("avx512f", 41)):
                if f(pf):
                    flags.append(name)
        except Exception:
            pass
        return flags
    try:
        with open("/proc/cpuinfo") as fh:
            for line in fh:
                if line.startswith("flags"):
                    have = set(line.split(":", 1)[1].split())
                    return [x for x in ("avx", "avx2", "fma", "avx512f", "avx512_vnni", "avx_vnni") if x in have]
    except OSError:
        pass
    return flags


def memory_mb() -> tuple[float, float]:
    """(total, available) physical memory in MB."""
    try:
        import psutil

        vm = psutil.virtual_memory()
        return vm.total / 1e6, vm.available / 1e6
    except Exception:
        pass
    if sys.platform == "win32":
        import ctypes

        class MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = MS()
        m.dwLength = ctypes.sizeof(MS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.ullTotalPhys / 1e6, m.ullAvailPhys / 1e6
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0]) / 1000
        return info.get("MemTotal", 0.0), info.get("MemAvailable", 0.0)
    except OSError:
        return 0.0, 0.0


def gpu_adapters() -> list[dict]:
    """Graphics adapters with driver versions (integrated GPUs included)."""
    if sys.platform == "win32":
        try:
            cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                   "Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion,AdapterCompatibility,"
                   "AdapterRAM | ConvertTo-Json -Compress"]
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=10, creationflags=0x08000000).stdout
            data = json.loads(out) if out.strip() else []
            data = [data] if isinstance(data, dict) else data
            return [{"name": d.get("Name"), "driver": d.get("DriverVersion"), "vendor": d.get("AdapterCompatibility"),
                     "vram_mb": (d.get("AdapterRAM") or 0) / 1e6} for d in data]
        except Exception:
            return []
    try:
        out = subprocess.run(["lspci"], capture_output=True, text=True, timeout=5).stdout
        return [{"name": l.split(": ", 1)[-1], "driver": None, "vendor": None, "vram_mb": None}
                for l in out.splitlines() if "VGA" in l or "3D controller" in l or "Display" in l]
    except Exception:
        return []


@lru_cache(maxsize=1)
def system_info() -> dict:
    total, avail = memory_mb()
    try:
        import onnxruntime as ort

        ort_version = ort.__version__
    except Exception:
        ort_version = None
    try:
        import psutil

        physical = psutil.cpu_count(logical=False)
    except Exception:
        physical = None
    return {
        "os": f"{platform.system()} {platform.release()} ({platform.version()})",
        "machine": platform.machine(),
        "cpu": _cpu_name(),
        "cpu_flags": _cpu_flags(),
        "logical_cores": os.cpu_count(),
        "physical_cores": physical,
        "ram_total_mb": round(total),
        "ram_available_mb": round(avail),
        "gpus": gpu_adapters(),
        "onnxruntime": ort_version,
        "python": platform.python_version(),
    }
