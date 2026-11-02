"""Logs estruturados (JSON por linha) com redação de credenciais e tokens."""
from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
import time
from collections import deque
from pathlib import Path

_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    # rtsp://usuario:senha@host  -> rtsp://***:***@host
    # Guloso até o último "@" da autoridade: senhas com "@" não vazam parcialmente.
    (re.compile(r"(?i)\b(rtsps?|https?|onvif)://[^/\s]*@"), r"\1://***:***@"),
    (re.compile(r"(?i)(?<![A-Za-z0-9_])((?:mt|token|password|pwd|pass)=)[^&\s\"']+"), r"\1***"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1***"),
    (re.compile(r'(?i)("?(?:password|senha|token|secret)"?\s*[:=]\s*)"[^"]*"'), r'\1"***"'),
    (re.compile(r"(?i)(<(?:\w+:)?Password[^>]*>)[^<]*(</)"), r"\1***\2"),
]


def redact(text: str) -> str:
    for pat, rep in _REDACTIONS:
        text = pat.sub(rep, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover - formatação quebrada
            msg = str(record.msg)
        record.msg = redact(msg)
        record.args = None
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
            + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "ctx", None)
        if isinstance(extra, dict):
            payload.update({k: v for k, v in extra.items() if k not in payload})
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


class MemoryHandler(logging.Handler):
    """Mantém as últimas linhas em memória para o painel administrativo."""

    def __init__(self, capacity: int = 2000):
        super().__init__()
        self.buffer: deque[str] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buffer.append(self.format(record))
        except Exception:  # pragma: no cover
            pass


memory_handler = MemoryHandler()


def setup_logging(logs_dir: Path, level: str = "INFO", console: bool = True) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(level)
    fmt = JsonFormatter()
    redactor = RedactingFilter()

    fileh = logging.handlers.RotatingFileHandler(
        logs_dir / "server.log", maxBytes=10 * 1024 * 1024, backupCount=10, encoding="utf-8"
    )
    handlers: list[logging.Handler] = [fileh, memory_handler]
    if console and sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(redactor)
        root.addHandler(h)
    # O access log do uvicorn incluiria query strings (tokens de mídia); usamos o nosso.
    logging.getLogger("uvicorn.access").disabled = True
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True


def log(logger: logging.Logger, level: int, msg: str, **ctx) -> None:
    logger.log(level, msg, extra={"ctx": ctx})
