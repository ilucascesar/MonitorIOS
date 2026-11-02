"""Dependências comuns: runtime, autenticação, autorização e respostas utilitárias."""
from __future__ import annotations

import gzip
import ipaddress

from fastapi import Depends, HTTPException, Request
from fastapi.responses import Response

from ..runtime import Runtime
from ..security.auth import Principal

COOKIE_NAME = "monitor_session"
CSRF_HEADER = "x-requested-with"


def rt(request: Request) -> Runtime:
    return request.app.state.rt


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def is_loopback(request: Request) -> bool:
    try:
        return ipaddress.ip_address(client_ip(request)).is_loopback
    except ValueError:
        return client_ip(request) in ("testclient", "localhost")


def _bearer(request: Request) -> str:
    h = request.headers.get("authorization", "")
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return ""


def current_principal(request: Request) -> Principal:
    runtime = rt(request)
    token = _bearer(request)
    via_cookie = False
    if not token:
        token = request.cookies.get(COOKIE_NAME, "")
        via_cookie = bool(token)
    p = runtime.auth.authenticate(token)
    if not p:
        raise HTTPException(401, "não autenticado", headers={"WWW-Authenticate": "Bearer"})
    if via_cookie and request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(CSRF_HEADER) != "monitor":
        # Cookie só é aceito em requisições do próprio painel (proteção CSRF).
        raise HTTPException(403, "cabeçalho X-Requested-With ausente")
    request.state.principal = p
    return p


def require_admin(request: Request, p: Principal = Depends(current_principal)) -> Principal:
    if not p.is_admin:
        raise HTTPException(403, "requer perfil de administrador")
    if not rt(request).cfg.settings.admin_remote_access and not is_loopback(request):
        raise HTTPException(403, "administração permitida apenas no próprio servidor (admin_remote_access=false)")
    return p


def media_principal(request: Request, scope: str) -> Principal:
    """Aceita Bearer/cookie OU token de mídia ``?mt=`` (para AVPlayer)."""
    runtime = rt(request)
    token = _bearer(request) or request.cookies.get(COOKIE_NAME, "")
    if token:
        p = runtime.auth.authenticate(token)
        if p:
            return p
    mt = request.query_params.get("mt", "")
    if mt:
        p = runtime.auth.verify_media_token(mt, scope)
        if p:
            return p
    raise HTTPException(401, "não autenticado")


def playlist_response(request: Request, text: str) -> Response:
    body = text.encode("utf-8")
    headers = {"Cache-Control": "no-store"}
    if "gzip" in request.headers.get("accept-encoding", "") and len(body) > 1024:
        body = gzip.compress(body, 5)
        headers["Content-Encoding"] = "gzip"
    return Response(body, media_type="application/vnd.apple.mpegurl", headers=headers)
