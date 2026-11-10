"""Engine registry."""

from __future__ import annotations

from avatar_engine.engines.accelerated_swap import AcceleratedSwapEngine
from avatar_engine.engines.base import IAvatarEngine
from avatar_engine.engines.lite_adaptive import LiteAdaptiveEngine
from avatar_engine.engines.lite_cpu_audio import LiteCpuAudioEngine
from avatar_engine.engines.lite_cpu_swap import LiteCpuSwapEngine
from avatar_engine.engines.synthetic_head import SyntheticHeadEngine

ENGINES: dict[str, type[IAvatarEngine]] = {
    "lite_cpu_audio": LiteCpuAudioEngine,
    "lite_cpu_swap": LiteCpuSwapEngine,
    "lite_adaptive": LiteAdaptiveEngine,
    "accelerated_swap": AcceleratedSwapEngine,
    "synthetic_head": SyntheticHeadEngine,
}


def create(name: str) -> IAvatarEngine:
    try:
        return ENGINES[name]()
    except KeyError:
        raise ValueError(f"unknown engine {name!r}") from None


def describe() -> list[dict]:
    return [cls.info().to_dict() for cls in ENGINES.values()]
