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
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from ..config import ESTIMACION, UMBRALES, Estimacion, Umbrales
from ..db.models import SesionReal, Vuelta
from ..fechas import RangoSemana
from ..plan.models import ORDEN_DIAS, PlanSemanal, Sesion
from .vueltas import alinear

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
    actividades: tuple[str, ...] = ()
    # km trotados de recuperacion que quedaron fuera de la comparacion. Solo
    # tiene valor cuando las vueltas permitieron separarlos; sin esto la linea
    # del reporte diria "8.6km -> 8.6km" y se perderia que se corrieron 10.
    recuperacion_km: float | None = None
    # Todos los tipos planificados ese dia. `tipo_plan` sigue siendo el de la
    # corrida principal (o `fuerza`/`rest` cuando es lo unico), para que el
    # formateo del mensaje no tenga que cambiar de criterio.
    tipos_plan: tuple[str, ...] = ()
    # Estado de la fuerza cuando el dia tiene fuerza ADEMAS de una corrida. Si
    # la fuerza es lo unico del dia sigue yendo en `estado`, como siempre.
    fuerza: dict[str, Any] | None = None
    # Corridas planificadas ese dia. Pesa el porcentaje global: un dia con dos
    # sesiones cumplidas vale dos, no una.
    corridas_planificadas: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "dia": self.dia,
            "fecha": self.fecha,
            "tipo_plan": self.tipo_plan,
            "tipos_plan": list(self.tipos_plan or (self.tipo_plan,)),
            "objetivo": self.objetivo,
            "unidad": self.unidad,
            "real": self.real,
            "pct": self.pct,
            "estado": self.estado,
            "nota": self.nota,
            "actividades": list(self.actividades),
            "recuperacion_km": self.recuperacion_km,
            "fuerza": self.fuerza,
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


def _descontar_recuperacion(
    sesion_plan: Sesion,
    corridas: Sequence[SesionReal],
    real_total: float,
    vueltas: Mapping[tuple[str, str], Sequence[Vuelta]],
) -> tuple[float, str, float | None]:
    """Deja `real` en la distancia DECLARADA, comparable contra la del plan.

    El objetivo de una sesion con `estructura=` es la suma de los bloques, que
    no incluye la recuperacion trotada entre repeticiones. Lo que trae Strava
    como distancia de la actividad si la incluye, asi que compararlos da un
    porcentaje alto por construccion. Con las vueltas se puede emparejar cada
    segmento declarado con la vuelta que le corresponde y dejar fuera el resto.

    Cuando no se puede emparejar se devuelve el total de siempre y la nota dice
    por que: el numero queda inflado, pero se sabe que lo esta.
    """
    objetivo = sesion_plan.objetivo_km() or 0.0
    if len(corridas) > 1:
        motivo = (
            f"{len(corridas)} actividades ese dia y no se sabe cual fue la sesion "
            "de series"
        )
    else:
        alineacion = alinear(
            sesion_plan.estructura.segmentos_km(),
            vueltas.get(corridas[0].clave, ()),
        )
        if alineacion.ok:
            return round(alineacion.declarada_km, 2), (
                f"{alineacion.declarada_km:g}km declarados de {real_total:g}km "
                f"reales; los {alineacion.recuperacion_km:g}km restantes son "
                "recuperacion entre repeticiones, que el plan no declara"
            ), alineacion.recuperacion_km
        motivo = alineacion.motivo
    return real_total, (
        f"comparado contra los {objetivo:g}km duros de `estructura=` pero sobre el "
        "total real, que incluye la recuperacion entre repeticiones: el porcentaje "
        f"sale alto. No se pudo separar porque {motivo}"
    ), None


def _objetivo_agregado(
    planificadas: Sequence[Sesion], est: Estimacion = ESTIMACION
) -> tuple[float | None, str, str]:
    """(objetivo, unidad, nota) del dia sumando todas las corridas planificadas.

    Con una sola sesion es su objetivo en su unidad nativa, igual que siempre.
    Con varias:

    - misma unidad -> se suman y se compara en esa unidad;
    - unidades mezcladas -> el dia se evalua en km, estimando los minutos con
      `km_para_volumen`, y la nota lo dice. Si alguna no se puede estimar, no
      hay objetivo: el dia queda como dato faltante, nunca como incumplimiento.
    """
    if len(planificadas) == 1:
        s = planificadas[0]
        if s.objetivo_km() is not None:
            return s.objetivo_km(), "km", ""
        return s.objetivo_min(), "min", ""

    unidades = {s.unidad for s in planificadas}
    if unidades == {"km"}:
        total = sum(s.objetivo_km() or 0.0 for s in planificadas)
        return round(total, 2), "km", f"{len(planificadas)} sesiones planificadas, sumadas"
    if unidades == {"min"}:
        total = sum(s.objetivo_min() or 0.0 for s in planificadas)
        return round(total, 2), "min", f"{len(planificadas)} sesiones planificadas, sumadas"

    total = 0.0
    for s in planificadas:
        km, _ = s.km_para_volumen(est)
        if km is None:
            return None, "km", (
                f"el dia mezcla km y minutos y la sesion `{s.tipo}` no se puede "
                "estimar en km (sin ritmo): no se compara para no inventar un objetivo"
            )
        total += km
    return round(total, 2), "km", (
        f"{len(planificadas)} sesiones planificadas en unidades distintas: el dia "
        "se compara en km, estimando las prescritas por tiempo"
    )


def _evaluar_dia(
    planificadas: Sequence[Sesion],
    fecha: date,
    reales: Sequence[SesionReal],
    fuerza_completada: bool,
    umbrales: Umbrales,
    vueltas: Mapping[tuple[str, str], Sequence[Vuelta]] = MappingProxyType({}),
) -> DiaAdherencia:
    ids = tuple(s.id_externo for s in reales)
    letra = planificadas[0].dia
    tipos = tuple(s.tipo for s in planificadas)
    sesion_fuerza = next((s for s in planificadas if s.es_fuerza), None)
    corridas_plan = [s for s in planificadas if not s.es_fuerza and not s.es_descanso]

    # `tipo_plan` conserva su semantica de siempre: el tipo de la corrida
    # principal, o `fuerza`/`rest` cuando es lo unico que hay ese dia.
    tipo_plan = corridas_plan[0].tipo if corridas_plan else planificadas[0].tipo
    base = dict(
        dia=letra,
        fecha=fecha.isoformat(),
        tipo_plan=tipo_plan,
        tipos_plan=tipos,
        corridas_planificadas=len(corridas_plan),
    )

    if not corridas_plan and all(s.es_descanso for s in planificadas):
        hubo = bool(reales)
        nombres = ", ".join(s.tipo for s in reales)
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

    if not corridas_plan:  # solo fuerza
        return DiaAdherencia(
            **base,
            objetivo=None,
            unidad=None,
            real=None,
            pct=None,
            estado=FUERZA_OK if fuerza_completada else FUERZA_PENDIENTE,
            nota="" if fuerza_completada else "no detectada en la fuente ni marcada con /fuerza",
            actividades=ids,
        )

    # Ademas de correr, el dia puede tener fuerza. Va en su propio bloque para
    # no mezclar un check binario con un porcentaje de kilometros.
    if sesion_fuerza is not None:
        base["fuerza"] = {
            "estado": FUERZA_OK if fuerza_completada else FUERZA_PENDIENTE,
            "nota": "" if fuerza_completada else "no detectada en la fuente ni marcada con /fuerza",
        }

    # Sesion de carrera: solo se compara contra corridas. La fuerza registrada
    # ese dia no entra, y tampoco la bici o la natacion, que traen distancia
    # propia y falsearian tanto los km como los minutos del dia.
    corridas = [s for s in reales if s.es_run]
    objetivo, unidad, nota_objetivo = _objetivo_agregado(corridas_plan)

    if objetivo is None:
        return DiaAdherencia(
            **base,
            objetivo=None,
            unidad=unidad,
            real=None,
            pct=None,
            estado=DATO_FALTANTE,
            nota=nota_objetivo,
            actividades=ids,
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

    nota = nota_objetivo
    recuperacion = None
    estructuradas = [s for s in corridas_plan if s.estructura is not None]
    # El descuento de recuperacion empareja UNA sesion planificada con UNA
    # actividad real. Con mas de una de cualquiera de los dos lados no se sabe
    # que vuelta pertenece a que sesion, y adivinarlo produciria un numero
    # peor que el total honesto.
    if len(estructuradas) == 1 and len(corridas_plan) == 1 and unidad == "km":
        real, nota_rec, recuperacion = _descontar_recuperacion(
            estructuradas[0], corridas, real, vueltas
        )
        nota = (nota + "; " if nota else "") + nota_rec
    elif estructuradas and unidad == "km":
        nota = (nota + "; " if nota else "") + (
            "hay `estructura=` pero el dia tiene varias sesiones planificadas: no se "
            "pudo separar la recuperacion, el porcentaje sale alto"
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
        recuperacion_km=recuperacion,
    )


def _dia_pendiente(planificadas: Sequence[Sesion], fecha: date) -> DiaAdherencia:
    """Dia de una semana en curso que todavia no llega. No es incumplimiento."""
    corridas_plan = [s for s in planificadas if not s.es_fuerza and not s.es_descanso]
    if corridas_plan:
        objetivo, unidad, _ = _objetivo_agregado(corridas_plan)
    else:
        objetivo, unidad = None, None
    return DiaAdherencia(
        dia=planificadas[0].dia,
        fecha=fecha.isoformat(),
        tipo_plan=corridas_plan[0].tipo if corridas_plan else planificadas[0].tipo,
        tipos_plan=tuple(s.tipo for s in planificadas),
        corridas_planificadas=len(corridas_plan),
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
    vueltas: Mapping[tuple[str, str], Sequence[Vuelta]] | None = None,
) -> ResumenAdherencia:
    """`hasta` (inclusive) evalua una semana en curso; None evalua los 7 dias.

    `vueltas` mapea (fuente, id_externo) -> vueltas de esa actividad. Solo se usan para las
    sesiones con `estructura=`, para separar el trabajo declarado de la
    recuperacion. Sin ellas el calculo es el de antes, con la nota que lo dice.
    """
    vueltas = MappingProxyType({}) if vueltas is None else vueltas
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
            and not plan.es_descanso(letra)
        )
        if futuro or hoy_sin_nada:
            dias.append(_dia_pendiente(plan.dia(letra), fecha))
            continue
        transcurridos.append(letra)
        dias.append(
            _evaluar_dia(
                plan.dia(letra),
                fecha,
                reales_dia,
                plan.fuerza_completada.get(letra, False),
                umbrales,
                vueltas,
            )
        )

    # Se cuentan SESIONES planificadas, no dias: un dia con dos corridas
    # cumplidas vale dos. Con una sesion por dia el numero es el de siempre.
    evaluados = [d for d in dias if d.estado in (CUMPLIDA, BAJO_PLAN, SOBRE_PLAN, SIN_SESION)]
    evaluables = sum(max(d.corridas_planificadas, 1) for d in evaluados)
    cumplidas = sum(
        max(d.corridas_planificadas, 1) for d in evaluados if d.estado == CUMPLIDA
    )
    # La fuerza se cuenta por su propio lado, este o no sola en su dia.
    fuerza = [
        d
        for d in dias
        if d.estado != PENDIENTE
        and ("fuerza" in (d.tipos_plan or (d.tipo_plan,)))
    ]

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
        sesiones_evaluables=evaluables,
        sesiones_cumplidas=cumplidas,
        dias_sin_dato=sum(1 for d in dias if d.estado == DATO_FALTANTE),
        fuerza_planificadas=len(fuerza),
        fuerza_cumplidas=sum(
            1
            for d in fuerza
            if d.estado == FUERZA_OK
            or (d.fuerza or {}).get("estado") == FUERZA_OK
        ),
        no_planificadas=tuple(d.dia for d in dias if d.estado == DESCANSO_ROTO),
        avisos=tuple(avisos),
    )
