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
    (d / "face_detection_yunet_2023mar.onnx").write_bytes(b"tampered")  # user copy overrides the bundled one
    with pytest.raises(manifest.ModelError, match="checksum"):
        manifest.resolve("yunet_face_detector")
    fake_models["inswapper_compatible_swapper"].unlink()
    with pytest.raises(manifest.ModelError, match="missing"):
        manifest.resolve("inswapper_compatible_swapper")
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
    return lambda *a: (dict(fake_models), {k: "h" + k for k in fake_models}, ["unaudited"])


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


def test_device_ids_and_virtual_camera_filter():
    import cv2

    from avatar_engine.vision.capture import _is_own_output, parse_device

    assert parse_device("msmf:2") == (2, cv2.CAP_MSMF)
    assert parse_device("dshow:0") == (0, cv2.CAP_DSHOW)
    assert parse_device("1") == (1, None) and parse_device(None) == (0, None)
    assert parse_device("file.mp4") == ("file.mp4", None)
    assert _is_own_output("OBS Virtual Camera") and not _is_own_output("Integrated Webcam")


def test_bundled_yunet_resolves_without_acknowledgement(monkeypatch, tmp_path):
    monkeypatch.setenv("AVATAR_LIVE_MODELS_DIR", str(tmp_path))  # empty user models dir
    r = manifest.resolve("yunet_face_detector")
    assert r.audited and r.warning is None and r.path.parent.name == "bundled"
    from avatar_engine.vision.face_detect import FaceDetector

    assert FaceDetector().backend == "yunet"


def test_missing_model_message_names_the_file_to_download(monkeypatch, tmp_path):
    monkeypatch.setenv("AVATAR_LIVE_MODELS_DIR", str(tmp_path))
    with pytest.raises(manifest.ModelError, match="w600k_r50.onnx"):
        manifest.resolve("arcface_recognition")


def test_camera_enumeration_runs_in_a_fresh_thread(monkeypatch):
    import threading

    from avatar_engine.vision import capture

    caller = threading.get_ident()
    seen = {}
    assert capture._in_fresh_thread(lambda: seen.setdefault("t", threading.get_ident()), 2) != caller
    with pytest.raises(ValueError):
        capture._in_fresh_thread(lambda: (_ for _ in ()).throw(ValueError("x")), 2)
    report = []
    capture.list_video_devices(max_index=1, report=report)
    assert report  # diagnostics always filled


def test_original_upstream_file_names_are_accepted(tmp_path, monkeypatch, fake_models):
    import shutil

    monkeypatch.setenv("AVATAR_LIVE_MODELS_DIR", str(tmp_path))
    shutil.copy(fake_models["inswapper_compatible_swapper"], tmp_path / "inswapper_128.onnx")
    shutil.copy(fake_models["arcface_recognition"], tmp_path / "w600k_r50.onnx")
    res = manifest.acknowledge_present()
    assert {m["id"] for m in res["acknowledged"]} == {"inswapper_compatible_swapper", "arcface_recognition"}
    assert manifest.resolve("inswapper_compatible_swapper").path.name == "inswapper_128.onnx"
    assert manifest.resolve("arcface_recognition").path.name == "w600k_r50.onnx"


def test_acknowledge_reports_missing_models(tmp_path, monkeypatch):
    monkeypatch.setenv("AVATAR_LIVE_MODELS_DIR", str(tmp_path))
    res = manifest.acknowledge_present()
    assert res["acknowledged"] == [] and {m["id"] for m in res["missing"]} >= {"arcface_recognition"}


def test_status_hint_explains_missing_avatar(profile, synthetic_face, fake_detector, fake_models):
    img, _, _ = synthetic_face
    eng = LiteCpuSwapEngine(fake_pipeline_factory(fake_detector), _resolver(fake_models))
    eng.initialize(_swap_cfg(profile), {})
    eng.load_profile(profile)
    assert "webcam" in eng.status_hint().lower()  # no frames yet
    eng.frames_in, eng.frames_processed, eng.no_face_frames = 5, 5, 5
    assert "rostro" in eng.status_hint()
    eng.last_error = "RuntimeError: boom"
    assert "boom" in eng.status_hint()


def test_session_hint_prefers_camera_error(profile):
    from avatar_engine.session import Session

    class Cam:
        error = "cannot open camera 'msmf:0'"

    s = Session({"engine": "lite_cpu_audio", "drive_mode": "audio", "profile_id": profile.profile_id},
                None, lambda n, p: None)
    s.inputs.append(Cam())
    assert s.hint().startswith("Webcam no disponible")


def test_camera_falls_back_to_next_api(monkeypatch):
    import cv2

    from avatar_engine.vision import capture

    class FakeCap:
        def __init__(self, dev, api=None):
            self.dev, self.api = dev, api

        def isOpened(self):
            return self.api != cv2.CAP_MSMF  # MSMF fails, DirectShow works

        def set(self, *a):
            return True

        def read(self):
            return True, np.zeros((480, 640, 3), np.uint8)

        def release(self):
            pass

    monkeypatch.setattr(capture.cv2, "VideoCapture", FakeCap)
    cam = capture.CameraCapture(lambda f, t: None, "msmf:1")
    monkeypatch.setattr(cam, "_candidates", lambda: [(1, cv2.CAP_MSMF, "msmf:1"),
                                                     (0, cv2.CAP_DSHOW, "dshow:0 (Integrated Camera)")])
    c = cam._open()
    assert c.api == cv2.CAP_DSHOW and cam.opened_via.startswith("dshow:0")

    monkeypatch.setattr(cam, "_candidates", lambda: [(1, cv2.CAP_MSMF, "msmf:1")])
    with pytest.raises(capture.CameraUnavailable, match="msmf:1: not opened"):
        cam._open()


def test_status_frame_wraps_long_text():
    from avatar_engine.overlay import _wrap

    lines = _wrap("Webcam no disponible: " + "palabra " * 40, 0.7, 2, 600)
    assert 2 <= len(lines) <= 6
