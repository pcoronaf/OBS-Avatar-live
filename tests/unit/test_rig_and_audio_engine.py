import numpy as np

from avatar_engine import config
from avatar_engine.engines.animator import Animator
from avatar_engine.engines.lite_cpu_audio import LiteCpuAudioEngine
from avatar_engine.engines.rig2d import PhotoRig, RigParams
from avatar_engine.frame_ring import now_ns


def test_rig_neutral_equals_base_and_animates(synthetic_face):
    img, lm, bbox = synthetic_face
    rig = PhotoRig(img, lm, bbox, 1280, 720)
    neutral = rig.render(RigParams())
    assert neutral.shape == (720, 1280, 4)
    assert np.array_equal(neutral[..., :3], rig.base)
    mouth = rig.render(RigParams(jaw=1.0))
    x0, y0, x1, y1 = rig.mouth_roi
    assert np.abs(mouth[y0:y1, x0:x1, :3].astype(int) - rig.base[y0:y1, x0:x1].astype(int)).mean() > 2
    # Only the head region changes; corners of the frame (background/body) stay still
    moved = rig.render(RigParams(head_roll_deg=3, head_tx=0.05))
    assert np.array_equal(moved[:20, :20], neutral[:20, :20])
    assert np.array_equal(moved[-20:, :20], neutral[-20:, :20])


def test_rig_blink_closes_eye_region(synthetic_face):
    img, lm, bbox = synthetic_face
    rig = PhotoRig(img, lm, bbox, 1280, 720)
    e = rig.eyes[0]
    x0, y0, x1, y1 = e["roi"]
    open_ = rig.render(RigParams())[y0:y1, x0:x1, :3].astype(float)
    closed = rig.render(RigParams(blink_l=1.0))[y0:y1, x0:x1, :3].astype(float)
    # eye white (bright) mostly replaced by skin
    assert (closed.mean(axis=2) > 235).sum() < 0.5 * (open_.mean(axis=2) > 235).sum()


def test_params_clamped():
    p = RigParams(jaw=5, head_roll_deg=40, head_tx=1).clamp()
    assert p.jaw == 1 and p.head_roll_deg == 4 and p.head_tx == 0.12


def test_background_modes(synthetic_face):
    img, lm, bbox = synthetic_face
    rig = PhotoRig(img, lm, bbox, 640, 360, background="transparent")
    out = rig.render(RigParams())
    assert out[..., 3].min() < 50 and out[..., 3].max() == 255


def test_blinks_are_aperiodic():
    a = Animator(seed=1)
    t, closures, prev = 0, [], 0.0
    starts = []
    while t < 60e9:
        p, _ = a.step(int(t))
        if p.blink_l > 0.5 and prev <= 0.5:
            starts.append(t / 1e9)
        prev = p.blink_l
        t += 33_333_333
    gaps = np.diff(starts)
    assert 8 <= len(starts) <= 35
    assert gaps.std() > 0.3  # not periodic


def test_frozen_animator_is_neutral():
    a = Animator(seed=2)
    a.frozen = True
    p, _ = a.step(5_000_000_000)
    assert p == RigParams()


def test_audio_engine_end_to_end(profile):
    cfg = config.load({"engine": "lite_cpu_audio", "drive_mode": "audio", "profile_id": profile.profile_id,
                       "output": {"width": 640, "height": 360}, "audio": {"mouth_delay_ms": 0}})
    eng = LiteCpuAudioEngine()
    eng.initialize(cfg, {})
    eng.load_profile(profile)
    eng.start_session("s1")
    t = np.arange(16000) / 16000
    pcm = (np.sin(2 * np.pi * 200 * t) * 9000 * (t > 0.3)).astype(np.int16)
    ts = now_ns()
    for i in range(0, len(pcm), 320):
        eng.push_audio(pcm[i:i + 320], 16000, ts + i * 62_500)
    f = eng.poll_frame(ts + len(pcm) * 62_500)
    assert f.bgra.shape == (360, 640, 4)
    assert f.extra["mouth"]["open"] > 0.3
    m = eng.get_metrics()
    assert m["engine"] == "lite_cpu_audio" and m["audio_frames"] == 50


def test_audio_input_none_never_opens_microphone(profile, monkeypatch):
    import avatar_engine.audio.capture as capture
    from avatar_engine.session import Session

    def boom(*a, **k):
        raise AssertionError("microphone must not be opened")

    monkeypatch.setattr(capture, "MicrophoneCapture", boom)
    s = Session({"engine": "lite_cpu_audio", "drive_mode": "audio", "profile_id": profile.profile_id,
                 "audio_input": "none", "output": {"width": 640, "height": 360}}, None, lambda n, p: None)
    s.start()
    try:
        assert s.inputs == []
        m = s.metrics()
        assert m["audio_disabled"] is True and m["audio_stalled"] is False
        f = s.engine.poll_frame(now_ns())
        assert f.extra["mouth"]["open"] == 0.0
    finally:
        s.stop()
