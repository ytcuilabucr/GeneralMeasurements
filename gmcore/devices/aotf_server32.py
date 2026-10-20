"""Runs inside msl-loadlib's 32-bit server: the vendor's AotfLibrary.dll is
32-bit only, and this program is 64-bit. Imports nothing from gmcore (the
32-bit server has only the standard library and msl.loadlib).

AotfLibrary.h (Crystal Technology, 2004), all C calling convention:

    HANDLE AotfOpen (int iInstance);
    BOOL   AotfClose (HANDLE hAotfController);
    BOOL   AotfWrite (HANDLE hAotfController, UINT uiLength, PVOID pData);
    BOOL   AotfRead (HANDLE hAotfController, UINT uiLength, PVOID pData, PUINT puiBytesRead);
    BOOL   AotfIsReadDataAvailable (HANDLE hAotfController);

Reading follows the vendor's example (AotfLibraryTest.cpp): one byte at a
time while AotfIsReadDataAvailable says there is more.
"""

import ctypes
from ctypes import c_int, c_uint, c_void_p

from msl.loadlib import Server32  # pyright: ignore[reportMissingImports]


class AotfServer32(Server32):
    def __init__(self, host, port, **kwargs):
        super().__init__(kwargs["dll"], "cdll", host, port)
        lib = self.lib
        lib.AotfOpen.restype, lib.AotfOpen.argtypes = c_void_p, [c_int]
        lib.AotfClose.restype, lib.AotfClose.argtypes = c_int, [c_void_p]
        lib.AotfWrite.restype = c_int
        lib.AotfWrite.argtypes = [c_void_p, c_uint, ctypes.c_char_p]
        lib.AotfRead.restype = c_int
        lib.AotfRead.argtypes = [c_void_p, c_uint, ctypes.c_char_p, ctypes.POINTER(c_uint)]
        lib.AotfIsReadDataAvailable.restype = c_int
        lib.AotfIsReadDataAvailable.argtypes = [c_void_p]
        self.handle = None

    def open(self, instance):
        """True when controller `instance` (0, 1, ...) answered AotfOpen."""
        self.handle = self.lib.AotfOpen(int(instance)) or None
        return self.handle is not None

    def write(self, data):
        if self.handle is None:
            raise RuntimeError("AOTF controller not open")
        if not self.lib.AotfWrite(self.handle, len(data), data):
            raise RuntimeError("AotfWrite failed")
        return True

    def read_available(self, max_bytes=65536):
        """Everything the controller has sent so far (b"" when nothing)."""
        if self.handle is None:
            raise RuntimeError("AOTF controller not open")
        out = bytearray()
        buf = ctypes.create_string_buffer(1)
        n = c_uint(0)
        while len(out) < max_bytes and self.lib.AotfIsReadDataAvailable(self.handle):
            if not self.lib.AotfRead(self.handle, 1, buf, ctypes.byref(n)):
                raise RuntimeError("AotfRead failed")
            if n.value:
                out += buf.raw[:n.value]
        return bytes(out)

    def close(self):
        if self.handle is not None:
            self.lib.AotfClose(self.handle)
            self.handle = None
        return True
