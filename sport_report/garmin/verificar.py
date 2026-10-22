"""Verificacion de solo lectura contra la API Personal de Garmin.

    python -m sport_report.garmin.verificar            # chequeo corto
    python -m sport_report.garmin.verificar --volcar   # + claves y JSON crudo

Confirma lo que la especificacion promete y que el reporte necesita para que
Garmin pueda reemplazar a Strava como respaldo: actividades por fecha con
dinamica de carrera, el archivo FIT (streams y vueltas), zonas de FC, HRV,
sueno con score, FC en reposo y Body Battery.

Imprime la LONGITUD de la clave y el vencimiento del token, nunca la clave ni
el token. No escribe nada: ni base, ni plan, ni Telegram.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import zipfile
from datetime import date, datetime, timedelta
from typing import Any

from .. import config, logging_setup
from .auth import resumen_clave, vencimiento_token
from .client import GarminClient
from .errors import GarminError, SinPermiso

#: Lo que el reporte usa de cada corrida, con el nombre que trae Garmin.
CAMPOS_CORRIDA = (
    "distance",
    "movingDuration",
    "averageHR",
    "maxHR",
    "averageRunningCadenceInStepsPerMinute",
    "avgPower",
    "avgGroundContactTime",
    "avgVerticalOscillation",
    "avgVerticalRatio",
    "avgStrideLength",
    "lapCount",
    "hrTimeInZone_1",
)


def _es_corrida(act: dict[str, Any]) -> bool:
    tipo = (act.get("activityType") or {}).get("typeKey") or ""
    return "run" in tipo


def _volcar(titulo: str, dato: Any, volcar: bool, largo: int = 3000) -> None:
    if volcar:
        print(f"--- {titulo} (crudo) ---")
        print(json.dumps(dato, indent=2, ensure_ascii=False, default=str)[:largo])


def _seccion(nombre: str, fn, problemas: list[str]) -> Any:
    """Corre un chequeo y anota la falla sin cortar los demas."""
    try:
        return fn()
    except SinPermiso as exc:
        print(f"{nombre:<15}: NO disponible ({exc})")
        problemas.append(nombre)
    except GarminError as exc:
        print(f"{nombre:<15}: FALLA ({exc})")
        problemas.append(nombre)
    return None


def _fit(cli: GarminClient, actividad_id: Any) -> str:
    crudo = cli.archivo_actividad(actividad_id)
    try:
        with zipfile.ZipFile(io.BytesIO(crudo)) as z:
            nombres = z.namelist()
            fit = next((n for n in nombres if n.lower().endswith(".fit")), None)
            if not fit:
                return f"ZIP de {len(crudo)} bytes sin .fit: {nombres}"
            datos = z.read(fit)
    except zipfile.BadZipFile:
        datos, fit = crudo, "(sin ZIP)"
    firma = datos[8:12] == b".FIT"
    return f"{fit}, {len(datos)} bytes, firma FIT {'OK' if firma else 'NO reconocida'}"


def revisar(dias: int = 14, volcar: bool = False, cli: GarminClient | None = None) -> int:
    cli = cli or GarminClient()
    problemas: list[str] = []
    hasta = datetime.now(config.TZ).date()
    desde = hasta - timedelta(days=dias)
    try:
        print(f"base           : {cli.base}")
        print(f"clave          : {resumen_clave()}")
        try:
            token = cli.token()
        except GarminError as exc:
            print(f"\n=> FALLA al pedir el token: {exc}", file=sys.stderr)
            return 1
        exp = vencimiento_token(token)
        print(f"token          : OK, vence {exp.isoformat() if exp else '(sin exp legible)'}")
        print(f"rango          : {desde} a {hasta}")

        ajustes = _seccion("ajustes", cli.ajustes_usuario, problemas)
        if ajustes is not None:
            print(f"ajustes        : zona horaria {ajustes.get('timeZone')!r}, "
                  f"sistema {ajustes.get('measurementSystem')!r}")

        acts = _seccion("actividades", lambda: cli.actividades(desde, hasta), problemas)
        corridas: list[dict[str, Any]] = []
        if acts is not None:
            tipos = sorted({(a.get("activityType") or {}).get("typeKey") or "?" for a in acts})
            corridas = [a for a in acts if _es_corrida(a)]
            print(f"actividades    : {len(acts)} ({len(corridas)} corridas)")
            print(f"tipos vistos   : {tipos or '(ninguno)'}")
            if corridas:
                print("campos en corridas (presentes / total):")
                for campo in CAMPOS_CORRIDA:
                    n = sum(1 for a in corridas if a.get(campo) is not None)
                    print(f"  {campo:<40} {n}/{len(corridas)}")
                _volcar("primera corrida del listado", corridas[0], volcar)

        if corridas:
            aid = corridas[-1].get("activityId")
            det = _seccion("detalle", lambda: cli.actividad(aid), problemas)
            if det is not None:
                resumen = det.get("summaryDTO") or {}
                tramos = sorted({s.get("splitType") for s in det.get("splitSummaries") or []})
                vueltas = (det.get("metadataDTO") or {}).get("lapCount")
                print(f"detalle {aid}: GCT={resumen.get('groundContactTime')} "
                      f"osc={resumen.get('verticalOscillation')} "
                      f"ratio={resumen.get('verticalRatio')} vueltas={vueltas}")
                print(f"  tramos agregados: {tramos or '(ninguno)'}")
                if volcar:
                    print("  claves summaryDTO:", ", ".join(sorted(resumen)))
            fit = _seccion("archivo FIT", lambda: _fit(cli, aid), problemas)
            if fit is not None:
                print(f"archivo FIT    : {fit}")

        zonas = _seccion("zonas de FC", cli.zonas_hr, problemas)
        if zonas is not None:
            for z in zonas:
                pisos = [z.get(f"zone{i}Floor") for i in range(1, 6)]
                print(f"zonas de FC    : {z.get('sport')} ({z.get('trainingMethod')}) "
                      f"pisos {pisos}, FCmax {z.get('maxHeartRateUsed')}")
            if not zonas:
                print("zonas de FC    : lista vacia")

        hrv = _seccion("HRV", lambda: cli.hrv(desde, hasta), problemas)
        if hrv is not None:
            dias_hrv = hrv.get("hrvSummaries") or []
            print(f"HRV            : {len(dias_hrv)} noches")
            if dias_hrv:
                u = dias_hrv[-1]
                print(f"  ultima {u.get('calendarDate')}: lastNightAvg={u.get('lastNightAvg')} "
                      f"status={u.get('status')} baseline={u.get('baseline')}")
            _volcar("HRV", hrv, volcar, 1500)

        sueno = _seccion("sueno", lambda: cli.sueno(max(desde, hasta - timedelta(days=30)), hasta), problemas)
        if sueno is not None:
            print(f"sueno          : {len(sueno)} registros")
            if sueno:
                u = sueno[-1]
                # Puede venir plano o envuelto en dailySleepDTO; se informa lo que haya.
                dto = u.get("dailySleepDTO", u) if isinstance(u, dict) else {}
                puntajes = dto.get("sleepScores") or u.get("sleepScores") or {}
                print(f"  ultimo {dto.get('calendarDate')}: sleepTimeSeconds="
                      f"{dto.get('sleepTimeSeconds')}")
                print(f"  claves de sleepScores: {sorted(puntajes) or '(ninguna)'}")
                if volcar:
                    print("  claves:", ", ".join(sorted(u)))
            _volcar("sueno (ultimo)", sueno[-1:] if sueno else sueno, volcar, 2500)

        diarios = _seccion("resumen diario", lambda: cli.resumenes_diarios(desde, hasta), problemas)
        if diarios is not None:
            print(f"resumen diario : {len(diarios)} dias")
            if diarios:
                u = diarios[-1]
                print(f"  ultimo {u.get('calendarDate')}: FC reposo="
                      f"{(u.get('heartRate') or {}).get('restingValue')} "
                      f"Body Battery max={(u.get('bodyBattery') or {}).get('maxValue')}")
            _volcar("resumen diario (ultimo)", diarios[-1:] if diarios else diarios, volcar, 2000)

        if problemas:
            print(f"\n=> token OK, pero fallaron: {', '.join(problemas)}")
            return 2
        print("\n=> la API Personal de Garmin responde en todo lo que el reporte necesita")
        return 0
    finally:
        cli.cerrar()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Verifica el acceso a la API Personal de Garmin")
    p.add_argument("dias", nargs="?", type=int, default=14, help="dias hacia atras")
    p.add_argument("--volcar", action="store_true", help="imprime claves y JSON crudo")
    args = p.parse_args(argv)
    logging_setup.setup("verificar_garmin")
    return revisar(args.dias, args.volcar)


if __name__ == "__main__":
    raise SystemExit(main())
