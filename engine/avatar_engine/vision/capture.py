"""Webcam capture on its own thread with a drop-oldest queue (spec §7)."""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections import deque
from typing import Callable

import cv2
import numpy as np

from avatar_engine.frame_ring import now_ns


class CameraUnavailable(RuntimeError):
    pass


VIRTUAL_CAMERA_NAMES = ("obs virtual camera", "obs-camera", "obs camera")


def _is_own_output(name: str) -> bool:
    """Never offer OBS's own virtual camera as input: it would feed the avatar back into itself."""
    n = (name or "").lower()
    return any(v in n for v in VIRTUAL_CAMERA_NAMES)


log = logging.getLogger("avatar_engine.devices")


def _in_fresh_thread(fn, timeout: float):
    """Run `fn` in a new thread. Windows camera enumeration initialises COM per thread; a thread
    whose COM state was set differently by another library (e.g. PortAudio) makes it fail silently."""
    box: dict = {}

    def run():
        try:
            box["value"] = fn()
        except Exception as exc:  # reported to the caller
            box["error"] = exc

    t = threading.Thread(target=run, name="device-enum", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"device enumeration took longer than {timeout:.0f} s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def list_video_devices(max_index: int = 4, report: list | None = None) -> list[dict]:
    """Cameras with their real names. Ids carry the capture backend ("msmf:0", "dshow:1") because
    Media Foundation and DirectShow number devices differently. `report` collects diagnostics."""
    report = report if report is not None else []
    out: list[dict] = []
    try:
        from cv2_enumerate_cameras import enumerate_cameras
    except Exception as exc:
        enumerate_cameras = None
        report.append(f"cv2_enumerate_cameras unavailable: {exc}")
    if enumerate_cameras is not None:
        backends = [(cv2.CAP_MSMF, "msmf"), (cv2.CAP_DSHOW, "dshow")] if sys.platform == "win32" else \
            [(cv2.CAP_ANY, "any")]
        seen = set()
        for api, tag in backends:
            try:
                cams = _in_fresh_thread(lambda api=api: enumerate_cameras(api), 5.0) or []
            except Exception as exc:
                report.append(f"{tag}: enumeration failed: {exc}")
                continue
            report.append(f"{tag}: {len(cams)} device(s): " + ", ".join(c.name for c in cams))
            for c in cams:
                key = (c.name, getattr(c, "path", None))
                if _is_own_output(c.name) or key in seen:
                    continue
                seen.add(key)
                out.append({"id": f"{tag}:{c.index}" if tag != "any" else str(c.index), "name": c.name})
            if out:
                break  # prefer the first backend that reports cameras (MSMF, OpenCV's default)
    if not out:
        # Fallback: open indexes one by one (no names). Bounded so the plugin's refresh does not time out;
        # a camera already in use by another app (or an OBS "Video Capture Device") may not open here.
        backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
        deadline = time.monotonic() + 3.0
        for i in range(max_index):
            if time.monotonic() > deadline:
                report.append("probe: stopped after 3 s")
                break
            cap = cv2.VideoCapture(i, backend)
            ok = cap.isOpened()
            cap.release()
            report.append(f"probe index {i}: {'opened' if ok else 'not available'}")
            if ok:
                out.append({"id": f"dshow:{i}" if sys.platform == "win32" else str(i), "name": f"Camera {i}"})
    log.info("video devices: %s | %s", [d["name"] for d in out], "; ".join(report))
    return out


def parse_device(dev) -> tuple[int | str, int | None]:
    """'msmf:0' -> (0, CAP_MSMF); 'dshow:1' -> (1, CAP_DSHOW); '2' -> (2, None); other strings = path/URL."""
    if dev in (None, ""):
        return 0, None
    if isinstance(dev, int):
        return dev, None
    s_ = str(dev)
    for tag, api in (("msmf:", cv2.CAP_MSMF), ("dshow:", cv2.CAP_DSHOW)):
        if s_.startswith(tag) and s_[len(tag):].isdigit():
            return int(s_[len(tag):]), api
    if s_.isdigit():
        return int(s_), None
    if not (os.path.exists(s_) or "://" in s_):  # a camera *name* (older saved settings): look it up
        for d in list_video_devices():
            if d["name"].lower() == s_.lower():
                return parse_device(d["id"])
    return s_, None


class LatestFrameQueue:
    """Bounded queue; when full the oldest frame is dropped and counted."""

    def __init__(self, maxlen: int = 2) -> None:
        self._d: deque[tuple[np.ndarray, int]] = deque(maxlen=maxlen)
        self._cv = threading.Condition()
        self.dropped = 0

    def put(self, frame: np.ndarray, ts: int) -> None:
        with self._cv:
            if len(self._d) == self._d.maxlen:
                self.dropped += 1
            self._d.append((frame, ts))
            self._cv.notify()

    def get_latest(self, timeout: float = 0.1) -> tuple[np.ndarray, int] | None:
        """Returns the newest frame and discards older ones (latency over completeness)."""
        with self._cv:
            if not self._d and not self._cv.wait(timeout):
                return None
            if not self._d:
                return None
            self.dropped += len(self._d) - 1
            item = self._d[-1]
            self._d.clear()
            return item

    def clear(self) -> None:
        with self._cv:
            self._d.clear()


class CameraCapture:
    def __init__(self, on_frame: Callable[[np.ndarray, int], None], device: str | int | None = 0,
                 width: int = 640, height: int = 480, fps: int = 30) -> None:
        self.on_frame = on_frame
        self.device = device
        self.width, self.height, self.fps = width, height, fps
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.cap = None
        self.frames = 0
        self.last_frame_ns = 0
        self.error: str | None = None

    def _open(self):
        if os.environ.get("AVATAR_LIVE_FAULT") == "device":
            raise CameraUnavailable("fault injection: camera device lost")
        dev, api = parse_device(self.device)
        if isinstance(dev, int) and api is not None:
            cap = cv2.VideoCapture(dev, api)
        elif isinstance(dev, int) and sys.platform == "win32":
            cap = cv2.VideoCapture(dev, cv2.CAP_MSMF)
            if not cap.isOpened():
                cap = cv2.VideoCapture(dev, cv2.CAP_DSHOW)
        else:
            cap = cv2.VideoCapture(dev)
        if not cap.isOpened():
            raise CameraUnavailable(f"cannot open camera {self.device!r} (in use by another app or OBS source?)")
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    def _run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            try:
                if self.cap is None:
                    self.cap = self._open()
                    self.error = None
                ok, frame = self.cap.read()
                if not ok or frame is None:
                    raise CameraUnavailable("camera read failed")
                failures = 0
                if frame.shape[1] != self.width or frame.shape[0] != self.height:
                    frame = cv2.resize(frame, (self.width, self.height), interpolation=cv2.INTER_AREA)
                ts = now_ns()
                self.frames += 1
                self.last_frame_ns = ts
                self.on_frame(frame, ts)
            except CameraUnavailable as exc:
                self.error = str(exc)
                if self.cap is not None:
                    self.cap.release()
                    self.cap = None
                failures += 1
                self._stop.wait(min(2.0, 0.2 * failures))  # retry with backoff (device unplugged/replugged)

    def set_resolution(self, width: int, height: int) -> None:
        self.width, self.height = width, height
        if self.cap is not None:
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="camera", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        if self.cap is not None:
            self.cap.release()
            self.cap = None


class SyntheticVideoSource:
    """Feeds frames from a video file or a list of images in real time (tests, benchmark, acceptance)."""

    def __init__(self, on_frame: Callable[[np.ndarray, int], None], frames: list[np.ndarray] | str,
                 fps: float = 30.0) -> None:
        if isinstance(frames, str):
            cap = cv2.VideoCapture(frames)
            frames = []
            while True:
                ok, f = cap.read()
                if not ok:
                    break
                frames.append(f)
            cap.release()
        if not frames:
            raise CameraUnavailable("no frames")
        self.frames_list = frames
        self.on_frame, self.fps = on_frame, fps
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="synthetic-video", daemon=True)
        self.error = None
        self.last_frame_ns = 0

    def _run(self) -> None:
        i, nxt = 0, time.perf_counter()
        while not self._stop.is_set():
            ts = now_ns()
            self.last_frame_ns = ts
            self.on_frame(self.frames_list[i % len(self.frames_list)], ts)
            i += 1
            nxt += 1.0 / self.fps
            self._stop.wait(max(0.0, nxt - time.perf_counter()))

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
