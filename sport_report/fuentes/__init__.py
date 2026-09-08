"""Fuentes de datos reales: intervals.icu (principal) y Strava (respaldo).

La orquestacion comun de la ingesta vive en `comun.py`; cada fuente concreta es
una subclase de `IngestaBase` en su propio paquete (`sport_report/strava`,
`sport_report/intervals`). El selector con respaldo se agrega mas adelante.
"""
from __future__ import annotations

from .comun import IngestaBase, ResumenIngesta

__all__ = ["IngestaBase", "ResumenIngesta"]
