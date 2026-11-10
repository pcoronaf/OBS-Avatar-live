"""E3 — Accelerated Face Swap (phase 3, experimental): same pipeline, GPU EPs incl. CUDA."""

from __future__ import annotations

from avatar_engine.engines.lite_adaptive import LiteAdaptiveEngine


class AcceleratedSwapEngine(LiteAdaptiveEngine):
    name = "accelerated_swap"
    allow_cuda = True
    experimental = True

    def discover_candidates(self):
        inf = self.config["inference"]
        if inf.get("provider", "auto") == "auto" and inf.get("provider_priority") == ["openvino", "directml", "cpu"]:
            inf = dict(inf, provider_priority=["cuda", "directml", "openvino", "cpu"])
            self.config = dict(self.config, inference=inf)
        return super().discover_candidates()
