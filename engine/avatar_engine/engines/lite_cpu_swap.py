"""E2 — Lite CPU Face Swap: photo + webcam, ONNX CPUExecutionProvider.

Inference runs on a worker thread at whatever rate the CPU allows (capped by
the adaptive controller); the output clock keeps 30 fps and marks reused
frames. Unique (transformed) FPS is reported separately (ADR-003).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

import numpy as np

from avatar_engine.engines.base import EngineFrame, EngineInfo, EngineUnavailable, IAvatarEngine, next_generation_id
from avatar_engine.frame_ring import now_ns
from avatar_engine.imaging import framing_matrix, hex_to_bgr, to_bgra
from avatar_engine.metrics import FpsCounter, ProcessStats, RollingStats, StageTimer
from avatar_engine.models import manifest
from avatar_engine.performance.adaptive_controller import AdaptiveController, QualitySettings, WindowSample
from avatar_engine.performance.provider_probe import ProviderCandidate, discover
from avatar_engine.vision.capture import LatestFrameQueue

SWAP_MODELS = ("yunet_face_detector", "arcface_recognition", "inswapper_compatible_swapper")


def resolve_models() -> tuple[dict[str, Path], dict[str, str], list[str]]:
    paths_, hashes, warnings = {}, {}, []
    for mid in SWAP_MODELS:
        try:
            r = manifest.resolve(mid)
        except manifest.ModelError as exc:
            raise EngineUnavailable(str(exc)) from exc
        paths_[mid], hashes[mid] = r.path, r.sha256
        if r.warning:
            warnings.append(r.warning)
    return paths_, hashes, warnings


class LiteCpuSwapEngine(IAvatarEngine):
    name = "lite_cpu_swap"
    drive_modes = ("webcam",)
    window_s = 12.0

    def __init__(self, pipeline_factory: Callable | None = None, model_resolver: Callable | None = None) -> None:
        super().__init__()
        from avatar_engine.engines.swap_pipeline import build_pipeline

        self.pipeline_factory = pipeline_factory or build_pipeline
        self.model_resolver = model_resolver or resolve_models
        self.emit: Callable[[str, dict], None] = lambda name, payload: None
        self.on_capture_resolution: Callable[[int, int], None] = lambda w, h: None
        self.queue = LatestFrameQueue(2)
        self.pipeline = None
        self.candidate: ProviderCandidate = ProviderCandidate("cpu", "CPUExecutionProvider", "CPU")
        self.models: dict[str, Path] = {}
        self.model_hashes: dict[str, str] = {}
        self.warnings: list[str] = []
        self.stages = StageTimer()
        self.unique_fps = FpsCounter()
        self.e2e = RollingStats(10.0)
        self.proc = ProcessStats()
        self.controller: AdaptiveController | None = None
        self._latest: EngineFrame | None = None
        self._neutral: np.ndarray | None = None
        self._seq = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.fallback_reason: str | None = None
        self.benchmark_timestamp: str | None = None
        self.no_face_frames = 0
        self.stale_input_drops = 0
        self.runtime_errors = 0
        self._last_inference_start = 0.0
        self._profile_img: np.ndarray | None = None
        self._profile_lm: np.ndarray | None = None

    @classmethod
    def info(cls) -> EngineInfo:
        return EngineInfo(cls.name, "0.1.0", list(cls.drive_modes), list(SWAP_MODELS))

    # ------------------------------------------------------------------ lifecycle
    def initialize(self, config: dict[str, Any], capabilities: dict[str, Any] | None = None) -> EngineInfo:
        self.config = config
        inf = config["inference"]
        self.models, self.model_hashes, self.warnings = self.model_resolver()
        self.queue = LatestFrameQueue(inf.get("frame_queue_size", 2))
        base = QualitySettings(enhancement=False, detector_size=inf.get("detector_size", 640),
                               detection_every_n_frames=inf["detection_every_n_frames"],
                               capture_width=inf.get("capture_width", 640), capture_height=inf.get("capture_height", 480),
                               max_inference_fps=float(config["output"]["fps"]))
        self.controller = AdaptiveController(base, inf["minimum_unique_swap_fps"], inf["max_end_to_end_latency_ms"],
                                             inf.get("cpu_budget_pct", 60), 3, inf.get("adaptive_quality", True))
        self.candidates = self.discover_candidates()
        return self.info()

    def discover_candidates(self) -> list[ProviderCandidate]:
        return discover(cpu_only=True)

    def build(self, candidate: ProviderCandidate):
        inf = self.config["inference"]
        s = self.controller.settings if self.controller else None
        pipe = self.pipeline_factory(candidate, self.models, inf["max_threads"],
                                     s.detection_every_n_frames if s else inf["detection_every_n_frames"],
                                     s.detector_size if s else inf.get("detector_size", 640))
        if self._profile_img is not None:
            pipe.set_source(self._profile_img, None)
        return pipe

    def load_profile(self, profile) -> str:
        self.profile = profile
        self._profile_img = profile.load_image()
        self._profile_lm = profile.landmarks
        self.select_provider()
        self._neutral = self._compose(self._profile_img)
        return profile.profile_id

    def select_provider(self) -> None:
        self.pipeline = self.build(self.candidate)

    def start_session(self, session_id: str) -> int:
        gen = super().start_session(session_id)
        self._stop.clear()
        self._thread = threading.Thread(target=self._worker, name=f"{self.name}-worker", daemon=True)
        self._thread.start()
        return gen

    def stop_session(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self.queue.clear()
        super().stop_session()

    # ------------------------------------------------------------------ inputs/outputs
    def push_video(self, frame_bgr: np.ndarray, monotonic_ts: int) -> None:
        self.queue.put(frame_bgr, monotonic_ts)

    def poll_frame(self, now: int) -> EngineFrame | None:
        if (self.frozen or self.paused) and self._neutral is not None:
            return EngineFrame(self._neutral, -1, 0, now, self.generation_id, paused=True)
        with self._lock:
            return self._latest

    def _compose(self, frame: np.ndarray) -> np.ndarray:
        out = self.config["output"]
        import cv2

        h, w = frame.shape[:2]
        M = framing_matrix(w, h, out["width"], out["height"], out.get("zoom", 1.0), out.get("offset_x", 0.0),
                           out.get("offset_y", 0.0))
        img = cv2.warpAffine(frame, M, (out["width"], out["height"]), flags=cv2.INTER_LINEAR,
                             borderMode=cv2.BORDER_CONSTANT, borderValue=hex_to_bgr(out.get("background_color", "#202020")))
        return to_bgra(img)

    # ------------------------------------------------------------------ worker
    def _apply_settings(self, s: QualitySettings) -> None:
        if self.pipeline is None:
            return
        self.pipeline.set_detection_interval(s.detection_every_n_frames)
        if getattr(self.pipeline.detector, "input_size", s.detector_size) != s.detector_size:
            self.pipeline.detector.input_size = s.detector_size
        self.on_capture_resolution(s.capture_width, s.capture_height)

    def _evaluate_window(self) -> None:
        if self.controller is None:
            return
        w = WindowSample(self.unique_fps.fps(), self.e2e.p(95), self.proc.cpu_pct(), 0)
        d = self.controller.observe(w)
        if d.action in ("degrade", "upgrade"):
            self._apply_settings(d.settings)
            self.emit("state", {"state": "degraded" if d.level else "running",
                                "detail": f"quality level {d.level}: {d.reason}"})
        elif d.action == "exhausted":
            self.on_exhausted(d.reason)

    def on_exhausted(self, reason: str) -> None:
        self.fallback_reason = f"quality ladder exhausted ({reason})"
        self.emit("fallback", {"from": self.name, "to": "lite_cpu_audio", "reason": self.fallback_reason,
                               "recommendation": True})

    def on_runtime_error(self, exc: Exception) -> None:
        self.runtime_errors += 1
        self.emit("error", {"code": "inference_failed", "message": str(exc), "recoverable": True})
        time.sleep(0.2)

    def _worker(self) -> None:
        window_start = time.monotonic()
        stale_ns = int(self.config.get("stale_frame_ms", 500) * 1e6)
        while not self._stop.is_set():
            if time.monotonic() - window_start >= self.window_s:
                window_start = time.monotonic()
                self._evaluate_window()
            item = self.queue.get_latest(0.1)
            if item is None or self.pipeline is None or self.paused or self.frozen:
                continue
            frame, ts = item
            if now_ns() - ts > stale_ns:  # capture arrived too late: never process stale input
                self.stale_input_drops += 1
                continue
            cap = self.controller.settings.max_inference_fps if self.controller else 30.0
            if time.monotonic() - self._last_inference_start < 1.0 / max(cap, 0.5):
                continue
            self._last_inference_start = time.monotonic()
            try:
                res = self.pipeline.process(frame)
            except Exception as exc:  # provider/device failure
                self.on_runtime_error(exc)
                continue
            for k, v in res.timings_ms.items():
                self.stages.add(k, v)
            if not res.face_found:
                self.no_face_frames += 1
                continue  # keep last result; the scheduler turns it into a visible stale state after 500 ms
            t0 = time.perf_counter()
            bgra = self._compose(res.frame)
            self.stages.add("compose", (time.perf_counter() - t0) * 1000)
            created = now_ns()
            self.e2e.add((created - ts) / 1e6)
            self._seq += 1
            self.unique_fps.tick()
            with self._lock:
                self._latest = EngineFrame(bgra, self._seq, ts, created, self.generation_id,
                                           degraded=bool(self.controller and self.controller.level >= 5))

    def switch_provider(self, candidate: ProviderCandidate, reason: str) -> bool:
        """Swap provider between frames: build new pipeline, drain queue, new generation."""
        try:
            pipe = self.build(candidate)
        except Exception as exc:
            self.emit("error", {"code": "provider_switch_failed", "message": str(exc), "recoverable": True})
            return False
        old = self.candidate
        with self._lock:
            self.pipeline = pipe
            self.candidate = candidate
            self.queue.clear()
            self._latest = None
            self.generation_id = next_generation_id()
        self.unique_fps.clear()
        self.e2e.clear()
        self.fallback_reason = reason
        self.emit("fallback", {"from": old.label, "to": candidate.label, "reason": reason,
                               "generation_id": self.generation_id})
        return True

    def get_metrics(self) -> dict[str, Any]:
        lvl = self.controller.level if self.controller else 0
        return {
            "engine": self.name,
            "execution_provider_selected": self.candidate.ort_name,
            "provider_device": self.candidate.device,
            "face_swap_unique_fps": round(self.unique_fps.fps(), 2),
            "inference_fps": round(self.unique_fps.fps(), 2),
            "capture_to_compose_p50_ms": _r(self.e2e.p(50)),
            "capture_to_compose_p95_ms": _r(self.e2e.p(95)),
            "stage_p50_ms": self.stages.p50(),
            "stage_p95_ms": self.stages.p95(),
            "dropped_frames": self.queue.dropped + self.stale_input_drops,
            "no_face_frames": self.no_face_frames,
            "runtime_errors": self.runtime_errors,
            "quality_level": lvl,
            "quality_settings": self.controller.settings.__dict__ if self.controller else None,
            "fallback_reason": self.fallback_reason,
            "benchmark_timestamp": self.benchmark_timestamp,
            "model_warnings": self.warnings,
        }


def _r(v):
    return None if v is None else round(v, 1)
