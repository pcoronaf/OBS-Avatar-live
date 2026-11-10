import numpy as np

from avatar_engine.audio.vad import EnergyVAD, rms_dbfs
from avatar_engine.audio.visemes import VisemeEstimator

SR = 16000


def tone(freq, seconds, amp=8000):
    t = np.arange(int(SR * seconds)) / SR
    return (np.sin(2 * np.pi * freq * t) * amp).astype(np.int16)


def noise(seconds, amp=200, seed=0):
    return (np.random.default_rng(seed).standard_normal(int(SR * seconds)) * amp).astype(np.int16)


def test_rms():
    assert rms_dbfs(np.zeros(320, np.int16)) < -100
    assert -4 < rms_dbfs(tone(440, 0.02, 32767)) < -2


def test_vad_absorbs_background_noise_and_detects_speech():
    v = EnergyVAD()
    for i in range(150):  # 3 s of noise
        active = v.process(rms_dbfs(noise(0.02, seed=i)))
    assert not active
    assert v.process(rms_dbfs(tone(200, 0.02) + noise(0.02)))


def test_mouth_follows_speech_and_closes_on_silence():
    est = VisemeEstimator()
    est.push(noise(1.0))
    speaking = est.push(tone(200, 0.5) + noise(0.5))
    assert max(s.open for s in speaking) > 0.4
    silent = est.push(noise(0.6, seed=3))
    assert silent[-1].open < 0.05 and not silent[-1].speaking


def test_sensitivity_scales_opening():
    lo, hi = VisemeEstimator(sensitivity=0.5), VisemeEstimator(sensitivity=2.0)
    sig = np.concatenate([noise(0.5), tone(200, 0.4, 3000)])
    assert max(s.open for s in hi.push(sig)) > max(s.open for s in lo.push(sig))


def test_fricative_keeps_mouth_narrow():
    est = VisemeEstimator()
    est.push(noise(0.5))
    hiss = noise(0.4, amp=6000, seed=9)  # broadband, high ZCR
    hp = np.diff(hiss.astype(np.float32), prepend=0).clip(-32768, 32767).astype(np.int16)
    states = est.push(hp)
    assert any(s.viseme in ("SS", "FV") for s in states)
    assert max(s.open for s in states) <= 0.25


def test_frame_chunking_any_length():
    est = VisemeEstimator()
    out = est.push(np.zeros(1000, np.int16))
    assert len(out) == 3  # 3 x 320 samples, 40 kept for later
    assert len(est.push(np.zeros(280, np.int16))) == 1
