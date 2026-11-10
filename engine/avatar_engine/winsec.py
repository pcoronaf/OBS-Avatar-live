"""Win32 helpers: per-user security attributes, named events, named mappings.

Only imported on Windows. Kernel objects created here grant access only to the
current user SID and SYSTEM (spec §8: "Named Pipes con permisos de sesión").
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

if sys.platform != "win32":  # pragma: no cover - import guard
    raise ImportError("winsec is Windows-only")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

TOKEN_QUERY = 0x0008
TokenUser = 1
PAGE_READWRITE = 0x04
FILE_MAP_ALL_ACCESS = 0x000F001F
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
WAIT_OBJECT_0 = 0
SDDL_REVISION_1 = 1


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.CreateFileMappingW.restype = wintypes.HANDLE
kernel32.CreateFileMappingW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                        wintypes.DWORD, wintypes.LPCWSTR]
kernel32.OpenFileMappingW.restype = wintypes.HANDLE
kernel32.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.MapViewOfFile.restype = ctypes.c_void_p
kernel32.MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
kernel32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.SetEvent.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.LocalFree.argtypes = [ctypes.c_void_p]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.DWORD)]
advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG)]


def _check(ok, what: str):
    if not ok:
        raise OSError(ctypes.get_last_error(), f"{what} failed")
    return ok


def current_user_sid() -> str:
    token = wintypes.HANDLE()
    _check(advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)),
           "OpenProcessToken")
    try:
        size = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, TokenUser, None, 0, ctypes.byref(size))
        buf = ctypes.create_string_buffer(size.value)
        _check(advapi32.GetTokenInformation(token, TokenUser, buf, size, ctypes.byref(size)), "GetTokenInformation")
        psid = ctypes.c_void_p.from_buffer(buf).value  # TOKEN_USER.User.Sid
        sid_str = wintypes.LPWSTR()
        _check(advapi32.ConvertSidToStringSidW(psid, ctypes.byref(sid_str)), "ConvertSidToStringSidW")
        try:
            return sid_str.value
        finally:
            kernel32.LocalFree(sid_str)
    finally:
        kernel32.CloseHandle(token)


class UserOnlySecurity:
    """SECURITY_ATTRIBUTES whose DACL allows only the current user and SYSTEM."""

    def __init__(self) -> None:
        sddl = f"D:P(A;;GA;;;{current_user_sid()})(A;;GA;;;SY)"
        self._sd = ctypes.c_void_p()
        _check(advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, SDDL_REVISION_1, ctypes.byref(self._sd), None), "ConvertStringSecurityDescriptor")
        self.attrs = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), self._sd, False)

    @property
    def pointer(self):
        return ctypes.byref(self.attrs)

    def close(self) -> None:
        if self._sd:
            kernel32.LocalFree(self._sd)
            self._sd = ctypes.c_void_p()


class NamedMapping:
    def __init__(self, name: str, size: int, create: bool) -> None:
        self.size = size
        self._sec = UserOnlySecurity() if create else None
        if create:
            self.handle = kernel32.CreateFileMappingW(INVALID_HANDLE_VALUE, self._sec.pointer, PAGE_READWRITE,
                                                      (size >> 32) & 0xFFFFFFFF, size & 0xFFFFFFFF, name)
            if self.handle and ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
                kernel32.CloseHandle(self.handle)
                raise FileExistsError(name)
        else:
            self.handle = kernel32.OpenFileMappingW(FILE_MAP_ALL_ACCESS, False, name)
        _check(self.handle, "CreateFileMapping/OpenFileMapping")
        self.address = kernel32.MapViewOfFile(self.handle, FILE_MAP_ALL_ACCESS, 0, 0, size)
        _check(self.address, "MapViewOfFile")
        self.buf = memoryview((ctypes.c_char * size).from_address(self.address)).cast("B")

    def close(self) -> None:
        if getattr(self, "buf", None) is not None:
            self.buf.release()
            self.buf = None
        if self.address:
            kernel32.UnmapViewOfFile(self.address)
            self.address = None
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = None
        if self._sec:
            self._sec.close()


class NamedEvent:
    def __init__(self, name: str, create: bool = True) -> None:
        self._sec = UserOnlySecurity()
        self.handle = kernel32.CreateEventW(self._sec.pointer, False, False, name)  # auto-reset
        _check(self.handle, "CreateEventW")

    def set(self) -> None:
        kernel32.SetEvent(self.handle)

    def wait(self, timeout_ms: int) -> bool:
        return kernel32.WaitForSingleObject(self.handle, timeout_ms) == WAIT_OBJECT_0

    def close(self) -> None:
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = None
        self._sec.close()


SYNCHRONIZE = 0x00100000


def process_alive(pid: int) -> bool:
    h = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not h:
        return False
    try:
        return kernel32.WaitForSingleObject(h, 0) != WAIT_OBJECT_0
    finally:
        kernel32.CloseHandle(h)
