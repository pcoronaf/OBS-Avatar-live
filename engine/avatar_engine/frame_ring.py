"""Shared-memory frame ring (writer + reader). Layout: docs/PROTOCOL.md §2.

The engine writes BGRA8 frames into a 3–8 slot ring guarded by per-slot
seqlocks; the OBS plugin copies the newest stable slot without blocking.
"""

from __future__ import annotations

import os
import struct
import sys
import threading
import time
from dataclasses import dataclass

import numpy as np

MAGIC = 0x4C415641
VERSION = 1
HEADER_SIZE = 256
SLOT_HEADER_SIZE = 128
PIXEL_FORMAT_BGRA8 = 1
NO_SLOT = 0xFFFFFFFF

FLAG_SYNTHETIC = 1 << 0
FLAG_REPEATED = 1 << 1
FLAG_DEGRADED = 1 << 2
FLAG_PAUSED = 1 << 3

RING_RUNNING = 1 << 0
RING_PAUSED = 1 << 1
RING_DEGRADED = 1 << 2

# Ring header offsets
H_MAGIC, H_VERSION, H_HEADER_SIZE, H_SLOT_COUNT, H_SLOT_HDR = 0, 4, 8, 12, 16
H_CAPACITY, H_MAX_W, H_MAX_H, H_LATEST_SLOT, H_PID = 24, 32, 36, 40, 44
H_LATEST_SEQ, H_GENERATION, H_HEARTBEAT, H_FLAGS = 48, 56, 64, 72

# Slot header offsets
S_SEQLOCK, S_SEQUENCE, S_TS, S_GEN = 0, 8, 16, 24
S_W, S_H, S_STRIDE, S_FMT, S_FLAGS, S_LAT, S_SIZE = 32, 36, 40, 44, 48, 52, 56


def ring_name(session: str) -> str:
    return f"Local\\AvatarLive_{session}_frames" if sys.platform == "win32" else f"avatarlive_{session}_frames"


def event_name(session: str) -> str:
    return f"Local\\AvatarLive_{session}_ready"


def now_ns() -> int:
    """Monotonic clock shared with the plugin (QPC on Windows)."""
    return time.perf_counter_ns()


def _align(n: int, a: int = 64) -> int:
    return (n + a - 1) // a * a


def ring_size(slot_count: int, capacity: int) -> int:
    return HEADER_SIZE + slot_count * (SLOT_HEADER_SIZE + capacity)


class _Shm:
    """Platform shared memory: named Win32 mapping or POSIX shm (0600)."""

    def __init__(self, name: str, size: int, create: bool) -> None:
        self.name = name
        self._posix = None
        self._win = None
        if sys.platform == "win32":
            from avatar_engine.winsec import NamedMapping

            self._win = NamedMapping(name, size, create)
            self.buf = self._win.buf
        else:
            from multiprocessing import shared_memory

            kwargs = {"track": False} if sys.version_info >= (3, 13) else {}
            self._posix = shared_memory.SharedMemory(name=name, create=create, size=size if create else 0,
                                                     **kwargs)
            if sys.version_info < (3, 13):  # avoid resource_tracker unlinking the writer's segment
                try:
                    from multiprocessing import resource_tracker

                    resource_tracker.unregister(self._posix._name, "shared_memory")  # type: ignore[attr-defined]
                except Exception:
                    pass
            self.buf = self._posix.buf

    def close(self, unlink: bool = False) -> None:
        if self._win is not None:
            self._win.close()
            self._win = None
        if self._posix is not None:
            self.buf = None
            self._posix.close()
            if unlink:
                try:
                    self._posix.unlink()
                except FileNotFoundError:
                    pass
            self._posix = None


@dataclass
class Frame:
    sequence: int
    timestamp_ns: int
    generation_id: int
    width: int
    height: int
    stride: int
    flags: int
    processing_latency_us: int
    pixels: np.ndarray  # (h, w, 4) uint8 BGRA copy


class FrameRingWriter:
    def __init__(self, session: str, max_width: int = 1920, max_height: int = 1080, slot_count: int = 4) -> None:
        if not 3 <= slot_count <= 8:
            raise ValueError("slot_count must be 3..8")
        self.session = session
        self.slot_count = slot_count
        self.max_width = max_width
        self.max_height = max_height
        self.capacity = _align(max_width * 4 * max_height)
        self.size = ring_size(slot_count, self.capacity)
        self._shm = _Shm(ring_name(session), self.size, create=True)
        self.buf = self._shm.buf
        self.buf[:HEADER_SIZE] = bytes(HEADER_SIZE)
        struct.pack_into("<IIIII", self.buf, 0, MAGIC, VERSION, HEADER_SIZE, slot_count, SLOT_HEADER_SIZE)
        struct.pack_into("<QII", self.buf, H_CAPACITY, self.capacity, max_width, max_height)
        struct.pack_into("<II", self.buf, H_LATEST_SLOT, NO_SLOT, os.getpid())
        for i in range(slot_count):
            off = self._slot_off(i)
            self.buf[off:off + SLOT_HEADER_SIZE] = bytes(SLOT_HEADER_SIZE)
        self._event = None
        if sys.platform == "win32":
            from avatar_engine.winsec import NamedEvent

            self._event = NamedEvent(event_name(session))
        self._lock = threading.Lock()
        self.sequence = 0
        self.latest_slot = NO_SLOT
        self.generation_id = 1
        self.set_generation(1)
        self.heartbeat()

    def _slot_off(self, i: int) -> int:
        return HEADER_SIZE + i * (SLOT_HEADER_SIZE + self.capacity)

    def set_generation(self, generation_id: int) -> None:
        self.generation_id = generation_id
        struct.pack_into("<Q", self.buf, H_GENERATION, generation_id)

    def set_ring_flags(self, flags: int) -> None:
        struct.pack_into("<I", self.buf, H_FLAGS, flags)

    def heartbeat(self) -> None:
        struct.pack_into("<Q", self.buf, H_HEARTBEAT, now_ns())

    def write(self, bgra: np.ndarray, flags: int = FLAG_SYNTHETIC, timestamp_ns: int | None = None,
              processing_latency_us: int = 0) -> int:
        if bgra.dtype != np.uint8 or bgra.ndim != 3 or bgra.shape[2] != 4:
            raise ValueError("frame must be HxWx4 uint8 BGRA")
        h, w = bgra.shape[:2]
        if w > self.max_width or h > self.max_height:
            raise ValueError("frame exceeds ring max dimensions")
        flags |= FLAG_SYNTHETIC  # disclosure flag is mandatory (spec §8, ADR-006)
        with self._lock:
            return self._write_locked(bgra, flags, timestamp_ns, processing_latency_us)

    def _write_locked(self, bgra, flags, timestamp_ns, processing_latency_us) -> int:
        h, w = bgra.shape[:2]
        stride = w * 4
        slot = 0 if self.latest_slot == NO_SLOT else (self.latest_slot + 1) % self.slot_count
        off = self._slot_off(slot)
        (lock,) = struct.unpack_from("<Q", self.buf, off + S_SEQLOCK)
        if lock & 1:
            lock += 1
        struct.pack_into("<Q", self.buf, off + S_SEQLOCK, lock + 1)  # odd: writing
        data_off = off + SLOT_HEADER_SIZE
        dst = np.frombuffer(self.buf, dtype=np.uint8, count=stride * h, offset=data_off).reshape(h, stride)
        dst[:] = np.ascontiguousarray(bgra).reshape(h, stride)
        del dst
        self.sequence += 1
        ts = now_ns() if timestamp_ns is None else timestamp_ns
        struct.pack_into("<QQQIIIIIII", self.buf, off + S_SEQUENCE, self.sequence, ts, self.generation_id,
                         w, h, stride, PIXEL_FORMAT_BGRA8, flags, min(processing_latency_us, 0xFFFFFFFF),
                         stride * h)
        struct.pack_into("<Q", self.buf, off + S_SEQLOCK, lock + 2)  # even: stable
        struct.pack_into("<Q", self.buf, H_LATEST_SEQ, self.sequence)
        struct.pack_into("<I", self.buf, H_LATEST_SLOT, slot)
        self.latest_slot = slot
        self.heartbeat()
        if self._event is not None:
            self._event.set()
        return self.sequence

    def close(self) -> None:
        if self._event is not None:
            self._event.close()
            self._event = None
        if self._shm is not None:
            self.buf = None
            self._shm.close(unlink=True)
            self._shm = None


class FrameRingReader:
    """Reference reader (mirrors the plugin's C++ reader); used by tests and preview."""

    def __init__(self, session: str) -> None:
        header_probe = _Shm(ring_name(session), HEADER_SIZE, create=False)
        magic, version, hsize, count, shsize = struct.unpack_from("<IIIII", header_probe.buf, 0)
        (capacity,) = struct.unpack_from("<Q", header_probe.buf, H_CAPACITY)
        header_probe.close()
        if magic != MAGIC or version != VERSION or hsize != HEADER_SIZE or shsize != SLOT_HEADER_SIZE:
            raise ValueError("incompatible frame ring")
        self.slot_count = count
        self.capacity = capacity
        self._shm = _Shm(ring_name(session), ring_size(count, capacity), create=False)
        self.buf = self._shm.buf
        self.last_sequence = 0

    def header(self) -> dict:
        max_w, max_h, latest_slot, pid = struct.unpack_from("<IIII", self.buf, H_MAX_W)
        latest_seq, gen, hb = struct.unpack_from("<QQQ", self.buf, H_LATEST_SEQ)
        (flags,) = struct.unpack_from("<I", self.buf, H_FLAGS)
        return {"max_width": max_w, "max_height": max_h, "latest_slot": latest_slot, "writer_pid": pid,
                "latest_sequence": latest_seq, "generation_id": gen, "heartbeat_ns": hb, "ring_flags": flags}

    def read_latest(self) -> Frame | None:
        hdr = self.header()
        slot = hdr["latest_slot"]
        if slot == NO_SLOT or slot >= self.slot_count or hdr["latest_sequence"] == self.last_sequence:
            return None
        off = HEADER_SIZE + slot * (SLOT_HEADER_SIZE + self.capacity)
        (s1,) = struct.unpack_from("<Q", self.buf, off)
        if s1 & 1:
            return None
        seq, ts, gen, w, h, stride, fmt, flags, lat, size = struct.unpack_from("<QQQIIIIIII", self.buf,
                                                                              off + S_SEQUENCE)
        if fmt != PIXEL_FORMAT_BGRA8 or w > hdr["max_width"] or h > hdr["max_height"] or stride < w * 4:
            return None
        data = np.frombuffer(self.buf, dtype=np.uint8, count=stride * h, offset=off + SLOT_HEADER_SIZE)
        pixels = data.reshape(h, stride)[:, : w * 4].reshape(h, w, 4).copy()
        del data
        (s2,) = struct.unpack_from("<Q", self.buf, off)
        if s1 != s2 or gen != hdr["generation_id"] or not flags & FLAG_SYNTHETIC:
            return None
        self.last_sequence = seq
        return Frame(seq, ts, gen, w, h, stride, flags, lat, pixels)

    def close(self) -> None:
        if self._shm is not None:
            self.buf = None
            self._shm.close()
            self._shm = None
