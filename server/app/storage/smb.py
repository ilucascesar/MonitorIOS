"""Conexão autenticada ao compartilhamento SMB do roteador (WNetAddConnection2).

O serviço roda como LocalSystem, que não herda as unidades mapeadas nem as credenciais
salvas do usuário; por isso o servidor abre a sessão SMB explicitamente com o usuário e a
senha configurados (senha cifrada com DPAPI no banco).
"""
from __future__ import annotations

import logging
import sys

log = logging.getLogger("storage.smb")

ERROR_MESSAGES = {
    5: "acesso negado",
    53: "caminho de rede não encontrado (roteador indisponível?)",
    67: "nome do compartilhamento não encontrado (pendrive desconectado?)",
    86: "senha de rede incorreta",
    1219: "já existe conexão com este servidor usando outro usuário",
    1326: "usuário ou senha incorretos",
    1450: "recursos insuficientes (o roteador recusou a sessão SMB)",
    2202: "nome de usuário inválido",
}


def connect(share: str, username: str, password: str) -> tuple[bool, str]:
    if sys.platform != "win32":
        return False, "SMB autenticado só é suportado no Windows"
    import ctypes
    from ctypes import wintypes

    class NETRESOURCE(ctypes.Structure):
        _fields_ = [
            ("dwScope", wintypes.DWORD), ("dwType", wintypes.DWORD), ("dwDisplayType", wintypes.DWORD),
            ("dwUsage", wintypes.DWORD), ("lpLocalName", wintypes.LPWSTR), ("lpRemoteName", wintypes.LPWSTR),
            ("lpComment", wintypes.LPWSTR), ("lpProvider", wintypes.LPWSTR),
        ]

    RESOURCETYPE_DISK = 1
    nr = NETRESOURCE()
    nr.dwType = RESOURCETYPE_DISK
    nr.lpRemoteName = share
    mpr = ctypes.WinDLL("mpr")
    rc = mpr.WNetAddConnection2W(ctypes.byref(nr), password or None, username or None, 0)
    if rc == 0:
        return True, "conectado"
    if rc == 1219:
        # Já existe sessão (ex.: com as mesmas credenciais); considera utilizável.
        return True, ERROR_MESSAGES[1219]
    return False, f"erro {rc}: {ERROR_MESSAGES.get(rc, 'falha ao conectar')}"


def disconnect(share: str) -> None:
    if sys.platform != "win32":
        return
    import ctypes

    ctypes.WinDLL("mpr").WNetCancelConnection2W(share, 0, True)
