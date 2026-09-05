"""Errores del cliente de Strava."""
from __future__ import annotations


class StravaError(RuntimeError):
    """Fallo generico contra la API."""


class StravaAuthError(StravaError):
    """No hay credenciales utilizables: falta el refresh_token o Strava lo rechazo.

    Requiere intervencion manual (re-autorizar), no se resuelve reintentando.
    """


class StravaRateLimit(StravaError):
    """Cuota agotada y la espera excede el maximo tolerado por la corrida."""


class SinPermiso(StravaError):
    """El token no tiene el scope necesario para ese endpoint."""
