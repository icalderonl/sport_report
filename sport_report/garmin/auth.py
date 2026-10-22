"""Autenticacion de la API Personal de Garmin: clave personal -> token de 24 h.

La clave se crea en Garmin Connect (Ajustes -> Personal API) y se muestra una
sola vez. No sirve directo contra los endpoints: se cambia por un access token
(un JWT que vale 24 h, o menos si la clave vence antes) con un POST y la
cabecera `Authorization: ApiKey <clave>`. No hay refresh token: cuando el
token vence se pide otro con la misma clave.

A diferencia de intervals.icu, la clave SI vence. El vencimiento no viene en
la respuesta; Garmin solo avisa por correo una semana antes.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

from .. import config
from .errors import GarminAuthError

RUTA_TOKEN = "/personalapitoken-service/personal-api/personal-api-key/token"


def clave(valor: str | None = None) -> str:
    valor = (valor if valor is not None else config.GARMIN_API_KEY).strip()
    if not valor:
        raise GarminAuthError(
            "falta GARMIN_API_KEY en el .env. Se crea en Garmin Connect -> "
            "Ajustes -> Personal API y se muestra una sola vez"
        )
    return valor


def resumen_clave(valor: str | None = None) -> str:
    """Longitud y ultimos dos caracteres, nunca la clave."""
    valor = (valor if valor is not None else config.GARMIN_API_KEY).strip()
    if not valor:
        return "ausente"
    return f"{len(valor)} caracteres, termina en ...{valor[-2:]}"


def vencimiento_token(token: str) -> datetime | None:
    """Claim `exp` del JWT, sin verificar la firma (solo se informa).

    None si el token no tiene la forma de un JWT o no trae `exp`.
    """
    partes = token.split(".")
    if len(partes) != 3:
        return None
    carga = partes[1] + "=" * (-len(partes[1]) % 4)
    try:
        datos = json.loads(base64.urlsafe_b64decode(carga))
        return datetime.fromtimestamp(int(datos["exp"]), tz=timezone.utc)
    except (ValueError, KeyError, TypeError):
        return None
