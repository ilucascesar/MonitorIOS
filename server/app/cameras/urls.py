"""Montagem de URLs RTSP com credenciais em tempo de execução (nunca persistidas em claro)."""
from __future__ import annotations

from urllib.parse import quote, urlsplit, urlunsplit

from ..security import secrets as vault


def strip_credentials(url: str) -> str:
    if not url:
        return ""
    p = urlsplit(url)
    if p.username is None and p.password is None:
        return url
    host = p.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = host + (f":{p.port}" if p.port else "")
    return urlunsplit((p.scheme, netloc, p.path, p.query, p.fragment))


def with_credentials(url: str, username: str, password: str) -> str:
    if not url or not username:
        return url
    p = urlsplit(url)
    if p.username:
        return url
    cred = quote(username, safe="") + (":" + quote(password, safe="") if password else "")
    return urlunsplit((p.scheme, f"{cred}@{p.netloc}", p.path, p.query, p.fragment))


def build_stream_url(cam: dict, which: str = "main") -> str:
    url = cam.get("sub_url") if which == "sub" else cam.get("main_url")
    if which == "sub" and not url:
        url = cam.get("main_url")
    if not url:
        return ""
    password = vault.decrypt(cam.get("password_enc") or "") if cam.get("password_enc") else ""
    return with_credentials(url, cam.get("username") or "", password)
