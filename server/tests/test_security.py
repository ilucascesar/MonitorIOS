import time
from pathlib import Path

import pytest

from app.cameras.urls import build_stream_url, strip_credentials, with_credentials
from app.logging_setup import redact
from app.security import secrets as vault
from app.security.auth import InvalidCredentials, LoginRateLimiter, RateLimited, hash_password, verify_password
from app.security.paths import safe_join, sanitize_folder


def test_password_hash_roundtrip_and_salting():
    h1, h2 = hash_password("segredo-forte"), hash_password("segredo-forte")
    assert h1 != h2
    assert verify_password("segredo-forte", h1)
    assert not verify_password("errada", h1)
    assert not verify_password("x", "lixo")


def test_secret_encryption_roundtrip():
    enc = vault.encrypt("senha da câmera")
    assert "senha" not in enc
    assert vault.decrypt(enc) == "senha da câmera"
    assert vault.encrypt("") == ""


def test_rate_limiter_locks_and_resets():
    lim = LoginRateLimiter(max_failures=3, window=60, base_lockout=30)
    for _ in range(3):
        lim.failure("ip:1")
    assert lim.retry_after("ip:1") > 25
    lim.success("ip:1")
    assert lim.retry_after("ip:1") == 0


def test_login_lockout_and_unknown_user(runtime):
    runtime.auth.create_user("admin", "senha-muito-boa", "admin")
    for _ in range(5):
        with pytest.raises(InvalidCredentials):
            runtime.auth.login("admin", "errada", "10.0.0.1")
    with pytest.raises(RateLimited):
        runtime.auth.login("admin", "senha-muito-boa", "10.0.0.1")
    with pytest.raises(InvalidCredentials):
        runtime.auth.login("ninguem", "x", "10.0.0.2")


def test_session_and_media_token_scope(runtime):
    runtime.auth.create_user("u", "senha-muito-boa")
    token, info = runtime.auth.login("u", "senha-muito-boa", "1.1.1.1")
    p = runtime.auth.authenticate(token)
    assert p and p.username == "u"
    mt = runtime.auth.issue_media_token(p, "cam:1", 60)
    assert runtime.auth.verify_media_token(mt, "cam:1")
    assert runtime.auth.verify_media_token(mt, "cam:2") is None
    assert runtime.auth.verify_media_token(mt[:-2] + "xx", "cam:1") is None
    expired = runtime.auth.issue_media_token(p, "cam:1", -1)
    assert runtime.auth.verify_media_token(expired, "cam:1") is None
    runtime.auth.logout(p.session_id)
    assert runtime.auth.authenticate(token) is None
    assert runtime.auth.verify_media_token(mt, "cam:1") is None  # revogado junto com a sessão


def test_password_change_revokes_sessions(runtime):
    runtime.auth.create_user("u", "senha-muito-boa")
    token, _ = runtime.auth.login("u", "senha-muito-boa", "1.1.1.1")
    runtime.auth.set_password("u", "outra-senha-boa")
    assert runtime.auth.authenticate(token) is None


@pytest.mark.parametrize("rel", ["../x.ts", "a/../../x.ts", "..\\..\\Windows\\win.ini", "/etc/passwd", "C:/x.ts", "a/b?.ts", ""])
def test_safe_join_rejects_traversal(tmp_path, rel):
    with pytest.raises(ValueError):
        safe_join(tmp_path, rel)


def test_safe_join_accepts_normal(tmp_path):
    assert safe_join(tmp_path, "Garagem/2026-11-01/14/abc_20261101-140000.ts") == tmp_path / "Garagem" / "2026-11-01" / "14" / "abc_20261101-140000.ts"


def test_sanitize_folder():
    assert sanitize_folder("Câmera da Garagem!") == "Camera_da_Garagem"
    assert sanitize_folder("../..") == "camera"


def test_redaction():
    line = "Error opening rtsp://admin:S3nh@!@192.168.1.7:8554/stream0 token=abc&mt=xyz Bearer abc.def"
    out = redact(line)
    assert "S3nh" not in out and "admin:" not in out
    assert "mt=***" in out and "token=***" in out and "abc.def" not in out
    assert "<Password>***</" in redact("<Password>segredo</Password>")


def test_rtsp_credentials_never_stored_in_url():
    assert strip_credentials("rtsp://u:p@1.2.3.4:554/s") == "rtsp://1.2.3.4:554/s"
    assert with_credentials("rtsp://1.2.3.4/s", "us er", "p@ss:") == "rtsp://us%20er:p%40ss%3A@1.2.3.4/s"
    cam = {"main_url": "rtsp://1.2.3.4/s", "sub_url": "", "username": "u", "password_enc": vault.encrypt("p")}
    assert build_stream_url(cam, "sub") == "rtsp://u:p@1.2.3.4/s"
