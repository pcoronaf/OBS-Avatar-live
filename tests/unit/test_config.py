import json
from pathlib import Path

import pytest

from avatar_engine import config

ROOT = Path(__file__).resolve().parents[2]


def test_packaged_schema_matches_contract():
    a = (ROOT / "contracts" / "config.schema.json").read_text(encoding="utf-8")
    b = (ROOT / "engine" / "avatar_engine" / "config.schema.json").read_text(encoding="utf-8")
    assert json.loads(a) == json.loads(b)


def test_spec_example_config_is_valid():
    cfg = {
        "schema_version": 2, "engine": "lite_adaptive", "profile_id": "local-profile-001", "drive_mode": "webcam",
        "output": {"width": 1280, "height": 720, "fps": 30},
        "inference": {"provider": "auto", "provider_priority": ["openvino", "directml", "cpu"], "cpu_only": False,
                      "startup_benchmark": True, "benchmark_duration_seconds": 8, "max_end_to_end_latency_ms": 250,
                      "minimum_unique_swap_fps": 5, "max_threads": 4, "input_face_size": 256,
                      "detection_every_n_frames": 6, "frame_queue_size": 2, "enhancement": False,
                      "adaptive_quality": True},
        "audio": {"sample_rate_hz": 16000, "frame_ms": 20, "vad_enabled": True},
        "privacy": {"local_only": True, "store_audio": False, "store_video": False, "disclosure_overlay": True},
    }
    out = config.load(cfg)
    assert out["inference"]["max_threads"] == 4
    assert out["stale_frame_ms"] == 500


def test_disclosure_cannot_be_disabled():
    with pytest.raises(config.ConfigError):
        config.load({"engine": "lite_cpu_audio", "drive_mode": "audio", "profile_id": "a",
                     "privacy": {"disclosure_overlay": False}})


def test_local_only_and_unknown_keys():
    with pytest.raises(config.ConfigError):
        config.load({"engine": "lite_cpu_audio", "drive_mode": "audio", "profile_id": "a",
                     "privacy": {"local_only": False}})
    with pytest.raises(config.ConfigError):
        config.load({"engine": "lite_cpu_audio", "drive_mode": "audio", "profile_id": "a", "upload": True})


def test_engine_drive_mode_mismatch():
    with pytest.raises(config.ConfigError):
        config.load({"engine": "lite_cpu_audio", "drive_mode": "webcam", "profile_id": "a"})


def test_cpu_only_profile_forces_cpu():
    out = config.load({"engine": "lite_adaptive", "drive_mode": "webcam", "profile_id": "a",
                       "user_profile": "cpu_only"})
    assert out["inference"]["cpu_only"] is True
    assert out["inference"]["provider_priority"] == ["cpu"]
