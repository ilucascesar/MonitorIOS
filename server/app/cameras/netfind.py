"""Acha uma câmera pelo MAC quando o roteador troca o IP dela (DHCP sem reserva)."""
from __future__ import annotations

import ipaddress
import socket
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit, urlunsplit


def mac_of(ip: str) -> str:
    """MAC de ``ip`` na rede local, como "00-33-7a-b1-5b-76"; "" se não responder."""
    if sys.platform != "win32":
        return ""  # shortcut: só Windows (SendARP), que é onde o servidor roda
    import ctypes

    try:
        if not ipaddress.ip_address(ip).is_private:
            return ""
        dest = int.from_bytes(socket.inet_aton(ip), "little")  # IPAddr em ordem de rede
    except (ValueError, OSError):
        return ""
    send_arp = ctypes.WinDLL("iphlpapi").SendARP
    send_arp.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    buf = (ctypes.c_ubyte * 8)()
    size = ctypes.c_ulong(8)
    if send_arp(dest, 0, buf, ctypes.byref(size)) != 0 or size.value != 6:
        return ""
    return "-".join(f"{b:02x}" for b in buf[:6])


def find_host(mac: str, near: str, port: int, probe, lookup=mac_of) -> str | None:
    """IP da rede /24 de ``near`` com a porta ``port`` aberta e o MAC ``mac``."""
    try:
        net = ipaddress.ip_network(f"{near}/24", strict=False)
    except ValueError:
        return None  # host é nome, não IP
    if not net.is_private:
        return None
    hosts = [str(h) for h in net.hosts()]
    with ThreadPoolExecutor(64) as ex:
        open_ = [ip for ip, ok in zip(hosts, ex.map(lambda ip: probe(ip, port, 0.5), hosts)) if ok]
    return next((ip for ip in open_ if lookup(ip) == mac), None)


def with_host(url: str, old: str, new: str) -> str:
    """Troca o host ``old`` por ``new`` numa URL rtsp://, mantendo usuário e porta."""
    if not url:
        return url
    p = urlsplit(url)
    if p.hostname != old:
        return url
    userinfo = p.netloc.rpartition("@")[0]
    netloc = (userinfo + "@" if userinfo else "") + new + (f":{p.port}" if p.port else "")
    return urlunsplit(p._replace(netloc=netloc))
