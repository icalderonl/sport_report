"""Agregados mensuales. Solo carrera: calle, pista, cinta, trail.

**Alcance (decision explicita):** el mensual mide CARRERA y nada mas. Fuerza y
cualquier otro deporte quedan fuera de volumen, carga, adherencia y dinamica.
El filtro es el `es_run` que ya existe (`config.TIPOS_RUN`), el mismo que usan
el volumen semanal y el grafico; no hay una regla nueva que se pueda desviar.

Se recalcula todo desde la base, semana a semana, reusando los mismos modulos
que el semanal (`acwr`, `foster`, `adherencia`). No se leen los JSON de
`data/reportes/`: puede faltar la semana de una corrida que fallo, y entonces
el mensual no seria reproducible.

Y como el JSON mensual SI lleva la serie semanal al prompt —al contrario que el
semanal, al que se le quita `volumen_historico` para que el modelo no derive
tendencias—, todo lo derivable va precalculado aca: progresion, semanas al
alza, delta contra el mes anterior, promedios y maximos. El modelo cita; no
calcula.
"""
from __future__ import annotations

import logging
import statistics
from datetime import date, timedelta
from typing import Any, Sequence

from .. import config
from ..config import UMBRALES, Umbrales
from ..db.models import BienestarDia, SesionReal
from ..db.repo import Repo
from ..fechas import MesRango, RangoSemana, ahora_local, mes_anterior, semanas_del_mes
from ..plan.models import ORDEN_DIAS, PlanSemanal
from ..plan.store import PlanStore
from . import acwr as m_acwr
from . import adherencia as m_adh
from . import fatiga as m_fatiga
from . import foster as m_foster

#: Tipos de plan que cuentan como "entrenamiento de calidad" para el mensual.
#: El largo ("long") se reporta aparte: es volumen, no intensidad.
TIPOS_CALIDAD = ("tempo", "series", "fartlek")

log = logging.getLogger(__name__)

VERSION_REPORTE_MENSUAL = 2

ALCANCE = "solo carrera"


def _promedio(valores: Sequence[float]) -> float | None:
    return round(statistics.fmean(valores), 1) if valores else None


def _delta(a: float | None, b: float | None) -> float | None:
    return round(a - b, 1) if a is not None and b is not None else None


def _corridas(sesiones: Sequence[SesionReal]) -> list[SesionReal]:
    """El filtro de alcance, en un solo lugar."""
    return [s for s in sesiones if s.es_run]


def _tendencia_mes(
    actual: Sequence[SesionReal],
    previo: Sequence[SesionReal],
    atributo: str,
    unidad: str,
) -> dict[str, Any]:
    """Promedio del mes y delta contra el mes anterior, por sesion de carrera."""
    a = [v for v in (getattr(s, atributo) for s in actual) if v]
    p = [v for v in (getattr(s, atributo) for s in previo) if v]
    valor, anterior = _promedio(a), _promedio(p)
    return {
        "valor": valor,
        "mes_anterior": anterior,
        "delta": _delta(valor, anterior),
        "n_sesiones": len(a),
        "unidad": unidad,
        "disponible": valor is not None,
        "motivo": (
            ""
            if valor is not None
            else f"ninguna de las {len(actual)} corridas del mes reporto este dato"
        ),
    }


def _clasificar_sesiones(
    rango: RangoSemana, plan: PlanSemanal | None, sesiones: Sequence[SesionReal]
) -> tuple[float | None, dict[str, Any]]:
    """(km del largo de la semana, detalle de calidad), segun lo prescrito.

    El tipo de una corrida real (tempo, series, fartlek, largo...) no existe en
    la fuente: solo el plan lo declara. Se clasifica cada corrida por lo que el
    plan pedia ESE dia, el mismo criterio que ya usa `tipo_plan` en
    `engine/adherencia.py`. Sin plan esa semana no hay con que clasificar, y se
    dice explicitamente en vez de adivinar a partir del ritmo o la distancia.
    """
    calidad_km = {t: 0.0 for t in TIPOS_CALIDAD}
    n_calidad = 0
    if plan is None:
        return None, {**calidad_km, "n_sesiones": n_calidad, "km": 0.0}

    por_fecha: dict[date, list[SesionReal]] = {}
    for s in sesiones:
        por_fecha.setdefault(s.fecha, []).append(s)

    largo_km: float | None = None
    for i, letra in enumerate(ORDEN_DIAS):
        fecha = rango.inicio + timedelta(days=i)
        corridas_plan = [s for s in plan.dia(letra) if not s.es_fuerza and s.tipo != "rest"]
        if not corridas_plan:
            continue
        reales = por_fecha.get(fecha, [])
        if not reales:
            continue
        km_real = round(sum(s.distancia_km or 0.0 for s in reales), 2)
        # Con mas de una corrida planificada el dia el tipo de la principal
        # manda, igual que `tipo_plan` en adherencia: no hay como saber cual
        # actividad real corresponde a cual linea del plan.
        tipo = corridas_plan[0].tipo
        if tipo == "long":
            largo_km = (largo_km or 0.0) + km_real
        elif tipo in calidad_km:
            calidad_km[tipo] += km_real
            n_calidad += 1

    return largo_km, {
        **{t: round(v, 2) for t, v in calidad_km.items()},
        "n_sesiones": n_calidad,
        "km": round(sum(calidad_km.values()), 2),
    }


def _semana(
    rango: RangoSemana,
    repo: Repo,
    plan_store: PlanStore,
    umbrales: Umbrales,
) -> dict[str, Any]:
    """Las cifras de una semana, recalculadas. Solo corridas."""
    sesiones = _corridas(repo.sesiones_entre(rango.inicio, rango.fin))
    carga_por_dia = repo.carga_diaria(
        rango.fin - timedelta(days=m_acwr.DIAS_CRONICA - 1), rango.fin
    )
    r_acwr = m_acwr.calcular(
        carga_por_dia,
        fin=rango.fin,
        primera_fecha=repo.primera_fecha(),
        sesiones_sin_carga=sum(1 for s in sesiones if s.carga is None),
        umbrales=umbrales,
    )
    r_foster = m_foster.calcular(carga_por_dia, inicio=rango.inicio, umbrales=umbrales)

    anclado = plan_store.para_semana(rango)
    plan = anclado.plan if anclado else None
    r_adh = m_adh.calcular(
        plan,
        rango,
        sesiones,
        umbrales=umbrales,
        vueltas=repo.vueltas_entre(rango.inicio, rango.fin),
    )

    largo_km, calidad = _clasificar_sesiones(rango, plan, sesiones)

    km = round(sum(s.distancia_km or 0.0 for s in sesiones), 1)
    return {
        "lunes": rango.clave,
        "km": km,
        "corridas": len(sesiones),
        "carga": r_foster.carga_semanal,
        "acwr": r_acwr.ratio,
        "acwr_confiable": r_acwr.confiable,
        "monotony": r_foster.monotony,
        "monotony_confiable": r_foster.confiable,
        # None (no 0.0) cuando la semana no tenia plan: no es 0% de adherencia.
        "adherencia_pct": r_adh.pct_global if plan else None,
        "con_plan": plan is not None,
        "largo_km": largo_km,
        "calidad": calidad,
    }


def _volumen(semanas: list[dict[str, Any]], km_mes_anterior: float | None) -> dict[str, Any]:
    kms = [s["km"] for s in semanas]
    total = round(sum(kms), 1)
    promedio = _promedio(kms)

    # Progresion: primera semana contra ultima. Con menos de dos semanas no
    # existe progresion que reportar, y decir 0% seria afirmar algo falso.
    if len(kms) >= 2 and kms[0] > 0:
        progresion = round((kms[-1] - kms[0]) / kms[0] * 100, 1)
    else:
        progresion = None
    al_alza = sum(1 for a, b in zip(kms, kms[1:]) if b > a)

    confiable = len([k for k in kms if k > 0]) >= 3
    motivo = (
        ""
        if confiable
        else (
            f"solo {len([k for k in kms if k > 0])} de {len(kms)} semanas del mes "
            "tienen kilometros: no alcanza para hablar de progresion"
        )
    )
    return {
        "total_km": total,
        "por_semana": [{"lunes": s["lunes"], "km": s["km"]} for s in semanas],
        "promedio_km": promedio,
        "progresion_pct": progresion if confiable else None,
        "semanas_al_alza": al_alza,
        "semanas": len(kms),
        "mes_anterior_km": km_mes_anterior,
        "delta_km": _delta(total, km_mes_anterior),
        "confiable": confiable,
        "motivo": motivo,
    }


def _serie(
    semanas: list[dict[str, Any]], clave: str, clave_confiable: str
) -> dict[str, Any]:
    """Promedio y maximo de una metrica semanal a lo largo del mes."""
    validos = [
        s[clave] for s in semanas if s[clave] is not None and s[clave_confiable]
    ]
    todos = [s[clave] for s in semanas if s[clave] is not None]
    return {
        "por_semana": [
            {"lunes": s["lunes"], "valor": s[clave], "confiable": s[clave_confiable]}
            for s in semanas
        ],
        "promedio": _promedio(validos),
        "maximo": round(max(validos), 2) if validos else None,
        "confiable": bool(validos),
        "motivo": (
            ""
            if validos
            else (
                f"ninguna de las {len(semanas)} semanas del mes tiene un valor "
                "confiable" + (f" ({len(todos)} calculados pero no confiables)" if todos else "")
            )
        ),
    }


def _adherencia(semanas: list[dict[str, Any]]) -> dict[str, Any]:
    con_plan = [s for s in semanas if s["con_plan"] and s["adherencia_pct"] is not None]
    return {
        "por_semana": [
            {"lunes": s["lunes"], "pct": s["adherencia_pct"], "con_plan": s["con_plan"]}
            for s in semanas
        ],
        "promedio_pct": _promedio([s["adherencia_pct"] for s in con_plan]),
        "semanas_con_plan": len(con_plan),
        "semanas_sin_plan": len(semanas) - len(con_plan),
        "confiable": bool(con_plan),
        "motivo": (
            ""
            if con_plan
            else "ninguna semana del mes tenia plan cargado con que comparar"
        ),
        "nota": "cuenta solo sesiones de carrera; la fuerza no entra en el mensual",
    }


def _largos_mes(semanas: list[dict[str, Any]]) -> dict[str, Any]:
    """El largo semanal a lo largo del mes, tal como lo clasifico `_semana`."""
    con_dato = [s["largo_km"] for s in semanas if s["largo_km"] is not None]
    return {
        "por_semana": [{"lunes": s["lunes"], "km": s["largo_km"]} for s in semanas],
        "promedio_km": _promedio(con_dato),
        "maximo_km": round(max(con_dato), 1) if con_dato else None,
        "semanas_con_largo": len(con_dato),
        "disponible": bool(con_dato),
        "motivo": (
            ""
            if con_dato
            else "ninguna semana del mes tenia un 'largo' prescrito en el plan, "
            "o no habia plan cargado con que identificarlo"
        ),
    }


def _calidad_mes(semanas: list[dict[str, Any]], total_km_mes: float) -> dict[str, Any]:
    """Sesiones de tempo/series/fartlek del mes: cuantas y que parte del volumen."""
    por_tipo = {
        t: round(sum(s["calidad"][t] for s in semanas), 2) for t in TIPOS_CALIDAD
    }
    km_total = round(sum(por_tipo.values()), 2)
    n_sesiones = sum(s["calidad"]["n_sesiones"] for s in semanas)
    semanas_sin_plan = sum(1 for s in semanas if not s["con_plan"])
    disponible = semanas_sin_plan < len(semanas) if semanas else False
    return {
        **{f"{t}_km": v for t, v in por_tipo.items()},
        "km_total": km_total,
        "n_sesiones": n_sesiones,
        "pct_volumen_total": (
            round(km_total / total_km_mes * 100, 1) if total_km_mes else None
        ),
        "semanas_sin_plan": semanas_sin_plan,
        "disponible": disponible,
        "motivo": (
            ""
            if disponible
            else "ninguna semana del mes tenia plan cargado: no se pudo "
            "clasificar las sesiones por tipo"
        ),
    }


def _cambios_dinamica(dinamica: dict[str, Any], umbrales: Umbrales) -> list[str]:
    """Metricas de dinamica cuyo cambio mes-contra-mes supera el umbral.

    Devuelve frases listas para el reporte, no solo los nombres: el gatillo del
    segundo mensaje mensual (fatiga y dinamica) tiene que poder explicarse sin
    volver a mirar el JSON.
    """
    revisar = (
        ("cadencia", "cadencia", umbrales.cadencia_cambio_spm),
        ("gct", "GCT", umbrales.gct_cambio_ms),
        ("oscilacion_vertical", "oscilacion vertical", umbrales.oscilacion_vertical_cambio_cm),
        ("ratio_vertical", "ratio vertical", umbrales.ratio_vertical_cambio_pct),
    )
    motivos = []
    for clave, nombre, umbral in revisar:
        d = dinamica[clave]
        if d["delta"] is not None and abs(d["delta"]) >= umbral:
            signo = "+" if d["delta"] > 0 else ""
            motivos.append(
                f"{nombre} cambio {signo}{d['delta']:g} {d['unidad']} respecto al "
                f"mes anterior (umbral {umbral:g})"
            )
    return motivos


def _fatiga_mensual(
    bienestar: Sequence[BienestarDia],
    bienestar_previo: Sequence[BienestarDia],
    semanas_fuente: list[dict[str, Any]],
    cfg: config.Bienestar,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Tendencia de bienestar del mes contra el mes anterior, y sus alertas.

    Se reusa el mismo modulo del semanal, con la ventana de un mes: la regla de
    no fusionar bienestar y carga vale igual aca. Las alertas se devuelven
    aparte (no las trae `to_json`) porque alimentan el gatillo del segundo
    mensaje mensual, ademas de la lista `alertas` de siempre.
    """
    degradadas = [s for s in semanas_fuente if s.get("fallback")]
    r = m_fatiga.calcular(
        bienestar,
        bienestar_previo,
        descanso_planificado=(),
        descanso_tomado=(),
        cfg=cfg,
        fuente_degradada=False,
    )
    d = r.to_json()
    d["nota"] = (
        "dato de cuerpo completo, no atribuible solo a la carrera; se incluye "
        "como contexto de fatiga"
    )
    if degradadas:
        d["semanas_sin_bienestar"] = [s["semana"] for s in degradadas]
    return d, r.alertas


def construir(
    mes: MesRango,
    repo: Repo,
    plan_store: PlanStore | None = None,
    carrera: Any = None,
    umbrales: Umbrales = UMBRALES,
) -> dict[str, Any]:
    """JSON mensual. Unica fuente de cifras del reporte mensual."""
    plan_store = plan_store or PlanStore()
    previo = mes_anterior(mes)

    semanas_rango = semanas_del_mes(mes)
    semanas = [_semana(r, repo, plan_store, umbrales) for r in semanas_rango]

    sesiones_mes = _corridas(repo.sesiones_entre(mes.inicio, mes.fin))
    sesiones_previo = _corridas(repo.sesiones_entre(previo.inicio, previo.fin))
    km_previo = (
        round(sum(s.distancia_km or 0.0 for s in sesiones_previo), 1)
        if sesiones_previo
        else None
    )

    fuentes = repo.fuentes_entre(
        semanas_rango[0].clave if semanas_rango else mes.inicio.isoformat(),
        semanas_rango[-1].clave if semanas_rango else mes.fin.isoformat(),
    )
    degradadas = [f["semana"] for f in fuentes if f["fallback"]]

    bienestar = repo.bienestar_entre(mes.inicio, mes.fin)
    bienestar_previo = repo.bienestar_entre(previo.inicio, previo.fin)

    # Semanas a caballo entre dos meses: se asignan al mes de su lunes, y aca
    # se dice cuales quedaron partidas para que el texto no presente el mes
    # como si cerrara exacto.
    incompletas = [
        r.clave for r in semanas_rango if not mes.contiene(r.fin)
    ]

    avisos: list[str] = []
    if degradadas:
        avisos.append(
            f"{len(degradadas)} semana(s) del mes se ingirieron desde el respaldo "
            f"({', '.join(degradadas)}): sin GCT, oscilacion vertical, ratio "
            "vertical ni bienestar. La dinamica del mes sale solo de las demas"
        )
    if not sesiones_mes:
        avisos.append("el mes no tiene ninguna corrida registrada")

    dinamica = {
        "cadencia": _tendencia_mes(sesiones_mes, sesiones_previo, "cadencia_spm", "spm"),
        "gct": _tendencia_mes(sesiones_mes, sesiones_previo, "gct_ms", "ms"),
        "oscilacion_vertical": _tendencia_mes(
            sesiones_mes, sesiones_previo, "oscilacion_vertical_cm", "cm"
        ),
        "ratio_vertical": _tendencia_mes(
            sesiones_mes, sesiones_previo, "ratio_vertical_pct", "%"
        ),
        "semanas_con_dinamica": len(semanas_rango) - len(degradadas),
    }

    r_acwr_mes = _serie(semanas, "acwr", "acwr_confiable")
    r_mono_mes = _serie(semanas, "monotony", "monotony_confiable")
    volumen = _volumen(semanas, km_previo)
    fatiga_descanso, alertas_fatiga = _fatiga_mensual(
        bienestar, bienestar_previo, fuentes, config.BIENESTAR
    )

    alertas: list[str] = []
    if r_acwr_mes["maximo"] is not None and r_acwr_mes["maximo"] > umbrales.acwr_alto:
        alertas.append(
            f"ACWR maximo del mes {r_acwr_mes['maximo']} sobre el umbral "
            f"{umbrales.acwr_alto}"
        )
    if r_mono_mes["maximo"] is not None and r_mono_mes["maximo"] > umbrales.monotony_alta:
        alertas.append(
            f"Monotony maximo del mes {r_mono_mes['maximo']} sobre el umbral "
            f"{umbrales.monotony_alta}"
        )
    alertas.extend(alertas_fatiga)

    # Gatillo del segundo mensaje mensual (fatiga y dinamica): solo se envia si
    # hubo algo que amerite revisarlo, no en cada corrida (spec del atleta).
    cambios_dinamica = _cambios_dinamica(dinamica, umbrales)
    motivos_mensaje2 = list(alertas) + cambios_dinamica

    return {
        "version": VERSION_REPORTE_MENSUAL,
        "generado_en": ahora_local().isoformat(timespec="seconds"),
        "mes": {
            "clave": mes.clave,
            "inicio": mes.inicio.isoformat(),
            "fin": mes.fin.isoformat(),
            "semanas": len(semanas_rango),
            "semanas_incompletas": incompletas,
            "alcance": ALCANCE,
            "tipos_incluidos": list(config.TIPOS_RUN),
            "nota_alcance": (
                "este reporte mide solo carrera (calle, pista, cinta, trail). "
                "La fuerza y cualquier otro deporte quedan fuera de todas las "
                "cifras de abajo"
            ),
        },
        "volumen": volumen,
        "carga": {
            "total": round(sum(s["carga"] or 0.0 for s in semanas), 1),
            "por_semana": [{"lunes": s["lunes"], "carga": s["carga"]} for s in semanas],
            "corridas": sum(s["corridas"] for s in semanas),
        },
        "acwr": r_acwr_mes,
        "monotony": r_mono_mes,
        "adherencia": _adherencia(semanas),
        "largos": _largos_mes(semanas),
        "calidad": _calidad_mes(semanas, volumen["total_km"]),
        "dinamica": dinamica,
        "fatiga_descanso": fatiga_descanso,
        "carrera": carrera.contexto() if carrera is not None else None,
        "revisar_fatiga_dinamica": {
            "relevante": bool(motivos_mensaje2),
            "motivos": motivos_mensaje2,
        },
        "fuentes_usadas": [
            {"semana": f["semana"], "fuente": f["fuente"], "fallback": f["fallback"]}
            for f in fuentes
        ],
        "alertas": alertas,
        "avisos_datos": avisos,
        "umbrales": {
            "acwr_alto": umbrales.acwr_alto,
            "acwr_bajo": umbrales.acwr_bajo,
            "monotony_alta": umbrales.monotony_alta,
            "cadencia_cambio_spm": umbrales.cadencia_cambio_spm,
            "gct_cambio_ms": umbrales.gct_cambio_ms,
            "oscilacion_vertical_cambio_cm": umbrales.oscilacion_vertical_cambio_cm,
            "ratio_vertical_cambio_pct": umbrales.ratio_vertical_cambio_pct,
            "nota": "valores de literatura general, no calibrados a este atleta",
        },
    }
