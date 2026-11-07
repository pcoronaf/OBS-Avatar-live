"""One running avatar session: engine + inputs + output clock + metrics."""

from __future__ import annotations

import threading
import uuid
from typing import Any, Callable

from avatar_engine import config as cfgmod
from avatar_engine import engine_factory, frame_ring as fr
from avatar_engine.engines.base import IAvatarEngine
from avatar_engine.metrics import ProcessStats
from avatar_engine.performance.provider_probe import fault
from avatar_engine.privacy import profile_store
from avatar_engine.scheduler import OutputScheduler

Emit = Callable[[str, dict], None]


class Session:
    def __init__(self, user_cfg: dict[str, Any], writer: fr.FrameRingWriter | None, emit: Emit,
                 engine: IAvatarEngine | None = None, profile_root=None) -> None:
        self.config = cfgmod.load(user_cfg)
        if self.config["output"]["width"] > (writer.max_width if writer else 1920) or \
                self.config["output"]["height"] > (writer.max_height if writer else 1080):
            raise cfgmod.ConfigError("output larger than the frame ring")
        self.writer = writer
        self.emit = emit
        self.session_id = "s-" + uuid.uuid4().hex[:12]
        self.engine = engine or engine_factory.create(self.config["engine"])
        self.engine.emit = emit  # type: ignore[attr-defined]
        self.profile = profile_store.load_profile(self.config["profile_id"], profile_root)  # requires consent
        self.inputs: list[Any] = []
        self.scheduler: OutputScheduler | None = None
        self.proc = ProcessStats()
        self.state = "starting"
        self._lock = threading.Lock()

    # -- inputs ---------------------------------------------------------------------------
    def _on_video(self, frame, ts):
        if fault("latency"):
            ts -= 600_000_000  # simulate capture arriving 600 ms late
        self.engine.push_video(frame, ts)

    def _open_inputs(self) -> None:
        mode = self.config["drive_mode"]
        if mode == "audio":
            from avatar_engine.audio.capture import MicrophoneCapture, WavFileCapture

            src = self.config.get("audio_input")
            a = self.config["audio"]
            if isinstance(src, str) and src.startswith("file:"):
                cap = WavFileCapture(self.engine.push_audio, src[5:], a["frame_ms"])
            else:
                cap = MicrophoneCapture(self.engine.push_audio, src, a["sample_rate_hz"], a["frame_ms"])
            self.inputs.append(cap)
        elif mode == "webcam":
            from avatar_engine.vision.capture import CameraCapture, SyntheticVideoSource

            src = self.config.get("video_input")
            inf = self.config["inference"]
            if isinstance(src, str) and src.startswith("file:"):
                cap = SyntheticVideoSource(self._on_video, src[5:])
            else:
                cap = CameraCapture(self._on_video, src if src is not None else 0, inf.get("capture_width", 640),
                                    inf.get("capture_height", 480))
                self.engine.on_capture_resolution = cap.set_resolution  # type: ignore[attr-defined]
            self.inputs.append(cap)

    # -- lifecycle ------------------------------------------------------------------------
    def start(self) -> dict:
        self.emit("state", {"state": "starting", "detail": f"engine {self.config['engine']}"})
        self.engine.initialize(self.config, {})
        self._open_inputs()
        for i in self.inputs:  # webcam frames may feed the Lite Adaptive benchmark
            i.start()
        self.engine.load_profile(self.profile)
        gen = self.engine.start_session(self.session_id)
        if self.writer is not None:
            self.writer.set_generation(gen)
            self.writer.set_ring_flags(fr.RING_RUNNING)
        self.scheduler = OutputScheduler(self.engine, self.writer, self.config)
        self.scheduler.start()
        self.state = "running"
        self.emit("state", {"state": "running", "detail": ""})
        m = self.engine.get_metrics()
        return {"session_id": self.session_id, "generation_id": gen, "engine": self.engine.name,
                "provider": m.get("execution_provider_selected"), "warnings": m.get("model_warnings") or []}

    def sync_generation(self) -> None:
        """After a provider switch the engine bumps generation_id; publish it to the ring."""
        if self.writer is not None and self.writer.generation_id != self.engine.generation_id:
            self.writer.set_generation(self.engine.generation_id)

    def pause(self, paused: bool) -> None:
        self.engine.pause(paused)
        if self.scheduler:
            self.scheduler.forced_status = ("Avatar Live en pausa", "") if paused else None
        if self.writer is not None:
            self.writer.set_ring_flags(fr.RING_RUNNING | (fr.RING_PAUSED if paused else 0))
        self.state = "paused" if paused else "running"
        self.emit("state", {"state": self.state, "detail": ""})

    def freeze_neutral(self, enabled: bool) -> None:
        self.engine.freeze_neutral(enabled)

    def stop(self) -> None:
        if self.scheduler:
            self.scheduler.stop()
        for i in self.inputs:
            try:
                i.stop()
            except Exception:
                pass
        self.inputs.clear()
        try:
            self.engine.shutdown()
        except Exception:
            pass
        if self.writer is not None:
            self.writer.set_ring_flags(0)
        self.state = "stopped"
        self.emit("state", {"state": "stopped", "detail": ""})

    def metrics(self) -> dict:
        self.sync_generation()
        m = {k: None for k in ("execution_provider_selected", "provider_device", "face_swap_unique_fps",
                               "inference_fps", "fallback_reason", "benchmark_timestamp")}
        m.update(self.engine.get_metrics())
        if self.scheduler:
            m.update(self.scheduler.metrics())
        input_errors = [getattr(i, "error", None) for i in self.inputs if getattr(i, "error", None)]
        m.update({
            "state": self.state,
            "cpu_utilization_pct": round(self.proc.cpu_pct(), 1),
            "ram_mb": round(self.proc.ram_mb(), 1),
            "input_errors": input_errors,
            "synthetic_label": True,
            "session_id": self.session_id,
            "generation_id": self.engine.generation_id,
        })
        return m
