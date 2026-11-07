import os
import secrets
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Every test gets its own AvatarLive data dir (profiles, models, benchmarks)."""
    monkeypatch.setenv("AVATAR_LIVE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("AVATAR_LIVE_MODELS_DIR", raising=False)
    monkeypatch.delenv("AVATAR_LIVE_FAULT", raising=False)
    from avatar_engine.performance import system_info

    system_info.system_info.cache_clear()
    yield tmp_path / "data"


@pytest.fixture
def session_name():
    return "t" + secrets.token_hex(8)


@pytest.fixture
def synthetic_face():
    from avatar_engine.selftest import synthetic_face as sf

    return sf()


class FakeDetector:
    """Returns the synthetic face's landmarks (scaled to the frame)."""

    backend = "fake"

    def __init__(self, lm, bbox, ref_size, input_size=640):
        self.lm, self.bbox, self.ref = lm, bbox, ref_size
        self.input_size = input_size
        self.calls = 0

    def detect(self, img, max_faces=1):
        from avatar_engine.vision.face_detect import Face

        self.calls += 1
        sx, sy = img.shape[1] / self.ref[0], img.shape[0] / self.ref[1]
        s = np.array([sx, sy], np.float32)
        bb = self.bbox.copy()
        bb[:2] *= s
        bb[2:] *= s
        return [Face(bb, self.lm * s, 0.99)]


@pytest.fixture
def fake_detector(synthetic_face):
    img, lm, bbox = synthetic_face
    return FakeDetector(lm, bbox, (img.shape[1], img.shape[0]))


@pytest.fixture
def profile(synthetic_face, fake_detector, isolated_data_dir, tmp_path):
    import cv2

    from avatar_engine.privacy import profile_store

    img, _, _ = synthetic_face
    photo = tmp_path / "face.png"
    cv2.imwrite(str(photo), img)
    prof, _ = profile_store.create_profile(photo, "Test", {"rights_confirmed": True, "subject_is_self": True},
                                           detector=fake_detector)
    return prof


@pytest.fixture
def fake_models(tmp_path):
    from tests_helpers import make_fake_models

    return make_fake_models(tmp_path / "models")


def wav_file(path: Path, seconds: float = 2.0) -> Path:
    import wave

    t = np.arange(int(16000 * seconds)) / 16000
    env = (np.sin(2 * np.pi * 3 * t) > 0).astype(np.float32)
    pcm = (np.sin(2 * np.pi * 180 * t) * 9000 * env).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm.tobytes())
    return path


@pytest.fixture
def speech_wav(tmp_path):
    return wav_file(tmp_path / "speech.wav")


sys.path.insert(0, str(Path(__file__).parent))
