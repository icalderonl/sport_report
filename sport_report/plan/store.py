"""Persistencia del plan semanal.

`semana: N` del plan es una etiqueta del usuario (numero de semana de su ciclo),
no una fecha: no sirve para saber a que lunes-domingo corresponde el plan. Por
eso al guardar se ancla el plan a un rango de fechas explicito y el plan anterior
se archiva. Sin esto, el reporte del lunes evaluaria contra el plan de la semana
que recien empieza si el usuario lo cargo el domingo por la noche.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

from .. import config
from ..fechas import RangoSemana, ahora_local, semana_actual, semana_de, semana_siguiente
from ..storage import escribir_json, leer_json, lock
from .models import ORDEN_DIAS, PlanSemanal, plan_desde_json

VERSION_FORMATO = 1


@dataclass(frozen=True)
class PlanAnclado:
    """Un plan con el rango de fechas al que aplica."""

    plan: PlanSemanal
    rango: RangoSemana
    cargado_en: str

    def to_json(self) -> dict[str, Any]:
        return {
            "version": VERSION_FORMATO,
            "rango": {"inicio": self.rango.inicio.isoformat(), "fin": self.rango.fin.isoformat()},
            "cargado_en": self.cargado_en,
            "plan": self.plan.to_json(),
        }

    @staticmethod
    def from_json(d: dict[str, Any]) -> "PlanAnclado":
        return PlanAnclado(
            plan=plan_desde_json(d["plan"]),
            rango=semana_de(date.fromisoformat(d["rango"]["inicio"])),
            cargado_en=d.get("cargado_en", ""),
        )


class DiaSinFuerza(ValueError):
    pass


class PlanStore:
    def __init__(self, actual: Path | None = None, archivo: Path | None = None):
        self.actual = actual or config.PLAN_ACTUAL_PATH
        self.archivo = archivo or config.PLANES_DIR

    # -- lectura ---------------------------------------------------------

    def cargar(self) -> PlanAnclado | None:
        d = leer_json(self.actual)
        return PlanAnclado.from_json(d) if d else None

    def para_semana(self, rango: RangoSemana) -> PlanAnclado | None:
        """Plan que aplica a una semana dada. Busca en el actual y en el archivo."""
        vigente = self.cargar()
        if vigente and vigente.rango.inicio == rango.inicio:
            return vigente
        d = leer_json(self.archivo / f"{rango.clave}.json")
        return PlanAnclado.from_json(d) if d else None

    # -- escritura -------------------------------------------------------

    def guardar(self, plan: PlanSemanal, rango: RangoSemana | None = None) -> PlanAnclado:
        """Guarda el plan y archiva el anterior si cubria otra semana."""
        destino = rango or semana_actual()
        with lock(self.actual):
            previo = self.cargar()
            if previo and previo.rango.inicio != destino.inicio:
                escribir_json(
                    self.archivo / f"{previo.rango.clave}.json",
                    previo.to_json(),
                    con_lock=False,
                )
            anclado = PlanAnclado(
                plan=plan, rango=destino, cargado_en=ahora_local().isoformat(timespec="seconds")
            )
            escribir_json(self.actual, anclado.to_json(), con_lock=False)
        return anclado

    def marcar_fuerza(
        self, dia: str, completado: bool = True, rango: RangoSemana | None = None
    ) -> PlanAnclado:
        """Marca (o desmarca) una sesion de fuerza.

        La usan /fuerza (sobre el plan vigente) y la ingesta automatica, que
        puede tener que tocar un plan ya archivado: el cron del lunes ingiere la
        semana que acaba de cerrar.
        """
        dia = dia.upper()
        if dia not in ORDEN_DIAS:
            raise DiaSinFuerza(f"dia '{dia}' invalido")

        with lock(self.actual):
            vigente = self.cargar()
            if rango is None or (vigente and vigente.rango.inicio == rango.inicio):
                anclado, destino = vigente, self.actual
            else:
                d = leer_json(self.archivo / f"{rango.clave}.json")
                anclado = PlanAnclado.from_json(d) if d else None
                destino = self.archivo / f"{rango.clave}.json"

            if anclado is None:
                raise FileNotFoundError(
                    f"no hay plan cargado para {rango}" if rango else "no hay plan cargado"
                )
            if not anclado.plan.sesiones[dia].es_fuerza:
                raise DiaSinFuerza(
                    f"el plan no tiene fuerza el {anclado.plan.sesiones[dia].nombre_dia}"
                )
            estado = dict(anclado.plan.fuerza_completada)
            estado[dia] = completado
            nuevo = replace(anclado, plan=replace(anclado.plan, fuerza_completada=estado))
            escribir_json(destino, nuevo.to_json(), con_lock=False)
        return nuevo


def resolver_rango(sufijo: str) -> RangoSemana:
    """Rango destino de un /setplan segun el sufijo del comando.

    Sin sufijo: la semana en curso. Con `proxima`/`siguiente`: la que viene
    (para cargar el domingo por la noche el plan del lunes).
    """
    s = sufijo.strip().lower()
    if not s:
        return semana_actual()
    if s in ("proxima", "próxima", "siguiente", "next"):
        return semana_siguiente()
    raise ValueError(
        f"sufijo '{sufijo}' no reconocido en /setplan "
        "(usa `/setplan` para la semana en curso o `/setplan proxima`)"
    )
