"""Performance smoke tests. Thresholds are generous sanity bounds for CI machines,
NOT acceptance criteria — those are measured on the reference Windows machine
(docs/ACCEPTANCE_TESTS.md). Run with `pytest tests/performance -s` to print numbers."""

import gc
import os
import time

import numpy as np
import pytest

from avatar_engine import config
from avatar_engine.engines.lite_cpu_audio import LiteCpuAudioEngine
from avatar_engine.engines.rig2d import PhotoRig, RigParams
from avatar_engine.metrics import ProcessStats, percentile
from avatar_engine.scheduler import OutputScheduler

SOAK_S = float(os.environ.get("AVATAR_SOAK_SECONDS", "8"))


def test_rig_render_budget_720p(synthetic_face):
    img, lm, bbox = synthetic_face
    rig = PhotoRig(img, lm, bbox, 1280, 720)
    rng = np.random.default_rng(0)
    times = []
    for i in range(120):
        p = RigParams(jaw=rng.random(), round=rng.random() * 0.5, blink_l=rng.random(), blink_r=rng.random(),
                      head_roll_deg=rng.uniform(-2, 2), head_tx=rng.uniform(-0.03, 0.03), gaze_x=rng.uniform(-1, 1))
        t = time.perf_counter()
        rig.render(p)
        times.append((time.perf_counter() - t) * 1000)
    p50, p95 = percentile(times, 50), percentile(times, 95)
    print(f"\nrig 720p render: P50 {p50:.2f} ms, P95 {p95:.2f} ms (budget 33.3 ms at 30 fps)")
    assert p95 < 33.3


def test_audio_engine_soak_memory_and_rate(profile):
    """Short soak of the full output clock; the 60-minute version is AT-B-3 (set AVATAR_SOAK_SECONDS=3600)."""
    cfg = config.load({"engine": "lite_cpu_audio", "drive_mode": "audio", "profile_id": profile.profile_id})
    eng = LiteCpuAudioEngine()
    eng.initialize(cfg, {})
    eng.load_profile(profile)
    eng.start_session("soak")
    sched = OutputScheduler(eng, None, cfg)
    stats = ProcessStats()
    t = np.arange(320) / 16000
    chunk = (np.sin(2 * np.pi * 200 * t) * 8000).astype(np.int16)
    sched.start()
    try:
        time.sleep(1.0)
        gc.collect()
        ram0 = stats.ram_mb()
        end = time.time() + SOAK_S
        from avatar_engine.frame_ring import now_ns

        while time.time() < end:
            eng.push_audio(chunk, 16000, now_ns())
            time.sleep(0.02)
        fps = sched.output_fps.fps()
        gc.collect()
        growth = stats.ram_mb() - ram0
    finally:
        sched.stop()
    print(f"\nsoak {SOAK_S:.0f}s: output {fps:.1f} fps, late ticks {sched.late_ticks}, RAM growth {growth:.1f} MB")
    assert fps > 24
    assert growth < 50
