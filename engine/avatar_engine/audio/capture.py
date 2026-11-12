"""Microphone capture (16 kHz mono int16). Nothing is recorded or stored.

Uses `sounddevice` (PortAudio, WASAPI shared mode on Windows) when installed.
Shared mode never takes exclusive control of the microphone, so the meeting app
keeps receiving the real microphone directly (spec §2.6, §6).
"""

from __future__ import annotations

import sys
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


# Windows exposes every microphone once per host API. WASAPI (shared mode) is preferred: low latency
# and it never takes the device away from the meeting app. MME is the most compatible fallback.
HOSTAPI_PREFERENCE = ("Windows WASAPI", "MME", "Windows DirectSound", "Windows WDM-KS")
ALIAS_NAMES = ("microsoft sound mapper", "primary sound capture driver")


def _rank(hostapi_name: str) -> int:
    try:
        return HOSTAPI_PREFERENCE.index(hostapi_name)
    except ValueError:
        return len(HOSTAPI_PREFERENCE)


def _input_devices(sd) -> list[dict]:
    """All input devices as dicts with index, name, hostapi name, channels, default rate."""
    try:
        hostapis = [h["name"] for h in sd.query_hostapis()]
        devs = sd.query_devices()
    except Exception:
        return []
    out = []
    for i, d in enumerate(devs):
        if d.get("max_input_channels", 0) <= 0:
            continue
        name = str(d["name"]).strip()
        if any(a in name.lower() for a in ALIAS_NAMES):
            continue
        out.append({"index": i, "name": name, "hostapi": hostapis[d["hostapi"]] if d["hostapi"] < len(hostapis) else "",
                    "channels": int(d["max_input_channels"]), "rate": float(d.get("default_samplerate") or 16000)})
    return out


def _same_device(a: str, b: str) -> bool:
    """MME truncates names to 31 characters, so compare by prefix as well."""
    a, b = a.lower(), b.lower()
    return a == b or (min(len(a), len(b)) >= 24 and (a.startswith(b) or b.startswith(a)))


def list_input_devices(sd=None) -> list[dict]:
    """One entry per physical microphone; the id is its name (stable across reboots/replugging)."""
    if sd is None and sys.platform == "win32":
        # Same COM isolation as the camera enumeration (vision.capture._in_fresh_thread).
        from avatar_engine.vision.capture import _in_fresh_thread

        try:
            return _in_fresh_thread(lambda: _list_input_devices_impl(None), 5.0) or []
        except Exception:
            return []
    return _list_input_devices_impl(sd)


def _list_input_devices_impl(sd=None) -> list[dict]:
    if sd is None:
        try:
            import sounddevice as sd
        except Exception:
            return []
    best: list[dict] = []
    for d in sorted(_input_devices(sd), key=lambda d: (_rank(d["hostapi"]), -len(d["name"]))):
        if not any(_same_device(d["name"], b["name"]) for b in best):
            best.append(d)
    return [{"id": d["name"], "name": d["name"]} for d in best]


def resolve_input_candidates(sd, device) -> list[dict | None]:
    """Devices to try, best first. None means the system default device."""
    if device in (None, "", "default"):
        return [None]
    devs = _input_devices(sd)
    if str(device).isdigit():
        idx = int(device)
        return [d for d in devs if d["index"] == idx] or [None]
    want = str(device).strip()
    matches = [d for d in devs if _same_device(d["name"], want)]
    if not matches:
        matches = [d for d in devs if want.lower() in d["name"].lower()]
    if not matches:
        raise AudioUnavailable(f"microphone not found: {want!r}")
    return sorted(matches, key=lambda d: _rank(d["hostapi"]))


class _Resampler:
    """Streaming linear resampler to 16 kHz with a box pre-filter when downsampling (enough for
    the energy/spectral features that drive the mouth)."""

    def __init__(self, in_rate: float, out_rate: int) -> None:
        self.ratio = in_rate / out_rate
        self.k = max(1, int(round(self.ratio))) if self.ratio >= 1.5 else 1
        self.pos = 0.0
        self.tail = np.zeros(0, np.float32)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        if abs(self.ratio - 1.0) < 1e-6:
            return x.astype(np.int16)
        buf = np.concatenate([self.tail, x.astype(np.float32)])
        if self.k > 1:
            buf_f = np.convolve(buf, np.ones(self.k, np.float32) / self.k, mode="same")
        else:
            buf_f = buf
        n_out = int((len(buf) - 1 - self.pos) / self.ratio) + 1 if len(buf) > 1 else 0
        if n_out <= 0:
            self.tail = buf
            return np.zeros(0, np.int16)
        t = self.pos + np.arange(n_out) * self.ratio
        y = np.interp(t, np.arange(len(buf)), buf_f)
        consumed = int(np.floor(t[-1] + self.ratio))
        consumed = min(consumed, len(buf) - 1)
        self.pos = t[-1] + self.ratio - consumed
        self.tail = buf[consumed:]
        return np.clip(y, -32768, 32767).astype(np.int16)


class MicrophoneCapture:
    def __init__(self, callback: Callback, device: str | None = None, sample_rate: int = 16000,
                 frame_ms: int = 20, sd=None) -> None:
        if sd is None:
            try:
                import sounddevice as sd
            except Exception as exc:  # pragma: no cover - depends on host
                raise AudioUnavailable(f"microphone support unavailable: {exc}") from exc
        self._sd = sd
        self.callback = callback
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.stream = None
        self.device_info: dict | None = None
        errors = []
        for cand in resolve_input_candidates(sd, device):
            try:
                self._open(cand)
                self.device_info = cand
                break
            except Exception as exc:
                errors.append(f"{cand['hostapi'] if cand else 'default'}: {exc}")
        if self.stream is None:
            raise AudioUnavailable("cannot open microphone (" + "; ".join(errors) + ")")

    def _open(self, cand: dict | None) -> None:
        sd = self._sd
        if cand is None:
            info = sd.query_devices(kind="input")
            rate, channels, index = float(info.get("default_samplerate") or 16000), int(info["max_input_channels"]), None
        else:
            rate, channels, index = cand["rate"], cand["channels"], cand["index"]
        channels = max(1, min(2, channels))
        # WASAPI shared mode only accepts the device's own rate: capture there and resample ourselves.
        if cand is None or cand["hostapi"] != "Windows WASAPI":
            try:
                sd.check_input_settings(device=index, samplerate=self.sample_rate, channels=1, dtype="int16")
                rate, channels = float(self.sample_rate), 1
            except Exception:
                pass
        self._channels = channels
        self._resample = _Resampler(rate, self.sample_rate)
        self.stream = sd.InputStream(samplerate=rate, channels=channels, dtype="int16",
                                     blocksize=int(rate * self.frame_ms / 1000), device=index,
                                     callback=self._on_audio, latency="low")

    def _on_audio(self, indata, frames, time_info, status) -> None:  # PortAudio thread: keep it short
        x = indata[:, 0] if self._channels == 1 else indata.mean(axis=1)
        pcm = self._resample(x)
        if len(pcm):
            self.callback(pcm, self.sample_rate, now_ns())

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
