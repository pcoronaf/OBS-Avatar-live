import numpy as np

from avatar_engine import config, frame_ring as fr
from avatar_engine.engines.base import EngineFrame, IAvatarEngine
from avatar_engine.scheduler import OutputScheduler


class StubEngine(IAvatarEngine):
    name = "stub"

    def __init__(self):
        super().__init__()
        self.frame = None

    def initialize(self, c, caps):
        pass

    def load_profile(self, p):
        return "x"

    def poll_frame(self, now):
        return self.frame

    def get_metrics(self):
        return {}


def cfg():
    return config.load({"engine": "lite_cpu_audio", "drive_mode": "audio", "profile_id": "p",
                        "output": {"width": 320, "height": 180, "fps": 30}})


def make(clock):
    e = StubEngine()
    return e, OutputScheduler(e, None, cfg(), clock=lambda: clock[0])


def face():
    img = np.zeros((180, 320, 4), np.uint8)
    img[..., 1] = 200  # green "face"
    return img


def test_reused_frames_marked_repeated():
    clock = [1_000_000_000]
    e, s = make(clock)
    e.frame = EngineFrame(face(), 1, clock[0] - 50_000_000, clock[0], 1)
    _, f1 = s.tick()
    clock[0] += 33_000_000
    _, f2 = s.tick()
    assert not f1 & fr.FLAG_REPEATED and f2 & fr.FLAG_REPEATED
    assert s.repeated_pct() == 50.0
    assert s.capture_to_output.p(95) >= 50


def test_stale_face_is_never_shown():
    clock = [1_000_000_000]
    e, s = make(clock)
    e.frame = EngineFrame(face(), 1, 0, clock[0], 1)
    clock[0] += 11_000_000_000  # > stale_max_ms (10 s)
    img, flags = s.tick()
    assert flags & fr.FLAG_DEGRADED
    assert img[90, 160, 1] < 100  # no face pixels in the middle
    assert s.frames_stale == 1


def test_label_burned_into_every_frame():
    clock = [1_000_000_000]
    e, s = make(clock)
    e.frame = EngineFrame(np.full((180, 320, 4), 255, np.uint8), 1, 0, clock[0], 1)
    img, _ = s.tick()
    # bottom-left label box darkens a white frame
    assert img[150:175, 10:120, :3].mean() < 200


def test_engine_exception_does_not_stop_clock():
    clock = [1]
    e, s = make(clock)

    def boom(now):
        raise RuntimeError("x")

    e.poll_frame = boom
    img, flags = s.tick()
    assert flags & fr.FLAG_DEGRADED and "engine error" in s.state_detail


def test_fps_counter():
    from avatar_engine.metrics import FpsCounter

    c = FpsCounter(window_s=3.0)
    for i in range(46):
        c.tick(i / 30)
    assert abs(c.fps(now=45 / 30) - 30.0) < 0.01
    assert c.fps(now=45 / 30 + 2.0) == 0.0  # stalled producer


def test_slow_updates_show_dimmed_face_with_banner_then_status():
    clock = [1_000_000_000]
    e, s = make(clock)
    e.frame = EngineFrame(face(), 1, 0, clock[0], 1)
    clock[0] += 2_000_000_000  # 2 s old: slow, not dead
    img, flags = s.tick()
    assert flags & fr.FLAG_DEGRADED and flags & fr.FLAG_REPEATED
    assert 60 < img[90, 160, 1] < 200  # the face is still there, dimmed (200 * 3/5 = 120)
    assert img[2:15, :, 2].mean() > 100  # red banner on top
    clock[0] += 9_000_000_000  # 11 s old: neutral screen
    img, _ = s.tick()
    assert img[90, 160, 1] < 100


def test_fps_counter_slow_producer():
    from avatar_engine.metrics import FpsCounter

    c = FpsCounter(window_s=20.0)
    for i in range(4):
        c.tick(i * 5.0)  # one face every 5 s
    assert abs(c.fps(now=16.0) - 0.2) < 1e-6
    assert c.fps(now=40.0) == 0.0
