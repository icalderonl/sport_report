"""Monotony y Strain (Foster).

Monotony = media / desviacion estandar de la carga diaria de los ultimos 7 dias.
Strain   = carga semanal total x Monotony.

Se usa la desviacion poblacional (dividiendo por n) sobre los 7 valores diarios,
que es lo que hace la formulacion original de Foster. Los dias de descanso entran
como 0: son parte de la variabilidad que la metrica quiere medir.

Con desviacion 0 (semana perfectamente plana, o sin datos) la division no existe:
se marca `confiable: false` en vez de devolver infinito o un numero enorme.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Mapping

from ..config import UMBRALES, Umbrales

DIAS = 7


@dataclass(frozen=True)
class ResultadoFoster:
    carga_semanal: float
    media_diaria: float
    desviacion: float
    monotony: float | None
    strain: float | None
    confiable: bool
    motivo: str
    alerta: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "carga_semanal": self.carga_semanal,
            "media_diaria": self.media_diaria,
            "desviacion": self.desviacion,
            "monotony": self.monotony,
            "strain": self.strain,
            "confiable": self.confiable,
            "motivo": self.motivo,
            "alerta": self.alerta,
        }


def calcular(
    carga_por_dia: Mapping[date, float],
    inicio: date,
    umbrales: Umbrales = UMBRALES,
) -> ResultadoFoster:
    """`inicio` es el primer dia de la ventana de 7 (el lunes de la semana)."""
    diarias = [
        float(carga_por_dia.get(inicio + timedelta(days=i), 0.0)) for i in range(DIAS)
    ]
    total = round(sum(diarias), 1)
    media = statistics.fmean(diarias)
    desv = statistics.pstdev(diarias)

    if total <= 0:
        return ResultadoFoster(
            carga_semanal=0.0,
            media_diaria=0.0,
            desviacion=0.0,
            monotony=None,
            strain=None,
            confiable=False,
            motivo="sin carga registrada en la semana",
        )
    if desv == 0:
        return ResultadoFoster(
            carga_semanal=total,
            media_diaria=round(media, 1),
            desviacion=0.0,
            monotony=None,
            strain=None,
            confiable=False,
            motivo="desviacion estandar 0 (carga diaria identica los 7 dias)",
        )

    monotony = round(media / desv, 2)
    strain = round(total * monotony, 1)
    alerta = ""
    if monotony > umbrales.monotony_alta:
        alerta = (
            f"Monotony {monotony} sobre el umbral {umbrales.monotony_alta}: "
            "semana poco variada"
        )

    return ResultadoFoster(
        carga_semanal=total,
        media_diaria=round(media, 1),
        desviacion=round(desv, 2),
        monotony=monotony,
        strain=strain,
        confiable=True,
        motivo="",
        alerta=alerta,
    )
