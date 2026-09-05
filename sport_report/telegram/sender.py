"""Envio de mensajes a Telegram via HTTP.

Separado del bot a proposito: el cron semanal manda el reporte sin levantar el
runtime del bot (que corre como servicio aparte).
"""
from __future__ import annotations

import logging

import httpx

from .. import config
from .formato import trozos

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/sendMessage"


def enviar(texto: str, chat_id: str | None = None, token: str | None = None) -> bool:
    """Manda un mensaje. Devuelve False en vez de reventar: el cron ya fallo
    bastante si llega aca, y el error queda en el log."""
    token = token or config.TELEGRAM_BOT_TOKEN
    chat_id = chat_id or config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        log.error("TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID sin configurar")
        return False

    ok = True
    with httpx.Client(timeout=30) as cli:
        for parte in trozos(texto):
            try:
                r = cli.post(
                    API.format(token=token),
                    json={"chat_id": chat_id, "text": parte, "disable_web_page_preview": True},
                )
                r.raise_for_status()
            except httpx.HTTPError as exc:
                log.error("fallo el envio a Telegram: %s", exc)
                ok = False
    return ok
