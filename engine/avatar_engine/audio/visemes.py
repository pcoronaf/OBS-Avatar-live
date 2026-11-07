"""Low-cost audio → mouth-shape estimator (spec §5 E1).

This is a *heuristic* estimator, not phoneme recognition:
* jaw opening follows speech energy above the noise floor (the RMS fallback);
* lip rounding (O/U) vs spreading (E/I) is inferred from the spectral
  centroid / low-band ratio of voiced frames;
* fricatives (S/F) are inferred from zero-crossing rate + high-band energy and
  keep the mouth narrowly open;
* bilabial closures (M/B/P) are approximated by short energy dips inside speech.
It can be replaced by an ONNX viseme model behind the same `MouthState` output
once a model has been benchmarked (see `VisemeEstimator.model`).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from avatar_engine.audio.vad import EnergyVAD, rms_dbfs

VISEMES = ("SIL", "AA", "EE", "OO", "MBP", "FV", "SS")


@dataclass
class MouthState:
    open: float = 0.0  # jaw opening 0..1
    round: float = 0.0  # lip rounding 0..1
    spread: float = 0.0  # lip spreading 0..1
    viseme: str = "SIL"
    speaking: bool = False
    level_db: float = -90.0
    timestamp_ns: int = 0

    def as_dict(self) -> dict:
        return {"open": round(self.open, 3), "round": round(self.round, 3), "spread": round(self.spread, 3),
                "viseme": self.viseme, "speaking": bool(self.speaking), "level_db": round(float(self.level_db), 1)}


class _Smoother:
    def __init__(self, attack: float, release: float) -> None:
        self.attack, self.release, self.v = attack, release, 0.0

    def __call__(self, target: float) -> float:
        a = self.attack if target > self.v else self.release
        self.v += (target - self.v) * a
        return self.v


class VisemeEstimator:
    model = "heuristic-spectral-v1"

    def __init__(self, sample_rate: int = 16000, frame_ms: int = 20, sensitivity: float = 1.0,
                 vad_enabled: bool = True) -> None:
        if sample_rate != 16000:
            raise ValueError("audio must be 16 kHz mono")
        self.sr = sample_rate
        self.frame = sample_rate * frame_ms // 1000
        self.sensitivity = sensitivity
        self.vad_enabled = vad_enabled
        self.vad = EnergyVAD(frame_ms=frame_ms)
        self._buf = np.zeros(0, np.int16)
        self._window = np.hanning(self.frame).astype(np.float32)
        freqs = np.fft.rfftfreq(self.frame, 1.0 / sample_rate)
        self._freqs = freqs
        self._low = (freqs >= 150) & (freqs < 900)
        self._mid = (freqs >= 900) & (freqs < 2500)
        self._high = (freqs >= 3500) & (freqs < 7500)
        self._open = _Smoother(0.55, 0.22)
        self._round = _Smoother(0.25, 0.12)
        self._spread = _Smoother(0.25, 0.12)
        self._prev_db = -90.0
        self.state = MouthState()

    def push(self, pcm: np.ndarray, timestamp_ns: int = 0) -> list[MouthState]:
        """Feed int16 mono PCM of any length; returns one state per complete frame."""
        pcm = np.asarray(pcm)
        if pcm.dtype != np.int16:
            pcm = np.clip(pcm * (32767.0 if pcm.dtype.kind == "f" else 1), -32768, 32767).astype(np.int16)
        self._buf = np.concatenate([self._buf, pcm.reshape(-1)])
        out = []
        n = 0
        while len(self._buf) - n >= self.frame:
            out.append(self._process(self._buf[n:n + self.frame], timestamp_ns))
            n += self.frame
        self._buf = self._buf[n:]
        return out

    def _process(self, frame: np.ndarray, ts: int) -> MouthState:
        x = frame.astype(np.float32) / 32768.0
        level = rms_dbfs(frame)
        speaking = self.vad.process(level) if self.vad_enabled else level > -50
        over = max(0.0, level - max(self.vad.noise_db + 6.0, -60.0))
        # ~30 dB above the floor ≈ fully open; sensitivity scales the gain
        target_open = float(np.clip(over / 30.0 * self.sensitivity, 0.0, 1.0)) if speaking else 0.0
        spec = np.abs(np.fft.rfft(x * self._window)) ** 2 + 1e-12
        total = spec.sum()
        low, mid, high = spec[self._low].sum() / total, spec[self._mid].sum() / total, spec[self._high].sum() / total
        centroid = float((spec * self._freqs).sum() / total)
        zcr = float(np.mean(np.abs(np.diff(np.signbit(x).astype(np.int8)))))
        viseme = "SIL"
        tr = ts_ = 0.0
        if speaking and target_open > 0.02:
            if zcr > 0.25 and high > 0.25:
                viseme = "SS" if centroid > 4000 else "FV"
                target_open = min(target_open, 0.18)
                ts_ = 0.4
            elif self._prev_db - level > 9 and level < self.vad.noise_db + 14:
                viseme = "MBP"  # sharp dip inside speech ≈ lip closure
                target_open = 0.0
            elif low > 0.6 and centroid < 900:
                viseme, tr = "OO", float(np.clip((0.75 - centroid / 1200), 0, 1))
            elif mid > 0.35 and centroid > 1500:
                viseme, ts_ = "EE", float(np.clip((centroid - 1500) / 1500, 0, 1))
            else:
                viseme = "AA"
        self._prev_db = level
        st = MouthState(
            open=self._open(target_open),
            round=self._round(tr),
            spread=self._spread(ts_),
            viseme=viseme,
            speaking=bool(speaking),
            level_db=float(level),
            timestamp_ns=ts,
        )
        self.state = st
        return st
