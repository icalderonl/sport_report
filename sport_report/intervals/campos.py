"""Nombres de campo de intervals.icu que no estan confirmados, en un solo sitio.

La API expone la dinamica de carrera y el bienestar, pero varios nombres exactos
no se pudieron confirmar sin la cuenta real. La respuesta a eso NO es adivinar
en el codigo de ingesta: es declarar los candidatos aca, tomar el primero que
exista, y dejar el dato en `None` si ninguno aparece.

`None` significa "la fuente no lo trajo", nunca cero: un GCT en 0 ms diria que
el pie no toco el suelo. El paso de despliegue

    python -m sport_report.intervals.verificar --volcar-claves

imprime las claves reales de una actividad y de un registro de bienestar, y dice
que candidato acerto. Con esa salida se cierra esta tabla y se confirman las
unidades, sin tocar nada mas.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Mapping

log = logging.getLogger(__name__)

#: campo propio -> nombres candidatos en la respuesta, en orden de preferencia.
CANDIDATOS: dict[str, tuple[str, ...]] = {
    # -- actividad: lo que Strava no expone y por lo que se cambio de fuente --
    "gct_ms": (
        "average_gct",
        "gct",
        "ground_time",
        "average_ground_contact_time",
        "icu_gct",
    ),
    "oscilacion_vertical_cm": (
        "average_vertical_oscillation",
        "vertical_oscillation",
        "average_vo",
        "icu_vertical_oscillation",
    ),
    "ratio_vertical_pct": (
        "average_vertical_ratio",
        "vertical_ratio",
        "average_vr",
        "icu_vertical_ratio",
    ),
    # -- actividad: confianza alta, vocabulario heredado de Strava -----------
    "distancia_m": ("distance", "icu_distance"),
    "duracion_mov_s": ("moving_time", "icu_moving_time"),
    "duracion_tot_s": ("elapsed_time", "icu_elapsed_time"),
    "hr_promedio": ("average_heartrate", "icu_average_hr", "average_hr"),
    "hr_maximo": ("max_heartrate", "icu_max_hr", "max_hr"),
    "cadencia": ("average_cadence", "icu_average_cadence"),
    "potencia_w": ("average_watts", "icu_average_watts", "icu_weighted_avg_watts"),
    # -- bienestar -----------------------------------------------------------
    "hrv": ("hrv", "hrvSDNN", "hrv_sdnn"),
    "hr_reposo": ("restingHR", "resting_hr", "restingHr"),
    "sueno_s": ("sleepSecs", "sleep_secs", "sleepSeconds"),
    "sueno_score": ("sleepScore", "sleep_score"),
    "readiness": ("readiness", "trainingReadiness", "training_readiness"),
    "body_battery": ("bodyBattery", "body_battery", "bodyBatteryMax"),
}


def primero(d: Mapping[str, Any] | None, campo: str) -> Any:
    """Primer candidato presente y no nulo. `None` si ninguno aparece.

    Un valor presente pero nulo cuenta como ausente: intervals.icu devuelve la
    clave con `null` cuando el dispositivo no midio ese dato.
    """
    if not d:
        return None
    for nombre in CANDIDATOS.get(campo, (campo,)):
        v = d.get(nombre)
        if v is not None:
            return v
    return None


def _numero(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def numero(d: Mapping[str, Any] | None, campo: str) -> float | None:
    """Como `primero`, pero devuelve `None` en vez de romper con basura."""
    return _numero(primero(d, campo))


def entero(d: Mapping[str, Any] | None, campo: str) -> int | None:
    n = numero(d, campo)
    return int(n) if n is not None else None


# -- unidades ---------------------------------------------------------------
#
# Garmin reporta GCT en milisegundos y oscilacion vertical en milimetros. Que
# intervals.icu lo pase tal cual o lo convierta a centimetros no se pudo
# confirmar, asi que se decide por magnitud en un solo lugar: una oscilacion
# real de carrera esta entre 0.4 y 15 cm, o entre 4 y 150 mm. Si el valor
# entra en el rango de mm se divide; si ya viene en cm se deja.
_OSC_CM_MAX = 20.0


def oscilacion_cm(valor: float | None) -> float | None:
    """Oscilacion vertical en cm, venga en cm o en mm."""
    if valor is None:
        return None
    return round(valor / 10.0, 2) if valor > _OSC_CM_MAX else round(valor, 2)


def gct_ms(valor: float | None) -> float | None:
    """Tiempo de contacto en ms. Si viniera en segundos, se convierte.

    Un GCT de carrera esta entre 150 y 400 ms; nunca por debajo de 1, salvo que
    lo que llego este en segundos.
    """
    if valor is None:
        return None
    return round(valor * 1000.0, 1) if valor < 5.0 else round(valor, 1)


def cadencia_spm(valor: float | None) -> float | None:
    """Cadencia en pasos por minuto.

    Igual que en Strava, una cadencia por debajo de 120 son rpm de una pierna y
    se duplica. Por encima ya son pasos.
    """
    if valor is None:
        return None
    return round(valor * 2, 1) if valor < 120 else round(valor, 1)


def informe_candidatos(d: Mapping[str, Any] | None, campos: tuple[str, ...]) -> list[str]:
    """Lineas legibles diciendo que candidato acerto. Lo usa `verificar`."""
    lineas: list[str] = []
    presentes = set(d or {})
    for campo in campos:
        opciones = CANDIDATOS.get(campo, (campo,))
        acierto = next((n for n in opciones if (d or {}).get(n) is not None), None)
        if acierto:
            lineas.append(f"  {campo:24s} <- {acierto} = {(d or {})[acierto]!r}")
        else:
            vistos = [n for n in opciones if n in presentes]
            detalle = f" (presentes pero nulos: {', '.join(vistos)})" if vistos else ""
            lineas.append(
                f"  {campo:24s} <- NINGUN CANDIDATO: probados {', '.join(opciones)}{detalle}"
            )
    return lineas


def tipos_vistos(actividades: list[Mapping[str, Any]]) -> dict[str, int]:
    """Cuenta los valores distintos de `type`. Lo usa `verificar`.

    Sirve para detectar a tiempo que la cinta llegue con un tipo que no esta en
    `config.TIPOS_RUN`: si eso pasa, el volumen se subestima en silencio.
    """
    cuenta: dict[str, int] = {}
    for a in actividades:
        t = str(a.get("type") or a.get("sport_type") or "Desconocido")
        cuenta[t] = cuenta.get(t, 0) + 1
    return dict(sorted(cuenta.items(), key=lambda kv: -kv[1]))


def adaptar_streams(crudo: Any) -> dict[str, list[Any]]:
    """Streams de intervals.icu -> la forma {clave: lista} que espera `metricas`.

    intervals.icu responde una lista de objetos `{"type": ..., "data": [...]}`,
    no el dict indexado por tipo de Strava. Se acepta cualquiera de las dos
    formas: es el punto donde una diferencia de la API real haria fallar la
    carga y el decoupling de toda la semana.
    """
    if isinstance(crudo, dict):
        salida: dict[str, list[Any]] = {}
        for k, v in crudo.items():
            if isinstance(v, dict):
                datos = v.get("data")
            else:
                datos = v
            if isinstance(datos, list):
                salida[k] = datos
        return salida

    if not isinstance(crudo, list):
        return {}

    salida = {}
    for item in crudo:
        if not isinstance(item, dict):
            continue
        clave = item.get("type") or item.get("name")
        datos = item.get("data")
        if isinstance(clave, str) and isinstance(datos, list):
            salida[clave] = datos
    return salida


def zonas_hr(sport_settings: Any) -> list[dict[str, int]] | None:
    """Zonas de HR de intervals.icu -> [{'min', 'max'}], o None si no sirven.

    Devolver None (en vez de inventar zonas) es lo que permite marcar la carga
    como imprecisa mas arriba, igual que con Strava.

    Acepta las dos formas plausibles: una lista de topes ([120, 140, ...]) o una
    lista de objetos con min/max. La ruta y la forma exactas hay que confirmarlas
    con `verificar --volcar-claves`; si no coinciden, la carga cae al peso plano,
    que es una degradacion ya soportada y marcada, no un fallo.
    """
    bruto = _lista_zonas(sport_settings)
    if not bruto:
        return None

    if all(isinstance(z, dict) for z in bruto):
        zonas = [
            {"min": int(_numero(z.get("min")) or 0), "max": int(_numero(z.get("max")) or -1)}
            for z in bruto
        ]
    else:
        topes = [_numero(z) for z in bruto]
        if any(t is None for t in topes):
            return None
        zonas = []
        anterior = 0
        for t in topes:
            zonas.append({"min": anterior, "max": int(t)})
            anterior = int(t)

    if len(zonas) < 5 or all(z["max"] <= 0 for z in zonas):
        log.warning("las zonas de HR de intervals.icu no son utilizables: %r", bruto)
        return None
    # Solo las 5 primeras: el motor de carga pondera Z1..Z5.
    return zonas[:5]


def _lista_zonas(sport_settings: Any) -> list[Any]:
    """Encuentra la lista de zonas en la respuesta, sin asumir una sola forma."""
    candidatos = ("hr_zones", "hrZones", "heart_rate_zones", "zones")
    if isinstance(sport_settings, dict):
        for c in candidatos:
            v = sport_settings.get(c)
            if isinstance(v, list) and v:
                return v
        return []
    if isinstance(sport_settings, list):
        # Una entrada por deporte: manda la de running si se puede distinguir.
        def es_run(d: Any) -> bool:
            if not isinstance(d, dict):
                return False
            tipos = d.get("types") or d.get("type") or ""
            return "Run" in (tipos if isinstance(tipos, str) else " ".join(map(str, tipos)))

        for pref in (es_run, lambda d: isinstance(d, dict)):
            for entrada in sport_settings:
                if pref(entrada):
                    encontrada = _lista_zonas(entrada)
                    if encontrada:
                        return encontrada
    return []


def buscador(d: Mapping[str, Any] | None) -> Callable[[str], Any]:
    """`primero` parcialmente aplicado, para no repetir el dict en cada linea."""

    def _buscar(campo: str) -> Any:
        return primero(d, campo)

    return _buscar
