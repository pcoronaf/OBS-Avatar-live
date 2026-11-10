"""`IAvatarEngine` contract (spec §4)."""

from __future__ import annotations

import abc
import itertools
from dataclasses import dataclass, field
from typing import Any

import numpy as np

_generation = itertools.count(1)


def next_generation_id() -> int:
    return next(_generation)


class EngineUnavailable(RuntimeError):
    """The engine cannot run on this machine/configuration (missing model, phase not implemented...)."""


@dataclass
class EngineInfo:
    name: str
    version: str
    drive_modes: list[str]
    needs_models: list[str]
    experimental: bool = False
    available: bool = True
    unavailable_reason: str | None = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class EngineFrame:
    """Engine output before final composition (label is added by the compositor)."""

    bgra: np.ndarray
    sequence: int  # engine-internal sequence of *unique* results
    capture_ts_ns: int  # timestamp of the input that produced it (0 if procedural)
    created_ts_ns: int
    generation_id: int
    degraded: bool = False
    paused: bool = False
    owned: bool = False  # True: produced for this tick only, the compositor may draw on it in place
    extra: dict[str, Any] = field(default_factory=dict)


class IAvatarEngine(abc.ABC):
    name = "base"
    drive_modes: tuple[str, ...] = ()

    def __init__(self) -> None:
        self.config: dict[str, Any] = {}
        self.generation_id = 0
        self.session_id: str | None = None
        self.profile = None
        self.paused = False
        self.frozen = False

    @classmethod
    def info(cls) -> EngineInfo:
        return EngineInfo(cls.name, "0.1.0", list(cls.drive_modes), [])

    @abc.abstractmethod
    def initialize(self, config: dict[str, Any], capabilities: dict[str, Any]) -> EngineInfo: ...

    @abc.abstractmethod
    def load_profile(self, profile) -> str: ...

    def start_session(self, session_id: str) -> int:
        self.session_id = session_id
        self.generation_id = next_generation_id()
        return self.generation_id

    def push_video(self, frame_bgr: np.ndarray, monotonic_ts: int) -> None:
        pass

    def push_audio(self, pcm_s16le_mono: np.ndarray, sample_rate: int, monotonic_ts: int) -> None:
        pass

    def set_expression(self, expression_id: str, intensity: float, duration_ms: int) -> None:
        pass

    @abc.abstractmethod
    def poll_frame(self, now_ns: int) -> EngineFrame | None:
        """Newest available result; may be the same `sequence` as last time (reuse)."""

    @abc.abstractmethod
    def get_metrics(self) -> dict[str, Any]: ...

    def set_quality(self, **kwargs) -> None:
        pass

    def pause(self, paused: bool) -> None:
        self.paused = paused

    def freeze_neutral(self, enabled: bool) -> None:
        self.frozen = enabled

    def stop_session(self) -> None:
        self.session_id = None

    def shutdown(self) -> None:
        self.stop_session()
