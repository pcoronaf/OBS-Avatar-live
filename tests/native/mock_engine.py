# OBS Avatar Live - minimal protocol mock (stdlib only, POSIX dev transport).
# SPDX-License-Identifier: GPL-2.0-or-later
"""Reference writer/server for docs/PROTOCOL.md used to test the C++ plugin core.

Implements the frame ring writer (seqlock) over POSIX shared memory and the
length-prefixed JSON control channel over a Unix socket. It is *not* the real
engine; it only produces a gradient test pattern with the synthetic flag set.

    python3 mock_engine.py --session <hex> --token <hex> [--frames N] [--fps F]
    python3 mock_engine.py serve --session <hex> --parent-pid <pid> [--frames N]
        (launcher-compatible form; token from AVATAR_LIVE_AUTH_TOKEN; exits after
        N frames to simulate an engine crash)
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import struct
import threading
import time
from multiprocessing import shared_memory

MAGIC = 0x4C415641
HEADER_SIZE = 256
SLOT_HEADER_SIZE = 128
NO_SLOT = 0xFFFFFFFF
FLAG_SYNTHETIC = 1


def ctl_path(session: str) -> str:
    tmp = os.environ.get("TMPDIR") or "/tmp"
    return os.path.join(tmp.rstrip("/"), f"avatarlive_{session}_ctl.sock")


class RingWriter:
    def __init__(self, session: str, max_w: int, max_h: int, slots: int = 4):
        self.slots = slots
        self.capacity = ((max_w * 4 * max_h) + 63) // 64 * 64
        size = HEADER_SIZE + slots * (SLOT_HEADER_SIZE + self.capacity)
        self.shm = shared_memory.SharedMemory(name=f"avatarlive_{session}_frames", create=True, size=size)
        self.buf = self.shm.buf
        self.generation = 1
        self.latest = NO_SLOT
        self.seq = 0
        struct.pack_into("<IIIIII", self.buf, 0, MAGIC, 1, HEADER_SIZE, slots, SLOT_HEADER_SIZE, 0)
        struct.pack_into("<QII", self.buf, 24, self.capacity, max_w, max_h)
        struct.pack_into("<II", self.buf, 40, NO_SLOT, os.getpid())
        struct.pack_into("<QQQI", self.buf, 48, 0, self.generation, time.perf_counter_ns(), 1)

    def slot_off(self, i: int) -> int:
        return HEADER_SIZE + i * (SLOT_HEADER_SIZE + self.capacity)

    def write(self, w: int, h: int, pixels: bytes, flags: int = FLAG_SYNTHETIC, generation: int | None = None):
        slot = 0 if self.latest == NO_SLOT else (self.latest + 1) % self.slots
        off = self.slot_off(slot)
        (lock,) = struct.unpack_from("<Q", self.buf, off)
        struct.pack_into("<Q", self.buf, off, lock + 1)  # odd: writing
        stride = w * 4
        self.buf[off + SLOT_HEADER_SIZE: off + SLOT_HEADER_SIZE + stride * h] = pixels
        self.seq += 1
        gen = self.generation if generation is None else generation
        struct.pack_into("<QQQIIIIIII", self.buf, off + 8, self.seq, time.perf_counter_ns(), gen,
                         w, h, stride, 1, flags, 0, stride * h)
        struct.pack_into("<Q", self.buf, off, lock + 2)  # even: stable
        struct.pack_into("<Q", self.buf, 48, self.seq)
        struct.pack_into("<I", self.buf, 40, slot)
        self.latest = slot
        struct.pack_into("<Q", self.buf, 64, time.perf_counter_ns())

    def close(self):
        try:
            self.shm.close()
            self.shm.unlink()
        except FileNotFoundError:
            pass


def send(conn: socket.socket, obj: dict) -> None:
    data = json.dumps(obj).encode()
    conn.sendall(struct.pack("<I", len(data)) + data)


def recv(conn: socket.socket) -> dict | None:
    hdr = b""
    while len(hdr) < 4:
        chunk = conn.recv(4 - len(hdr))
        if not chunk:
            return None
        hdr += chunk
    (n,) = struct.unpack("<I", hdr)
    body = b""
    while len(body) < n:
        chunk = conn.recv(n - len(body))
        if not chunk:
            return None
        body += chunk
    return json.loads(body)


def serve_control(session: str, token: str, ring: RingWriter, stop: threading.Event) -> None:
    path = ctl_path(session)
    if os.path.exists(path):
        os.unlink(path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    srv.bind(path)
    os.umask(old)
    srv.listen(1)
    srv.settimeout(0.2)
    try:
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            with conn:
                authed = False
                while not stop.is_set():
                    msg = recv(conn)
                    if msg is None:
                        break
                    rid, typ, payload = msg.get("id", 0), msg.get("type"), msg.get("payload", {})
                    if not authed:
                        if typ != "hello" or payload.get("auth_token") != token:
                            send(conn, {"v": 1, "id": rid, "type": "reply", "ok": False,
                                        "error": {"code": "unauthorized", "message": "bad hello"}})
                            break
                        authed = True
                        send(conn, {"v": 1, "id": rid, "type": "reply", "ok": True, "payload": {
                            "engine_version": "mock-0", "protocol_version": 1, "engines": ["mock"],
                            "frame_ring": {"name": f"/avatarlive_{session}_frames", "event_name": "",
                                           "slot_count": ring.slots, "max_width": 64, "max_height": 32}}})
                        send(conn, {"v": 1, "id": 0, "type": "event", "event": "state",
                                    "payload": {"state": "idle", "detail": "mock"}})
                        continue
                    if typ == "ping":
                        out = {"now_ns": time.perf_counter_ns()}
                    elif typ == "get_metrics":
                        out = {"engine": "mock", "state": "running", "output_fps": 30.0,
                               "face_swap_unique_fps": None, "synthetic_label": True}
                    elif typ == "start_session":
                        out = {"session_id": "mock", "generation_id": ring.generation, "engine": "mock",
                               "provider": "CPUExecutionProvider"}
                    elif typ == "shutdown":
                        send(conn, {"v": 1, "id": rid, "type": "reply", "ok": True, "payload": {}})
                        stop.set()
                        break
                    else:
                        out = {}
                    send(conn, {"v": 1, "id": rid, "type": "reply", "ok": True, "payload": out})
    finally:
        srv.close()
        if os.path.exists(path):
            os.unlink(path)


def pattern(w: int, h: int, seq: int) -> bytes:
    row = bytearray()
    for x in range(w):
        row += bytes(((x * 4 + seq) & 0xFF, (seq * 3) & 0xFF, 0x80, 0xFF))
    return bytes(row) * h


def main() -> int:
    import sys
    argv = sys.argv[1:]
    serve_mode = bool(argv) and argv[0] == "serve"
    if serve_mode:
        argv = argv[1:]
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--token", default=os.environ.get("AVATAR_LIVE_AUTH_TOKEN"))
    ap.add_argument("--parent-pid", type=int, default=0)
    ap.add_argument("--frames", type=int, default=30)
    ap.add_argument("--fps", type=float, default=60.0)
    ap.add_argument("--width", type=int, default=64)
    ap.add_argument("--height", type=int, default=32)
    ap.add_argument("--unlabeled-every", type=int, default=0, help="emit a frame without synthetic flag every N")
    args = ap.parse_args(argv)
    if not args.token:
        ap.error("--token or AVATAR_LIVE_AUTH_TOKEN required")
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit(0)))

    ring = RingWriter(args.session, 64, 32)
    stop = threading.Event()
    t = threading.Thread(target=serve_control, args=(args.session, args.token, ring, stop), daemon=True)
    t.start()
    print("READY", flush=True)
    try:
        for i in range(args.frames):
            if stop.is_set():
                break
            flags = FLAG_SYNTHETIC
            if args.unlabeled_every and (i + 1) % args.unlabeled_every == 0:
                flags = 0
            ring.write(args.width, args.height, pattern(args.width, args.height, i + 1), flags)
            time.sleep(1.0 / args.fps)
        if not serve_mode:
            stop.wait(timeout=5.0)
    finally:
        stop.set()
        t.join(timeout=2)
        ring.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
