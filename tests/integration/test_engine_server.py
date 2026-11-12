"""End-to-end: real `avatar-engine serve` process ↔ control client + frame ring reader."""

import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from avatar_engine import frame_ring as fr, ipc, protocol

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX dev transport (Windows covered by CI job)")


class Client:
    def __init__(self, session):
        self.conn = ipc.connect(session, timeout=10)
        self.replies, self.events = {}, []
        self._id = 0
        self._cv = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            while True:
                m = self.conn.recv()
                with self._cv:
                    if m["type"] == "event":
                        self.events.append(m)
                    else:
                        self.replies[m["id"]] = m
                    self._cv.notify_all()
        except ipc.ConnectionClosed:
            pass

    def call(self, type_, payload=None, timeout=20):
        self._id += 1
        mid = self._id
        self.conn.send(protocol.request(mid, type_, payload))
        with self._cv:
            self._cv.wait_for(lambda: mid in self.replies, timeout)
        return self.replies.get(mid)

    def close(self):
        self.conn.close()


@pytest.fixture
def engine_proc(isolated_data_dir, session_name, profile):
    token = secrets.token_hex(16)
    env = dict(os.environ, AVATAR_LIVE_AUTH_TOKEN=token, AVATAR_LIVE_DATA_DIR=str(isolated_data_dir),
               PYTHONPATH=str(ROOT / "engine"))
    p = subprocess.Popen([sys.executable, "-m", "avatar_engine", "serve", "--session", session_name,
                          "--max-width", "1280", "--max-height", "720"], env=env)
    yield p, token
    if p.poll() is None:
        p.kill()
        p.wait(5)


def _reader(session_name, timeout=10):
    deadline = time.time() + timeout
    while True:
        try:
            return fr.FrameRingReader(session_name)
        except FileNotFoundError:
            if time.time() > deadline:
                raise
            time.sleep(0.05)


def test_rejects_bad_token(engine_proc, session_name):
    c = Client(session_name)
    r = c.call("hello", {"auth_token": "wrong" * 8, "client": "t", "protocol_version": 1})
    assert r["ok"] is False and r["error"]["code"] == "unauthorized"
    c.close()


def test_full_session_lifecycle(engine_proc, session_name, profile, speech_wav):
    proc, token = engine_proc
    c = Client(session_name)
    r = c.call("hello", {"auth_token": token, "client": "obs-avatar-live", "client_version": "test",
                         "protocol_version": 1})
    assert r["ok"], r
    assert {e["name"] for e in r["payload"]["engines"]} >= {"lite_cpu_audio", "lite_adaptive"}
    assert r["payload"]["frame_ring"]["name"] == fr.ring_name(session_name)
    reader = _reader(session_name)
    try:
        # idle frames while stopped (face-free, paused flag, label)
        time.sleep(0.4)
        idle = reader.read_latest()
        assert idle is not None and idle.flags & fr.FLAG_PAUSED and idle.flags & fr.FLAG_SYNTHETIC

        assert c.call("list_profiles")["payload"]["profiles"][0]["profile_id"] == profile.profile_id
        bad = c.call("start_session", {"engine": "lite_cpu_audio", "drive_mode": "audio",
                                       "profile_id": profile.profile_id, "privacy": {"disclosure_overlay": False}})
        assert not bad["ok"] and bad["error"]["code"] == "invalid_request"

        r = c.call("start_session", {"schema_version": 2, "engine": "lite_cpu_audio", "drive_mode": "audio",
                                     "profile_id": profile.profile_id, "audio_input": f"file:{speech_wav}",
                                     "output": {"width": 1280, "height": 720, "fps": 30}})
        assert r["ok"], r
        gen = r["payload"]["generation_id"]
        time.sleep(1.5)
        f = reader.read_latest()
        assert f is not None and f.generation_id == gen and (f.width, f.height) == (1280, 720)
        assert not f.flags & fr.FLAG_DEGRADED
        # disclosure label region (bottom-left) is the dark label box
        assert f.pixels[680:700, 30:200, :3].mean() < 120
        m = c.call("get_metrics")["payload"]
        assert m["synthetic_label"] is True and m["engine"] == "lite_cpu_audio"
        assert 20 <= m["output_fps"] <= 35, m["output_fps"]
        assert m["audio_frames"] > 20 and m["audio_stalled"] is False
        assert any(e["event"] == "metrics" for e in c.events)

        assert c.call("pause")["ok"]
        time.sleep(0.2)
        assert reader.read_latest().flags & fr.FLAG_DEGRADED
        assert c.call("resume")["ok"]
        assert c.call("freeze_neutral", {"enabled": True})["ok"]
        assert c.call("set_expression", {"expression_id": "smile", "intensity": 0.6, "duration_ms": 500})["ok"]
        assert c.call("delete_profile", {"profile_id": profile.profile_id})["ok"] is False  # in use
        assert c.call("stop_session")["ok"]
        assert c.call("get_metrics")["error"]["code"] == "no_session"
        assert c.call("start_session", {"engine": "synthetic_head", "drive_mode": "audio",
                                        "profile_id": profile.profile_id})["error"]["code"] == "engine_unavailable"
        assert c.call("shutdown")["ok"]
        proc.wait(10)
        assert proc.returncode == 0
    finally:
        reader.close()
        c.close()


def test_create_profile_requires_consent(engine_proc, session_name, tmp_path):
    _, token = engine_proc
    c = Client(session_name)
    c.call("hello", {"auth_token": token, "protocol_version": 1})
    r = c.call("create_profile", {"photo_path": str(tmp_path / "x.png"), "display_name": "x", "consent": {}})
    assert not r["ok"] and r["error"]["code"] == "consent_required"
    c.close()


def test_engine_exits_when_parent_dies(isolated_data_dir, session_name):
    parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    env = dict(os.environ, AVATAR_LIVE_AUTH_TOKEN=secrets.token_hex(16), PYTHONPATH=str(ROOT / "engine"))
    p = subprocess.Popen([sys.executable, "-m", "avatar_engine", "serve", "--session", session_name,
                          "--parent-pid", str(parent.pid)], env=env)
    try:
        time.sleep(1.0)
        assert p.poll() is None
        parent.kill()
        parent.wait()
        p.wait(10)
        assert p.returncode == 0
    finally:
        for x in (p, parent):
            if x.poll() is None:
                x.kill()


def test_acknowledge_models_requires_confirmation(engine_proc, session_name):
    _, token = engine_proc
    c = Client(session_name)
    c.call("hello", {"auth_token": token, "protocol_version": 1})
    r = c.call("acknowledge_models", {})
    assert not r["ok"] and r["error"]["code"] == "invalid_request"
    r = c.call("acknowledge_models", {"license_accepted": True})
    assert r["ok"] and {m["id"] for m in r["payload"]["missing"]} >= {"inswapper_compatible_swapper"}
    c.close()
