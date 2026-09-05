"""Manejo de semanas lunes-domingo en la zona horaria local.

Todo el sistema define el "dia" y la "semana" en TZ_LOCAL (America/Santiago por
defecto), aunque en SQLite las marcas de tiempo se guarden en UTC.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import NamedTuple

from .config import TZ
from .plan.models import ORDEN_DIAS

# Indice de datetime.weekday() -> letra del dia usada en el plan.
_LETRA_POR_WEEKDAY = ("L", "M", "W", "J", "V", "S", "D")


class RangoSemana(NamedTuple):
    inicio: date  # lunes
    fin: date  # domingo

    def __str__(self) -> str:
        return f"{self.inicio.isoformat()} a {self.fin.isoformat()}"

    @property
    def clave(self) -> str:
        """Identificador estable de la semana, usado como nombre de archivo."""
        return self.inicio.isoformat()

    def contiene(self, d: date) -> bool:
        return self.inicio <= d <= self.fin

    def dias(self) -> list[date]:
        return [self.inicio + timedelta(days=i) for i in range(7)]


def ahora_local() -> datetime:
    return datetime.now(TZ)


def hoy_local() -> date:
    return ahora_local().date()


def letra_dia(d: date) -> str:
    return _LETRA_POR_WEEKDAY[d.weekday()]


def fecha_de_letra(rango: RangoSemana, letra: str) -> date:
    return rango.inicio + timedelta(days=ORDEN_DIAS.index(letra))


def semana_de(d: date) -> RangoSemana:
    inicio = d - timedelta(days=d.weekday())
    return RangoSemana(inicio, inicio + timedelta(days=6))


def semana_actual() -> RangoSemana:
    return semana_de(hoy_local())


def semana_siguiente(rango: RangoSemana | None = None) -> RangoSemana:
    base = rango or semana_actual()
    return semana_de(base.inicio + timedelta(days=7))


def semana_anterior(rango: RangoSemana | None = None) -> RangoSemana:
    base = rango or semana_actual()
    return semana_de(base.inicio - timedelta(days=7))


def semana_a_reportar(momento: datetime | None = None) -> RangoSemana:
    """Semana que evalua el reporte semanal.

    El cron corre el lunes 07:00, asi que la semana cerrada es la anterior.
    Si por alguna razon corre entre martes y domingo, se reporta la semana en
    curso hasta ese punto.
    """
    m = momento or ahora_local()
    actual = semana_de(m.date())
    return semana_anterior(actual) if m.weekday() == 0 else actual


def parse_rango(clave: str) -> RangoSemana:
    return semana_de(date.fromisoformat(clave))
