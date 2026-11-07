import time

import numpy as np
import pytest

from avatar_engine import config
from avatar_engine.engines import swap_pipeline as sp
from avatar_engine.engines.lite_adaptive import LiteAdaptiveEngine
from avatar_engine.engines.lite_cpu_swap import LiteCpuSwapEngine
from avatar_engine.frame_ring import now_ns
from avatar_engine.models import manifest
from avatar_engine.performance.provider_probe import ProviderCandidate, discover
from avatar_engine.vision import alignment
from tests_helpers import fake_pipeline_factory

CPU = ProviderCandidate("cpu", "CPUExecutionProvider", "CPU")


def test_alignment_maps_landmarks_to_template(synthetic_face):
    img, lm, _ = synthetic_face
    crop, M = alignment.align(img, lm, 112)
    mapped = np.hstack([lm, np.ones((5, 1), np.float32)]) @ M.T
    assert np.abs(mapped - alignment.TEMPLATE_112).max() < 12  # similarity fit of a cartoon face
    assert crop.shape == (112, 112, 3)
    assert np.allclose(alignment.template(128)[0], alignment.TEMPLATE_112[0] + [8, 0])


def test_paste_back_only_touches_face_area(synthetic_face):
    img, lm, _ = synthetic_face
    crop, M = alignment.align(img, lm, 128)
    out = alignment.paste_back(img.copy(), np.zeros_like(crop), M)
    assert np.array_equal(out[:50, :50], img[:50, :50])
    assert out[int(lm[2][1]), int(lm[2][0])].mean() < img[int(lm[2][1]), int(lm[2][0])].mean()


def test_pipeline_with_fake_models(synthetic_face, fake_detector, fake_models):
    img, lm, _ = synthetic_face
    pipe = fake_pipeline_factory(fake_detector)(CPU, fake_models, 1, 3)
    pipe.set_source(img)
    assert pipe.swapper.emap is not None and pipe.source_latent.shape == (1, 512)
    for _ in range(4):
        res = pipe.process(img)
    assert res.face_found and set(res.timings_ms) >= {"detect", "align", "swap", "blend"}
    assert fake_detector.calls == 3  # 1 for the source photo + frames 0 and 3 (flow in between)
    assert not np.array_equal(res.frame, img)


def test_provider_fault_injection(fake_models, monkeypatch):
    monkeypatch.setenv("AVATAR_LIVE_FAULT", "provider")
    with pytest.raises(sp.ProviderError):
        sp.make_session(fake_models["inswapper_compatible_swapper"],
                        ProviderCandidate("directml", "DmlExecutionProvider", "GPU", {"device_id": 0}), 1)
    sp.make_session(fake_models["inswapper_compatible_swapper"], CPU, 1)  # CPU unaffected


def test_cpu_only_never_lists_accelerators():
    assert [c.key for c in discover(["openvino", "directml", "cuda"], cpu_only=True)] == ["cpu"]
    assert discover(["openvino"], cpu_only=False)[-1].key == "cpu"  # always a CPU fallback


def test_unaudited_models_require_acknowledgement(fake_models, monkeypatch):
    d = fake_models["arcface_recognition"].parent
    monkeypatch.setenv("AVATAR_LIVE_MODELS_DIR", str(d))
    with pytest.raises(manifest.ModelError, match="acknowledged"):
        manifest.resolve("arcface_recognition")
    manifest.acknowledge("arcface_recognition", manifest.sha256_file(fake_models["arcface_recognition"]), d)
    r = manifest.resolve("arcface_recognition")
    assert not r.audited and r.warning
    with pytest.raises(manifest.ModelError, match="missing"):
        manifest.resolve("yunet_face_detector")
    # pinned checksum mismatch
    man = manifest.load_manifest()
    for m in man["models"]:
        if m["id"] == "arcface_recognition":
            m["sha256"] = "0" * 64
    with pytest.raises(manifest.ModelError, match="checksum"):
        manifest.resolve("arcface_recognition", man)


def _swap_cfg(profile, **inf):
    return config.load({"engine": "lite_cpu_swap", "drive_mode": "webcam", "profile_id": profile.profile_id,
                        "output": {"width": 640, "height": 360},
                        "inference": {"detection_every_n_frames": 3, **inf}})


def _resolver(fake_models):
    return lambda: (dict(fake_models), {k: "h" + k for k in fake_models}, ["unaudited"])


def test_lite_cpu_swap_engine_runs(profile, synthetic_face, fake_detector, fake_models):
    img, _, _ = synthetic_face
    eng = LiteCpuSwapEngine(fake_pipeline_factory(fake_detector), _resolver(fake_models))
    eng.initialize(_swap_cfg(profile), {})
    eng.load_profile(profile)
    eng.start_session("s")
    try:
        deadline = time.time() + 3
        while time.time() < deadline and eng._seq < 5:
            eng.push_video(img, now_ns())
            time.sleep(0.02)
        f = eng.poll_frame(now_ns())
        assert f is not None and f.bgra.shape == (360, 640, 4) and f.capture_ts_ns > 0
        m = eng.get_metrics()
        assert m["execution_provider_selected"] == "CPUExecutionProvider"
        assert m["face_swap_unique_fps"] > 0 and m["model_warnings"] == ["unaudited"]
        eng.freeze_neutral(True)
        assert eng.poll_frame(now_ns()).paused
    finally:
        eng.stop_session()


def test_stale_capture_is_dropped(profile, synthetic_face, fake_detector, fake_models):
    img, _, _ = synthetic_face
    eng = LiteCpuSwapEngine(fake_pipeline_factory(fake_detector), _resolver(fake_models))
    eng.initialize(_swap_cfg(profile), {})
    eng.load_profile(profile)
    eng.start_session("s")
    try:
        for _ in range(10):
            eng.push_video(img, now_ns() - 900_000_000)
            time.sleep(0.03)
        assert eng._seq == 0 and eng.stale_input_drops > 0
    finally:
        eng.stop_session()


def test_lite_adaptive_benchmarks_selects_and_caches(profile, fake_detector, fake_models, monkeypatch):
    eng = LiteAdaptiveEngine(fake_pipeline_factory(fake_detector), _resolver(fake_models))
    cfg = _swap_cfg(profile, benchmark_duration_seconds=1)
    cfg["engine"] = "lite_adaptive"
    eng.initialize(cfg, {})
    fake_gpu = ProviderCandidate("directml", "DmlExecutionProvider", "iGPU", {"device_id": 0})
    eng.candidates = [fake_gpu, CPU]  # DirectML is not installed here -> must fail benchmark and be rejected
    eng.load_profile(profile)
    assert eng.candidate.key == "cpu"
    assert "directml:iGPU" in (eng.fallback_reason or "")
    assert eng.benchmark_timestamp
    from avatar_engine import paths

    assert list(paths.benchmarks_dir().glob("*.json"))
    # second start uses the cache (no new benchmark)
    called = []
    monkeypatch.setattr("avatar_engine.performance.startup_benchmark.run_benchmark",
                        lambda *a, **k: called.append(1) or [])
    eng2 = LiteAdaptiveEngine(fake_pipeline_factory(fake_detector), _resolver(fake_models))
    eng2.initialize(cfg, {})
    eng2.candidates = [fake_gpu, CPU]
    eng2.load_profile(profile)
    assert not called and eng2.candidate.key == "cpu"


def test_runtime_errors_fall_back_to_cpu(profile, fake_detector, fake_models):
    eng = LiteAdaptiveEngine(fake_pipeline_factory(fake_detector), _resolver(fake_models))
    cfg = _swap_cfg(profile, startup_benchmark=False)
    cfg["engine"] = "lite_adaptive"
    eng.initialize(cfg, {})
    eng.candidates = [CPU]
    eng.load_profile(profile)
    events = []
    eng.emit = lambda n, p: events.append((n, p))
    eng.candidate = ProviderCandidate("openvino", "OpenVINOExecutionProvider", "GPU")  # pretend we run on a GPU
    gen = eng.generation_id
    for _ in range(3):
        eng.on_runtime_error(RuntimeError("device removed"))
    assert eng.candidate.key == "cpu" and eng.generation_id != gen
    assert any(n == "fallback" for n, _ in events)
