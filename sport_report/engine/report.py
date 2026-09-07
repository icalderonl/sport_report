"""Motor de calculo: produce el JSON que es la unica fuente de cifras.

La capa narrativa (fase 6) recibe exactamente este JSON y no puede recalcular ni
agregar numeros. Todo lo que el reporte pueda decir tiene que estar aca, con su
bandera de confiabilidad al lado.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from .. import config
from ..config import UMBRALES, Umbrales
from ..db.models import SesionReal
from ..db.repo import Repo
from ..fechas import RangoSemana, ahora_local, semana_anterior
from ..plan.store import PlanStore
from . import acwr as m_acwr
from . import adherencia as m_adh
from . import foster as m_foster

log = logging.getLogger(__name__)

VERSION_REPORTE = 1


@dataclass(frozen=True)
class Tendencia:
    valor: float | None
    anterior: float | None
    delta: float | None
    n: int

    def to_json(self) -> dict[str, Any]:
        return {
            "valor": self.valor,
            "semana_anterior": self.anterior,
            "delta": self.delta,
            "n_sesiones": self.n,
        }


def _promedio(valores: list[float]) -> float | None:
    return round(statistics.fmean(valores), 1) if valores else None


def _cadencia(actual: list[SesionReal], previa: list[SesionReal]) -> Tendencia:
    a = [s.cadencia_spm for s in actual if s.cadencia_spm]
    p = [s.cadencia_spm for s in previa if s.cadencia_spm]
    va, vp = _promedio(a), _promedio(p)
    delta = round(va - vp, 1) if va is not None and vp is not None else None
    return Tendencia(valor=va, anterior=vp, delta=delta, n=len(a))


def _decoupling(sesiones: list[SesionReal], umbrales: Umbrales) -> dict[str, Any]:
    valores = [s.decoupling_pct for s in sesiones if s.decoupling_pct is not None]
    # Solo las corridas pueden tener deriva: contar una salida en bici como
    # "sesion sin dato" inventaba un hueco que no existe.
    sin_dato = sum(1 for s in sesiones if s.es_run and s.decoupling_pct is None)
    promedio = _promedio(valores)
    maximo = round(max(valores), 2) if valores else None
    alerta = ""
    if maximo is not None and maximo > umbrales.decoupling_alto_pct:
        alerta = (
            f"deriva cardiaca maxima {maximo}% sobre el umbral "
            f"{umbrales.decoupling_alto_pct}%"
        )
    return {
        "promedio_pct": promedio,
        "maximo_pct": maximo,
        "n_sesiones": len(valores),
        "sin_dato": sin_dato,
        "alerta": alerta,
    }


def construir(
    rango: RangoSemana,
    repo: Repo,
    plan_store: PlanStore | None = None,
    umbrales: Umbrales = UMBRALES,
    hasta: date | None = None,
) -> dict[str, Any]:
    """`hasta` (inclusive) construye el reporte de una semana en curso.

    Todas las ventanas moviles terminan ahi en vez de en el domingo, para que
    ACWR y Monotony no cuenten como descanso dias que aun no han llegado.
    """
    plan_store = plan_store or PlanStore()
    anclado = plan_store.para_semana(rango)
    plan = anclado.plan if anclado else None

    # `<=`, no `<`: el domingo la semana TODAVIA esta en curso. Con `<` se
    # apagaba todo el modo "semana en curso" justo el ultimo dia, y la sesion
    # del domingo aparecia como no registrada en vez de como pendiente.
    corte = hasta if hasta is not None and hasta <= rango.fin else None
    fin_ventana = corte or rango.fin
    # `max(0, ...)`: con un `hasta` anterior al lunes no ha transcurrido ningun
    # dia. Sin el clamp salia negativo y Monotony reventaba con fmean([]).
    dias_semana = max(0, (corte - rango.inicio).days + 1) if corte else 7

    sesiones = repo.sesiones_entre(rango.inicio, fin_ventana)
    previa = semana_anterior(rango)
    sesiones_previas = repo.sesiones_entre(previa.inicio, previa.fin)

    # Ventana de 28 dias terminando el ultimo dia considerado.
    ini_cronica = fin_ventana - timedelta(days=m_acwr.DIAS_CRONICA - 1)
    carga_por_dia = repo.carga_diaria(ini_cronica, fin_ventana)
    # Solo las corridas aportan carga; otro deporte sin HR no es un hueco.
    sin_carga = sum(1 for s in sesiones if s.es_run and s.carga is None)
    primera_fecha = repo.primera_fecha()

    r_acwr = m_acwr.calcular(
        carga_por_dia,
        fin=fin_ventana,
        primera_fecha=primera_fecha,
        sesiones_sin_carga=sin_carga,
        umbrales=umbrales,
    )
    r_foster = m_foster.calcular(
        carga_por_dia, inicio=rango.inicio, umbrales=umbrales, dias=dias_semana
    )
    # Las vueltas solo hacen falta para las sesiones con `estructura=`, pero
    # traerlas de una es una consulta y no N: el motor decide cuales usa.
    r_adh = m_adh.calcular(
        plan, rango, sesiones, umbrales=umbrales, hasta=corte,
        vueltas=repo.vueltas_entre(rango.inicio, fin_ventana),
    )
    serie = repo.volumen_semanal(fin_ventana, config.SEMANAS_GRAFICO)

    impreciso = any(s.carga_impreciso for s in sesiones)
    avisos: list[str] = list(r_adh.avisos)
    if impreciso:
        avisos.append(
            "la carga se calculo con peso de zona fijo (el atleta no tiene zonas "
            "de HR en Strava): las cifras de carga, ACWR y Monotony son imprecisas"
        )
    if sin_carga:
        avisos.append(
            f"{sin_carga} sesion(es) de la semana sin HR: no aportan carga y por eso "
            "no cuentan para ACWR ni Monotony"
        )
    if r_adh.dias_sin_dato:
        avisos.append(
            f"{r_adh.dias_sin_dato} dia(s) con sesion registrada pero sin el dato que "
            "pide el plan: figuran como dato faltante, no como incumplimiento"
        )

    # Una sola vez: es pura y barata, pero calcularla en dos sitios es lo que
    # se desincroniza cuando alguien le agrega un parametro.
    r_deriva = _decoupling(sesiones, umbrales)
    alertas = [a for a in (r_acwr.alerta, r_foster.alerta, r_deriva["alerta"]) if a]

    return {
        "version": VERSION_REPORTE,
        "generado_en": ahora_local().isoformat(timespec="seconds"),
        "semana": {
            "inicio": rango.inicio.isoformat(),
            "fin": rango.fin.isoformat(),
            "numero_plan": plan.semana if plan else None,
            "plan_cargado": plan is not None,
            "en_curso": corte is not None,
            "hasta": fin_ventana.isoformat(),
            "dias_transcurridos": dias_semana,
        },
        "volumen": {
            "planificado_km": r_adh.volumen_planificado_km,
            "real_km": r_adh.volumen_real_km,
            "pct": r_adh.volumen_pct,
            "estimado_km": r_adh.volumen_estimado_km,
            # Total de los 7 dias, no solo los transcurridos.
            "planificado_semana_km": r_adh.volumen_planificado_semana_km,
        },
        "volumen_historico": {
            "semanas": [{"lunes": d.isoformat(), "km": v} for d, v in serie],
            "primera_fecha_con_datos": primera_fecha.isoformat() if primera_fecha else None,
            "ultima_en_curso": corte is not None,
        },
        "carga": {
            "semanal": r_foster.carga_semanal,
            "por_dia": {
                (rango.inicio + timedelta(days=i)).isoformat(): round(
                    carga_por_dia.get(rango.inicio + timedelta(days=i), 0.0), 1
                )
                for i in range(7)
            },
            "impreciso": impreciso,
            "sesiones_sin_carga": sin_carga,
        },
        "acwr": r_acwr.to_json(),
        "monotony": r_foster.to_json(),
        "deriva_cardiaca": r_deriva,
        "cadencia": _cadencia(sesiones, sesiones_previas).to_json(),
        "adherencia": r_adh.to_json(),
        "sesiones": [
            {
                "fecha": s.fecha_local,
                "dia": s.dia_semana,
                "tipo": s.tipo_strava,
                # La lista incluye TODA la actividad de la semana, tambien la
                # que no es carrera. Solo las marcadas `es_run` entran en los
                # totales de volumen y carga de arriba.
                "es_run": s.es_run,
                "nombre": s.nombre,
                "distancia_km": s.distancia_km,
                "duracion_min": round(s.duracion_min, 1) if s.duracion_min else None,
                "hr_promedio": s.hr_promedio,
                "cadencia_spm": s.cadencia_spm,
                "potencia_w": s.potencia_w,
                "decoupling_pct": s.decoupling_pct,
                "carga": s.carga,
            }
            for s in sesiones
        ],
        "alertas": alertas,
        "avisos_datos": avisos,
        "umbrales": {
            "acwr_alto": umbrales.acwr_alto,
            "acwr_bajo": umbrales.acwr_bajo,
            "monotony_alta": umbrales.monotony_alta,
            "decoupling_alto_pct": umbrales.decoupling_alto_pct,
            "nota": "valores de literatura general, no calibrados a este atleta",
        },
    }
