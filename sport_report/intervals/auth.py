"""Autenticacion de intervals.icu: API key personal + HTTP Basic.

Es deliberadamente mucho mas simple que la de Strava, y es una de las razones
del cambio de fuente: no hay OAuth, no hay access_token que expire, no hay
refresh_token que rote y haya que persistir antes de usarlo. La clave se pone
una vez en el `.env` y no cambia.

El usuario de Basic Auth es el literal `API_KEY` —no el id del atleta ni un
correo— y la contrasena es la clave personal.
"""
from __future__ import annotations

from .. import config
from .errors import IntervalsAuthError

#: Usuario fijo del esquema Basic de intervals.icu.
USUARIO = "API_KEY"


def credenciales(clave: str | None = None) -> tuple[str, str]:
    """Par (usuario, contrasena) para `httpx.Client(auth=...)`.

    Falla temprano y con un mensaje accionable si no hay clave: sin esto el
    error aparecia como un 401 opaco a mitad de la corrida.
    """
    clave = (clave if clave is not None else config.INTERVALS_API_KEY).strip()
    if not clave:
        raise IntervalsAuthError(
            "falta INTERVALS_API_KEY en el .env. Se saca de intervals.icu -> "
            "Settings -> Developer Settings, y se copia por scp: no la teclees "
            "en la Pi (ver deploy/runbook-intervals.md)"
        )
    return USUARIO, clave


def resumen_clave(clave: str | None = None) -> str:
    """Descripcion de la clave para logs y diagnostico, SIN la clave.

    Solo la longitud y los ultimos dos caracteres: alcanza para detectar un
    caracter perdido al copiar el `.env` y no filtra el secreto a un log que
    despues se lee por pantalla o se pega en un mensaje.
    """
    clave = (clave if clave is not None else config.INTERVALS_API_KEY).strip()
    if not clave:
        return "ausente"
    return f"{len(clave)} caracteres, termina en ...{clave[-2:]}"
