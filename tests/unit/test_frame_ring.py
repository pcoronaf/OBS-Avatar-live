import struct

import numpy as np
import pytest

from avatar_engine import frame_ring as fr


def test_header_layout(session_name):
    w = fr.FrameRingWriter(session_name, 64, 32, slot_count=4)
    try:
        b = w.buf
        assert struct.unpack_from("<IIIII", b, 0) == (0x4C415641, 1, 256, 4, 128)
        assert bytes(b[0:4]) == b"AVAL"
        cap, mw, mh = struct.unpack_from("<QII", b, 24)
        assert (mw, mh) == (64, 32) and cap % 64 == 0 and cap >= 64 * 32 * 4
        assert struct.unpack_from("<I", b, 40)[0] == fr.NO_SLOT
    finally:
        w.close()


def test_roundtrip_and_slot_rotation(session_name):
    w = fr.FrameRingWriter(session_name, 64, 32)
    r = fr.FrameRingReader(session_name)
    try:
        assert r.read_latest() is None
        slots = []
        for i in range(6):
            img = np.full((32, 64, 4), i, np.uint8)
            w.write(img, flags=fr.FLAG_REPEATED)
            slots.append(w.latest_slot)
            f = r.read_latest()
            assert f.sequence == i + 1
            assert int(f.pixels.mean()) == i
            assert f.flags & fr.FLAG_SYNTHETIC and f.flags & fr.FLAG_REPEATED
        assert slots[:5] == [0, 1, 2, 3, 0]
        assert r.read_latest() is None  # nothing new
    finally:
        r.close()
        w.close()


def test_synthetic_flag_is_forced(session_name):
    w = fr.FrameRingWriter(session_name, 16, 16)
    r = fr.FrameRingReader(session_name)
    try:
        w.write(np.zeros((16, 16, 4), np.uint8), flags=0)
        assert r.read_latest().flags & fr.FLAG_SYNTHETIC
    finally:
        r.close()
        w.close()


def test_torn_and_stale_generation_rejected(session_name):
    w = fr.FrameRingWriter(session_name, 16, 16)
    r = fr.FrameRingReader(session_name)
    try:
        w.write(np.zeros((16, 16, 4), np.uint8))
        off = fr.HEADER_SIZE + w.latest_slot * (fr.SLOT_HEADER_SIZE + w.capacity)
        lock = struct.unpack_from("<Q", w.buf, off)[0]
        struct.pack_into("<Q", w.buf, off, lock + 1)  # pretend writer is mid-write
        assert r.read_latest() is None
        struct.pack_into("<Q", w.buf, off, lock)
        w.set_generation(99)  # frame belongs to an old generation
        assert r.read_latest() is None
    finally:
        r.close()
        w.close()


def test_rejects_oversize(session_name):
    w = fr.FrameRingWriter(session_name, 16, 16)
    try:
        with pytest.raises(ValueError):
            w.write(np.zeros((32, 16, 4), np.uint8))
    finally:
        w.close()
