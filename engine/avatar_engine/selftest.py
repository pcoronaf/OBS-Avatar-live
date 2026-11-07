"""Offline self-test used by the installer build (`avatar-engine selftest`)."""

from __future__ import annotations

import secrets
import tempfile
import time
import traceback
from pathlib import Path


def synthetic_face(width: int = 640, height: int = 800):
    """A drawn face (no real person) for tests and the self-test."""
    import cv2
    import numpy as np

    img = np.full((height, width, 3), (90, 120, 80), np.uint8)
    cx, cy = width // 2, int(height * 0.47)
    cv2.ellipse(img, (cx, cy), (int(width * 0.24), int(height * 0.27)), 0, 0, 360, (140, 170, 215), -1)
    for dx in (-0.09, 0.09):
        ex = int(cx + dx * width)
        cv2.ellipse(img, (ex, cy - 40), (30, 14), 0, 0, 360, (250, 250, 250), -1)
        cv2.circle(img, (ex, cy - 40), 10, (60, 40, 20), -1)
    cv2.ellipse(img, (cx, cy + 110), (50, 11), 0, 0, 360, (90, 90, 190), -1)
    lm = np.array([[cx - 0.09 * width, cy - 40], [cx + 0.09 * width, cy - 40], [cx, cy + 40],
                   [cx - 50, cy + 110], [cx + 50, cy + 110]], np.float32)
    bbox = np.array([cx - 0.24 * width, cy - 0.27 * height, 0.48 * width, 0.54 * height], np.float32)
    return img, lm, bbox


def run() -> tuple[bool, dict]:
    report: dict = {}
    ok = True

    def step(name, fn):
        nonlocal ok
        t0 = time.perf_counter()
        try:
            fn()
            report[name] = f"ok ({(time.perf_counter() - t0) * 1000:.0f} ms)"
        except Exception as exc:
            ok = False
            report[name] = f"FAIL: {exc}\n{traceback.format_exc(limit=3)}"

    def t_config():
        from avatar_engine import config

        config.load({"engine": "lite_cpu_audio", "drive_mode": "audio", "profile_id": "x"})

    def t_ring():
        import numpy as np

        from avatar_engine.frame_ring import FrameRingReader, FrameRingWriter

        s = "selftest" + secrets.token_hex(4)
        w = FrameRingWriter(s, 320, 240)
        r = FrameRingReader(s)
        try:
            w.write(np.full((240, 320, 4), 9, np.uint8))
            f = r.read_latest()
            assert f is not None and int(f.pixels[0, 0, 0]) == 9
        finally:
            r.close()
            w.close()

    def t_onnxruntime():
        import onnxruntime as ort

        assert "CPUExecutionProvider" in ort.get_available_providers()

    def t_rig():
        from avatar_engine.engines.rig2d import PhotoRig, RigParams

        img, lm, bbox = synthetic_face()
        rig = PhotoRig(img, lm, bbox, 1280, 720)
        out = rig.render(RigParams(jaw=0.7, blink_l=1, blink_r=1, head_roll_deg=1))
        assert out.shape == (720, 1280, 4)

    def t_visemes():
        import numpy as np

        from avatar_engine.audio.visemes import VisemeEstimator

        v = VisemeEstimator()
        t = np.arange(16000) / 16000
        loud = (np.sin(2 * np.pi * 220 * t) * 8000 * (t > 0.5)).astype(np.int16)
        states = v.push(loud)
        assert max(s.open for s in states[-10:]) > 0.2

    def t_haar():
        import cv2

        assert not cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml").empty()

    for name, fn in (("config", t_config), ("frame_ring", t_ring), ("onnxruntime_cpu", t_onnxruntime),
                     ("rig2d", t_rig), ("visemes", t_visemes), ("opencv_haar", t_haar)):
        step(name, fn)
    return ok, report
