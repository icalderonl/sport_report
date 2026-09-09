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
    (re.compile(r"/bot(\d+:[A-Za-z0-9_-]+)"), "/bot<TOKEN>"),
    # Por si alguna vez una clave viaja en query string (intervals.icu usa
    # Basic Auth, pero el respaldo de Strava sirve tokens por parametro).
    (
        re.compile(r"((?:access_token|refresh_token|api_key|key)=)[^&\s]+", re.I),
        r"\1<REDACTADO>",
    ),
)


def _redactar(texto: str) -> str:
    for patron, reemplazo in _SECRETOS:
        texto = patron.sub(reemplazo, texto)
    return texto


class _SinSecretos(logging.Filter):
    """Tapa credenciales en cualquier registro, venga de donde venga.

    Se engancha a los HANDLERS y no a un logger: un filtro puesto en un logger
    solo mira lo que se registra a traves de el, no lo que le llega propagado
    desde `httpx` u otra libreria. En el handler pasa todo lo que va a salir.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        texto = record.getMessage()
        limpio = _redactar(texto)
        if limpio != texto:
            # Se guarda el mensaje ya interpolado, asi que los args sobran: si
            # se dejaran, el formateador intentaria `msg % args` otra vez.
            record.msg = limpio
            record.args = ()

        # El traceback NO va en `getMessage()`: lo anade el formateador despues,
        # y httpx mete la URL entera en el texto de sus excepciones ("401 for
        # url ..."), justo el caso en que uno esta mirando credenciales. El
        # formateador cachea su render en `exc_text`, asi que rellenarlo aqui ya
        # redactado hace que los handlers usen esta version y no la cruda.
        if record.exc_info:
            if record.exc_text is None:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            record.exc_text = _redactar(record.exc_text)
        if record.stack_info:
            record.stack_info = _redactar(record.stack_info)
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
