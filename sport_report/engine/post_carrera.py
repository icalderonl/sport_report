"""Informe post-carrera: analisis de la carrera y cierre del ciclo.

Se dispara el mismo dia de una carrera declarada con /carrera, a las 21:00 (ver
`run_post_carrera`). Tres bloques, todos calculados aca:

- **La carrera**: la corrida mas larga de ese dia. Tiempo, ritmo, FC, dinamica
  y deriva cardiaca, tal como los trajo la fuente.
- **Parciales y fatiga**: mitades y cuartos a partir de las vueltas del reloj,
  y las senales de fatiga que de ahi se leen (split positivo, caida en el
  ultimo cuarto, deriva cardiaca). No hay FC por vuelta en la base, asi que la
  unica senal cardiaca es el decoupling de la actividad entera.
- **El ciclo** (solo la carrera principal): volumen, pico, afinamiento y
  adherencia de las semanas que llevaron a la carrera, y la comparacion con el
  ciclo anterior si hay un informe previo.

Mismo contrato que el semanal y el mensual: el modelo narra y no calcula. Todo
lo derivable va precalculado, y lo que no se pudo medir va en null con su
motivo, nunca en cero.
"""
from __future__ import annotations

import logging
import statistics
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

from .. import config
from ..config import POST_CARRERA, UMBRALES, PostCarrera, Umbrales
from ..db.models import BienestarDia, SesionReal, Vuelta
from ..db.repo import Repo
from ..fechas import RangoSemana, ahora_local, semana_de
from ..plan.carrera import Carrera
from ..plan.store import PlanStore
from ..storage import leer_json
from .mensual import _promedio, _semana

log = logging.getLogger(__name__)

VERSION_REPORTE_POST_CARRERA = 1


def ruta_informe(fecha: date) -> Path:
    return config.DATA_DIR / "reportes" / "carreras" / f"{fecha.isoformat()}.json"


# --------------------------------------------------------------------------
# Formatos legibles. Van en el JSON junto al numero: el modelo cita "5:02/km"
# tal cual y no convierte segundos a minutos (eso seria calcular).
# --------------------------------------------------------------------------


def fmt_ritmo(s_km: float | None) -> str | None:
    if s_km is None:
        return None
    total = int(round(s_km))
    return f"{total // 60}:{total % 60:02d}/km"


def fmt_tiempo(segundos: float | None) -> str | None:
    if segundos is None:
        return None
    total = int(round(segundos))
    h, resto = divmod(total, 3600)
    m, s = divmod(resto, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _pct(a: float, b: float) -> float:
    return round((a - b) / b * 100, 1)


# --------------------------------------------------------------------------
# La carrera
# --------------------------------------------------------------------------


def actividad_de_carrera(sesiones: Iterable[SesionReal], fecha: date) -> SesionReal | None:
    """La corrida mas larga del dia de la carrera.

    La fuente no marca cual actividad fue la competencia. El calentamiento y el
    enfriamiento suelen ir en actividades aparte y siempre son mas cortos, asi
    que la mas larga es la carrera.
    """
    del_dia = [s for s in sesiones if s.es_run and s.fecha == fecha and s.distancia_km]
    return max(del_dia, key=lambda s: s.distancia_km or 0.0, default=None)


def _actividad(s: SesionReal | None, fecha: date) -> dict[str, Any]:
    if s is None:
        return {
            "disponible": False,
            "motivo": (
                f"no hay ninguna corrida con distancia registrada el {fecha.isoformat()}"
            ),
        }
    ritmo = (
        s.duracion_mov_s / s.distancia_km
        if s.duracion_mov_s and s.distancia_km
        else None
    )
    return {
        "disponible": True,
        "motivo": "",
        "fuente": s.fuente,
        "id_externo": s.id_externo,
        "nombre": s.nombre,
        "distancia_km": round(s.distancia_km, 2) if s.distancia_km else None,
        "tiempo_mov_s": s.duracion_mov_s,
        "tiempo_mov": fmt_tiempo(s.duracion_mov_s),
        "tiempo_total_s": s.duracion_tot_s,
        "tiempo_total": fmt_tiempo(s.duracion_tot_s),
        "ritmo_s_km": round(ritmo, 1) if ritmo else None,
        "ritmo": fmt_ritmo(ritmo),
        "hr_promedio": s.hr_promedio,
        "hr_maximo": s.hr_maximo,
        "cadencia_spm": s.cadencia_spm,
        "potencia_w": s.potencia_w,
        "gct_ms": s.gct_ms,
        "oscilacion_vertical_cm": s.oscilacion_vertical_cm,
        "ratio_vertical_pct": s.ratio_vertical_pct,
        "decoupling_pct": s.decoupling_pct,
    }


# --------------------------------------------------------------------------
# Parciales
# --------------------------------------------------------------------------


def _ritmo_tramo(vueltas: Sequence[Vuelta], desde_km: float, hasta_km: float) -> float | None:
    """Ritmo (s/km) entre dos distancias acumuladas.

    Una vuelta que cruza el borde se reparte en proporcion a su distancia,
    suponiendo ritmo constante dentro de ella: las vueltas automaticas son de
    1 km y el error de esa suposicion es de segundos.
    """
    km = seg = 0.0
    acumulado = 0.0
    for v in vueltas:
        ini, fin = acumulado, acumulado + v.distancia_km
        acumulado = fin
        solape = min(fin, hasta_km) - max(ini, desde_km)
        if solape <= 0:
            continue
        km += solape
        seg += v.duracion_mov_s * solape / v.distancia_km
    return seg / km if km > 0 else None


def _sin_parciales(motivo: str) -> dict[str, Any]:
    return {"disponible": False, "motivo": motivo}


def parciales(
    vueltas: Sequence[Vuelta],
    distancia_km: float | None,
    cfg: PostCarrera = POST_CARRERA,
) -> dict[str, Any]:
    validas = [v for v in vueltas if v.distancia_km and v.duracion_mov_s]
    if not validas:
        return _sin_parciales("la actividad no trae vueltas: no hay parciales que leer")
    cubierto = sum(v.distancia_km for v in validas)
    if len(validas) < 4:
        return _sin_parciales(
            f"solo {len(validas)} vuelta(s): no alcanza para separar mitades y cuartos"
        )
    if distancia_km and cubierto < distancia_km * cfg.cobertura_vueltas_min:
        return _sin_parciales(
            f"las vueltas cubren {round(cubierto, 2)} de {round(distancia_km, 2)} km: "
            "las mitades no representarian la carrera"
        )

    mitad = cubierto / 2
    r1 = _ritmo_tramo(validas, 0, mitad)
    r2 = _ritmo_tramo(validas, mitad, cubierto)
    split = _pct(r2, r1)
    if abs(split) < cfg.split_parejo_pct:
        tipo = "parejo"
    elif split < 0:
        tipo = "negativo"
    else:
        tipo = "positivo"

    cuarto = cubierto / 4
    cuartos = [_ritmo_tramo(validas, i * cuarto, (i + 1) * cuarto) for i in range(4)]
    tres_primeros = _ritmo_tramo(validas, 0, 3 * cuarto)
    caida_final = _pct(cuartos[3], tres_primeros)

    # La ultima vuelta suele ser un resto (los 97 m finales de un 10K): queda
    # fuera de "mas rapida / mas lenta" para no ganar ni perder por eso.
    mediana = statistics.median(v.distancia_km for v in validas)
    completas = [v for v in validas if v.distancia_km >= 0.9 * mediana]
    rapida = min(completas, key=lambda v: v.ritmo_s_km)
    lenta = max(completas, key=lambda v: v.ritmo_s_km)
    ritmos = [v.ritmo_s_km for v in completas]
    variabilidad = (
        round(statistics.pstdev(ritmos) / statistics.fmean(ritmos) * 100, 1)
        if len(ritmos) >= 2
        else None
    )

    return {
        "disponible": True,
        "motivo": "",
        "n_vueltas": len(validas),
        "km_cubiertos": round(cubierto, 2),
        "primera_mitad_s_km": round(r1, 1),
        "primera_mitad": fmt_ritmo(r1),
        "segunda_mitad_s_km": round(r2, 1),
        "segunda_mitad": fmt_ritmo(r2),
        "split_pct": split,
        "tipo_split": tipo,
        "cuartos": [
            {"cuarto": i + 1, "ritmo_s_km": round(r, 1), "ritmo": fmt_ritmo(r)}
            for i, r in enumerate(cuartos)
        ],
        "caida_ultimo_cuarto_pct": caida_final,
        "vuelta_mas_rapida": {"vuelta": rapida.indice, "ritmo": fmt_ritmo(rapida.ritmo_s_km)},
        "vuelta_mas_lenta": {"vuelta": lenta.indice, "ritmo": fmt_ritmo(lenta.ritmo_s_km)},
        "variabilidad_pct": variabilidad,
        "nota": (
            "split_pct > 0 es segunda mitad mas lenta. caida_ultimo_cuarto_pct "
            "compara el ultimo cuarto contra el promedio de los tres primeros. "
            "variabilidad_pct es la dispersion del ritmo entre vueltas completas"
        ),
    }


def fatiga(
    actividad: dict[str, Any],
    p: dict[str, Any],
    cfg: PostCarrera = POST_CARRERA,
    umbrales: Umbrales = UMBRALES,
) -> dict[str, Any]:
    """Senales de fatiga, decididas aca con umbrales y no por el modelo."""
    senales: list[str] = []
    evaluadas: list[str] = []
    no_evaluables: list[str] = []

    if p.get("disponible"):
        evaluadas += ["split", "ultimo cuarto"]
        if p["split_pct"] >= cfg.split_positivo_pct:
            senales.append(
                f"segunda mitad {p['split_pct']:g}% mas lenta que la primera "
                f"({p['segunda_mitad']} contra {p['primera_mitad']}; umbral "
                f"{cfg.split_positivo_pct:g}%)"
            )
        if p["caida_ultimo_cuarto_pct"] >= cfg.caida_final_pct:
            senales.append(
                f"ultimo cuarto {p['caida_ultimo_cuarto_pct']:g}% mas lento que "
                f"los tres primeros ({p['cuartos'][3]['ritmo']}; umbral "
                f"{cfg.caida_final_pct:g}%)"
            )
    else:
        no_evaluables.append(f"parciales: {p.get('motivo', 'sin datos')}")

    deriva = actividad.get("decoupling_pct")
    if deriva is not None:
        evaluadas.append("deriva cardiaca")
        if deriva > umbrales.decoupling_alto_pct:
            senales.append(
                f"deriva cardiaca de {deriva:g}% (umbral {umbrales.decoupling_alto_pct:g}%): "
                "la FC subio respecto al ritmo a medida que avanzaba la carrera"
            )
    elif actividad.get("disponible"):
        no_evaluables.append(
            "deriva cardiaca: la actividad no trae decoupling (sin pulsometro o "
            "stream demasiado corto)"
        )

    return {
        "hay_senales": bool(senales),
        "senales": senales,
        "evaluadas": evaluadas,
        "no_evaluables": no_evaluables,
        "nota": (
            "sin FC por vuelta en la base: la unica senal cardiaca es la deriva "
            "de la actividad completa"
        ),
    }


# --------------------------------------------------------------------------
# Bienestar alrededor de la carrera
# --------------------------------------------------------------------------

_CAMPOS_BIENESTAR = (("hrv", "ms"), ("hr_reposo", "lpm"), ("sueno_h", "h"))


def _bienestar(dias: Sequence[BienestarDia], fecha: date) -> dict[str, Any]:
    por_fecha = {d.fecha_local: d for d in dias}
    previos = [d for d in dias if d.fecha_local < fecha.isoformat()]
    salida: dict[str, Any] = {}
    for campo, unidad in _CAMPOS_BIENESTAR:
        dia = por_fecha.get(fecha.isoformat())
        siguiente = por_fecha.get((fecha + timedelta(days=1)).isoformat())
        base = [getattr(d, campo) for d in previos if getattr(d, campo) is not None]
        salida[campo] = {
            "dia_carrera": getattr(dia, campo) if dia else None,
            "dia_siguiente": getattr(siguiente, campo) if siguiente else None,
            "promedio_28_dias_previos": _promedio(base),
            "dias_base": len(base),
            "unidad": unidad,
        }
    hay = any(
        v["dia_carrera"] is not None or v["dia_siguiente"] is not None
        for v in salida.values()
    )
    salida["disponible"] = hay
    salida["motivo"] = "" if hay else "sin datos de bienestar esos dias (solo los da intervals.icu)"
    salida["nota"] = (
        "dato de cuerpo completo: 'dia_carrera' es la medicion de la manana de la "
        "carrera (la noche previa), 'dia_siguiente' la de la manana despues; "
        "null si el informe sale el mismo dia de la carrera"
    )
    return salida


# --------------------------------------------------------------------------
# El ciclo
# --------------------------------------------------------------------------


def informes_previos(antes_de: date) -> list[dict[str, Any]]:
    """Informes post-carrera ya escritos, del mas reciente al mas viejo."""
    carpeta = ruta_informe(antes_de).parent
    if not carpeta.exists():
        return []
    salida = []
    for p in sorted(carpeta.glob("*.json"), reverse=True):
        try:
            if date.fromisoformat(p.stem) >= antes_de:
                continue
            d = leer_json(p)
        except (ValueError, OSError) as exc:
            log.warning("informe post-carrera ilegible %s: %s", p, exc)
            continue
        if isinstance(d, dict):
            salida.append(d)
    return salida


def _inicio_ciclo(
    fecha: date, previos: Sequence[dict[str, Any]], cfg: PostCarrera
) -> tuple[date, str]:
    semana_carrera = semana_de(fecha).inicio
    por_defecto = semana_carrera - timedelta(weeks=cfg.semanas_ciclo - 1)
    for d in previos:
        if not d.get("ciclo"):
            continue
        anterior = date.fromisoformat(d["carrera"]["fecha"])
        siguiente = semana_de(anterior).inicio + timedelta(weeks=1)
        if siguiente > por_defecto:
            nombre = d["carrera"].get("nombre") or anterior.isoformat()
            return siguiente, f"empieza la semana siguiente a la carrera anterior ({nombre})"
        break
    return por_defecto, f"las {cfg.semanas_ciclo} semanas que terminan en la de la carrera"


def ciclo(
    carrera: Carrera,
    km_carrera: float | None,
    repo: Repo,
    plan_store: PlanStore,
    previos: Sequence[dict[str, Any]],
    cfg: PostCarrera = POST_CARRERA,
    cfg_carrera: config.Carrera = config.CARRERA,
    umbrales: Umbrales = UMBRALES,
) -> dict[str, Any]:
    inicio, criterio = _inicio_ciclo(carrera.fecha, previos, cfg)
    semana_carrera = semana_de(carrera.fecha)
    rangos: list[RangoSemana] = []
    lunes = inicio
    while lunes <= semana_carrera.inicio:
        rangos.append(semana_de(lunes))
        lunes += timedelta(weeks=1)
    semanas = [_semana(r, repo, plan_store, umbrales) for r in rangos]

    # La semana de la carrera queda fuera del pico, del promedio y del
    # afinamiento: la carrera misma no es entrenamiento.
    entrenamiento = semanas[:-1]
    kms = [s["km"] for s in entrenamiento]
    total = round(sum(s["km"] for s in semanas), 1)
    pico = max(entrenamiento, key=lambda s: s["km"], default=None)

    n_af = cfg_carrera.semanas_afinamiento
    af = entrenamiento[-n_af:] if len(entrenamiento) > n_af else []
    if af and pico and pico["km"] > 0 and pico not in af:
        prom_af = _promedio([s["km"] for s in af])
        afinamiento = {
            "disponible": True,
            "motivo": "",
            "n_semanas": len(af),
            "semanas": [{"lunes": s["lunes"], "km": s["km"]} for s in af],
            "promedio_km": prom_af,
            "reduccion_vs_pico_pct": _pct(prom_af, pico["km"]),
        }
    else:
        afinamiento = {
            "disponible": False,
            "motivo": (
                "el ciclo es demasiado corto para separar el afinamiento"
                if not af
                else "el pico de volumen cayo dentro de las semanas de afinamiento: "
                "no hubo reduccion que medir"
                if pico in af
                else "sin kilometros en el ciclo"
            ),
            "n_semanas": len(af),
            "semanas": [{"lunes": s["lunes"], "km": s["km"]} for s in af],
        }

    semana_final = semanas[-1]["km"] if semanas else 0.0
    con_plan = [s["adherencia_pct"] for s in semanas if s["adherencia_pct"] is not None]

    sesiones = [
        s
        for s in repo.sesiones_entre(inicio, semana_carrera.inicio - timedelta(days=1))
        if s.es_run and s.distancia_km
    ]
    mas_larga = max(sesiones, key=lambda s: s.distancia_km, default=None)

    return {
        "inicio": inicio.isoformat(),
        "fin": semana_carrera.fin.isoformat(),
        "criterio_inicio": criterio,
        "semanas": len(semanas),
        "total_km": total,
        "km_carrera": km_carrera,
        "km_semana_carrera_sin_carrera": (
            round(semana_final - km_carrera, 1) if km_carrera is not None else None
        ),
        "promedio_semanal_km": _promedio(kms),
        "pico": {"lunes": pico["lunes"], "km": pico["km"]} if pico else None,
        "semanas_sin_corridas": sum(1 for s in entrenamiento if s["corridas"] == 0),
        "corrida_mas_larga": (
            {
                "fecha": mas_larga.fecha_local,
                "km": round(mas_larga.distancia_km, 1),
            }
            if mas_larga
            else None
        ),
        "afinamiento": afinamiento,
        "adherencia": {
            "promedio_pct": _promedio(con_plan),
            "semanas_con_plan": len(con_plan),
            "semanas_sin_plan": len(semanas) - len(con_plan),
            "confiable": bool(con_plan),
            "motivo": "" if con_plan else "ninguna semana del ciclo tenia plan cargado",
        },
        "por_semana": [
            {"lunes": s["lunes"], "km": s["km"], "adherencia_pct": s["adherencia_pct"]}
            for s in semanas
        ],
        "nota": (
            "total_km incluye la semana de la carrera; promedio, pico y "
            "afinamiento la excluyen. Solo carrera: la fuerza y otros deportes "
            "no entran"
        ),
    }


# --------------------------------------------------------------------------
# Comparaciones
# --------------------------------------------------------------------------


def _comparacion_ciclo(actual: dict[str, Any], previos: Sequence[dict[str, Any]]) -> dict[str, Any]:
    anterior = next((d for d in previos if d.get("ciclo")), None)
    if anterior is None:
        return {"disponible": False, "motivo": "no hay un informe de ciclo anterior con que comparar"}
    c = anterior["ciclo"]

    def delta(a, b):
        return round(a - b, 1) if a is not None and b is not None else None

    pico_a = (actual.get("pico") or {}).get("km")
    pico_b = (c.get("pico") or {}).get("km")
    adh_a = actual["adherencia"]["promedio_pct"]
    adh_b = (c.get("adherencia") or {}).get("promedio_pct")
    return {
        "disponible": True,
        "motivo": "",
        "carrera": anterior["carrera"].get("nombre") or anterior["carrera"]["fecha"],
        "fecha": anterior["carrera"]["fecha"],
        "semanas": c.get("semanas"),
        "total_km": c.get("total_km"),
        "delta_total_km": delta(actual["total_km"], c.get("total_km")),
        "promedio_semanal_km": c.get("promedio_semanal_km"),
        "delta_promedio_semanal_km": delta(
            actual["promedio_semanal_km"], c.get("promedio_semanal_km")
        ),
        "pico_km": pico_b,
        "delta_pico_km": delta(pico_a, pico_b),
        "adherencia_pct": adh_b,
        "delta_adherencia_pct": delta(adh_a, adh_b),
    }


def _carreras_similares(
    actividad: dict[str, Any], previos: Sequence[dict[str, Any]], cfg: PostCarrera
) -> list[dict[str, Any]]:
    """Carreras anteriores de distancia parecida, con la diferencia de ritmo."""
    if not actividad.get("disponible") or not actividad.get("distancia_km"):
        return []
    km = actividad["distancia_km"]
    salida = []
    for d in previos:
        a = d.get("actividad") or {}
        if not a.get("disponible") or not a.get("distancia_km") or not a.get("ritmo_s_km"):
            continue
        if abs(a["distancia_km"] - km) / km * 100 > cfg.distancia_similar_pct:
            continue
        dr = (
            round(actividad["ritmo_s_km"] - a["ritmo_s_km"], 1)
            if actividad.get("ritmo_s_km")
            else None
        )
        salida.append(
            {
                "carrera": d["carrera"].get("nombre") or d["carrera"]["fecha"],
                "fecha": d["carrera"]["fecha"],
                "distancia_km": a["distancia_km"],
                "tiempo_mov": a.get("tiempo_mov"),
                "ritmo": a.get("ritmo"),
                "hr_promedio": a.get("hr_promedio"),
                # Negativo = esta carrera fue mas rapida.
                "delta_ritmo_s_km": dr,
            }
        )
    return salida[:3]


# --------------------------------------------------------------------------
# Ensamblado
# --------------------------------------------------------------------------


def construir(
    carrera: Carrera,
    es_principal: bool,
    repo: Repo,
    plan_store: PlanStore | None = None,
    cfg: PostCarrera = POST_CARRERA,
    umbrales: Umbrales = UMBRALES,
    carreras: Sequence[Carrera] = (),
) -> dict[str, Any]:
    """JSON del informe post-carrera. Unica fuente de sus cifras.

    `carreras` son todas las declaradas: las anteriores sirven de referencia
    para el estimado que habia la vispera.
    """
    # Import local: el estimador reusa helpers de este modulo.
    from . import estimador

    plan_store = plan_store or PlanStore()
    fecha = carrera.fecha
    s = actividad_de_carrera(repo.sesiones_entre(fecha, fecha), fecha)
    actividad = _actividad(s, fecha)
    p = (
        parciales(repo.vueltas(s.fuente, s.id_externo), s.distancia_km, cfg)
        if s is not None
        else _sin_parciales("sin actividad de carrera")
    )
    previos = informes_previos(fecha)

    avisos: list[str] = []
    if not actividad["disponible"]:
        avisos.append(actividad["motivo"])
    if s is not None and s.fuente == config.STRAVA:
        avisos.append(
            "la carrera viene de Strava: sin GCT, oscilacion ni ratio vertical"
        )

    datos_ciclo = None
    comparacion_ciclo = {"disponible": False, "motivo": "no es la carrera principal"}
    if es_principal:
        datos_ciclo = ciclo(
            carrera,
            actividad.get("distancia_km"),
            repo,
            plan_store,
            previos,
            cfg,
            umbrales=umbrales,
        )
        comparacion_ciclo = _comparacion_ciclo(datos_ciclo, previos)

    return {
        "version": VERSION_REPORTE_POST_CARRERA,
        "generado_en": ahora_local().isoformat(timespec="seconds"),
        "carrera": {
            "fecha": fecha.isoformat(),
            "nombre": carrera.nombre,
            "principal": es_principal,
        },
        "actividad": actividad,
        "parciales": p,
        "fatiga": fatiga(actividad, p, cfg, umbrales),
        "bienestar": _bienestar(
            repo.bienestar_entre(fecha - timedelta(days=28), fecha + timedelta(days=1)),
            fecha,
        ),
        "ciclo": datos_ciclo,
        "estimado_vs_real": estimador.comparar(
            estimador.estimar(carrera, repo, hoy=fecha, carreras=carreras),
            s.duracion_mov_s if s is not None else None,
        ),
        "comparacion": {
            "ciclo_anterior": comparacion_ciclo,
            "carreras_similares": _carreras_similares(actividad, previos, cfg),
        },
        "umbrales": {
            "split_positivo_pct": cfg.split_positivo_pct,
            "split_parejo_pct": cfg.split_parejo_pct,
            "caida_final_pct": cfg.caida_final_pct,
            "decoupling_alto_pct": umbrales.decoupling_alto_pct,
            "nota": "convenciones de lectura, no calibradas a este atleta",
        },
        "avisos_datos": avisos,
    }
