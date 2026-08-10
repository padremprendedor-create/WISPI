"""Logging rotativo.

REGLA DE PRIVACIDAD, no negociable: este logger NUNCA registra el `vkCode` de
teclas que no sean del combo, ni el texto transcrito cuando
`logging.include_text` es false (el default). Un hook de teclado global tiene
forma de keylogger; lo unico que impide que lo sea es que no escriba lo que ve.
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
LOG_PATH = LOG_DIR / "wispi.log"

_FMT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"


def setup(level: str = "INFO", console: bool = False,
          max_bytes: int = 5 * 1024 * 1024, backup_count: int = 3) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("wispi")
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    root.handlers.clear()
    root.propagate = False

    fh = RotatingFileHandler(LOG_PATH, maxBytes=max_bytes, backupCount=backup_count,
                             encoding="utf-8")
    fh.setFormatter(logging.Formatter(_FMT))
    root.addHandler(fh)

    if console:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter(_FMT))
        root.addHandler(sh)

    # faster-whisper y httpx son ruidosos en DEBUG.
    for noisy in ("httpx", "httpcore", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return root


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"wispi.{name}")


def redact(text: str, include_text: bool) -> str:
    """Devuelve el texto solo si esta permitido; si no, su forma anonima."""
    if include_text:
        return text
    return f"<{len(text)} chars, {len(text.split())} palabras>"
