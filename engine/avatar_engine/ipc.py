"""Control transport: Win32 named pipe (user-only DACL, local clients only) or a
Unix socket (0600) for development/tests. Length-prefixed JSON framing."""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

from avatar_engine import protocol


def pipe_name(session: str) -> str:
    if sys.platform == "win32":
        return rf"\\.\pipe\AvatarLive_{session}_ctl"
    return str(Path(tempfile.gettempdir()) / f"avatarlive_{session}_ctl.sock")


class ConnectionClosed(Exception):
    pass


class Connection:
    """Thread-safe send; recv from a single reader thread."""

    def __init__(self) -> None:
        self._send_lock = threading.Lock()
        self.peer_pid: int | None = None

    def _read_exact(self, n: int) -> bytes:  # pragma: no cover - abstract
        raise NotImplementedError

    def _write_all(self, data: bytes) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def recv(self) -> dict[str, Any]:
        n = protocol.decode_length(self._read_exact(4))
        return protocol.decode_body(self._read_exact(n) if n else b"")

    def send(self, msg: dict[str, Any]) -> None:
        data = protocol.encode(msg)
        with self._send_lock:
            self._write_all(data)

    def close(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError


class SocketConnection(Connection):
    def __init__(self, sock: socket.socket) -> None:
        super().__init__()
        self.sock = sock
        try:
            import struct as _s

            creds = sock.getsockopt(socket.SOL_SOCKET, getattr(socket, "SO_PEERCRED", 17), _s.calcsize("3i"))
            self.peer_pid = _s.unpack("3i", creds)[0]
        except (OSError, AttributeError):
            self.peer_pid = None

    def _read_exact(self, n: int) -> bytes:
        chunks = []
        while n:
            try:
                b = self.sock.recv(n)
            except OSError as exc:
                raise ConnectionClosed(str(exc)) from exc
            if not b:
                raise ConnectionClosed("peer closed")
            chunks.append(b)
            n -= len(b)
        return b"".join(chunks)

    def _write_all(self, data: bytes) -> None:
        try:
            self.sock.sendall(data)
        except OSError as exc:
            raise ConnectionClosed(str(exc)) from exc

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


class _UnixServer:
    def __init__(self, session: str) -> None:
        self.path = pipe_name(session)
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            self.sock.bind(self.path)
        finally:
            os.umask(old)
        os.chmod(self.path, 0o600)
        self.sock.listen(1)
        self.sock.settimeout(0.5)
        self._closed = False

    def accept(self) -> Connection | None:
        """Returns None on timeout so the caller can check for shutdown."""
        try:
            s, _ = self.sock.accept()
        except socket.timeout:
            return None
        except OSError:
            if self._closed:
                return None
            raise
        s.settimeout(None)
        return SocketConnection(s)

    def close(self) -> None:
        self._closed = True
        self.sock.close()
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


if sys.platform == "win32":  # pragma: no cover - exercised on Windows CI
    import _winapi
    import ctypes
    from ctypes import wintypes

    from avatar_engine.winsec import UserOnlySecurity, kernel32

    PIPE_ACCESS_DUPLEX = 0x3
    FILE_FLAG_OVERLAPPED = 0x40000000
    FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
    PIPE_TYPE_BYTE = 0x0
    PIPE_WAIT = 0x0
    PIPE_REJECT_REMOTE_CLIENTS = 0x8
    ERROR_PIPE_CONNECTED = 535
    ERROR_IO_PENDING = 997

    kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
    kernel32.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                          wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    kernel32.GetNamedPipeClientProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
    kernel32.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
    kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]

    class PipeConnection(Connection):
        def __init__(self, handle: int, server_side: bool = False) -> None:
            super().__init__()
            self.handle = handle
            self.server_side = server_side
            pid = wintypes.ULONG(0)
            if kernel32.GetNamedPipeClientProcessId(handle, ctypes.byref(pid)):
                self.peer_pid = pid.value
            self._pending: Any = None

        def _read_exact(self, n: int) -> bytes:
            chunks = []
            while n:
                try:
                    ov, err = _winapi.ReadFile(self.handle, n, overlapped=True)
                    self._pending = ov
                    nread, err = ov.GetOverlappedResult(True)
                except OSError as exc:
                    raise ConnectionClosed(str(exc)) from exc
                finally:
                    self._pending = None
                data = ov.getbuffer()
                if not data:
                    raise ConnectionClosed("pipe closed")
                chunks.append(bytes(data))
                n -= len(data)
            return b"".join(chunks)

        def _write_all(self, data: bytes) -> None:
            view = memoryview(data)
            while view:
                try:
                    ov, err = _winapi.WriteFile(self.handle, view, overlapped=True)
                    written, err = ov.GetOverlappedResult(True)
                except OSError as exc:
                    raise ConnectionClosed(str(exc)) from exc
                if written <= 0:
                    raise ConnectionClosed("write failed")
                view = view[written:]

        def close(self) -> None:
            if self.handle:
                if self._pending is not None:
                    try:
                        self._pending.cancel()
                    except OSError:
                        pass
                if self.server_side:
                    # DisconnectNamedPipe discards unread data: let the client drain the last
                    # reply first (bounded, so a stuck client cannot hang the engine).
                    h = self.handle
                    t = threading.Thread(target=lambda: kernel32.FlushFileBuffers(h), daemon=True)
                    t.start()
                    t.join(1.0)
                    kernel32.DisconnectNamedPipe(self.handle)
                _winapi.CloseHandle(self.handle)
                self.handle = 0

    class _PipeServer:
        def __init__(self, session: str) -> None:
            self.name = pipe_name(session)
            self._sec = UserOnlySecurity()
            self._first = True
            self._closed = False

        def _create(self) -> int:
            flags = PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED
            if self._first:
                flags |= FILE_FLAG_FIRST_PIPE_INSTANCE  # refuse to share a name squatted by another process
            h = kernel32.CreateNamedPipeW(self.name, flags, PIPE_TYPE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
                                          1, 1 << 16, 1 << 16, 0, self._sec.pointer)
            if not h or h == wintypes.HANDLE(-1).value:
                raise OSError(ctypes.get_last_error(), "CreateNamedPipeW failed")
            self._first = False
            return h

        def accept(self) -> Connection | None:
            if self._closed:
                return None
            h = self._create()
            ov = _winapi.ConnectNamedPipe(h, overlapped=True)
            while True:
                res = _winapi.WaitForMultipleObjects([ov.event], False, 500)
                if res == _winapi.WAIT_TIMEOUT:
                    if self._closed:
                        ov.cancel()
                        _winapi.CloseHandle(h)
                        return None
                    continue
                try:
                    ov.GetOverlappedResult(True)
                except OSError:
                    _winapi.CloseHandle(h)
                    return None
                return PipeConnection(h, server_side=True)

        def close(self) -> None:
            self._closed = True
            self._sec.close()


def create_server(session: str):
    if sys.platform == "win32":
        return _PipeServer(session)  # noqa: F821
    return _UnixServer(session)


def connect(session: str, timeout: float = 5.0) -> Connection:
    """Client side (used by tests and the `selftest` CLI)."""
    if sys.platform == "win32":  # pragma: no cover
        import time

        deadline = time.monotonic() + timeout
        while True:
            try:
                h = _winapi.CreateFile(pipe_name(session), _winapi.GENERIC_READ | _winapi.GENERIC_WRITE, 0,
                                       _winapi.NULL, _winapi.OPEN_EXISTING, _winapi.FILE_FLAG_OVERLAPPED,
                                       _winapi.NULL)
                return PipeConnection(h)
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
    import time

    deadline = time.monotonic() + timeout
    while True:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(pipe_name(session))
            return SocketConnection(s)
        except (FileNotFoundError, ConnectionRefusedError):
            s.close()
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)
