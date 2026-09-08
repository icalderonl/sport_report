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


# --------------------------------------------------------------------------
# Meses (reporte mensual, spec 7bis)
# --------------------------------------------------------------------------


class MesRango(NamedTuple):
    inicio: date  # dia 1
    fin: date  # ultimo dia del mes

    def __str__(self) -> str:
        return f"{self.inicio.isoformat()} a {self.fin.isoformat()}"

    @property
    def clave(self) -> str:
        """Identificador estable del mes, usado como nombre de archivo."""
        return self.inicio.strftime("%Y-%m")

    def contiene(self, d: date) -> bool:
        return self.inicio <= d <= self.fin


def mes_de(d: date) -> MesRango:
    inicio = d.replace(day=1)
    # El dia 28 del mes siguiente evita tener que saber cuantos dias trae cada
    # mes y funciona igual en febrero.
    siguiente = (inicio + timedelta(days=31)).replace(day=1)
    return MesRango(inicio, siguiente - timedelta(days=1))


def mes_anterior(mes: MesRango | None = None) -> MesRango:
    base = mes or mes_de(hoy_local())
    return mes_de(base.inicio - timedelta(days=1))


def semanas_del_mes(mes: MesRango) -> list[RangoSemana]:
    """Semanas cuyo LUNES cae dentro del mes.

    El criterio tiene que ser uno y estable: una semana a caballo entre dos
    meses no se puede partir sin inventar cifras, asi que se asigna entera al
    mes de su lunes. Las que quedan a caballo se listan aparte en el reporte
    para que el texto no presente el mes como si cerrara justo.
    """
    salida: list[RangoSemana] = []
    lunes = semana_de(mes.inicio).inicio
    if lunes < mes.inicio:  # el mes no empieza en lunes
        lunes += timedelta(days=7)
    while lunes <= mes.fin:
        salida.append(semana_de(lunes))
        lunes += timedelta(days=7)
    return salida
