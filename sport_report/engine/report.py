"""Motor de calculo: produce el JSON que es la unica fuente de cifras.

La capa narrativa (fase 6) recibe exactamente este JSON y no puede recalcular ni
agregar numeros. Todo lo que el reporte pueda decir tiene que estar aca, con su
bandera de confiabilidad al lado.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any

from .. import config
from ..config import UMBRALES, Umbrales
from ..db.models import SesionReal
from ..db.repo import Repo
from ..fechas import RangoSemana, ahora_local, semana_anterior
from ..plan.models import ORDEN_DIAS
from ..plan.store import PlanStore
from . import acwr as m_acwr
from . import adherencia as m_adh
from . import fatiga as m_fatiga
from . import foster as m_foster

log = logging.getLogger(__name__)

# v2: agrega los bloques `fuente`, `gct`, `oscilacion_vertical`,
# `ratio_vertical` y `fatiga_descanso`. Nadie bifurca sobre este numero; esta
# para poder mirar un reporte archivado y saber que esperaba traer.
VERSION_REPORTE = 2


@dataclass(frozen=True)
class Tendencia:
    valor: float | None
    anterior: float | None
    delta: float | None
    n: int
    #: Solo para las metricas que no todas las fuentes dan. `None` = siempre
    #: disponible (cadencia), que es como se comportaba antes.
    unidad: str | None = None
    disponible: bool | None = None
    motivo: str = ""

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "valor": self.valor,
            "semana_anterior": self.anterior,
            "delta": self.delta,
            "n_sesiones": self.n,
        }
        if self.unidad is not None:
            d["unidad"] = self.unidad
            d["disponible"] = bool(self.disponible)
            d["motivo"] = self.motivo
        return d


def _promedio(valores: list[float]) -> float | None:
    return round(statistics.fmean(valores), 1) if valores else None


def _tendencia(
    actual: list[SesionReal], previa: list[SesionReal], atributo: str
) -> Tendencia:
    """Promedio semanal de un atributo por sesion y delta contra la anterior."""
    a = [v for v in (getattr(s, atributo) for s in actual) if v]
    p = [v for v in (getattr(s, atributo) for s in previa) if v]
    va, vp = _promedio(a), _promedio(p)
    delta = round(va - vp, 1) if va is not None and vp is not None else None
    return Tendencia(valor=va, anterior=vp, delta=delta, n=len(a))


def _dinamica(
    actual: list[SesionReal],
    previa: list[SesionReal],
    atributo: str,
    unidad: str,
    fuente: dict[str, Any],
) -> Tendencia:
    """Dinamica avanzada, que SOLO da intervals.icu.

    Cuando no hay valor, el motivo distingue los tres casos que se arreglan de
    forma distinta: la semana vino del respaldo, el reloj no lo midio, o no hay
    corridas de las que sacarlo. Nunca se devuelve 0.0: un GCT de cero diria
    que el pie no toco el suelo.
    """
    t = _tendencia(actual, previa, atributo)
    if t.valor is not None:
        return replace(t, unidad=unidad, disponible=True)

    corridas = sum(1 for s in actual if s.es_run)
    if fuente.get("fallback"):
        motivo = (
            f"la semana se ingirio desde {fuente.get('usada')} (respaldo), que no "
            "expone dinamica de carrera"
        )
    elif not corridas:
        motivo = "no hubo corridas esta semana"
    else:
        motivo = f"el reloj no reporto este dato en ninguna de las {corridas} corridas"
    return replace(t, valor=None, unidad=unidad, disponible=False, motivo=motivo)


def _fuente(repo: Repo, clave: str) -> dict[str, Any]:
    """Que fuente sirvio la semana y que quedo fuera por eso.

    Se lee de la base y no del resumen de la ingesta para que salga bien
    tambien cuando la corrida va con --sin-ingesta o se rehace un reporte
    viejo. `no_disponibles` es la lista de bloques que esa fuente no puede
    llenar: asi el texto del reporte y el JSON no pueden discrepar.
    """
    from ..fuentes import NO_DISPONIBLE

    fila = repo.fuente_semana(clave)
    if not fila:
        return {
            "principal": config.FUENTE_PRINCIPAL,
            "usada": None,
            "fallback": False,
            "motivo": "no hay registro de que fuente sirvio esta semana",
            "no_disponibles": [],
        }
    usada = fila["fuente"]
    return {
        "principal": config.FUENTE_PRINCIPAL,
        "usada": usada,
        "fallback": fila["fallback"],
        "motivo": fila["detalle"] or "",
        "no_disponibles": list(NO_DISPONIBLE.get(usada, ())),
    }


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
            "la carga se calculo con peso de zona fijo (no se pudieron leer las "
            "zonas de HR del atleta): las cifras de carga, ACWR y Monotony son "
            "imprecisas"
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
    fuente = _fuente(repo, rango.clave)

    # -- fatiga y descanso: primer uso del bienestar que se venia guardando --
    bienestar = repo.bienestar_entre(rango.inicio, fin_ventana)
    bienestar_previa = repo.bienestar_entre(previa.inicio, previa.fin)
    dias_descanso_plan = (
        tuple(d for d in ORDEN_DIAS if plan.es_descanso(d)) if plan else ()
    )
    r_fatiga = m_fatiga.calcular(
        bienestar,
        bienestar_previa,
        r_acwr=r_acwr,
        r_foster=r_foster,
        descanso_planificado=dias_descanso_plan,
        # Se derivan de los estados que ya calculo la adherencia: no hay una
        # segunda definicion de "dia de descanso tomado" que se pueda desviar.
        descanso_tomado=tuple(
            d.dia for d in r_adh.dias if d.estado == m_adh.DESCANSO_OK
        ),
        descanso_roto=tuple(
            d.dia for d in r_adh.dias if d.estado == m_adh.DESCANSO_ROTO
        ),
        fuente_degradada=bool(fuente.get("fallback")),
    )

    alertas = [a for a in (r_acwr.alerta, r_foster.alerta, r_deriva["alerta"]) if a]
    alertas.extend(r_fatiga.alertas)
    if fuente["fallback"]:
        # Al frente: es lo primero que hay que saber de esta semana. Nombra lo
        # que falta en vez de dejarlo como un hueco silencioso.
        avisos.insert(
            0,
            f"esta semana los datos vienen de {fuente['usada']} (respaldo): no hay "
            "GCT, oscilacion vertical, ratio vertical ni bienestar (HRV, sueno, "
            "sleep score, Body Battery, readiness). La semana no esta completa",
        )

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
        "fuente": fuente,
        "acwr": r_acwr.to_json(),
        "monotony": r_foster.to_json(),
        "deriva_cardiaca": r_deriva,
        "cadencia": _tendencia(sesiones, sesiones_previas, "cadencia_spm").to_json(),
        # Las tres que solo da intervals.icu. Con el respaldo activo salen en
        # null con `disponible: false` y el motivo, nunca en cero.
        "gct": _dinamica(sesiones, sesiones_previas, "gct_ms", "ms", fuente).to_json(),
        "oscilacion_vertical": _dinamica(
            sesiones, sesiones_previas, "oscilacion_vertical_cm", "cm", fuente
        ).to_json(),
        "ratio_vertical": _dinamica(
            sesiones, sesiones_previas, "ratio_vertical_pct", "%", fuente
        ).to_json(),
        "fatiga_descanso": r_fatiga.to_json(),
        "adherencia": r_adh.to_json(),
        "sesiones": [
            {
                "fecha": s.fecha_local,
                "dia": s.dia_semana,
                "tipo": s.tipo,
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
                "gct_ms": s.gct_ms,
                "oscilacion_vertical_cm": s.oscilacion_vertical_cm,
                "ratio_vertical_pct": s.ratio_vertical_pct,
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
            "hrv_caida_ms": config.BIENESTAR.hrv_caida_ms,
            "readiness_bajo": config.BIENESTAR.readiness_bajo,
            "nota": "valores de literatura general, no calibrados a este atleta",
        },
    }
