"""Errores del cliente de intervals.icu.

Todos heredan de `IntervalsError` porque es lo que el selector de fuentes
atrapa para caer al respaldo de Strava: cualquier fallo de esta fuente —red,
credencial, timeout, 5xx— tiene la misma consecuencia.
"""
from __future__ import annotations


class IntervalsError(RuntimeError):
    """Fallo generico contra la API."""


class IntervalsAuthError(IntervalsError):
    """La clave falta o intervals.icu la rechazo (401).

    No se reintenta: una clave mal copiada no se arregla insistiendo. Dispara
    el respaldo y el reporte de la semana dice que quedo degradada.
    """


class IntervalsRateLimit(IntervalsError):
    """Cuota agotada y la espera excede el maximo tolerado por la corrida."""


class SinPermiso(IntervalsError):
    """El recurso no existe o la cuenta no puede verlo (403/404).

    Se usa igual que en Strava: una actividad sin streams o sin intervalos es
    un caso normal, y quien llama decide que hacer con la falta.
    """
