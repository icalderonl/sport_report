"""Logging a archivo, para diagnosticar corridas de cron sin acceso interactivo."""
from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler

from .config import LOG_DIR

# La API de Telegram lleva el token del bot DENTRO de la ruta, y httpx registra
# cada peticion con la URL completa en nivel INFO. Sin esto, el token acaba en
# logs/*.log, en la salida del cron y en cualquier pantallazo de una corrida.
# Visto de verdad el 2026-09-09 en la primera corrida real de la fase 2.
#
# Se redacta en el filtro y no subiendo el nivel de httpx: esas lineas son las
# que dicen si intervals.icu respondio y con que rango de fechas, y son lo
# primero que se mira cuando una corrida sale rara.
_SECRETOS = (
    re.compile(r"/bot(\d+:[A-Za-z0-9_-]+)"),
    # Por si alguna vez una clave viaja en query string (intervals.icu usa
    # Basic Auth, pero el respaldo de Strava sirve tokens por parametro).
    re.compile(r"((?:access_token|refresh_token|api_key|key)=)[^&\s]+", re.I),
)


class _SinSecretos(logging.Filter):
    """Tapa credenciales en cualquier registro, venga de donde venga."""

    def filter(self, record: logging.LogRecord) -> bool:
        texto = record.getMessage()
        limpio = _SECRETOS[0].sub("/bot<TOKEN>", texto)
        limpio = _SECRETOS[1].sub(r"\1<REDACTADO>", limpio)
        if limpio != texto:
            # Se reemplaza el mensaje ya formateado: los args ya se consumieron.
            record.msg = limpio
            record.args = ()
        return True


def setup(nombre: str, nivel: int = logging.INFO) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    archivo = RotatingFileHandler(
        LOG_DIR / f"{nombre}.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    archivo.setFormatter(fmt)
    consola = logging.StreamHandler(sys.stderr)
    consola.setFormatter(fmt)
    sin_secretos = _SinSecretos()
    archivo.addFilter(sin_secretos)
    consola.addFilter(sin_secretos)

    root = logging.getLogger()
    root.setLevel(nivel)
    root.handlers.clear()
    root.addHandler(archivo)
    root.addHandler(consola)
    return logging.getLogger(nombre)
