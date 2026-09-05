"""ACWR (Acute:Chronic Workload Ratio).

Aguda   = promedio diario de carga de los ultimos 7 dias.
Cronica = promedio diario de carga de los ultimos 28 dias.
Ratio   = aguda / cronica.

Los dias de descanso cuentan como carga 0 (es lo que hace que el ratio detecte
un pico de volumen). Un dia con sesion pero SIN carga registrada (corrida sin
pulsometro) no es un cero real: no se puede distinguir de un descanso, y por eso
degrada la confiabilidad en vez de pasar desapercibido.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Mapping

from ..config import UMBRALES, Umbrales

DIAS_AGUDA = 7
DIAS_CRONICA = 28


@dataclass(frozen=True)
class ResultadoACWR:
    aguda: float | None
    cronica: float | None
    ratio: float | None
    confiable: bool
    motivo: str
    dias_historial: int
    dias_con_carga: int
    sesiones_sin_carga: int
    alerta: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "aguda": self.aguda,
            "cronica": self.cronica,
            "ratio": self.ratio,
            "confiable": self.confiable,
            "motivo": self.motivo,
            "dias_historial": self.dias_historial,
            "dias_con_carga": self.dias_con_carga,
            "sesiones_sin_carga": self.sesiones_sin_carga,
            "alerta": self.alerta,
        }


def _suma(carga: Mapping[date, float], desde: date, hasta: date) -> float:
    return sum(v for d, v in carga.items() if desde <= d <= hasta)


def calcular(
    carga_por_dia: Mapping[date, float],
    fin: date,
    primera_fecha: date | None = None,
    sesiones_sin_carga: int = 0,
    umbrales: Umbrales = UMBRALES,
) -> ResultadoACWR:
    ini_aguda = fin - timedelta(days=DIAS_AGUDA - 1)
    ini_cronica = fin - timedelta(days=DIAS_CRONICA - 1)

    aguda = round(_suma(carga_por_dia, ini_aguda, fin) / DIAS_AGUDA, 1)
    cronica = round(_suma(carga_por_dia, ini_cronica, fin) / DIAS_CRONICA, 1)
    dias_con_carga = sum(1 for d, v in carga_por_dia.items() if ini_cronica <= d <= fin and v)

    if primera_fecha is None:
        dias_historial = 0
    else:
        dias_historial = min((fin - primera_fecha).days + 1, DIAS_CRONICA)

    motivos: list[str] = []
    if dias_historial < umbrales.acwr_dias_minimos:
        motivos.append(
            f"solo {dias_historial} dias de historico real (se necesitan "
            f"{umbrales.acwr_dias_minimos})"
        )
    if dias_con_carga < umbrales.acwr_sesiones_minimas:
        motivos.append(
            f"solo {dias_con_carga} dias con carga registrada en la ventana de 28 "
            f"(se necesitan {umbrales.acwr_sesiones_minimas})"
        )
    if cronica <= 0:
        motivos.append("carga cronica en cero: no se puede calcular el ratio")
    if sesiones_sin_carga:
        motivos.append(
            f"{sesiones_sin_carga} sesion(es) sin HR quedaron fuera del calculo"
        )

    ratio = round(aguda / cronica, 2) if cronica > 0 else None
    confiable = not motivos

    alerta = ""
    if confiable and ratio is not None:
        if ratio > umbrales.acwr_alto:
            alerta = f"ACWR {ratio} sobre el umbral {umbrales.acwr_alto}: pico de carga"
        elif ratio < umbrales.acwr_bajo:
            alerta = f"ACWR {ratio} bajo el umbral {umbrales.acwr_bajo}: perdida de carga"

    return ResultadoACWR(
        aguda=aguda,
        cronica=cronica,
        ratio=ratio,
        confiable=confiable,
        motivo="; ".join(motivos),
        dias_historial=dias_historial,
        dias_con_carga=dias_con_carga,
        sesiones_sin_carga=sesiones_sin_carga,
        alerta=alerta,
    )
