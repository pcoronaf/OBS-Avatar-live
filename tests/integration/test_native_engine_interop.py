"""C++ plugin core (tests/native avatar_ipc_selftest) ↔ the *real* Python engine."""

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NATIVE = ROOT / "tests" / "native"
BUILD = Path(os.environ.get("AVATAR_NATIVE_BUILD", Path(tempfile.gettempdir()) / "avatar_live_build_native"))

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("cmake") is None,
                                reason="POSIX + cmake required")


@pytest.fixture(scope="module")
def selftest():
    subprocess.run(["cmake", "-S", str(NATIVE), "-B", str(BUILD)], check=True, capture_output=True)
    subprocess.run(["cmake", "--build", str(BUILD)], check=True, capture_output=True)
    return BUILD / "avatar_ipc_selftest"


@pytest.fixture
def real_engine(isolated_data_dir):
    session, token = secrets.token_hex(16), secrets.token_hex(16)
    env = dict(os.environ, AVATAR_LIVE_AUTH_TOKEN=token, AVATAR_LIVE_DATA_DIR=str(isolated_data_dir),
               PYTHONPATH=str(ROOT / "engine"))
    p = subprocess.Popen([sys.executable, "-m", "avatar_engine", "serve", "--session", session], env=env)
    time.sleep(1.0)
    yield session, token
    p.kill()
    p.wait(5)


def test_cpp_reader_reads_engine_frames(selftest, real_engine):
    session, _ = real_engine
    out = subprocess.run([str(selftest), "read-ring", "--session", session, "--frames", "3", "--timeout-ms", "5000"],
                         capture_output=True, text=True, timeout=15)
    assert out.returncode == 0, out.stderr
    frames = [json.loads(l) for l in out.stdout.splitlines()]
    assert len(frames) == 3
    assert all(f["width"] == 1280 and f["height"] == 720 and f["flags"] & 1 for f in frames)
    seqs = [f["sequence"] for f in frames]
    assert seqs == sorted(seqs) and len(set(seqs)) == 3


def test_cpp_control_client_talks_to_engine(selftest, real_engine):
    session, token = real_engine
    out = subprocess.run([str(selftest), "ctl", "--session", session, "--token", token],
                         capture_output=True, text=True, timeout=15)
    replies = [json.loads(l) for l in out.stdout.splitlines()]
    assert replies[0]["type"] == "hello" and replies[0]["payload"]["protocol_version"] == 1
    assert replies[1]["type"] == "ping" and "now_ns" in replies[1]["payload"]
    # no session is running yet: the engine answers get_metrics with a typed error
    assert out.returncode == 1 and "no_session" in out.stderr
