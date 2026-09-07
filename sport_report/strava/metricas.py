"""Metricas por sesion calculadas sobre los streams. Funciones puras.

Todas devuelven `None` cuando el dato de entrada no alcanza, nunca un numero
inventado: esa distincion es la que despues permite reportar "dato faltante" en
vez de "incumplimiento" (spec 5 y 7).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

# Un salto grande en el stream de tiempo es una pausa (auto-pause, semaforo
# largo, reloj detenido). Contarlo entero inflaria la zona en la que quedo el
# ultimo pulso registrado.
MAX_SALTO_S = 60.0


@dataclass(frozen=True)
class CargaSesion:
    carga: float | None
    impreciso: bool
    minutos_por_zona: tuple[float, ...] = ()
    motivo: str = ""


def _tramos(
    streams: dict[str, Sequence[Any]], claves: tuple[str, ...]
) -> list[tuple[float, dict[str, float]]]:
    """Trocea los streams en (dt, valores) descartando pausas y muestras rotas."""
    tiempo = streams.get("time") or []
    series = {k: streams.get(k) or [] for k in claves}
    if not tiempo or any(not v for v in series.values()):
        return []

    n = min(len(tiempo), *(len(v) for v in series.values()))
    moving = streams.get("moving")
    salida: list[tuple[float, dict[str, float]]] = []
    for i in range(1, n):
        dt = float(tiempo[i]) - float(tiempo[i - 1])
        if dt <= 0 or dt > MAX_SALTO_S:
            continue
        if moving is not None and i < len(moving) and not moving[i]:
            continue
        valores: dict[str, float] = {}
        roto = False
        for k in claves:
            v = series[k][i]
            if v is None:
                roto = True
                break
            valores[k] = float(v)
            if k == "distance":
                previo = series[k][i - 1]
                if previo is None:
                    roto = True
                    break
                valores["_dd"] = float(v) - float(previo)
        if roto:
            continue
        salida.append((dt, valores))
    return salida


def indice_zona(hr: float, zonas: Sequence[dict[str, int]]) -> int:
    """Indice 0-4 de la zona en la que cae un pulso. La ultima zona no tiene tope."""
    for i, z in enumerate(zonas):
        tope = z.get("max", -1)
        if tope is None or tope <= 0:
            return i
        if hr <= tope:
            return i
    return len(zonas) - 1


def carga_trimp(
    streams: dict[str, Sequence[Any]],
    zonas: Sequence[dict[str, int]] | None,
    pesos: Sequence[float],
    peso_fallback: float,
) -> CargaSesion:
    """Carga tipo TRIMP: suma de minutos_en_zona x peso_de_zona.

    Reemplaza usar kilometros como unidad de carga porque el plan mezcla
    sesiones prescritas por distancia con otras por tiempo, y Strava siempre
    reporta duracion + HR real sin importar como se planifico (spec 5).
    """
    tramos = _tramos(streams, ("heartrate",))
    if not tramos:
        return CargaSesion(None, False, motivo="sin stream de HR utilizable")

    minutos_totales = sum(dt for dt, _ in tramos) / 60.0

    if not zonas:
        # Sin zonas del atleta no se puede ponderar: se usa un peso plano y se
        # marca impreciso. Nunca se presenta como dato fino.
        return CargaSesion(
            carga=round(minutos_totales * peso_fallback, 1),
            impreciso=True,
            motivo="zonas de HR no configuradas en Strava; peso de zona fijo",
        )

    minutos = [0.0] * len(zonas)
    for dt, v in tramos:
        minutos[indice_zona(v["heartrate"], zonas)] += dt / 60.0

    carga = sum(m * (pesos[i] if i < len(pesos) else pesos[-1]) for i, m in enumerate(minutos))
    return CargaSesion(
        carga=round(carga, 1),
        impreciso=False,
        minutos_por_zona=tuple(round(m, 2) for m in minutos),
    )


def deriva_cardiaca(
    streams: dict[str, Sequence[Any]], min_puntos: int
) -> float | None:
    """HR decoupling (Pw:HR): caida de eficiencia entre la 1a y la 2a mitad.

    Eficiencia = velocidad / HR. Positivo = la eficiencia bajo (hubo deriva).
    Devuelve None si el stream no alcanza: una sesion corta o con streams pobres
    no da un numero confiable, y un numero inventado es peor que ninguno.
    """
    tramos = _tramos(streams, ("heartrate", "distance"))
    if len(tramos) < min_puntos:
        return None

    total = sum(dt for dt, _ in tramos)
    if total <= 0:
        return None
    mitad = total / 2.0

    acumulado = 0.0
    mitades = [{"t": 0.0, "d": 0.0, "hr_pond": 0.0}, {"t": 0.0, "d": 0.0, "hr_pond": 0.0}]
    for dt, v in tramos:
        idx = 0 if acumulado < mitad else 1
        m = mitades[idx]
        m["t"] += dt
        m["d"] += max(0.0, v.get("_dd", 0.0))
        m["hr_pond"] += v["heartrate"] * dt
        acumulado += dt

    eficiencias: list[float] = []
    for m in mitades:
        if m["t"] <= 0 or m["d"] <= 0 or m["hr_pond"] <= 0:
            return None
        hr_media = m["hr_pond"] / m["t"]
        if hr_media <= 0:
            return None
        eficiencias.append((m["d"] / m["t"]) / hr_media)

    if eficiencias[0] <= 0:
        return None
    return round((eficiencias[0] - eficiencias[1]) / eficiencias[0] * 100.0, 2)


def cadencia_spm(actividad: dict[str, Any]) -> float | None:
    """Strava reporta la cadencia de carrera en rpm (una pierna); se pasa a spm."""
    rpm = actividad.get("average_cadence")
    if rpm is None:
        return None
    try:
        valor = float(rpm)
    except (TypeError, ValueError):
        return None
    return round(valor * 2, 1) if valor > 0 else None


def potencia_w(actividad: dict[str, Any]) -> float | None:
    """Potencia solo si la reporta un dispositivo real (ej. Stryd).

    Strava tambien devuelve `average_watts` estimada; esa no se usa, queda None
    (spec 5: nunca inventarla).
    """
    if not actividad.get("device_watts"):
        return None
    w = actividad.get("average_watts")
    try:
        return round(float(w), 1) if w is not None else None
    except (TypeError, ValueError):
        return None


def distancia_km(actividad: dict[str, Any]) -> float | None:
    """None (no 0.0) si la actividad no reporta distancia: es un dato faltante."""
    d = actividad.get("distance")
    try:
        metros = float(d) if d is not None else 0.0
    except (TypeError, ValueError):
        return None
    return round(metros / 1000.0, 3) if metros > 0 else None


def normalizar_vueltas(strava_id: int, crudas: Sequence[Any]) -> list["Vuelta"]:
    """Vueltas de Strava -> modelo propio, descartando las inservibles.

    Una vuelta sin distancia o sin tiempo no aporta nada a la alineacion contra
    el plan y solo puede estorbar, asi que se cae. El `indice` se toma de
    `lap_index` cuando viene: es lo que identifica la vuelta en Strava y permite
    decir en el reporte cuales se emparejaron.
    """
    from ..db.models import Vuelta

    salida: list[Vuelta] = []
    for i, v in enumerate(crudas):
        if not isinstance(v, dict):
            continue
        try:
            metros = float(v.get("distance") or 0.0)
            segundos = int(v.get("moving_time") or v.get("elapsed_time") or 0)
            indice = int(v.get("lap_index") or (i + 1))
        except (TypeError, ValueError):
            continue
        if metros <= 0 or segundos <= 0:
            continue
        salida.append(
            Vuelta(
                strava_id=strava_id,
                indice=indice,
                distancia_km=round(metros / 1000.0, 3),
                duracion_mov_s=segundos,
            )
        )
    salida.sort(key=lambda v: v.indice)
    return salida
