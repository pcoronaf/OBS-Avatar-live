"""Rolling latency percentiles, FPS counters and process resource usage."""

from __future__ import annotations

import os
import sys
import threading
import time
from collections import deque


def percentile(values, q: float) -> float | None:
    vals = sorted(values)
    if not vals:
        return None
    k = (len(vals) - 1) * q / 100.0
    lo = int(k)
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


class RollingStats:
    """Samples kept for `window_s` seconds (time-based window)."""

    def __init__(self, window_s: float = 10.0, max_samples: int = 4096) -> None:
        self.window_s = window_s
        self._d: deque[tuple[float, float]] = deque(maxlen=max_samples)
        self._lock = threading.Lock()

    def add(self, value: float, t: float | None = None) -> None:
        t = time.monotonic() if t is None else t
        with self._lock:
            self._d.append((t, value))
            self._trim(t)

    def _trim(self, now: float) -> None:
        while self._d and now - self._d[0][0] > self.window_s:
            self._d.popleft()

    def values(self, now: float | None = None) -> list[float]:
        with self._lock:
            self._trim(time.monotonic() if now is None else now)
            return [v for _, v in self._d]

    def p(self, q: float, now: float | None = None) -> float | None:
        return percentile(self.values(now), q)

    def mean(self, now: float | None = None) -> float | None:
        v = self.values(now)
        return sum(v) / len(v) if v else None

    def clear(self) -> None:
        with self._lock:
            self._d.clear()


class FpsCounter:
    def __init__(self, window_s: float = 3.0) -> None:
        self.window_s = window_s
        self._t: deque[float] = deque(maxlen=2048)
        self._lock = threading.Lock()

    def tick(self, t: float | None = None) -> None:
        with self._lock:
            self._t.append(time.monotonic() if t is None else t)

    def fps(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        with self._lock:
            while self._t and now - self._t[0] > self.window_s:
                self._t.popleft()
            n = len(self._t)
            if n < 2:
                return 0.0
            span = self._t[-1] - self._t[0]
            interval = span / (n - 1) if n > 1 else 0.0
            # Producer stopped (no tick for > 3 typical intervals, at least 1 s): report 0, not the old rate.
            if now - self._t[-1] > max(1.0, 3.0 * interval):
                return 0.0
            return (n - 1) / span if span > 0 else 0.0

    def clear(self) -> None:
        with self._lock:
            self._t.clear()


class StageTimer:
    """Per-stage latency (ms) with P50/P95."""

    def __init__(self, window_s: float = 10.0) -> None:
        self.window_s = window_s
        self.stages: dict[str, RollingStats] = {}

    def add(self, stage: str, ms: float) -> None:
        self.stages.setdefault(stage, RollingStats(self.window_s)).add(ms)

    def p95(self) -> dict[str, float]:
        return {k: round(v.p(95) or 0.0, 2) for k, v in self.stages.items()}

    def p50(self) -> dict[str, float]:
        return {k: round(v.p(50) or 0.0, 2) for k, v in self.stages.items()}


class ProcessStats:
    """CPU% (of the whole machine) and RSS for this process; psutil optional."""

    def __init__(self) -> None:
        self._psutil = None
        try:
            import psutil

            self._psutil = psutil.Process()
            self._psutil.cpu_percent(None)
        except Exception:
            self._psutil = None
        self._last = (time.monotonic(), self._cpu_time())

    @staticmethod
    def _cpu_time() -> float:
        t = os.times()
        return t.user + t.system

    def cpu_pct(self) -> float:
        if self._psutil is not None:
            return self._psutil.cpu_percent(None) / (os.cpu_count() or 1)
        now, cpu = time.monotonic(), self._cpu_time()
        dt = now - self._last[0]
        pct = 0.0 if dt <= 0 else (cpu - self._last[1]) / dt * 100.0 / (os.cpu_count() or 1)
        self._last = (now, cpu)
        return max(0.0, pct)

    def ram_mb(self) -> float:
        if self._psutil is not None:
            return self._psutil.memory_info().rss / 1e6
        if sys.platform == "win32":  # pragma: no cover
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                    (n, ctypes.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                                   "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                                                   "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            k32 = ctypes.WinDLL("kernel32")
            k32.GetCurrentProcess.restype = wintypes.HANDLE  # 64-bit pseudo-handle, not a C int
            fn = k32.K32GetProcessMemoryInfo
            fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
            fn.restype = wintypes.BOOL
            if not fn(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
                return 0.0
            return pmc.WorkingSetSize / 1e6
        try:
            with open("/proc/self/statm") as f:
                return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6
        except OSError:
            return 0.0
