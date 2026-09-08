"""Fatiga y descanso: tendencia de bienestar cruzada con la carga.

Es el primer uso del bienestar que la ingesta venia guardando. Solo lo provee
intervals.icu, asi que una semana que salio por el respaldo de Strava no tiene
nada que reportar aca — y eso se dice, no se rellena.

**La regla que no se negocia:** este modulo NO combina bienestar y carga en un
numero. No hay score, no hay indice, no hay ponderacion. `cruce_carga` enuncia
los dos hechos lado a lado con plantillas de Python, porque un HRV a la baja
junto a un ACWR alto es una senal mas fuerte que cualquiera de los dos solos,
pero fusionarlos daria una cifra sin respaldo metodologico que despues alguien
leeria como si significara algo.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..config import BIENESTAR, Bienestar
from ..db.models import BienestarDia

log = logging.getLogger(__name__)

#: (clave, atributo, unidad, si_bajar_es_peor). Lo ultimo solo se usa para
#: redactar la lectura del cruce, no para calcular nada.
METRICAS: tuple[tuple[str, str, str, bool], ...] = (
    ("hrv", "hrv", "ms", True),
    ("hr_reposo", "hr_reposo", "lpm", False),
    ("sueno_h", "sueno_h", "h", True),
    ("sueno_score", "sueno_score", "", True),
    ("readiness", "readiness", "", True),
    ("body_battery", "body_battery", "", True),
)


@dataclass(frozen=True)
class TendenciaBienestar:
    valor: float | None
    anterior: float | None
    delta: float | None
    n_dias: int
    unidad: str
    disponible: bool
    motivo: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "valor": self.valor,
            "semana_anterior": self.anterior,
            "delta": self.delta,
            "n_dias": self.n_dias,
            "unidad": self.unidad,
            "disponible": self.disponible,
            "motivo": self.motivo,
        }


@dataclass(frozen=True)
class ResultadoFatiga:
    disponible: bool
    motivo: str
    metricas: dict[str, TendenciaBienestar]
    descanso: dict[str, Any]
    cruce: dict[str, Any]
    alertas: tuple[str, ...] = field(default_factory=tuple)

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {"disponible": self.disponible, "motivo": self.motivo}
        for clave, t in self.metricas.items():
            d[clave] = t.to_json()
        d["descanso"] = self.descanso
        d["cruce_carga"] = self.cruce
        return d


def _promedio(valores: Sequence[float]) -> float | None:
    return round(statistics.fmean(valores), 1) if valores else None


def _tendencia(
    actual: Sequence[BienestarDia],
    previa: Sequence[BienestarDia],
    atributo: str,
    unidad: str,
    minimos: int,
) -> TendenciaBienestar:
    """Promedio de la semana y delta contra la anterior, o no disponible.

    Los dias sin ese dato no cuentan como ceros: se excluyen, y si quedan menos
    de `minimos` la metrica se declara no disponible con el numero de dias que
    si hubo. Un promedio de un solo dia no es una tendencia.
    """
    a = [v for v in (getattr(d, atributo) for d in actual) if v is not None]
    p = [v for v in (getattr(d, atributo) for d in previa) if v is not None]
    va, vp = _promedio(a), _promedio(p)
    delta = round(va - vp, 1) if va is not None and vp is not None else None

    if not a:
        return TendenciaBienestar(
            valor=None,
            anterior=vp,
            delta=None,
            n_dias=0,
            unidad=unidad,
            disponible=False,
            motivo="la fuente no reporto este dato en la semana",
        )
    if len(a) < minimos:
        return TendenciaBienestar(
            valor=va,
            anterior=vp,
            delta=delta,
            n_dias=len(a),
            unidad=unidad,
            disponible=False,
            motivo=(
                f"solo {len(a)} dia(s) con dato, se necesitan {minimos} para hablar "
                "de una tendencia"
            ),
        )
    return TendenciaBienestar(
        valor=va,
        anterior=vp,
        delta=delta,
        n_dias=len(a),
        unidad=unidad,
        disponible=True,
    )


def _texto_metrica(nombre: str, t: TendenciaBienestar) -> str:
    """Un hecho, con su cifra. Sin interpretacion."""
    if t.valor is None:
        return ""
    unidad = f" {t.unidad}" if t.unidad else ""
    if t.delta is None:
        return f"{nombre} {t.valor}{unidad} (sin semana anterior con que comparar)"
    signo = "+" if t.delta > 0 else ""
    return f"{nombre} {t.valor}{unidad} ({signo}{t.delta} vs la semana anterior)"


def calcular(
    bienestar_actual: Sequence[BienestarDia],
    bienestar_previa: Sequence[BienestarDia],
    r_acwr: Any = None,
    r_foster: Any = None,
    descanso_planificado: Sequence[str] = (),
    descanso_tomado: Sequence[str] = (),
    descanso_roto: Sequence[str] = (),
    cfg: Bienestar = BIENESTAR,
    fuente_degradada: bool = False,
) -> ResultadoFatiga:
    """Bloque de fatiga y descanso de la semana.

    `descanso_*` vienen ya derivados del resumen de adherencia (estados
    `descanso_respetado` / `actividad_no_planificada`): no se recalculan aca
    para que no haya dos definiciones de "dia de descanso tomado".
    """
    metricas = {
        clave: _tendencia(bienestar_actual, bienestar_previa, attr, unidad, cfg.dias_minimos)
        for clave, attr, unidad, _ in METRICAS
    }
    hay_algo = any(t.disponible for t in metricas.values())

    if fuente_degradada:
        motivo = (
            "la semana se ingirio desde el respaldo (Strava), que no expone "
            "bienestar: no hay HRV, HR de reposo, sueno, sleep score, Body "
            "Battery ni readiness"
        )
    elif not hay_algo:
        dias = max((t.n_dias for t in metricas.values()), default=0)
        motivo = (
            f"sin datos de bienestar suficientes esta semana ({dias} dia(s) con "
            f"algun dato, se necesitan {cfg.dias_minimos})"
        )
    else:
        motivo = ""

    # -- descanso --------------------------------------------------------
    extra = [d for d in descanso_tomado if d not in descanso_planificado]
    descanso = {
        "planificados": len(descanso_planificado),
        "tomados": len(descanso_tomado),
        "dias_planificados": list(descanso_planificado),
        "dias_tomados": list(descanso_tomado),
        # Descanso que no estaba en el plan: puede ser fatiga, y el dato existe
        # cruzando el plan contra lo real, sin preguntarle nada al atleta.
        "dias_extra": extra,
        "dias_rotos": list(descanso_roto),
    }

    # -- cruce con la carga ----------------------------------------------
    # Dos hechos, uno al lado del otro. NO hay ninguna operacion que los
    # combine: si aparece una, este modulo dejo de hacer lo que promete.
    acwr = getattr(r_acwr, "ratio", None)
    acwr_ok = bool(getattr(r_acwr, "confiable", False))
    monotony = getattr(r_foster, "monotony", None)
    monotony_ok = bool(getattr(r_foster, "confiable", False))

    partes_bienestar = [
        _texto_metrica(nombre, metricas[clave])
        for clave, nombre in (
            ("hrv", "HRV"),
            ("sueno_h", "sueno"),
            ("readiness", "readiness"),
        )
        if metricas[clave].disponible
    ]
    partes_carga = []
    if acwr is not None:
        partes_carga.append(f"ACWR {acwr}" + ("" if acwr_ok else " (no confiable)"))
    if monotony is not None:
        partes_carga.append(
            f"Monotony {monotony}" + ("" if monotony_ok else " (no confiable)")
        )

    if partes_bienestar and partes_carga:
        lectura = "; ".join(partes_bienestar) + ". Al mismo tiempo: " + ", ".join(partes_carga)
    elif partes_carga:
        lectura = "sin bienestar con que cruzar. Carga: " + ", ".join(partes_carga)
    elif partes_bienestar:
        lectura = "; ".join(partes_bienestar) + ". Sin cifras de carga confiables con que cruzar"
    else:
        lectura = "no hay bienestar ni carga con que decir nada de la semana"

    cruce = {
        "acwr": acwr,
        "acwr_confiable": acwr_ok,
        "monotony": monotony,
        "monotony_confiable": monotony_ok,
        "lectura": lectura,
        "nota": (
            "los dos datos se reportan por separado a proposito: no existe un "
            "indice que los combine"
        ),
    }

    # -- alertas ---------------------------------------------------------
    alertas: list[str] = []
    hrv, readiness = metricas["hrv"], metricas["readiness"]
    if hrv.disponible and hrv.delta is not None and hrv.delta <= -cfg.hrv_caida_ms:
        alertas.append(
            f"HRV bajo {abs(hrv.delta)} ms respecto a la semana anterior "
            f"(umbral {cfg.hrv_caida_ms} ms)"
        )
    if (
        readiness.disponible
        and readiness.valor is not None
        and readiness.valor < cfg.readiness_bajo
    ):
        alertas.append(
            f"Training Readiness promedio {readiness.valor}, bajo el umbral "
            f"{cfg.readiness_bajo}"
        )
    if extra:
        alertas.append(
            f"descanso no planificado el dia(s) {', '.join(extra)}: no estaba en el plan"
        )

    return ResultadoFatiga(
        disponible=hay_algo and not fuente_degradada,
        motivo=motivo,
        metricas=metricas,
        descanso=descanso,
        cruce=cruce,
        alertas=tuple(alertas),
    )
