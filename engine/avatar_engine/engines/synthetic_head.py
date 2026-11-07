"""E4 — Synthetic Talking Head (phase 4): not implemented.

A photoreal audio-driven talking-head model must first be evaluated for
latency, memory, quality and the license of code, weights and training data
(spec §5 E4). `inswapper` does not generate lip motion from audio.
"""

from __future__ import annotations

from avatar_engine.engines.base import EngineInfo, EngineUnavailable, IAvatarEngine


class SyntheticHeadEngine(IAvatarEngine):
    name = "synthetic_head"
    drive_modes = ("audio", "synthetic")
    REASON = "Phase 4: no talking-head model has been selected/validated yet. Use lite_cpu_audio."

    @classmethod
    def info(cls) -> EngineInfo:
        return EngineInfo(cls.name, "0.0.0", list(cls.drive_modes), [], experimental=True, available=False,
                          unavailable_reason=cls.REASON)

    def initialize(self, config, capabilities=None):
        raise EngineUnavailable(self.REASON)

    def load_profile(self, profile):
        raise EngineUnavailable(self.REASON)

    def poll_frame(self, now_ns):
        return None

    def get_metrics(self):
        return {"engine": self.name}
