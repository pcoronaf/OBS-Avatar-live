"""E2a — Lite Adaptive: automatic CPU / OpenVINO / DirectML selection by real benchmark."""

from __future__ import annotations

import cv2
import numpy as np

from avatar_engine.engines.base import EngineInfo
from avatar_engine.engines.lite_cpu_swap import SWAP_MODELS, LiteCpuSwapEngine
from avatar_engine.performance import startup_benchmark as sb
from avatar_engine.performance.provider_probe import ProviderCandidate, discover
from avatar_engine.vision import alignment


def representative_frames(img: np.ndarray, n: int = 12, size: tuple[int, int] = (640, 480)) -> list[np.ndarray]:
    """Small pose/scale/lighting variations of the profile photo, used when no webcam frames exist yet."""
    rng = np.random.default_rng(7)
    h, w = img.shape[:2]
    base = cv2.resize(img, size) if (w, h) != size else img
    out = []
    for i in range(n):
        ang, sc = rng.uniform(-8, 8), rng.uniform(0.9, 1.1)
        M = cv2.getRotationMatrix2D((size[0] / 2, size[1] / 2), ang, sc)
        M[:, 2] += rng.uniform(-20, 20, 2)
        f = cv2.warpAffine(base, M, size, borderMode=cv2.BORDER_REFLECT)
        out.append(cv2.convertScaleAbs(f, alpha=rng.uniform(0.85, 1.15), beta=rng.uniform(-15, 15)))
    return out


class LiteAdaptiveEngine(LiteCpuSwapEngine):
    name = "lite_adaptive"
    allow_cuda = False
    experimental = False

    @classmethod
    def info(cls) -> EngineInfo:
        return EngineInfo(cls.name, "0.1.0", list(cls.drive_modes), list(SWAP_MODELS), experimental=cls.experimental)

    def discover_candidates(self) -> list[ProviderCandidate]:
        inf = self.config["inference"]
        prov = inf.get("provider", "auto")
        if inf.get("cpu_only") or prov == "cpu":
            return discover(cpu_only=True)  # never initialises accelerated EPs
        prio = inf.get("provider_priority") or ["openvino", "directml", "cpu"]
        if prov != "auto":
            prio = [prov, "cpu"]
        if self.config.get("user_profile") == "intel_optimized":
            prio = ["openvino"] + [p for p in prio if p != "openvino"]
        return discover(prio, cpu_only=False, allow_cuda=self.allow_cuda)

    def select_provider(self) -> None:
        inf = self.config["inference"]
        self.viable: list[ProviderCandidate] = []
        if len(self.candidates) == 1 and not inf.get("startup_benchmark", True):
            self.candidate = self.candidates[0]
            self.pipeline = self.build(self.candidate)
            return
        out = self.config["output"]
        key = sb.cache_key(self.model_hashes, out["width"], out["height"], inf.get("input_face_size", 256),
                           {"cands": [c.label for c in self.candidates], "threads": inf["max_threads"]})
        results = sb.load_cached(key)
        if results is None:
            frames = self.benchmark_frames()
            probe = self._probe_crop(frames)
            results = sb.run_benchmark(self.candidates, self.build, frames, inf["benchmark_duration_seconds"], probe)
            sb.save_cached(key, results)
        self.benchmark_results = results
        self.benchmark_timestamp = max((r.timestamp for r in results), default=None)
        best, reasons = sb.select(results, inf["max_end_to_end_latency_ms"], inf["minimum_unique_swap_fps"],
                                  self.config.get("user_profile", "auto"))
        by_label = {c.label: c for c in self.candidates}
        ok_sorted = sorted((r for r in results if r.ok), key=lambda r: -r.unique_fps)
        self.viable = [by_label[r.label] for r in ok_sorted if r.label in by_label]
        if reasons:
            self.fallback_reason = "; ".join(reasons)
        chosen = by_label.get(best.label) if best else None
        if chosen is None:
            chosen = next(c for c in self.candidates if c.key == "cpu")
        self.candidate = chosen
        self.pipeline = self.build(chosen)

    def benchmark_frames(self) -> list[np.ndarray]:
        frames = []
        for _ in range(12):
            item = self.queue.get_latest(0.05)
            if item is not None:
                frames.append(item[0])
        if len(frames) >= 4:
            return frames
        return representative_frames(self._profile_img)

    def _probe_crop(self, frames: list[np.ndarray]) -> np.ndarray | None:
        try:  # fixed crop from the profile landmarks, used to compare each EP against the CPU output
            crop, _ = alignment.align(self._profile_img, self._profile_lm, 256)
            return crop
        except Exception:
            return None

    def on_runtime_error(self, exc: Exception) -> None:
        super().on_runtime_error(exc)
        if self.candidate.key != "cpu" and self.runtime_errors >= 3:
            cpu = next((c for c in self.candidates if c.key == "cpu"), ProviderCandidate("cpu", "CPUExecutionProvider", "CPU"))
            self.switch_provider(cpu, f"runtime errors on {self.candidate.label}: {exc}")
            self.runtime_errors = 0

    def on_exhausted(self, reason: str) -> None:
        others = [c for c in getattr(self, "viable", []) if c.label != self.candidate.label]
        if others and self.switch_provider(others[0], f"quality ladder exhausted on {self.candidate.label} ({reason})"):
            self.controller.reset(level=2)
            self._apply_settings(self.controller.settings)
            return
        super().on_exhausted(reason)
