"""Energy-based voice activity detection with an adaptive noise floor."""

from __future__ import annotations

import numpy as np


def rms_dbfs(pcm: np.ndarray) -> float:
    x = pcm.astype(np.float32) / 32768.0 if pcm.dtype == np.int16 else pcm.astype(np.float32)
    r = float(np.sqrt(np.mean(x * x) + 1e-12))
    return 20.0 * np.log10(r + 1e-9)


class EnergyVAD:
    """Speech if frame energy exceeds the noise floor by `margin_db`, with hangover.

    The noise floor uses minimum statistics: a low percentile of the frame
    levels over the last `window_s` seconds. Pauses between words keep it at
    the background level during continuous speech, while constant noise (fans,
    distant keyboards) is absorbed within a couple of seconds.
    """

    def __init__(self, frame_ms: int = 20, margin_db: float = 9.0, hangover_ms: int = 200,
                 min_speech_dbfs: float = -55.0, window_s: float = 2.5) -> None:
        from collections import deque

        self.margin_db = margin_db
        self.min_speech_dbfs = min_speech_dbfs
        self.hangover_frames = max(1, hangover_ms // frame_ms)
        self._levels = deque(maxlen=max(10, int(window_s * 1000 / frame_ms)))
        self.noise_db = -60.0
        self._hang = 0
        self.speaking = False

    def process(self, level_db: float) -> bool:
        self._levels.append(level_db)
        floor = float(np.percentile(self._levels, 10)) if len(self._levels) >= 5 else min(self.noise_db, level_db)
        self.noise_db = float(np.clip(floor, -90.0, -20.0))
        active = level_db > max(self.noise_db + self.margin_db, self.min_speech_dbfs)
        if active:
            self._hang = self.hangover_frames
        elif self._hang > 0:
            self._hang -= 1
        self.speaking = active or self._hang > 0
        return self.speaking
