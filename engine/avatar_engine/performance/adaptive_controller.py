"""Adaptive quality loop with hysteresis (spec §5 E2a, §7).

Metrics are evaluated in windows (default 12 s). A change requires at least
`hysteresis_windows` (≥3) consecutive bad (or good) windows, and every change
resets the counters, so the pipeline does not oscillate.

Degradation ladder (ordered, spec §5 E2a):
  L1 enhancer off · L2 detector 640→320 · L3 detect less often (tracking) ·
  L4 capture 640×360 · L5 cap inference rate (output stays 30 fps, frames marked
  repeated) · L6 recommend provider switch / Lite CPU Audio / visible pause.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class QualitySettings:
    enhancement: bool = False
    detector_size: int = 640
    detection_every_n_frames: int = 6
    capture_width: int = 640
    capture_height: int = 480
    max_inference_fps: float = 30.0

    def copy(self) -> "QualitySettings":
        return QualitySettings(**self.__dict__)


def ladder(base: QualitySettings) -> list[QualitySettings]:
    levels = [base.copy()]
    s = base.copy()
    s.enhancement = False
    levels.append(s.copy())
    s.detector_size = min(s.detector_size, 320)
    levels.append(s.copy())
    s.detection_every_n_frames = max(s.detection_every_n_frames * 2, 10)
    levels.append(s.copy())
    if s.capture_width > 640 or s.capture_height > 360:
        s.capture_width, s.capture_height = 640, 360
    levels.append(s.copy())
    s.max_inference_fps = min(s.max_inference_fps, 8.0)
    levels.append(s.copy())
    return levels  # index 0..5; level 6 = "exhausted" (fallback recommendation)


@dataclass
class WindowSample:
    unique_fps: float
    p95_ms: float | None
    cpu_pct: float | None
    backlog: int = 0


@dataclass
class Decision:
    action: str  # "none" | "degrade" | "upgrade" | "exhausted"
    level: int
    settings: QualitySettings
    reason: str = ""


@dataclass
class AdaptiveController:
    base: QualitySettings
    min_unique_fps: float = 5.0
    max_latency_ms: float = 250.0
    cpu_budget_pct: float = 60.0
    hysteresis_windows: int = 3
    enabled: bool = True
    level: int = 0
    _bad: int = 0
    _good: int = 0
    _levels: list = field(default_factory=list)
    history: list = field(default_factory=list)

    def __post_init__(self) -> None:
        self.hysteresis_windows = max(3, self.hysteresis_windows)
        self._levels = ladder(self.base)

    @property
    def settings(self) -> QualitySettings:
        return self._levels[min(self.level, len(self._levels) - 1)]

    @property
    def max_level(self) -> int:
        return len(self._levels)  # == "exhausted"

    def classify(self, w: WindowSample) -> tuple[str, str]:
        if w.unique_fps < self.min_unique_fps:
            return "bad", f"unique FPS {w.unique_fps:.1f} < {self.min_unique_fps:.1f}"
        if w.p95_ms is not None and w.p95_ms > self.max_latency_ms:
            return "bad", f"P95 {w.p95_ms:.0f} ms > {self.max_latency_ms:.0f} ms"
        if w.cpu_pct is not None and w.cpu_pct > self.cpu_budget_pct:
            return "bad", f"CPU {w.cpu_pct:.0f}% > budget {self.cpu_budget_pct:.0f}%"
        if w.backlog > 0:
            return "bad", f"input backlog {w.backlog}"
        headroom = (w.unique_fps > self.min_unique_fps * 1.6
                    and (w.p95_ms is None or w.p95_ms < self.max_latency_ms * 0.6)
                    and (w.cpu_pct is None or w.cpu_pct < self.cpu_budget_pct * 0.7))
        return ("good" if headroom else "ok"), ""

    def observe(self, w: WindowSample) -> Decision:
        kind, reason = self.classify(w)
        self.history.append((kind, self.level))
        if not self.enabled:
            return Decision("none", self.level, self.settings)
        if kind == "bad":
            self._bad += 1
            self._good = 0
        elif kind == "good":
            self._good += 1
            self._bad = 0
        else:
            self._bad = self._good = 0
        if self._bad >= self.hysteresis_windows:
            self._bad = 0
            if self.level < self.max_level:
                self.level += 1
            if self.level >= self.max_level:
                return Decision("exhausted", self.level, self.settings, reason)
            return Decision("degrade", self.level, self.settings, reason)
        if self._good >= self.hysteresis_windows and self.level > 0:
            self._good = 0
            self.level = min(self.level, self.max_level - 1) - 1
            return Decision("upgrade", self.level, self.settings, "sustained headroom")
        return Decision("none", self.level, self.settings)

    def reset(self, level: int = 0) -> None:
        self.level, self._bad, self._good = level, 0, 0
