"""Estimador de tiempo para la carrera objetivo.

Tres metodos, cada uno con su rango y su confianza, y una regla fija para
elegir el que se muestra primero:

1. **Riegel desde una carrera** (T2 = T1 * (D2/D1)^k): la referencia es una
   carrera declarada con /carrera que ya paso, con su actividad en la base. Es
   un esfuerzo maximo, que es lo que la formula supone.
2. **Riegel desde entrenamientos**: el mejor tramo continuo de 3, 5, 10, 15 o
   21 km de las ultimas semanas, armado con las vueltas. Un entrenamiento es
   submaximo, asi que el estimado sale **conservador** (mas lento que el
   potencial) y la confianza es baja siempre.
3. **Tanda (2011), solo maraton**: ritmo de maraton (s/km) = 17.1 +
   140*exp(-0.0053*K) + 0.55*P, con K = km semanales y P = ritmo medio de
   entrenamiento (s/km) de las 8 semanas previas. No necesita ninguna carrera.

El rango de Riegel sale de recorrer el exponente entre `exponente_min` y
`exponente_max`; el de Tanda, de su error tipico publicado. Ninguno corrige
por desnivel ni calor: la base no guarda ninguno de los dos. La cinta
(`VirtualRun`) y el trail quedan fuera de las referencias: su ritmo no es
comparable con el de una carrera en calle.

Todo es precalculado: el modelo de la narrativa cita, no estima.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable, Sequence

from ..config import ESTIMADOR, Estimador
from ..db.models import Vuelta
from ..db.repo import Repo
from ..fechas import semana_de
from ..plan.carrera import Carrera
from .post_carrera import actividad_de_carrera, fmt_ritmo, fmt_tiempo

ALTA = "alta"
MEDIA = "media"
BAJA = "baja"

#: Tipos que no sirven de referencia: cinta (calibracion, sin viento) y trail
#: (desnivel que la base no guarda).
TIPOS_EXCLUIDOS = ("VirtualRun", "TrailRun")


@dataclass(frozen=True)
class Esfuerzo:
    fecha: date
    distancia_km: float
    tiempo_s: float
    origen: str  # "carrera" | "entrenamiento"
    detalle: str = ""


def riegel(tiempo_s: float, d1: float, d2: float, k: float) -> float:
    return tiempo_s * (d2 / d1) ** k


def tanda_ritmo(km_semana: float, ritmo_s_km: float) -> float:
    """Ritmo de maraton (s/km) segun Tanda (2011). Ojo: es ritmo, no tiempo."""
    return 17.1 + 140.0 * math.exp(-0.0053 * km_semana) + 0.55 * ritmo_s_km


def _bloque(central_s: float, min_s: float, max_s: float, distancia: float) -> dict[str, Any]:
    return {
        "central_s": round(central_s),
        "central": fmt_tiempo(central_s),
        "ritmo": fmt_ritmo(central_s / distancia),
        "rango_s": [round(min_s), round(max_s)],
        "rango": f"{fmt_tiempo(min_s)}-{fmt_tiempo(max_s)}",
    }


# --------------------------------------------------------------------------
# Esfuerzos de referencia
# --------------------------------------------------------------------------


def mejor_tramo(vueltas: Sequence[Vuelta], largo_km: float) -> float | None:
    """Menor tiempo (s) en un tramo continuo de `largo_km`, o None.

    Ventana deslizante sobre las vueltas en orden. La vuelta que cierra la
    ventana se cuenta en proporcion, suponiendo ritmo parejo dentro de ella.
    """
    vs = [v for v in vueltas if v.distancia_km and v.duracion_mov_s]
    mejor = None
    for i in range(len(vs)):
        km = seg = 0.0
        for v in vs[i:]:
            falta = largo_km - km
            if v.distancia_km >= falta:
                seg += v.duracion_mov_s * falta / v.distancia_km
                km = largo_km
                break
            km += v.distancia_km
            seg += v.duracion_mov_s
        if km >= largo_km and (mejor is None or seg < mejor):
            mejor = seg
    return mejor


def esfuerzos_de_carreras(
    carreras: Iterable[Carrera], repo: Repo, hoy: date, cfg: Estimador
) -> list[Esfuerzo]:
    """Carreras ya corridas dentro de la vigencia, con su actividad."""
    desde = hoy - timedelta(weeks=cfg.semanas_carrera)
    salida = []
    for c in carreras:
        if not desde <= c.fecha < hoy:
            continue
        s = actividad_de_carrera(repo.sesiones_entre(c.fecha, c.fecha), c.fecha)
        if s is None or not s.duracion_mov_s or s.tipo in TIPOS_EXCLUIDOS:
            continue
        salida.append(
            Esfuerzo(
                c.fecha, s.distancia_km, float(s.duracion_mov_s), "carrera",
                c.nombre or c.fecha.isoformat(),
            )
        )
    return salida


def esfuerzos_de_entrenamiento(
    repo: Repo,
    hoy: date,
    cfg: Estimador,
    excluir: set[date] = frozenset(),
) -> list[Esfuerzo]:
    """El mejor tramo de cada largo estandar, por corrida, en la ventana."""
    desde = hoy - timedelta(weeks=cfg.semanas_entrenamiento)
    corridas = [
        s
        for s in repo.sesiones_entre(desde, hoy - timedelta(days=1))
        if s.es_run and s.tipo not in TIPOS_EXCLUIDOS and s.fecha not in excluir
    ]
    salida = []
    for s in corridas:
        vueltas = repo.vueltas(s.fuente, s.id_externo)
        for largo in cfg.segmentos_km:
            t = mejor_tramo(vueltas, largo)
            if t is not None:
                salida.append(
                    Esfuerzo(s.fecha, largo, t, "entrenamiento", s.nombre or "")
                )
    return salida


# --------------------------------------------------------------------------
# Metodos
# --------------------------------------------------------------------------


def _por_riegel(
    esfuerzos: Sequence[Esfuerzo], distancia: float, hoy: date, cfg: Estimador
) -> dict[str, Any] | None:
    """El esfuerzo que predice el tiempo mas rapido, dentro del factor maximo.

    Se elige el mas rapido y no el promedio: de varios esfuerzos, los mas
    lentos son los menos exigidos, no los que mejor miden el estado de forma.
    """
    validos = [
        e for e in esfuerzos if 1 / cfg.factor_max <= distancia / e.distancia_km <= cfg.factor_max
    ]
    if not validos:
        return None
    e = min(validos, key=lambda x: riegel(x.tiempo_s, x.distancia_km, distancia, cfg.exponente))
    central = riegel(e.tiempo_s, e.distancia_km, distancia, cfg.exponente)
    a = riegel(e.tiempo_s, e.distancia_km, distancia, cfg.exponente_min)
    b = riegel(e.tiempo_s, e.distancia_km, distancia, cfg.exponente_max)
    semanas = (hoy - e.fecha).days // 7
    factor = max(distancia / e.distancia_km, e.distancia_km / distancia)
    if e.origen == "carrera":
        confianza = (
            ALTA if semanas <= cfg.semanas_carrera_alta and factor <= 2 else MEDIA
        )
    else:
        confianza = BAJA
    return {
        "metodo": "riegel",
        "origen": e.origen,
        **_bloque(central, min(a, b), max(a, b), distancia),
        "confianza": confianza,
        "referencia": {
            "fecha": e.fecha.isoformat(),
            "detalle": e.detalle,
            "distancia_km": round(e.distancia_km, 2),
            "tiempo": fmt_tiempo(e.tiempo_s),
            "ritmo": fmt_ritmo(e.tiempo_s / e.distancia_km),
            "semanas_atras": semanas,
            "factor_distancia": round(factor, 1),
        },
        "nota": (
            "desde una carrera: esfuerzo maximo"
            if e.origen == "carrera"
            else "desde un entrenamiento, que es submaximo: el estimado es "
            "conservador (probablemente mas lento que lo que puedes correr)"
        ),
    }


def _por_tanda(
    repo: Repo, distancia: float, hoy: date, cfg: Estimador
) -> dict[str, Any] | None:
    if distancia < cfg.tanda_distancia_min_km:
        return None
    fin = semana_de(hoy).inicio - timedelta(days=1)  # ultima semana cerrada
    inicio = fin - timedelta(weeks=cfg.tanda_semanas) + timedelta(days=1)
    corridas = [
        s for s in repo.sesiones_entre(inicio, fin)
        if s.es_run and s.distancia_km and s.duracion_mov_s
    ]
    if not corridas:
        return None
    km = sum(s.distancia_km for s in corridas)
    k = km / cfg.tanda_semanas
    p = sum(s.duracion_mov_s for s in corridas) / km
    central = tanda_ritmo(k, p) * distancia
    error = cfg.tanda_see_s_km * cfg.tanda_rango_see * distancia
    fuera = []
    if not cfg.tanda_km_min <= k <= cfg.tanda_km_max:
        fuera.append(
            f"{round(k, 1):g} km/semana (muestra: {cfg.tanda_km_min:g}-{cfg.tanda_km_max:g})"
        )
    if not cfg.tanda_ritmo_min_s_km <= p <= cfg.tanda_ritmo_max_s_km:
        fuera.append(
            f"ritmo medio {fmt_ritmo(p)} (muestra: {fmt_ritmo(cfg.tanda_ritmo_min_s_km)}"
            f" a {fmt_ritmo(cfg.tanda_ritmo_max_s_km)})"
        )
    return {
        "metodo": "tanda",
        "origen": "volumen y ritmo de entrenamiento",
        **_bloque(central, central - error, central + error, distancia),
        "confianza": BAJA if fuera else MEDIA,
        "referencia": {
            "desde": inicio.isoformat(),
            "hasta": fin.isoformat(),
            "km_semana": round(k, 1),
            "ritmo_medio": fmt_ritmo(p),
            "corridas": len(corridas),
        },
        "fuera_de_muestra": fuera,
        "nota": (
            f"formula de Tanda para maraton; rango de +-{cfg.tanda_rango_see:g} errores "
            f"estandar ({cfg.tanda_see_s_km:g} s/km cada uno)"
            + ("; fuera de la muestra del paper: " + ", ".join(fuera) if fuera else "")
        ),
    }


_ORDEN_CONFIANZA = {ALTA: 0, MEDIA: 1, BAJA: 2}


def estimar(
    carrera: Carrera,
    repo: Repo,
    hoy: date,
    carreras: Iterable[Carrera] = (),
    cfg: Estimador = ESTIMADOR,
) -> dict[str, Any]:
    """Estimado para `carrera` con los datos anteriores a `hoy`.

    `hoy` es un parametro y no la fecha real: el informe post-carrera calcula
    el estimado que habia la vispera, para compararlo con el tiempo real.
    """
    base = {
        "carrera": {
            "fecha": carrera.fecha.isoformat(),
            "nombre": carrera.nombre,
            "distancia_km": carrera.distancia_km,
        },
        "calculado_al": hoy.isoformat(),
    }
    if carrera.distancia_km is None:
        return {
            **base,
            "disponible": False,
            "motivo": "la carrera no tiene distancia: declarala con `/carrera "
            f"{carrera.fecha.isoformat()} 21k <nombre>`",
            "estimaciones": [],
        }
    d = carrera.distancia_km
    otras = [c for c in carreras if c.fecha != carrera.fecha]
    de_carreras = esfuerzos_de_carreras(otras, repo, hoy, cfg)
    estimaciones = [
        e
        for e in (
            _por_riegel(de_carreras, d, hoy, cfg),
            _por_tanda(repo, d, hoy, cfg),
            _por_riegel(
                esfuerzos_de_entrenamiento(
                    repo, hoy, cfg, excluir={e.fecha for e in de_carreras}
                ),
                d, hoy, cfg,
            ),
        )
        if e is not None
    ]
    if not estimaciones:
        return {
            **base,
            "disponible": False,
            "motivo": (
                f"no hay carreras de los ultimos {cfg.semanas_carrera} semanas ni "
                f"tramos de entrenamiento con vueltas de los ultimos "
                f"{cfg.semanas_entrenamiento} con una distancia dentro de x"
                f"{cfg.factor_max:g} de {d:g} km"
            ),
            "estimaciones": [],
        }
    # Orden estable: por confianza y, a igual confianza, en el orden de arriba
    # (carrera antes que Tanda antes que entrenamiento).
    estimaciones.sort(key=lambda e: _ORDEN_CONFIANZA[e["confianza"]])
    return {
        **base,
        "disponible": True,
        "motivo": "",
        "principal": estimaciones[0],
        "estimaciones": estimaciones,
        "nota": (
            "estado de forma actual: no proyecta el entrenamiento que falta hasta "
            "la carrera. Sin correccion por desnivel ni calor (la base no los guarda). "
            "Riegel con exponente "
            f"{cfg.exponente:g} (rango {cfg.exponente_min:g}-{cfg.exponente_max:g})"
        ),
    }


def comparar(estimado: dict[str, Any], tiempo_real_s: float | None) -> dict[str, Any]:
    """Estimado de la vispera contra el tiempo real. Negativo = mas rapido."""
    if not estimado.get("disponible") or tiempo_real_s is None:
        return {"disponible": False, "motivo": estimado.get("motivo") or "sin tiempo real"}
    p = estimado["principal"]
    lo, hi = p["rango_s"]
    delta = tiempo_real_s - p["central_s"]
    return {
        "disponible": True,
        "motivo": "",
        "metodo": p["metodo"],
        "origen": p["origen"],
        "estimado": p["central"],
        "rango": p["rango"],
        "real": fmt_tiempo(tiempo_real_s),
        "diferencia_s": round(delta),
        "diferencia": ("-" if delta < 0 else "+") + fmt_tiempo(abs(delta)),
        "diferencia_pct": round(delta / p["central_s"] * 100, 1),
        "dentro_del_rango": lo <= tiempo_real_s <= hi,
    }


def resumen(est: dict[str, Any]) -> str:
    """Texto para Telegram. Lo arma el codigo; no pasa por el modelo."""
    c = est["carrera"]
    titulo = c["nombre"] or c["fecha"]
    if not est["disponible"]:
        return f"Estimado para {titulo}: no disponible ({est['motivo']})."
    lineas = [f"Estimado para {titulo} ({c['distancia_km']:g} km, {c['fecha']}):"]
    for e in est["estimaciones"]:
        r = e["referencia"]
        if e["metodo"] == "tanda":
            fuente = f"Tanda: {r['km_semana']:g} km/sem a {r['ritmo_medio']}"
        elif e["origen"] == "carrera":
            fuente = f"carrera {r['detalle']} ({r['distancia_km']:g} km en {r['tiempo']})"
        else:
            fuente = f"tramo de {r['distancia_km']:g} km en {r['tiempo']} ({r['fecha']})"
        lineas.append(
            f"- {e['central']} ({e['ritmo']}), rango {e['rango']}, "
            f"confianza {e['confianza']} — {fuente}"
        )
    lineas.append(
        "Con tu estado de forma actual: no proyecta lo que falta entrenar. "
        "Sin correccion por desnivel ni calor."
    )
    return "\n".join(lineas)
