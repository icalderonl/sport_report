"""Logging a archivo, para diagnosticar corridas de cron sin acceso interactivo."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .config import LOG_DIR


def setup(nombre: str, nivel: int = logging.INFO) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    archivo = RotatingFileHandler(
        LOG_DIR / f"{nombre}.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    archivo.setFormatter(fmt)
    consola = logging.StreamHandler(sys.stderr)
    consola.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(nivel)
    root.handlers.clear()
    root.addHandler(archivo)
    root.addHandler(consola)
    return logging.getLogger(nombre)
