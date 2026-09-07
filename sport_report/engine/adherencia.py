"""Adherencia al plan, dia por dia.

Cada dia se compara en la unidad nativa de la sesion planificada: si el plan
pide km se compara distancia, si pide minutos se compara duracion. Las tres
distinciones que la spec exige mantener separadas:

  - Sin sesion registrada  -> 0% (incumplimiento real).
  - Sesion registrada pero sin el dato en la unidad que pide el plan (indoor sin
    GPS cuando el plan pedia km) -> `null`, dato faltante. NO es 0%.
  - Sesiones con `estructura=` -> el objetivo es la distancia dura derivada de
    los bloques, no el `cantidad` tecleado a mano. Ojo con lo que eso implica:
    la distancia dura NO incluye la recuperacion trotada entre repeticiones
    —el plan nunca la declara— y lo real que llega de Strava SI la incluye, asi
    que los dos lados de la division no miden lo mismo y una sesion de series
    lee por encima de 100% por construccion. La banda de `_estado_por_pct` es
    ancha (80-120%) para absorber ese sesgo; no lo corrige.

El volumen real de la semana se calcula aparte, sumando el 100% de la distancia
de todas las sesiones: la recuperacion sigue contando para el volumen aunque no
cuente para el % de adherencia de esa sesion.

Para que el porcentaje de volumen compare la misma base en ambos lados, las
sesiones prescritas en minutos aportan sus km estimados al planificado (ver
`Sesion.km_para_volumen`). Sin eso el numerador incluye los km de una sesion que
el denominador ignora, y el porcentaje sale inflado.

Con `hasta` se evalua una semana en curso: los dias posteriores quedan en estado
`pendiente`, fuera del porcentaje de sesiones y fuera del volumen planificado.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable, Sequence

from ..config import UMBRALES, Umbrales
from ..db.models import SesionReal
from ..fechas import RangoSemana
from ..plan.models import ORDEN_DIAS, PlanSemanal, Sesion

# Estados posibles de un dia.
CUMPLIDA = "cumplida"
BAJO_PLAN = "bajo_plan"
SOBRE_PLAN = "sobre_plan"
SIN_SESION = "sin_sesion"
DATO_FALTANTE = "dato_faltante"
DESCANSO_OK = "descanso_respetado"
DESCANSO_ROTO = "actividad_no_planificada"
FUERZA_OK = "fuerza_cumplida"
FUERZA_PENDIENTE = "fuerza_pendiente"
# Solo aparece al consultar una semana en curso: el dia todavia no llega.
PENDIENTE = "pendiente"


@dataclass(frozen=True)
class DiaAdherencia:
    dia: str
    fecha: str
    tipo_plan: str
    objetivo: float | None
    unidad: str | None
    real: float | None
    pct: float | None
    estado: str
    nota: str = ""
    actividades: tuple[int, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "dia": self.dia,
            "fecha": self.fecha,
            "tipo_plan": self.tipo_plan,
            "objetivo": self.objetivo,
            "unidad": self.unidad,
            "real": self.real,
            "pct": self.pct,
            "estado": self.estado,
            "nota": self.nota,
            "actividades": list(self.actividades),
        }


@dataclass(frozen=True)
class ResumenAdherencia:
    dias: tuple[DiaAdherencia, ...]
    volumen_planificado_km: float
    volumen_real_km: float
    volumen_pct: float | None
    volumen_estimado_km: float = 0.0
    # Total de los 7 dias, para el resumen de la semana en curso.
    volumen_planificado_semana_km: float = 0.0
    dias_transcurridos: int = 7
    sesiones_evaluables: int = 0
    sesiones_cumplidas: int = 0
    dias_sin_dato: int = 0
    fuerza_planificadas: int = 0
    fuerza_cumplidas: int = 0
    no_planificadas: tuple[str, ...] = ()
    avisos: tuple[str, ...] = field(default_factory=tuple)

    @property
    def pct_global(self) -> float | None:
        if not self.sesiones_evaluables:
            return None
        return round(self.sesiones_cumplidas / self.sesiones_evaluables * 100, 1)

    @property
    def en_curso(self) -> bool:
        return self.dias_transcurridos < len(ORDEN_DIAS)

    def to_json(self) -> dict[str, Any]:
        return {
            "dias": [d.to_json() for d in self.dias],
            "volumen_planificado_km": self.volumen_planificado_km,
            "volumen_real_km": self.volumen_real_km,
            "volumen_pct": self.volumen_pct,
            "volumen_estimado_km": self.volumen_estimado_km,
            "volumen_planificado_semana_km": self.volumen_planificado_semana_km,
            "dias_transcurridos": self.dias_transcurridos,
            "en_curso": self.en_curso,
            "sesiones_evaluables": self.sesiones_evaluables,
            "sesiones_cumplidas": self.sesiones_cumplidas,
            "pct_global": self.pct_global,
            "dias_sin_dato": self.dias_sin_dato,
            "fuerza": {
                "planificadas": self.fuerza_planificadas,
                "cumplidas": self.fuerza_cumplidas,
            },
            "no_planificadas": list(self.no_planificadas),
            "avisos": list(self.avisos),
        }


def _real_en_unidad(
    sesiones: Sequence[SesionReal], unidad: str
) -> tuple[float | None, bool]:
    """Suma lo real en la unidad pedida. Devuelve (valor, hubo_dato_faltante)."""
    if unidad == "km":
        valores = [s.distancia_km for s in sesiones]
    else:
        valores = [s.duracion_min for s in sesiones]
    presentes = [v for v in valores if v is not None]
    falta = len(presentes) < len(valores)
    if not presentes:
        return None, True
    return round(sum(presentes), 2), falta


def _estado_por_pct(pct: float, umbrales: Umbrales) -> str:
    if pct < umbrales.adherencia_min_pct:
        return BAJO_PLAN
    if pct > umbrales.adherencia_max_pct:
        return SOBRE_PLAN
    return CUMPLIDA


def _evaluar_dia(
    sesion_plan: Sesion,
    fecha: date,
    reales: Sequence[SesionReal],
    fuerza_completada: bool,
    umbrales: Umbrales,
) -> DiaAdherencia:
    ids = tuple(s.strava_id for s in reales)
    base = dict(dia=sesion_plan.dia, fecha=fecha.isoformat(), tipo_plan=sesion_plan.tipo)

    if sesion_plan.es_descanso:
        hubo = bool(reales)
        nombres = ", ".join(s.tipo_strava for s in reales)
        return DiaAdherencia(
            **base,
            objetivo=None,
            unidad=None,
            real=None,
            pct=None,
            estado=DESCANSO_ROTO if hubo else DESCANSO_OK,
            nota=f"actividad no planificada: {nombres}" if hubo else "",
            actividades=ids,
        )

    if sesion_plan.es_fuerza:
        return DiaAdherencia(
            **base,
            objetivo=None,
            unidad=None,
            real=None,
            pct=None,
            estado=FUERZA_OK if fuerza_completada else FUERZA_PENDIENTE,
            nota="" if fuerza_completada else "no detectada en Strava ni marcada con /fuerza",
            actividades=ids,
        )

    # Sesion de carrera: solo se compara contra corridas. La fuerza registrada
    # ese dia no entra, y tampoco la bici o la natacion, que traen distancia
    # propia y falsearian tanto los km como los minutos del dia.
    corridas = [s for s in reales if s.es_run]
    unidad = "km" if sesion_plan.objetivo_km() is not None else "min"
    objetivo = sesion_plan.objetivo_km() if unidad == "km" else sesion_plan.objetivo_min()

    nota = ""
    if sesion_plan.estructura is not None:
        nota = (
            f"comparado contra {objetivo:g}km duros de `estructura=`, no contra el "
            "total real (incluye recuperacion entre reps)"
        )

    if not corridas:
        return DiaAdherencia(
            **base,
            objetivo=objetivo,
            unidad=unidad,
            real=None,
            pct=0.0,
            estado=SIN_SESION,
            nota="sin actividad registrada ese dia",
            actividades=(),
        )

    real, falta = _real_en_unidad(corridas, unidad)
    if real is None:
        return DiaAdherencia(
            **base,
            objetivo=objetivo,
            unidad=unidad,
            real=None,
            pct=None,
            estado=DATO_FALTANTE,
            nota=(
                f"hubo sesion pero sin {'distancia' if unidad == 'km' else 'duracion'} "
                "registrada; no es lo mismo que no entrenar"
            ),
            actividades=ids,
        )

    if falta:
        nota = (nota + "; " if nota else "") + "alguna sesion del dia no aporto el dato"

    pct = round(real / objetivo * 100, 1) if objetivo else None
    return DiaAdherencia(
        **base,
        objetivo=objetivo,
        unidad=unidad,
        real=real,
        pct=pct,
        estado=_estado_por_pct(pct, umbrales) if pct is not None else DATO_FALTANTE,
        nota=nota,
        actividades=ids,
    )


def _dia_pendiente(sesion_plan: Sesion, fecha: date) -> DiaAdherencia:
    """Dia de una semana en curso que todavia no llega. No es incumplimiento."""
    unidad = "km" if sesion_plan.objetivo_km() is not None else (
        "min" if sesion_plan.objetivo_min() is not None else None
    )
    objetivo = sesion_plan.objetivo_km() if unidad == "km" else sesion_plan.objetivo_min()
    return DiaAdherencia(
        dia=sesion_plan.dia,
        fecha=fecha.isoformat(),
        tipo_plan=sesion_plan.tipo,
        objetivo=objetivo,
        unidad=unidad,
        real=None,
        pct=None,
        estado=PENDIENTE,
        nota="todavia no llega",
    )


def calcular(
    plan: PlanSemanal | None,
    rango: RangoSemana,
    sesiones: Iterable[SesionReal],
    umbrales: Umbrales = UMBRALES,
    hasta: date | None = None,
) -> ResumenAdherencia:
    """`hasta` (inclusive) evalua una semana en curso; None evalua los 7 dias."""
    por_fecha: dict[date, list[SesionReal]] = {}
    for s in sesiones:
        por_fecha.setdefault(s.fecha, []).append(s)

    # El volumen real suma el 100% de la distancia de todas las CORRIDAS,
    # recuperacion incluida, sin importar como se evaluo cada dia. Otros
    # deportes quedan fuera: este reporte mide running, y una salida en bici
    # tambien trae `distancia_km`.
    volumen_real = round(
        sum(s.distancia_km or 0.0 for lista in por_fecha.values() for s in lista if s.es_run),
        2,
    )

    if plan is None:
        return ResumenAdherencia(
            dias=(),
            volumen_planificado_km=0.0,
            volumen_real_km=volumen_real,
            volumen_pct=None,
            dias_transcurridos=sum(
                1 for d in rango.dias() if hasta is None or d <= hasta
            ),
            avisos=("no habia plan cargado para esta semana: solo se reporta lo real",),
        )

    dias: list[DiaAdherencia] = []
    transcurridos: list[str] = []
    for i, letra in enumerate(ORDEN_DIAS):
        fecha = rango.inicio + timedelta(days=i)
        reales_dia = por_fecha.get(fecha, [])
        # Un dia futuro esta pendiente. HOY tambien, mientras no haya nada
        # registrado: el dia no ha terminado y marcarlo como incumplido a las
        # diez de la manana seria falso.
        futuro = hasta is not None and fecha > hasta
        hoy_sin_nada = (
            hasta is not None
            and fecha == hasta
            and not reales_dia
            and not plan.sesiones[letra].es_descanso
        )
        if futuro or hoy_sin_nada:
            dias.append(_dia_pendiente(plan.sesiones[letra], fecha))
            continue
        transcurridos.append(letra)
        dias.append(
            _evaluar_dia(
                plan.sesiones[letra],
                fecha,
                reales_dia,
                plan.fuerza_completada.get(letra, False),
                umbrales,
            )
        )

    evaluables = [d for d in dias if d.estado in (CUMPLIDA, BAJO_PLAN, SOBRE_PLAN, SIN_SESION)]
    cumplidas = [d for d in evaluables if d.estado == CUMPLIDA]
    fuerza = [d for d in dias if d.tipo_plan == "fuerza" and d.estado != PENDIENTE]

    # El volumen planificado se limita a los dias transcurridos: comparar lo
    # corrido hasta hoy contra el total de la semana daria siempre "bajo plan".
    planificado = plan.volumen_planificado_km(transcurridos)
    estimado = plan.volumen_estimado_km(transcurridos)
    planificado_semana = plan.volumen_planificado_km()

    # Que parte del planificado es estimada no va como aviso: se muestra en la
    # propia linea de volumen, pegada a la cifra, y repetirlo abajo es ruido.
    # El dato sigue en el JSON como `volumen.estimado_km`.
    avisos: list[str] = []
    sin_estimar = plan.dias_sin_estimar(transcurridos)
    if sin_estimar:
        nombres = ", ".join(sin_estimar)
        avisos.append(
            f"dia(s) {nombres}: prescritos en minutos y sin ritmo con que estimar km, "
            "quedan fuera del volumen planificado"
        )

    return ResumenAdherencia(
        dias=tuple(dias),
        volumen_planificado_km=planificado,
        volumen_real_km=volumen_real,
        volumen_pct=round(volumen_real / planificado * 100, 1) if planificado else None,
        volumen_estimado_km=estimado,
        volumen_planificado_semana_km=planificado_semana,
        dias_transcurridos=len(transcurridos),
        sesiones_evaluables=len(evaluables),
        sesiones_cumplidas=len(cumplidas),
        dias_sin_dato=sum(1 for d in dias if d.estado == DATO_FALTANTE),
        fuerza_planificadas=len(fuerza),
        fuerza_cumplidas=sum(1 for d in fuerza if d.estado == FUERZA_OK),
        no_planificadas=tuple(d.dia for d in dias if d.estado == DESCANSO_ROTO),
        avisos=tuple(avisos),
    )
