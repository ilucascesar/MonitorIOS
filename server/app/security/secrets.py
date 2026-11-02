"""Cifragem de segredos em repouso.

No Windows usa DPAPI com escopo de máquina (CRYPTPROTECT_LOCAL_MACHINE), para que tanto o
serviço (LocalSystem) quanto a CLI executada pelo administrador consigam decifrar. A pasta de
dados tem ACL restrita a SYSTEM e Administradores (ver install.ps1).

Em outros sistemas (somente desenvolvimento/testes) cai num modo "obfuscado" marcado com o
prefixo ``plain:`` e um aviso no log — nunca use isso em produção.
"""
from __future__ import annotations

import base64
import logging
import sys

log = logging.getLogger("security.secrets")

_ENTROPY = b"MonitorIOS-v1-secret-entropy"

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _CRYPTPROTECT_UI_FORBIDDEN = 0x1
    _CRYPTPROTECT_LOCAL_MACHINE = 0x4

    def _blob(data: bytes) -> _BLOB:
        buf = ctypes.create_string_buffer(data, len(data))
        b = _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        b._buf = buf  # mantém referência viva
        return b

    def _protect(data: bytes) -> bytes:
        inp, ent, out = _blob(data), _blob(_ENTROPY), _BLOB()
        ok = _crypt32.CryptProtectData(
            ctypes.byref(inp), "MonitorIOS", ctypes.byref(ent), None, None,
            _CRYPTPROTECT_UI_FORBIDDEN | _CRYPTPROTECT_LOCAL_MACHINE, ctypes.byref(out),
        )
        if not ok:
            raise OSError(ctypes.get_last_error(), "CryptProtectData falhou")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _kernel32.LocalFree(out.pbData)

    def _unprotect(data: bytes) -> bytes:
        inp, ent, out = _blob(data), _blob(_ENTROPY), _BLOB()
        ok = _crypt32.CryptUnprotectData(
            ctypes.byref(inp), None, ctypes.byref(ent), None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)
        )
        if not ok:
            raise OSError(ctypes.get_last_error(), "CryptUnprotectData falhou")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _kernel32.LocalFree(out.pbData)


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    raw = plaintext.encode("utf-8")
    if sys.platform == "win32":
        return "dpapi:" + base64.b64encode(_protect(raw)).decode("ascii")
    log.warning("DPAPI indisponível: segredo armazenado sem cifragem forte (apenas desenvolvimento)")
    return "plain:" + base64.b64encode(raw).decode("ascii")


def decrypt(token: str) -> str:
    if not token:
        return ""
    scheme, _, b64 = token.partition(":")
    data = base64.b64decode(b64)
    if scheme == "dpapi":
        if sys.platform != "win32":
            raise RuntimeError("segredo DPAPI só pode ser lido no Windows")
        return _unprotect(data).decode("utf-8")
    if scheme == "plain":
        return data.decode("utf-8")
    raise ValueError("formato de segredo desconhecido")
