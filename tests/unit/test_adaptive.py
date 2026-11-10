from avatar_engine.performance.adaptive_controller import AdaptiveController, QualitySettings, WindowSample, ladder
from avatar_engine.performance.startup_benchmark import BenchmarkResult, select

BAD = WindowSample(unique_fps=2.0, p95_ms=400, cpu_pct=90)
GOOD = WindowSample(unique_fps=20.0, p95_ms=80, cpu_pct=20)
OK = WindowSample(unique_fps=6.0, p95_ms=200, cpu_pct=50)


def test_ladder_order():
    lv = ladder(QualitySettings(enhancement=True, detector_size=640, detection_every_n_frames=6,
                                capture_width=1280, capture_height=720))
    assert lv[1].enhancement is False
    assert lv[2].detector_size == 320
    assert lv[3].detection_every_n_frames >= 10
    assert (lv[4].capture_width, lv[4].capture_height) == (640, 360)
    assert lv[5].max_inference_fps == 8.0


def test_hysteresis_requires_three_windows():
    c = AdaptiveController(QualitySettings())
    assert c.observe(BAD).action == "none"
    assert c.observe(BAD).action == "none"
    d = c.observe(BAD)
    assert d.action == "degrade" and d.level == 1
    # one good window does not undo it, an OK window resets the streak
    c.observe(GOOD)
    c.observe(GOOD)
    c.observe(OK)
    assert c.observe(GOOD).action == "none"
    c.observe(GOOD)
    assert c.observe(GOOD).action == "upgrade" and c.level == 0


def test_no_oscillation_on_alternating_windows():
    c = AdaptiveController(QualitySettings())
    for _ in range(20):
        c.observe(BAD)
        assert c.observe(GOOD).action == "none"
    assert c.level == 0


def test_exhaustion():
    c = AdaptiveController(QualitySettings())
    actions = [c.observe(BAD).action for _ in range(3 * 6)]
    assert actions[-1] == "exhausted"


def test_disabled_controller():
    c = AdaptiveController(QualitySettings(), enabled=False)
    assert all(c.observe(BAD).action == "none" for _ in range(9))


def _r(key, fps, p95=100, cpu=50, ok=True, diff=0.0):
    return BenchmarkResult({"key": key, "device": "X"}, ok, None if ok else "boom", unique_fps=fps, p95_ms=p95,
                           cpu_pct=cpu, max_abs_diff_vs_cpu=diff)


def test_select_prefers_viable_balanced():
    best, reasons = select([_r("cpu", 6), _r("openvino", 14, cpu=20), _r("directml", 30, p95=400)], 250, 5)
    assert best.candidate["key"] == "openvino"
    assert any("directml" in r for r in reasons)


def test_select_rejects_wrong_output_and_failures():
    best, reasons = select([_r("cpu", 6), _r("directml", 40, diff=0.3), _r("openvino", 0, ok=False)], 250, 5)
    assert best.candidate["key"] == "cpu" and len(reasons) == 2


def test_select_keeps_cpu_if_accelerator_not_clearly_faster():
    best, _ = select([_r("cpu", 10), _r("directml", 10.5)], 250, 5)
    assert best.candidate["key"] == "cpu"


def test_select_low_power_profile():
    best, _ = select([_r("cpu", 8, cpu=70), _r("openvino", 9, cpu=15)], 250, 5, "low_power")
    assert best.candidate["key"] == "openvino"


def test_select_falls_back_to_cpu_when_nothing_meets_limits():
    best, reasons = select([_r("cpu", 2, p95=600)], 250, 5)
    assert best.candidate["key"] == "cpu" and "degraded" in reasons[-1]
