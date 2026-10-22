"""Errores del cliente de la API Personal de Garmin.

Misma idea que en intervals.icu: todo hereda de `GarminError` para que quien
llame trate cualquier fallo de esta fuente de la misma forma.
"""
from __future__ import annotations


class GarminError(RuntimeError):
    """Fallo generico contra la API."""


class GarminAuthError(GarminError):
    """Falta la clave, vencio, o Garmin la rechazo (401).

    No se reintenta. La clave personal vence (30 dias a un ano) y se renueva a
    mano en Garmin Connect: insistir no la arregla.
    """


class SinPermiso(GarminError):
    """El recurso no existe o la cuenta no puede verlo (403/404)."""
