"""Errores del cliente de TrainingPeaks.

Misma idea que en Garmin e intervals.icu: todo hereda de `TPError` para que
quien llame trate cualquier fallo de esta fuente de la misma forma.
"""
from __future__ import annotations


class TPError(RuntimeError):
    """Fallo generico contra la API."""


class TPAuthError(TPError):
    """Falta la cookie, vencio, o TrainingPeaks la rechazo (401/403).

    No se reintenta: la cookie se renueva a mano copiandola del navegador.
    """
