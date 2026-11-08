import time

import numpy as np
import pytest

from avatar_engine import config
from avatar_engine.engines import dfm_pipeline as dp
from avatar_engine.engines.base import EngineUnavailable
from avatar_engine.engines.lite_adaptive import LiteAdaptiveEngine
from avatar_engine.engines.lite_cpu_swap import resolve_models
from avatar_engine.engines.swap_pipeline import make_session
from avatar_engine.frame_ring import now_ns
from avatar_engine.models.manifest import ModelError
from avatar_engine.performance.provider_probe import ProviderCandidate
from tests_helpers import fake_dfm_factory, make_fake_dfm

CPU = ProviderCandidate("cpu", "CPUExecutionProvider", "CPU")


def test_dfm_model_io_detection(tmp_path):
    m = dp.DfmModel(make_session(make_fake_dfm(tmp_path / "me.dfm", 192), CPU, 1))
    assert m.size == 192 and not m.morphable
    crop = np.full((192, 192, 3), 51, np.uint8)  # 0.2
    face, mask = m.run(crop)
    assert face.shape == (192, 192, 3) and mask.shape == (192, 192)
    assert abs(face.mean() - 0.8) < 1e-3 and abs(mask.mean() - 0.81) < 1e-3


def test_morphable_dfm(tmp_path):
    m = dp.DfmModel(make_session(make_fake_dfm(tmp_path / "amp.dfm", 128, morph=True), CPU, 1))
    assert m.morphable
    face, _ = m.run(np.zeros((128, 128, 3), np.uint8), morph=0.5)
    assert abs(face.mean() - 0.5) < 1e-3


def test_template_coverage_and_offset():
    base = dp.dfm_template(224)
    wide = dp.dfm_template(224, coverage=0.75)
    assert np.linalg.norm(wide[1] - wide[0]) < np.linalg.norm(base[1] - base[0])  # zoomed out: eyes closer
    assert np.allclose(dp.dfm_template(224, offset_y=0.1)[:, 1], base[:, 1] + 22.4)


def test_pipeline_swaps_only_face_area(tmp_path, synthetic_face, fake_detector):
    img, lm, _ = synthetic_face
    models = {"dfm_face_model": make_fake_dfm(tmp_path / "me.dfm")}
    pipe = fake_dfm_factory(fake_detector)(CPU, models, 1, 3)
    res = pipe.process(img)
    assert res.face_found and set(res.timings_ms) >= {"detect", "align", "swap", "blend"}
    nose = tuple(int(v) for v in lm[2][::-1])
    assert np.abs(res.frame[nose].astype(int) - img[nose].astype(int)).sum() > 50  # face replaced
    assert np.array_equal(res.frame[:30, :30], img[:30, :30])  # background untouched
    assert pipe.probe(np.zeros((100, 100, 3), np.uint8)).shape == (224, 224, 3)


def test_dfm_requires_rights_and_valid_file(tmp_path):
    cfg = {"inference": {"swap_model": "dfm", "dfm_model": str(tmp_path / "x.dfm"), "dfm_rights_confirmed": False}}
    with pytest.raises(EngineUnavailable, match="own face"):
        resolve_models(cfg)
    cfg["inference"]["dfm_rights_confirmed"] = True
    with pytest.raises(EngineUnavailable, match="not found"):
        resolve_models(cfg)
    with pytest.raises(ModelError, match=".dfm"):
        dp.validate_dfm_path(tmp_path / "model.onnx")


def test_config_accepts_dfm_options():
    c = config.load({"engine": "lite_adaptive", "drive_mode": "webcam", "profile_id": "p",
                     "inference": {"swap_model": "dfm", "dfm_model": "C:/m/me.dfm", "dfm_rights_confirmed": True,
                                   "dfm_coverage": 0.8, "dfm_offset_y": -0.05, "dfm_morph": 0.7}})
    assert c["inference"]["swap_model"] == "dfm"
    with pytest.raises(config.ConfigError):
        config.load({"engine": "lite_adaptive", "drive_mode": "webcam", "profile_id": "p",
                     "inference": {"dfm_coverage": 3}})


def test_lite_adaptive_runs_dfm(tmp_path, profile, synthetic_face, fake_detector):
    img, _, _ = synthetic_face
    dfm = make_fake_dfm(tmp_path / "me.dfm")
    cfg = config.load({"engine": "lite_adaptive", "drive_mode": "webcam", "profile_id": profile.profile_id,
                       "output": {"width": 640, "height": 360},
                       "inference": {"swap_model": "dfm", "dfm_model": str(dfm), "dfm_rights_confirmed": True,
                                     "benchmark_duration_seconds": 1, "detection_every_n_frames": 3}})
    resolver = lambda c: ({"yunet_face_detector": None, "dfm_face_model": dfm}, {"dfm_face_model": "h"}, [])
    eng = LiteAdaptiveEngine(fake_dfm_factory(fake_detector), resolver)
    eng.initialize(cfg, {})
    eng.candidates = [CPU]
    eng.load_profile(profile)
    eng.start_session("s")
    try:
        deadline = time.time() + 3
        while time.time() < deadline and eng._seq < 3:
            eng.push_video(img, now_ns())
            time.sleep(0.03)
        assert eng.poll_frame(now_ns()) is not None and eng.get_metrics()["face_swap_unique_fps"] > 0
    finally:
        eng.stop_session()
