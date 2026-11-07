"""Microphone capture (16 kHz mono int16). Nothing is recorded or stored.

Uses `sounddevice` (PortAudio, WASAPI shared mode on Windows) when installed.
Shared mode never takes exclusive control of the microphone, so the meeting app
keeps receiving the real microphone directly (spec §2.6, §6).
"""

from __future__ import annotations

import threading
import time
import wave
from pathlib import Path
from typing import Callable

import numpy as np

from avatar_engine.frame_ring import now_ns

Callback = Callable[[np.ndarray, int, int], None]  # pcm, sample_rate, monotonic_ts_ns


class AudioUnavailable(RuntimeError):
    pass


def list_input_devices() -> list[dict]:
    try:
        import sounddevice as sd
    except Exception:
        return []
    out = []
    try:
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) > 0:
                out.append({"id": str(i), "name": d["name"]})
    except Exception:
        return []
    return out


class MicrophoneCapture:
    def __init__(self, callback: Callback, device: str | None = None, sample_rate: int = 16000,
                 frame_ms: int = 20) -> None:
        try:
            import sounddevice as sd
        except Exception as exc:  # pragma: no cover - depends on host
            raise AudioUnavailable(f"microphone support unavailable: {exc}") from exc
        self._sd = sd
        self.callback = callback
        self.sample_rate = sample_rate
        self.blocksize = sample_rate * frame_ms // 1000
        dev = int(device) if device not in (None, "") and str(device).isdigit() else (device or None)
        self.stream = sd.InputStream(samplerate=sample_rate, channels=1, dtype="int16", blocksize=self.blocksize,
                                     device=dev, callback=self._on_audio, latency="low")

    def _on_audio(self, indata, frames, time_info, status) -> None:  # PortAudio thread: keep it short
        self.callback(indata[:, 0].copy(), self.sample_rate, now_ns())

    def start(self) -> None:
        self.stream.start()

    def stop(self) -> None:
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass


class WavFileCapture:
    """Plays a 16-bit WAV in real time (preview, tests, acceptance runs)."""

    def __init__(self, callback: Callback, path: str | Path, frame_ms: int = 20, loop: bool = True) -> None:
        with wave.open(str(path), "rb") as w:
            if w.getsampwidth() != 2:
                raise AudioUnavailable("WAV must be 16-bit PCM")
            sr, ch = w.getframerate(), w.getnchannels()
            data = np.frombuffer(w.readframes(w.getnframes()), np.int16)
        if ch > 1:
            data = data.reshape(-1, ch).mean(axis=1).astype(np.int16)
        if sr != 16000:
            idx = np.arange(0, len(data), sr / 16000.0)
            data = np.interp(idx, np.arange(len(data)), data).astype(np.int16)
        self.data, self.callback, self.loop = data, callback, loop
        self.block = 16000 * frame_ms // 1000
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="wav-capture", daemon=True)

    def _run(self) -> None:
        pos, period = 0, self.block / 16000.0
        nxt = time.perf_counter()
        while not self._stop.is_set():
            if pos + self.block > len(self.data):
                if not self.loop:
                    break
                pos = 0
            self.callback(self.data[pos:pos + self.block], 16000, now_ns())
            pos += self.block
            nxt += period
            time.sleep(max(0.0, nxt - time.perf_counter()))

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
