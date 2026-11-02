"""Usuários, sessões, limitação de tentativas e tokens de mídia assinados."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets as pysecrets
import threading
import time
from dataclasses import dataclass

from ..database import Database
from . import secrets as vault

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1
MIN_PASSWORD_LEN = 8


def hash_password(password: str) -> str:
    salt = pysecrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode("utf-8"), salt=base64.b64decode(salt_b64), n=int(n), r=int(r), p=int(p), dklen=32
        )
        return hmac.compare_digest(dk, base64.b64decode(hash_b64))
    except Exception:
        return False


_DUMMY_HASH = hash_password("dummy-password-for-timing")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class Principal:
    user_id: int
    username: str
    role: str
    session_id: int

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class LoginRateLimiter:
    """Bloqueio progressivo por IP e por usuário após falhas consecutivas."""

    def __init__(self, max_failures: int = 5, window: float = 900, base_lockout: float = 60, max_lockout: float = 3600):
        self.max_failures = max_failures
        self.window = window
        self.base_lockout = base_lockout
        self.max_lockout = max_lockout
        self._fail: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._strikes: dict[str, int] = {}
        self._lock = threading.Lock()

    def retry_after(self, *keys: str) -> float:
        now = time.time()
        with self._lock:
            return max([self._locked_until.get(k, 0) - now for k in keys] + [0])

    def failure(self, *keys: str) -> None:
        now = time.time()
        with self._lock:
            for k in keys:
                lst = [t for t in self._fail.get(k, []) if now - t < self.window]
                lst.append(now)
                self._fail[k] = lst
                if len(lst) >= self.max_failures:
                    strikes = self._strikes.get(k, 0) + 1
                    self._strikes[k] = strikes
                    self._locked_until[k] = now + min(self.base_lockout * 2 ** (strikes - 1), self.max_lockout)
                    self._fail[k] = []

    def success(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._fail.pop(k, None)
                self._strikes.pop(k, None)
                self._locked_until.pop(k, None)


class AuthService:
    def __init__(self, db: Database, session_ttl_hours: int = 720):
        self.db = db
        self.session_ttl = session_ttl_hours * 3600
        self.limiter = LoginRateLimiter()
        self._media_key = self._load_media_key()

    # ---------------------------------------------------------------- users
    def create_user(self, username: str, password: str, role: str = "viewer") -> int:
        username = username.strip()
        if not username or len(username) > 64:
            raise ValueError("usuário inválido")
        if len(password) < MIN_PASSWORD_LEN:
            raise ValueError(f"a senha deve ter pelo menos {MIN_PASSWORD_LEN} caracteres")
        if role not in ("admin", "viewer"):
            raise ValueError("papel inválido")
        cur = self.db.execute(
            "INSERT INTO users(username, password_hash, role, created_at) VALUES(?,?,?,?)",
            (username, hash_password(password), role, time.time()),
        )
        return int(cur.lastrowid)

    def set_password(self, username: str, password: str) -> None:
        if len(password) < MIN_PASSWORD_LEN:
            raise ValueError(f"a senha deve ter pelo menos {MIN_PASSWORD_LEN} caracteres")
        cur = self.db.execute("UPDATE users SET password_hash=? WHERE username=?", (hash_password(password), username))
        if cur.rowcount == 0:
            raise KeyError(username)
        # Troca de senha invalida todas as sessões do usuário.
        self.db.execute("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username=?)", (username,))

    def list_users(self) -> list[dict]:
        return self.db.query("SELECT id, username, role, created_at FROM users ORDER BY id")

    def delete_user(self, user_id: int) -> None:
        self.db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        self.db.execute("DELETE FROM users WHERE id=?", (user_id,))

    def has_users(self) -> bool:
        return self.db.one("SELECT 1 AS x FROM users LIMIT 1") is not None

    # --------------------------------------------------------------- login
    def login(self, username: str, password: str, client_ip: str, device: str = "") -> tuple[str, dict]:
        keys = (f"ip:{client_ip}", f"user:{username.lower()}")
        wait = self.limiter.retry_after(*keys)
        if wait > 0:
            raise RateLimited(wait)
        user = self.db.one("SELECT * FROM users WHERE username=?", (username,))
        ok = verify_password(password, user["password_hash"] if user else _DUMMY_HASH)
        if not user or not ok:
            self.limiter.failure(*keys)
            raise InvalidCredentials()
        self.limiter.success(*keys)
        token = pysecrets.token_urlsafe(32)
        now = time.time()
        cur = self.db.execute(
            "INSERT INTO sessions(token_hash, user_id, device, created_at, expires_at, last_used) VALUES(?,?,?,?,?,?)",
            (_token_hash(token), user["id"], device[:120], now, now + self.session_ttl, now),
        )
        return token, {
            "session_id": int(cur.lastrowid),
            "user": {"id": user["id"], "username": user["username"], "role": user["role"]},
            "expires_at": now + self.session_ttl,
        }

    def authenticate(self, token: str) -> Principal | None:
        if not token:
            return None
        row = self.db.one(
            """SELECT s.id AS sid, s.expires_at, s.last_used, u.id AS uid, u.username, u.role
               FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash=?""",
            (_token_hash(token),),
        )
        now = time.time()
        if not row or row["expires_at"] < now:
            return None
        if now - row["last_used"] > 300:
            # Renovação deslizante: sessões usadas continuam válidas.
            self.db.execute(
                "UPDATE sessions SET last_used=?, expires_at=? WHERE id=?", (now, now + self.session_ttl, row["sid"])
            )
        return Principal(row["uid"], row["username"], row["role"], row["sid"])

    def logout(self, session_id: int) -> None:
        self.db.execute("DELETE FROM sessions WHERE id=?", (session_id,))

    def purge_expired(self) -> None:
        self.db.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))

    def session_alive(self, session_id: int) -> Principal | None:
        row = self.db.one(
            """SELECT s.id AS sid, s.expires_at, u.id AS uid, u.username, u.role
               FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.id=?""",
            (session_id,),
        )
        if not row or row["expires_at"] < time.time():
            return None
        return Principal(row["uid"], row["username"], row["role"], row["sid"])

    # ---------------------------------------------------------- media token
    def _load_media_key(self) -> bytes:
        stored = self.db.get_secret("media_key")
        if stored:
            return base64.b64decode(vault.decrypt(stored))
        key = pysecrets.token_bytes(32)
        self.db.set_secret("media_key", vault.encrypt(base64.b64encode(key).decode()))
        return key

    def issue_media_token(self, principal: Principal, scope: str, ttl_seconds: int) -> str:
        """Token curto para URLs de HLS/arquivos (AVPlayer não envia cabeçalhos a todos os pedidos)."""
        body = json.dumps({"s": principal.session_id, "c": scope, "e": int(time.time() + ttl_seconds)}, separators=(",", ":"))
        b = base64.urlsafe_b64encode(body.encode()).rstrip(b"=")
        sig = base64.urlsafe_b64encode(hmac.new(self._media_key, b, hashlib.sha256).digest()[:20]).rstrip(b"=")
        return (b + b"." + sig).decode()

    def verify_media_token(self, token: str, scope: str) -> Principal | None:
        try:
            b, sig = token.encode().split(b".", 1)
            expected = base64.urlsafe_b64encode(hmac.new(self._media_key, b, hashlib.sha256).digest()[:20]).rstrip(b"=")
            if not hmac.compare_digest(sig, expected):
                return None
            body = json.loads(base64.urlsafe_b64decode(b + b"=" * (-len(b) % 4)))
        except Exception:
            return None
        if body.get("e", 0) < time.time():
            return None
        if body.get("c") not in ("*", scope):
            return None
        return self.session_alive(int(body.get("s", 0)))


class InvalidCredentials(Exception):
    pass


class RateLimited(Exception):
    def __init__(self, retry_after: float):
        super().__init__("muitas tentativas")
        self.retry_after = retry_after
