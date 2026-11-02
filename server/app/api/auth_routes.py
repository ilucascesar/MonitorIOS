from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..security.auth import InvalidCredentials, Principal, RateLimited
from .deps import COOKIE_NAME, client_ip, current_principal, rt

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    device: str = Field("", max_length=120)
    cookie: bool = False  # True apenas para o painel web


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response):
    runtime = rt(request)
    try:
        token, info = runtime.auth.login(body.username, body.password, client_ip(request), body.device)
    except RateLimited as e:
        raise HTTPException(429, f"muitas tentativas; aguarde {int(e.retry_after) + 1}s",
                            headers={"Retry-After": str(int(e.retry_after) + 1)})
    except InvalidCredentials:
        runtime.events.emit("warning", "auth", f"falha de login para '{body.username[:32]}' de {client_ip(request)}")
        raise HTTPException(401, "usuário ou senha inválidos")
    if body.cookie:
        response.set_cookie(
            COOKIE_NAME, token, httponly=True, samesite="strict", secure=request.url.scheme == "https",
            max_age=int(info["expires_at"] - __import__("time").time()), path="/",
        )
        return {"user": info["user"], "expires_at": info["expires_at"]}
    return {"token": token, "user": info["user"], "expires_at": info["expires_at"]}


@router.post("/logout")
def logout(request: Request, response: Response, p: Principal = Depends(current_principal)):
    rt(request).auth.logout(p.session_id)
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
def me(p: Principal = Depends(current_principal)):
    return {"id": p.user_id, "username": p.username, "role": p.role}
