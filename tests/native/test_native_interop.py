# SPDX-License-Identifier: GPL-2.0-or-later
"""Interop: C++ plugin core (avatar_ipc_selftest) <-> protocol mock (mock_engine.py).

Builds tests/native with CMake if needed. Skipped on Windows (POSIX dev transport)
and when cmake / a C++ compiler is unavailable.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
BUILD = Path(os.environ.get("AVATAR_NATIVE_BUILD", Path(tempfile.gettempdir()) / "avatar_live_build_native"))

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("cmake") is None, reason="POSIX + cmake required"
)


@pytest.fixture(scope="module")
def selftest() -> Path:
    exe = BUILD / "avatar_ipc_selftest"
    subprocess.run(["cmake", "-S", str(HERE), "-B", str(BUILD)], check=True, capture_output=True)
    subprocess.run(["cmake", "--build", str(BUILD)], check=True, capture_output=True)
    return exe


def _start_mock(session: str, token: str, *extra: str) -> subprocess.Popen:
    p = subprocess.Popen([sys.executable, str(HERE / "mock_engine.py"), "--session", session, "--token", token, *extra],
                         stdout=subprocess.PIPE, text=True)
    assert p.stdout is not None and p.stdout.readline().strip() == "READY"
    return p


def _expected_checksum(w: int, h: int, seq: int) -> int:
    row = sum(((x * 4 + seq) & 0xFF) + ((seq * 3) & 0xFF) + 0x80 + 0xFF for x in range(w))
    return (row * h) & 0xFFFFFFFF


def test_native_unittests(selftest: Path):
    subprocess.run([str(BUILD / "avatar_core_unittest")], check=True)


def test_ring_frames_roundtrip(selftest: Path):
    session, token = secrets.token_hex(16), secrets.token_hex(16)
    mock = _start_mock(session, token, "--frames", "40", "--fps", "60")
    try:
        out = subprocess.run([str(selftest), "read-ring", "--session", session, "--frames", "5", "--timeout-ms", "5000"],
                             capture_output=True, text=True, timeout=15)
        assert out.returncode == 0, out.stderr
        lines = [json.loads(line) for line in out.stdout.splitlines()]
        assert len(lines) == 5
        seqs = [f["sequence"] for f in lines]
        assert seqs == sorted(seqs) and len(set(seqs)) == 5
        for f in lines:
            assert (f["width"], f["height"]) == (64, 32)
            assert f["flags"] & 1
            assert f["generation_id"] == 1
            assert f["checksum"] == _expected_checksum(64, 32, f["sequence"])
    finally:
        mock.terminate()
        mock.wait(timeout=5)


def test_ring_rejects_unlabeled_frames(selftest: Path):
    session, token = secrets.token_hex(16), secrets.token_hex(16)
    # Every frame lacks the synthetic label -> reader must never accept one.
    mock = _start_mock(session, token, "--frames", "30", "--fps", "60", "--unlabeled-every", "1")
    try:
        out = subprocess.run([str(selftest), "read-ring", "--session", session, "--frames", "1", "--timeout-ms", "1500"],
                             capture_output=True, text=True, timeout=10)
        assert out.returncode == 1
        assert out.stdout.strip() == ""
    finally:
        mock.terminate()
        mock.wait(timeout=5)


def test_control_channel(selftest: Path):
    session, token = secrets.token_hex(16), secrets.token_hex(16)
    mock = _start_mock(session, token, "--frames", "1")
    try:
        out = subprocess.run([str(selftest), "ctl", "--session", session, "--token", token],
                             capture_output=True, text=True, timeout=15)
        assert out.returncode == 0, out.stderr
        replies = [json.loads(line) for line in out.stdout.splitlines()]
        assert [r["type"] for r in replies] == ["hello", "ping", "get_metrics"]
        assert replies[0]["payload"]["protocol_version"] == 1
        assert replies[2]["payload"]["synthetic_label"] is True
    finally:
        mock.terminate()
        mock.wait(timeout=5)


def test_control_channel_rejects_bad_token(selftest: Path):
    session, token = secrets.token_hex(16), secrets.token_hex(16)
    mock = _start_mock(session, token, "--frames", "1")
    try:
        out = subprocess.run([str(selftest), "ctl", "--session", session, "--token", "0" * 32, "--timeout-ms", "3000"],
                             capture_output=True, text=True, timeout=15)
        assert out.returncode == 1
        assert "unauthorized" in out.stderr
    finally:
        mock.terminate()
        mock.wait(timeout=5)


def test_controller_restarts_engine(selftest: Path, tmp_path: Path):
    # Launcher-compatible wrapper: the mock exits after 40 frames (simulated crash).
    wrapper = tmp_path / "fake-engine"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{HERE / "mock_engine.py"}" "$@" --frames 40 --fps 60\n')
    wrapper.chmod(0o755)
    out = subprocess.run([str(selftest), "controller", "--engine", str(wrapper), "--timeout-ms", "20000"],
                         capture_output=True, text=True, timeout=40)
    assert out.returncode == 0, out.stdout + out.stderr
    summary = json.loads(out.stdout.strip().splitlines()[-1])
    assert summary["frames_before"] > 0
    assert summary["frames_after"] >= 5
    assert summary["restarts"] >= 1
    assert summary["recovery_ms"] < 10000  # spec §12.A: recover in < 10 s


def test_controller_shutdown_is_bounded_while_connecting(selftest: Path, tmp_path: Path):
    # An engine that never opens its control pipe must not stall OBS on close.
    import time
    hang = tmp_path / "hang-engine"
    hang.write_text("#!/bin/sh\nexec sleep 30\n")
    hang.chmod(0o755)
    t0 = time.monotonic()
    out = subprocess.run([str(selftest), "controller", "--engine", str(hang), "--timeout-ms", "1000"],
                         capture_output=True, text=True, timeout=20)
    elapsed = time.monotonic() - t0
    assert out.returncode == 1  # no frames, as expected
    assert elapsed < 4.0, f"shutdown took {elapsed:.1f}s"
    assert subprocess.run(["pgrep", "-f", str(hang)], capture_output=True).returncode != 0, "engine left running"
