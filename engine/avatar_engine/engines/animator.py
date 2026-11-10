"""Procedural idle + speech animation for the photo rig.

Blinks are aperiodic (gamma-distributed intervals, occasional double blinks),
gaze makes small saccades that return toward the camera, and head motion is a
sum of slow incommensurate sines plus a small speech-energy nod. All values are
smoothed and clamped by `RigParams.clamp()` (anatomical limits).
"""

from __future__ import annotations

import math
import random
from collections import deque

from avatar_engine.audio.visemes import MouthState
from avatar_engine.engines.rig2d import RigParams


class Animator:
    def __init__(self, seed: int | None = None, mouth_delay_ms: float = 60.0) -> None:
        self.rng = random.Random(seed)
        self.mouth_delay_ns = int(mouth_delay_ms * 1e6)
        self._mouth: deque[MouthState] = deque(maxlen=200)
        self._next_blink = None
        self._blink_start = None
        self._double = False
        self._gaze = [0.0, 0.0]
        self._gaze_target = [0.0, 0.0]
        self._next_saccade = None
        self._phases = [self.rng.uniform(0, 2 * math.pi) for _ in range(6)]
        self._freqs = [0.11, 0.07, 0.17, 0.05, 0.13, 0.09]
        self._energy = 0.0
        self._expr = None  # (id, intensity, start_ns, duration_ns)
        self._last_ns = None
        self.frozen = False

    def push_mouth(self, st: MouthState) -> None:
        self._mouth.append(st)

    def set_expression(self, expression_id: str, intensity: float, duration_ms: int, now_ns: int) -> None:
        self._expr = (expression_id, max(0.0, min(1.0, intensity)), now_ns, max(1, duration_ms) * 1_000_000)

    def _mouth_at(self, now_ns: int) -> MouthState:
        target = now_ns - self.mouth_delay_ns
        chosen = None
        for st in self._mouth:
            if st.timestamp_ns <= target:
                chosen = st
            else:
                break
        if chosen is None:
            return MouthState()
        if target - chosen.timestamp_ns > 300_000_000:  # audio stalled: close the mouth
            return MouthState(timestamp_ns=chosen.timestamp_ns)
        return chosen

    def _blink(self, now_ns: int, speaking: bool) -> float:
        t = now_ns / 1e9
        if self._next_blink is None:
            self._next_blink = t + self.rng.uniform(0.8, 3.0)
        if self._blink_start is None and t >= self._next_blink:
            self._blink_start = t
        if self._blink_start is None:
            return 0.0
        dt = t - self._blink_start
        close, hold, open_ = 0.075, 0.035, 0.12
        if dt < close:
            v = dt / close
        elif dt < close + hold:
            v = 1.0
        elif dt < close + hold + open_:
            v = 1 - (dt - close - hold) / open_
        else:
            self._blink_start = None
            if not self._double and self.rng.random() < 0.15:
                self._double = True
                self._next_blink = t + 0.12
            else:
                self._double = False
                mean = 3.2 if speaking else 4.0
                self._next_blink = t + min(9.0, 0.6 + self.rng.gammavariate(2.2, (mean - 0.6) / 2.2))
            return 0.0
        return v * v * (3 - 2 * v)

    def _saccade(self, now_ns: int, dt: float) -> tuple[float, float]:
        t = now_ns / 1e9
        if self._next_saccade is None or t >= self._next_saccade:
            if self.rng.random() < 0.55:
                self._gaze_target = [0.0, 0.0]
            else:
                self._gaze_target = [self.rng.uniform(-0.6, 0.6), self.rng.uniform(-0.35, 0.25)]
            self._next_saccade = t + self.rng.uniform(1.2, 4.0)
        k = min(1.0, dt * 18.0)
        self._gaze[0] += (self._gaze_target[0] - self._gaze[0]) * k
        self._gaze[1] += (self._gaze_target[1] - self._gaze[1]) * k
        return self._gaze[0], self._gaze[1]

    def step(self, now_ns: int) -> tuple[RigParams, MouthState]:
        dt = 0.033 if self._last_ns is None else max(0.0, min(0.2, (now_ns - self._last_ns) / 1e9))
        self._last_ns = now_ns
        if self.frozen:
            return RigParams(), MouthState()
        m = self._mouth_at(now_ns)
        self._energy += ((m.open if m.speaking else 0.0) - self._energy) * min(1.0, dt * 4)
        blink = self._blink(now_ns, m.speaking)
        gx, gy = self._saccade(now_ns, dt)
        t = now_ns / 1e9
        s = [math.sin(2 * math.pi * f * t + ph) for f, ph in zip(self._freqs, self._phases)]
        roll = 1.1 * s[0] + 0.5 * s[1] + 0.6 * self._energy * s[2]
        tx = 0.025 * s[3] + 0.012 * s[4]
        ty = 0.018 * s[5] + 0.03 * self._energy
        smile = 0.0
        if self._expr is not None:
            eid, inten, start, dur = self._expr
            e = (now_ns - start) / dur
            if e >= 1.0:
                self._expr = None
            else:
                env = min(1.0, e * 5, (1 - e) * 5)
                if eid in ("smile", "happy"):
                    smile = inten * env
                elif eid == "neutral":
                    pass
        p = RigParams(jaw=m.open, round=m.round, spread=m.spread, smile=smile, blink_l=blink, blink_r=blink,
                      gaze_x=gx, gaze_y=gy, head_roll_deg=roll, head_tx=tx, head_ty=ty,
                      head_scale=0.006 * s[1])
        return p.clamp(), m
