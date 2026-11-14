"""Compositor + fixed-rate output clock (ADR-003, spec §7 queue policies).

Output runs at `output.fps` regardless of inference cadence. Each tick takes the
engine's newest result; if it is the same as last tick the frame is marked
*repeated*. If the newest result is older than `stale_frame_ms` the face is
replaced with a neutral status frame — a frozen face is never shown as current.
The disclosure label is burned into every frame here, unconditionally.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

import numpy as np

from avatar_engine import frame_ring as fr
from avatar_engine.engines.base import EngineFrame, IAvatarEngine
from avatar_engine.metrics import FpsCounter, RollingStats
from avatar_engine.overlay import DisclosureLabel, status_frame


class Compositor:
    def __init__(self, width: int, height: int, privacy: dict) -> None:
        self.width, self.height = width, height
        self.label = DisclosureLabel(privacy.get("disclosure_text") or "Avatar generado por IA",
                                     privacy.get("disclosure_position", "bottom_left"),
                                     privacy.get("disclosure_scale", 1.0))
        self._status_cache: dict[tuple[str, str], np.ndarray] = {}

    def status(self, title: str, detail: str = "") -> np.ndarray:
        key = (title, detail)
        if key not in self._status_cache:
            if len(self._status_cache) > 16:
                self._status_cache.clear()
            self._status_cache[key] = status_frame(self.width, self.height, title, detail)
        return self._status_cache[key].copy()

    def slow_banner(self, bgra: np.ndarray, rate: float) -> np.ndarray:
        """Last face, dimmed, with a visible banner: it is never passed off as a current image."""
        import cv2

        img = (bgra.astype(np.uint16) * 3 // 5).astype(np.uint8)
        h, w = img.shape[:2]
        s = max(h / 720.0, 0.35)
        bh = int(56 * s)
        img[:bh, :, :3] = (30, 30, 150)
        img[:bh, :, 3] = 255
        txt = "Actualizacion lenta: imagen no actual" + (f" ({rate:.1f} cuadros/s)" if rate > 0 else "")
        fs, th = 0.8 * s, max(1, int(2 * s))
        (tw, tht), _ = cv2.getTextSize(txt, cv2.FONT_HERSHEY_SIMPLEX, fs, th)
        cv2.putText(img, txt, ((w - tw) // 2, (bh + tht) // 2), cv2.FONT_HERSHEY_SIMPLEX, fs, (255, 255, 255, 255),
                    th, cv2.LINE_AA)
        return img

    def finalize(self, bgra: np.ndarray) -> np.ndarray:
        if bgra.shape[0] != self.height or bgra.shape[1] != self.width:
            import cv2

            bgra = cv2.resize(bgra, (self.width, self.height), interpolation=cv2.INTER_LINEAR)
        elif not bgra.flags.writeable:
            bgra = bgra.copy()
        return self.label.apply(bgra)


class OutputScheduler:
    def __init__(self, engine: IAvatarEngine, writer: fr.FrameRingWriter | None, config: dict,
                 on_frame: Callable[[np.ndarray, int], None] | None = None,
                 clock: Callable[[], int] = fr.now_ns) -> None:
        out = config["output"]
        self.engine = engine
        self.writer = writer
        self.on_frame = on_frame
        self.fps = out["fps"]
        self.period_ns = int(1e9 / self.fps)
        self.stale_ns = int(config.get("stale_frame_ms", 500) * 1e6)
        # Between stale_frame_ms and stale_max_ms the last face is shown dimmed with a visible
        # "slow update" banner; after stale_max_ms it is replaced by a neutral status screen.
        self.stale_max_ns = max(self.stale_ns, int(config.get("stale_max_ms", 10000) * 1e6))
        self._seq_times: list[int] = []
        self.compositor = Compositor(out["width"], out["height"], config.get("privacy", {}))
        self.clock = clock
        self.output_fps = FpsCounter()
        self.capture_to_output = RollingStats(10.0)
        self.frames_out = 0
        self.frames_repeated = 0
        self.frames_stale = 0
        self.late_ticks = 0
        self._recent: list[bool] = []
        self._last_seq = -1
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.state_detail = ""
        self.hint_fn: Callable[[], str] | None = None  # why there is no avatar frame yet
        self.forced_status: tuple[str, str] | None = None

    # -- one tick (unit-testable without a thread) -------------------------------------------
    def tick(self) -> tuple[np.ndarray, int]:
        now = self.clock()
        flags = fr.FLAG_SYNTHETIC
        latency_us = 0
        if self.forced_status is not None:
            img = self.compositor.status(*self.forced_status)
            flags |= fr.FLAG_DEGRADED
        else:
            ef: EngineFrame | None = None
            try:
                ef = self.engine.poll_frame(now)
            except Exception as exc:  # engine bug must not kill the clock
                self.state_detail = f"engine error: {exc}"
            if ef is None:
                hint = self.state_detail
                if not hint and self.hint_fn is not None:
                    try:
                        hint = self.hint_fn()
                    except Exception:
                        hint = ""
                img = self.compositor.status("Avatar Live", hint or "Iniciando motor...")
                flags |= fr.FLAG_DEGRADED
            elif now - ef.created_ts_ns > self.stale_max_ns:
                img = self.compositor.status("Avatar Live: señal degradada",
                                             "El avatar no se está actualizando")
                flags |= fr.FLAG_DEGRADED
                self.frames_stale += 1
            elif now - ef.created_ts_ns > self.stale_ns:
                self._note_seq(ef.sequence, ef.created_ts_ns)
                img = self.compositor.slow_banner(ef.bgra, self.unique_rate())
                flags |= fr.FLAG_DEGRADED | fr.FLAG_REPEATED
                self.frames_stale += 1
                self.frames_repeated += 1
            else:
                self._note_seq(ef.sequence, ef.created_ts_ns)
                img = ef.bgra if ef.owned else ef.bgra.copy()  # reused frames must stay clean
                repeated = ef.sequence == self._last_seq
                self._last_seq = ef.sequence
                if repeated:
                    flags |= fr.FLAG_REPEATED
                    self.frames_repeated += 1
                self._recent.append(repeated)
                if len(self._recent) > self.fps * 10:
                    self._recent.pop(0)
                if ef.degraded:
                    flags |= fr.FLAG_DEGRADED
                if ef.paused:
                    flags |= fr.FLAG_PAUSED
                if ef.capture_ts_ns:
                    lat_ms = (now - ef.capture_ts_ns) / 1e6
                    self.capture_to_output.add(lat_ms)
                    latency_us = int(lat_ms * 1000)
        img = self.compositor.finalize(img)
        if self.writer is not None:
            self.writer.write(img, flags=flags, timestamp_ns=now, processing_latency_us=latency_us)
        if self.on_frame is not None:
            self.on_frame(img, flags)
        self.frames_out += 1
        self.output_fps.tick()
        return img, flags

    def _note_seq(self, seq: int, created_ns: int) -> None:
        if not self._seq_times or self._seq_times[-1] != created_ns:
            self._seq_times.append(created_ns)
            del self._seq_times[:-6]

    def unique_rate(self) -> float:
        t = self._seq_times
        if len(t) < 2 or t[-1] <= t[0]:
            return 0.0
        return (len(t) - 1) / ((t[-1] - t[0]) / 1e9)

    def repeated_pct(self) -> float:
        return 100.0 * sum(self._recent) / len(self._recent) if self._recent else 0.0

    # -- clock thread -----------------------------------------------------------------------
    def _run(self) -> None:
        nxt = self.clock()
        while not self._stop.is_set():
            self.tick()
            nxt += self.period_ns
            now = self.clock()
            if now > nxt + self.period_ns:  # fell behind: skip, never queue (drop-oldest policy)
                self.late_ticks += 1
                nxt = now
            wait = (nxt - now) / 1e9
            if wait > 0:
                self._stop.wait(wait)

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="output-clock", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def metrics(self) -> dict:
        return {
            "output_fps": round(self.output_fps.fps(), 2),
            "repeated_frame_pct": round(self.repeated_pct(), 1),
            "capture_to_output_p50_ms": _r(self.capture_to_output.p(50)),
            "capture_to_output_p95_ms": _r(self.capture_to_output.p(95)),
            "stale_frames": self.frames_stale,
            "late_ticks": self.late_ticks,
            "frames_out": self.frames_out,
        }


def _r(v):
    return None if v is None else round(v, 1)


def sleep_until_ns(target: int) -> None:
    d = (target - fr.now_ns()) / 1e9
    if d > 0:
        time.sleep(d)
