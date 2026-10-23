"""Compara Garmin contra intervals.icu en las mismas corridas y noches.

    python -m sport_report.garmin.comparar [dias]

Es la prueba de que Garmin puede respaldar a intervals.icu sin cambiar las
cifras del reporte. Las dos fuentes leen el mismo FIT del reloj, asi que lo
esperable es que coincidan dentro de una tolerancia chica. Una diferencia
grande es una pista de unidades o de campo equivocado, y hay que resolverla
antes de que Garmin alimente ninguna metrica.

Solo lectura: no escribe base, plan ni Telegram.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable

from .. import config, logging_setup
from ..intervals import campos
from ..intervals.client import IntervalsClient
from ..intervals.errors import IntervalsError
from .client import GarminClient
from .errors import GarminError

#: Dos actividades son la misma si empiezan con menos de esto de diferencia.
VENTANA_EMPAREJAR_S = 120

OK, DIFERENCIA, FALTA = "ok", "DIFERENCIA", "falta"


@dataclass(frozen=True)
class Tolerancia:
    absoluta: float = 0.0
    relativa: float = 0.0  # fraccion del valor de intervals.icu

    def acepta(self, a: float, b: float) -> bool:
        return abs(a - b) <= max(self.absoluta, abs(a) * self.relativa)


@dataclass(frozen=True)
class Fila:
    metrica: str
    intervals: float | None
    garmin: float | None
    estado: str
    nota: str = ""


def _fila(metrica: str, a: float | None, b: float | None, tol: Tolerancia, nota: str = "") -> Fila:
    if a is None or b is None:
        return Fila(metrica, a, b, FALTA, nota)
    return Fila(metrica, a, b, OK if tol.acepta(a, b) else DIFERENCIA, nota)


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# -- corridas ---------------------------------------------------------------

#: metrica -> (lector intervals.icu, lector Garmin, tolerancia). Los lectores
#: de intervals pasan por las mismas conversiones que usa la ingesta.
METRICAS_CORRIDA: dict[str, tuple[Callable, Callable, Tolerancia]] = {
    "distancia_m": (
        lambda a: campos.numero(a, "distancia_m"),
        lambda g: _num(g.get("distance")),
        Tolerancia(absoluta=20, relativa=0.005),
    ),
    "tiempo_mov_s": (
        lambda a: campos.numero(a, "duracion_mov_s"),
        lambda g: _num(g.get("movingDuration")),
        Tolerancia(absoluta=5, relativa=0.005),
    ),
    "fc_promedio": (
        lambda a: campos.numero(a, "hr_promedio"),
        lambda g: _num(g.get("averageHR")),
        Tolerancia(absoluta=1),
    ),
    "fc_maxima": (
        lambda a: campos.numero(a, "hr_maximo"),
        lambda g: _num(g.get("maxHR")),
        Tolerancia(absoluta=1),
    ),
    "cadencia_spm": (
        lambda a: campos.cadencia_spm(campos.numero(a, "cadencia")),
        lambda g: campos.cadencia_spm(_num(g.get("averageRunningCadenceInStepsPerMinute"))),
        Tolerancia(absoluta=1),
    ),
    "gct_ms": (
        lambda a: campos.gct_ms(campos.numero(a, "gct_ms")),
        lambda g: campos.gct_ms(_num(g.get("avgGroundContactTime"))),
        Tolerancia(absoluta=2),
    ),
    "oscilacion_cm": (
        lambda a: campos.oscilacion_cm(campos.numero(a, "oscilacion_vertical_cm")),
        lambda g: campos.oscilacion_cm(_num(g.get("avgVerticalOscillation"))),
        Tolerancia(absoluta=0.1),
    ),
    "ratio_vertical_pct": (
        lambda a: campos.numero(a, "ratio_vertical_pct"),
        lambda g: _num(g.get("avgVerticalRatio")),
        Tolerancia(absoluta=0.1),
    ),
}


def _inicio_intervals(a: dict[str, Any]) -> datetime | None:
    crudo = a.get("start_date_local")
    try:
        return datetime.fromisoformat(str(crudo)[:19]) if crudo else None
    except ValueError:
        return None


def _inicio_garmin(g: dict[str, Any]) -> datetime | None:
    crudo = g.get("startTimeLocal")
    try:
        return datetime.fromisoformat(str(crudo)[:19]) if crudo else None
    except ValueError:
        return None


def es_corrida_garmin(g: dict[str, Any]) -> bool:
    return "run" in ((g.get("activityType") or {}).get("typeKey") or "")


def emparejar(
    de_intervals: list[dict[str, Any]], de_garmin: list[dict[str, Any]]
) -> tuple[list[tuple[dict, dict]], list[dict], list[dict]]:
    """Pares (intervals, garmin) por hora de inicio local, mas los sueltos."""
    libres = [g for g in de_garmin if _inicio_garmin(g)]
    pares, solo_intervals = [], []
    for a in de_intervals:
        ini = _inicio_intervals(a)
        candidatos = [
            g for g in libres
            if ini and abs((_inicio_garmin(g) - ini).total_seconds()) <= VENTANA_EMPAREJAR_S
        ]
        if not candidatos:
            solo_intervals.append(a)
            continue
        g = min(candidatos, key=lambda g: abs((_inicio_garmin(g) - ini).total_seconds()))
        libres.remove(g)
        pares.append((a, g))
    return pares, solo_intervals, libres


def comparar_corrida(a: dict[str, Any], g: dict[str, Any]) -> list[Fila]:
    return [_fila(m, li(a), lg(g), tol) for m, (li, lg, tol) in METRICAS_CORRIDA.items()]


# -- bienestar --------------------------------------------------------------


def _por_fecha(registros: list[dict[str, Any]], clave: str) -> dict[str, dict[str, Any]]:
    salida = {}
    for r in registros or []:
        dto = r.get("dailySleepDTO", r) if isinstance(r, dict) else {}
        fecha = str(dto.get(clave) or "")[:10]
        if fecha:
            salida[fecha] = dto
    return salida


def bienestar_garmin(
    hrv: dict[str, Any], sueno: list[dict[str, Any]], diarios: list[dict[str, Any]]
) -> dict[str, dict[str, float | None]]:
    """fecha -> metricas de bienestar con el nombre propio del reporte."""
    por_hrv = _por_fecha((hrv or {}).get("hrvSummaries") or [], "calendarDate")
    por_sueno = _por_fecha(sueno, "calendarDate")
    por_dia = _por_fecha(diarios, "calendarDate")
    salida: dict[str, dict[str, float | None]] = {}
    for fecha in sorted(set(por_hrv) | set(por_sueno) | set(por_dia)):
        s = por_sueno.get(fecha, {})
        segundos = _num(s.get("sleepTimeSeconds"))
        d = por_dia.get(fecha, {})
        salida[fecha] = {
            "hrv": _num(por_hrv.get(fecha, {}).get("lastNightAvg")),
            "hr_reposo": _num((d.get("heartRate") or {}).get("restingValue")),
            "sueno_h": round(segundos / 3600.0, 2) if segundos else None,
            "sueno_score": _num(((s.get("sleepScores") or {}).get("overall") or {}).get("value")),
            "body_battery": _num((d.get("bodyBattery") or {}).get("maxValue")),
            # Para explicar una diferencia de horas de sueno: intervals.icu
            # podria estar sumando siestas o tiempo despierto.
            "_siesta_h": _horas(s.get("napTimeSeconds")),
            "_despierto_h": _horas(s.get("awakeSleepSeconds")),
            "_sin_medir_h": _horas(s.get("unmeasurableSleepSeconds")),
        }
    return salida


def _horas(segundos: Any) -> float | None:
    n = _num(segundos)
    return round(n / 3600.0, 2) if n is not None else None


def desglose_sueno(g: dict[str, float | None]) -> str:
    """Partes del sueno de Garmin que no entran en `sueno_h`."""
    partes = [f"siesta {g.get('_siesta_h')}", f"despierto {g.get('_despierto_h')}",
              f"sin medir {g.get('_sin_medir_h')}"]
    return "Garmin aparte: " + ", ".join(partes) + " h"


TOLERANCIAS_BIENESTAR = {
    "hrv": Tolerancia(absoluta=1),
    "hr_reposo": Tolerancia(absoluta=1),
    "sueno_h": Tolerancia(absoluta=0.05),
    "sueno_score": Tolerancia(absoluta=1),
    # intervals.icu puede guardar otro momento del dia (no el maximo): sirve
    # de referencia, no como criterio.
    "body_battery": Tolerancia(absoluta=5),
}


def comparar_bienestar(
    de_intervals: list[dict[str, Any]], de_garmin: dict[str, dict[str, float | None]]
) -> dict[str, list[Fila]]:
    salida: dict[str, list[Fila]] = {}
    for crudo in de_intervals or []:
        fecha = str(crudo.get("id") or crudo.get("date") or "")[:10]
        if fecha not in de_garmin:
            continue
        g = de_garmin[fecha]
        segundos = campos.numero(crudo, "sueno_s")
        valores_i = {
            "hrv": campos.numero(crudo, "hrv"),
            "hr_reposo": campos.numero(crudo, "hr_reposo"),
            "sueno_h": round(segundos / 3600.0, 2) if segundos else None,
            "sueno_score": campos.numero(crudo, "sueno_score"),
            "body_battery": campos.numero(crudo, "body_battery"),
        }
        filas = []
        for m, tol in TOLERANCIAS_BIENESTAR.items():
            f = _fila(m, valores_i[m], g.get(m), tol, "referencial" if m == "body_battery" else "")
            if m == "sueno_h" and f.estado == DIFERENCIA:
                f = Fila(f.metrica, f.intervals, f.garmin, f.estado, desglose_sueno(g))
            filas.append(f)
        salida[fecha] = filas
    return salida


# -- salida -----------------------------------------------------------------


def _fmt(v: float | None) -> str:
    return "-" if v is None else f"{v:g}"


def _imprimir(filas: list[Fila]) -> None:
    for f in filas:
        nota = f" ({f.nota})" if f.nota else ""
        print(f"  {f.metrica:<20} {_fmt(f.intervals):>10} {_fmt(f.garmin):>10}  {f.estado}{nota}")


def _zonas(cli_i: IntervalsClient, cli_g: GarminClient) -> None:
    zi = cli_i.zonas_hr()
    zg = next((z for z in cli_g.zonas_hr() if z.get("sport") == "RUNNING"), None)
    print("\nZONAS DE FC (no tienen por que coincidir: cada sitio tiene su configuracion)")
    print(f"  intervals.icu, techos : {[z['max'] for z in zi] if zi else '(no disponibles)'}")
    if zg:
        print(f"  Garmin RUNNING, pisos : {[zg.get(f'zone{i}Floor') for i in range(1, 6)]}, "
              f"FCmax {zg.get('maxHeartRateUsed')}")
    else:
        print("  Garmin RUNNING        : (no disponibles)")


def comparar(dias: int = 30, cli_i: IntervalsClient | None = None,
             cli_g: GarminClient | None = None) -> int:
    dias = min(dias, 31)  # sueno de Garmin: maximo 31 dias por pedido
    cli_i = cli_i or IntervalsClient()
    cli_g = cli_g or GarminClient()
    hoy = datetime.now(config.TZ)
    desde_dt = hoy - timedelta(days=dias)
    desde, hasta = desde_dt.date(), hoy.date()
    total = diferencias = 0
    try:
        acts_i = [a for a in cli_i.actividades(desde_dt, hoy) if (a.get("type") or "") in config.TIPOS_RUN]
        acts_g = [g for g in cli_g.actividades(desde, hasta) if es_corrida_garmin(g)]
        pares, solo_i, solo_g = emparejar(acts_i, acts_g)
        print(f"rango {desde} a {hasta}: corridas intervals.icu={len(acts_i)} "
              f"Garmin={len(acts_g)} emparejadas={len(pares)}")
        print(f"  {'metrica':<20} {'intervals':>10} {'garmin':>10}  estado")
        for a, g in pares:
            print(f"\n{a.get('start_date_local')}  {a.get('name')!r}")
            filas = comparar_corrida(a, g)
            _imprimir(filas)
            total += sum(f.estado != FALTA for f in filas)
            diferencias += sum(f.estado == DIFERENCIA for f in filas)
        for a in solo_i:
            print(f"\nSOLO en intervals.icu: {a.get('start_date_local')} {a.get('name')!r}")
        for g in solo_g:
            print(f"\nSOLO en Garmin: {g.get('startTimeLocal')} {g.get('activityName')!r}")

        b_g = bienestar_garmin(cli_g.hrv(desde, hasta), cli_g.sueno(desde, hasta),
                               cli_g.resumenes_diarios(desde, hasta))
        por_fecha = comparar_bienestar(cli_i.bienestar(desde_dt, hoy), b_g)
        print(f"\nBIENESTAR: {len(por_fecha)} dias en ambas fuentes")
        for fecha, filas in sorted(por_fecha.items()):
            print(f"\n{fecha}")
            _imprimir(filas)
            criterio = [f for f in filas if f.nota != "referencial"]
            total += sum(f.estado != FALTA for f in criterio)
            diferencias += sum(f.estado == DIFERENCIA for f in criterio)

        _zonas(cli_i, cli_g)
    except (IntervalsError, GarminError) as exc:
        print(f"\n=> FALLA: {exc}", file=sys.stderr)
        return 1
    finally:
        cli_i.cerrar()
        cli_g.cerrar()

    print(f"\n=> {total - diferencias}/{total} comparaciones dentro de tolerancia")
    if diferencias:
        print("   revisa las filas DIFERENCIA antes de usar Garmin como respaldo")
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Compara Garmin contra intervals.icu")
    p.add_argument("dias", nargs="?", type=int, default=30, help="dias hacia atras (max 31)")
    args = p.parse_args(argv)
    logging_setup.setup("comparar_garmin")
    return comparar(args.dias)


if __name__ == "__main__":
    raise SystemExit(main())
