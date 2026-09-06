"""Envio de mensajes a Telegram via HTTP.

Separado del bot a proposito: el cron semanal manda el reporte sin levantar el
runtime del bot (que corre como servicio aparte).

Este es el unico fallo que hace fracasar la corrida entera, y el unico canal
para avisar de el es justamente este. Por eso reintenta: un corte de red de
unos segundos a las 07:00 del lunes no puede costar el reporte de la semana.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

from .. import config
from .formato import trozos

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/sendMessage"
API_FOTO = "https://api.telegram.org/bot{token}/sendPhoto"
# Telegram corta los pies de foto ahi; el reporte va en mensajes aparte.
LIMITE_CAPTION = 1024

# Esperas entre reintentos. Cubren el corte domestico tipico (router que se
# reinicia, wifi que se cae) sin acercarse al TimeoutStartSec de 30 min.
ESPERAS_S = (2.0, 8.0, 30.0)
# Tope al `retry_after` que manda Telegram en un 429: mas que esto no cabe en
# la corrida y es mejor fallar avisando.
ESPERA_MAX_S = 60.0


@dataclass(frozen=True)
class ResultadoEnvio:
    """Cuantos mensajes llegaron de los que habia que mandar.

    Es booleano para que quien solo quiera saber "salio bien" no cambie, pero
    distingue el fallo total del parcial: si el reporte iba en tres mensajes y
    fallo el tercero, la corrida es `parcial`, no `error`.
    """

    enviadas: int = 0
    totales: int = 0

    def __bool__(self) -> bool:
        return self.totales > 0 and self.enviadas == self.totales

    @property
    def parcial(self) -> bool:
        return 0 < self.enviadas < self.totales


def _espera_de_429(r: httpx.Response, por_defecto: float) -> float:
    """Telegram manda `parameters.retry_after` (segundos) al aplicar flood control."""
    try:
        segundos = float(r.json().get("parameters", {}).get("retry_after", 0))
    except (ValueError, AttributeError, TypeError):
        segundos = 0.0
    return min(max(segundos, por_defecto), ESPERA_MAX_S)


def _con_reintento(hacer_post: Callable[[], httpx.Response], dormir=time.sleep) -> bool:
    """Reintenta ante fallo de red, 429 y 5xx. Un 4xx no se reintenta: una
    peticion mal formada no mejora repitiendola.

    Recibe una funcion y no una peticion ya armada porque cada intento tiene
    que rehacerla desde cero: en el envio de la foto el archivo se reabre, y
    reintentar sobre un handle ya consumido subiria cero bytes.
    """
    for intento in range(len(ESPERAS_S) + 1):
        ultimo = intento == len(ESPERAS_S)
        espera = 0.0 if ultimo else ESPERAS_S[intento]
        try:
            r = hacer_post()
        except httpx.HTTPError as exc:
            if ultimo:
                log.error("fallo el envio a Telegram tras %d intentos: %s", intento + 1, exc)
                return False
            log.warning("fallo de red hacia Telegram (%s); reintento en %.0fs", exc, espera)
            dormir(espera)
            continue

        if r.status_code == 429:
            if ultimo:
                log.error("Telegram sigue limitando el envio (429); se abandona")
                return False
            espera = _espera_de_429(r, espera)
            log.warning("429 de Telegram; reintento en %.0fs", espera)
            dormir(espera)
            continue

        if r.status_code >= 500:
            if ultimo:
                log.error("Telegram responde %s de forma persistente", r.status_code)
                return False
            log.warning("%s de Telegram; reintento en %.0fs", r.status_code, espera)
            dormir(espera)
            continue

        if r.status_code >= 400:
            log.error("Telegram rechazo el envio (%s): %s", r.status_code, r.text[:300])
            return False
        return True
    return False


def enviar(
    texto: str,
    chat_id: str | None = None,
    token: str | None = None,
    dormir=time.sleep,
) -> ResultadoEnvio:
    """Manda un mensaje, partiendolo si hace falta. No lanza nunca: el estado
    del envio viaja en el resultado y el detalle queda en el log."""
    token = token or config.TELEGRAM_BOT_TOKEN
    chat_id = chat_id or config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        log.error("TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID sin configurar")
        return ResultadoEnvio()
    partes = [p for p in trozos(texto) if p.strip()]
    if not partes:
        log.error("no hay nada que enviar: el mensaje esta vacio")
        return ResultadoEnvio()

    url = API.format(token=token)
    enviadas = 0
    with httpx.Client(timeout=30) as cli:
        for parte in partes:
            cuerpo = {"chat_id": chat_id, "text": parte, "disable_web_page_preview": True}
            if not _con_reintento(lambda c=cuerpo: cli.post(url, json=c), dormir):
                break
            enviadas += 1
    if enviadas < len(partes):
        log.error("envio incompleto: %d de %d mensajes", enviadas, len(partes))
    return ResultadoEnvio(enviadas=enviadas, totales=len(partes))


def enviar_foto(
    ruta: Path,
    caption: str = "",
    chat_id: str | None = None,
    token: str | None = None,
    dormir=time.sleep,
) -> bool:
    """Manda una imagen. Devuelve False en vez de reventar, igual que `enviar`.

    El grafico es un extra: que no llegue nunca puede tumbar el reporte.
    """
    token = token or config.TELEGRAM_BOT_TOKEN
    chat_id = chat_id or config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        log.error("TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID sin configurar")
        return False
    if not Path(ruta).is_file():
        log.error("no existe la imagen a enviar: %s", ruta)
        return False

    url = API_FOTO.format(token=token)
    datos = {"chat_id": chat_id, "caption": caption[:LIMITE_CAPTION]}

    try:
        with httpx.Client(timeout=60) as cli:

            def _subir() -> httpx.Response:
                # Reabrir en cada intento: el handle del intento anterior ya
                # esta consumido y subiria cero bytes.
                with open(ruta, "rb") as f:
                    return cli.post(
                        url, data=datos, files={"photo": (Path(ruta).name, f, "image/png")}
                    )

            return _con_reintento(_subir, dormir)
    except OSError as exc:
        log.error("no se pudo leer la imagen a enviar: %s", exc)
        return False
