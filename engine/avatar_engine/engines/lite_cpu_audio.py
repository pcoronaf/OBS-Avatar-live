"""E1 — Lite CPU Audio: photo + microphone, no webcam, no neural inference per frame."""

from __future__ import annotations

import threading
import time
from typing import Any

import numpy as np

from avatar_engine.audio.visemes import VisemeEstimator
from avatar_engine.engines.animator import Animator
from avatar_engine.engines.base import EngineFrame, EngineInfo, IAvatarEngine
from avatar_engine.engines.rig2d import PhotoRig
from avatar_engine.frame_ring import now_ns
from avatar_engine.metrics import FpsCounter, StageTimer


class LiteCpuAudioEngine(IAvatarEngine):
    name = "lite_cpu_audio"
    drive_modes = ("audio",)

    def __init__(self) -> None:
        super().__init__()
        self.rig: PhotoRig | None = None
        self.visemes: VisemeEstimator | None = None
        self.animator = Animator()
        self._lock = threading.Lock()
        self.stages = StageTimer()
        self.render_fps = FpsCounter()
        self._seq = 0
        self._last_mouth = None
        self._audio_frames = 0
        self._last_audio_ns = 0
        self.audio_disabled = False

    @classmethod
    def info(cls) -> EngineInfo:
        return EngineInfo(cls.name, "0.1.0", list(cls.drive_modes), [], experimental=False)

    def initialize(self, config: dict[str, Any], capabilities: dict[str, Any] | None = None) -> EngineInfo:
        self.config = config
        a = config["audio"]
        self.visemes = VisemeEstimator(a["sample_rate_hz"], a["frame_ms"], a.get("sensitivity", 1.0),
                                       a.get("vad_enabled", True))
        self.animator = Animator(mouth_delay_ms=a.get("mouth_delay_ms", 60))
        return self.info()

    def load_profile(self, profile) -> str:
        out = self.config["output"]
        t0 = time.perf_counter()
        self.rig = PhotoRig(profile.load_image(), profile.landmarks, profile.bbox, out["width"], out["height"],
                            out.get("zoom", 1.0), out.get("offset_x", 0.0), out.get("offset_y", 0.0),
                            out.get("background", "photo"), out.get("background_color", "#202020"))
        self.stages.add("prepare", (time.perf_counter() - t0) * 1000)
        self.profile = profile
        return profile.profile_id

    def push_audio(self, pcm_s16le_mono: np.ndarray, sample_rate: int, monotonic_ts: int) -> None:
        if self.visemes is None:
            return
        t0 = time.perf_counter()
        frame_ns = self.visemes.frame * 1_000_000_000 // self.visemes.sr
        with self._lock:
            states = self.visemes.push(pcm_s16le_mono, monotonic_ts)
            # timestamp each analysis frame at its end within the chunk
            n = len(states)
            for i, st in enumerate(states):
                st.timestamp_ns = monotonic_ts - (n - 1 - i) * frame_ns
                self.animator.push_mouth(st)
        self._audio_frames += len(states)
        self._last_audio_ns = monotonic_ts
        self.stages.add("audio", (time.perf_counter() - t0) * 1000)

    def set_expression(self, expression_id: str, intensity: float, duration_ms: int) -> None:
        with self._lock:
            self.animator.set_expression(expression_id, intensity, duration_ms, now_ns())

    def freeze_neutral(self, enabled: bool) -> None:
        super().freeze_neutral(enabled)
        self.animator.frozen = enabled

    def set_quality(self, **kwargs) -> None:
        if "sensitivity" in kwargs and self.visemes is not None:
            self.visemes.sensitivity = float(kwargs["sensitivity"])

    def poll_frame(self, now: int) -> EngineFrame | None:
        if self.rig is None:
            return None
        t0 = time.perf_counter()
        with self._lock:
            params, mouth = self.animator.step(now)
        if self.paused:
            params = type(params)()
        bgra = self.rig.render(params)
        self._last_mouth = mouth
        self._seq += 1
        self.render_fps.tick()
        self.stages.add("render", (time.perf_counter() - t0) * 1000)
        return EngineFrame(bgra, self._seq, 0, now, self.generation_id, paused=self.paused or self.frozen, owned=True,
                           extra={"mouth": mouth.as_dict()})

    def get_metrics(self) -> dict[str, Any]:
        audio_age = (now_ns() - self._last_audio_ns) / 1e6 if self._last_audio_ns else None
        return {
            "engine": self.name,
            "execution_provider_selected": None,
            "provider_device": "CPU",
            "face_swap_unique_fps": None,
            "inference_fps": None,
            "render_fps": round(self.render_fps.fps(), 2),
            "stage_p50_ms": self.stages.p50(),
            "stage_p95_ms": self.stages.p95(),
            "audio_frames": self._audio_frames,
            "audio_age_ms": None if audio_age is None else round(audio_age, 1),
            "audio_disabled": self.audio_disabled,
            "audio_stalled": (not self.audio_disabled) and (audio_age is None or audio_age > 500),
            "viseme_model": VisemeEstimator.model,
            "mouth": self._last_mouth.as_dict() if self._last_mouth else None,
        }
