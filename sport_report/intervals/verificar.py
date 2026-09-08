"""Verificacion de solo lectura contra intervals.icu.

Dos usos, los dos pasos del despliegue (ver deploy/runbook-intervals.md):

1. `python -m sport_report.intervals.verificar`
   Confirma que la clave sirve. Imprime el nombre del atleta, su id real y la
   LONGITUD de la clave — nunca la clave. Si da 401, el caracter que falta esta
   en el .env: se vuelve a copiar por scp, no se re-teclea.

2. `python -m sport_report.intervals.verificar --volcar-claves`
   Imprime las claves reales de una actividad, de un registro de bienestar y de
   sport-settings, y dice que candidato de `campos.CANDIDATOS` acerto. Con esa
   salida se cierra esa tabla y se confirman unidades y vocabulario de tipos.

No escribe nada: ni base, ni plan, ni Telegram.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta

from .. import config, logging_setup
from . import campos
from .auth import resumen_clave
from .client import IntervalsClient
from .errors import IntervalsError

CAMPOS_ACTIVIDAD = (
    "gct_ms",
    "oscilacion_vertical_cm",
    "ratio_vertical_pct",
    "distancia_m",
    "duracion_mov_s",
    "hr_promedio",
    "hr_maximo",
    "cadencia",
    "potencia_w",
)
CAMPOS_BIENESTAR = (
    "hrv",
    "hr_reposo",
    "sueno_s",
    "sueno_score",
    "readiness",
    "body_battery",
)


def _rango(dias: int) -> tuple[datetime, datetime]:
    fin = datetime.now(config.TZ)
    return fin - timedelta(days=dias), fin


def _cabecera(cli: IntervalsClient) -> dict:
    print(f"base           : {cli.base}")
    print(f"atleta pedido  : {cli.atleta_id}")
    print(f"clave          : {resumen_clave()}")
    atleta = cli.atleta()
    print(f"atleta real    : {atleta.get('name') or '(sin nombre)'} (id={atleta.get('id')})")
    if str(atleta.get("id") or "") not in ("", cli.atleta_id):
        print(
            f"  AVISO: '{cli.atleta_id}' no es tu id. Pon "
            f"INTERVALS_ATHLETE_ID={atleta.get('id')} en el .env"
        )
    return atleta


def revisar(dias: int = 14, cli: IntervalsClient | None = None) -> int:
    """Chequeo corto: clave, atleta, actividades, tipos, zonas y bienestar."""
    cli = cli or IntervalsClient()
    try:
        _cabecera(cli)
        desde, hasta = _rango(dias)
        actividades = cli.actividades(desde, hasta)
        print(f"\nactividades ultimos {dias} dias: {len(actividades)}")

        tipos = campos.tipos_vistos(actividades)
        print("tipos vistos   :", tipos or "(ninguno)")
        # Es el aviso que evita subestimar el volumen en silencio: si la cinta
        # llega con un tipo que no esta en TIPOS_RUN, no cuenta como carrera.
        desconocidos = [
            t for t in tipos if t not in config.TIPOS_RUN and t not in config.TIPOS_FUERZA
        ]
        if desconocidos:
            print(
                f"  AVISO: {', '.join(desconocidos)} no estan en TIPOS_RUN ni en "
                "TIPOS_FUERZA. Si alguno es carrera (cinta, pista), agregalo a "
                "config.TIPOS_RUN o quedara fuera del volumen"
            )

        zonas = cli.zonas_hr()
        if zonas:
            print("zonas de HR    :", [z["max"] for z in zonas])
        else:
            print(
                "zonas de HR    : NO disponibles -> la carga saldra con peso plano "
                "y marcada como imprecisa"
            )

        bienestar = cli.bienestar(desde, hasta)
        con_datos = [d for d in bienestar if any(campos.numero(d, c) is not None for c in CAMPOS_BIENESTAR)]
        print(f"bienestar      : {len(bienestar)} registros, {len(con_datos)} con alguna metrica")

        corridas = [a for a in actividades if (a.get("type") or "") in config.TIPOS_RUN]
        if corridas:
            with_dyn = sum(1 for a in corridas if campos.numero(a, "gct_ms") is not None)
            print(f"dinamica       : {with_dyn}/{len(corridas)} corridas traen GCT")
            if not with_dyn:
                print(
                    "  AVISO: ninguna trae GCT con los candidatos actuales. Corre "
                    "--volcar-claves y cierra intervals/campos.py"
                )
        print("\n=> intervals.icu responde y la clave sirve")
        return 0
    except IntervalsError as exc:
        print(f"\n=> FALLA: {exc}", file=sys.stderr)
        return 1
    finally:
        cli.cerrar()


def volcar_claves(dias: int = 30, cli: IntervalsClient | None = None) -> int:
    """Imprime las claves reales y que candidato acerto. Paso del despliegue."""
    cli = cli or IntervalsClient()
    try:
        _cabecera(cli)
        desde, hasta = _rango(dias)

        actividades = cli.actividades(desde, hasta)
        corridas = [a for a in actividades if (a.get("type") or "") in config.TIPOS_RUN]
        if not corridas:
            print(f"\nno hay corridas en los ultimos {dias} dias; prueba con mas dias")
        else:
            # La actividad completa: el listado puede traer menos campos que el
            # detalle, y la dinamica es justo lo que suele faltar en el listado.
            act = cli.actividad(corridas[0].get("id"))
            act = act or corridas[0]
            print(f"\n--- ACTIVIDAD {act.get('id')} ({act.get('name')!r}) ---")
            print("claves:", ", ".join(sorted(act)))
            print("candidatos:")
            print("\n".join(campos.informe_candidatos(act, CAMPOS_ACTIVIDAD)))
            print(
                "\nunidades a confirmar: GCT en ms (150-400) y oscilacion en cm "
                "(0.4-15). Si llegan en segundos o mm, la conversion va en "
                "intervals/campos.py y en un solo sitio."
            )

            crudo = cli.streams(act.get("id"))
            print(f"\n--- STREAMS de {act.get('id')} ---")
            print("claves adaptadas:", ", ".join(sorted(crudo)) or "(ninguna)")
            print("largos:", {k: len(v) for k, v in crudo.items()})
            if "heartrate" not in crudo:
                print("  AVISO: sin 'heartrate' no hay carga TRIMP ni decoupling")

            vueltas = cli.intervalos(act.get("id"))
            print(f"\n--- INTERVALOS de {act.get('id')}: {len(vueltas)} ---")
            if vueltas:
                print("claves de la primera:", ", ".join(sorted(vueltas[0])))
                print(
                    "  comparalos con las vueltas de Strava para la misma sesion "
                    "antes de fiarte de la alineacion contra `estructura=`"
                )

        bienestar = cli.bienestar(desde, hasta)
        print(f"\n--- BIENESTAR: {len(bienestar)} registros ---")
        if bienestar:
            reciente = bienestar[-1]
            print("claves:", ", ".join(sorted(reciente)))
            print("candidatos:")
            print("\n".join(campos.informe_candidatos(reciente, CAMPOS_BIENESTAR)))

        ajustes = cli.sport_settings()
        print("\n--- SPORT-SETTINGS ---")
        print(json.dumps(ajustes, indent=2, ensure_ascii=False)[:2000])
        print("\nzonas interpretadas:", campos.zonas_hr(ajustes))
        print("\n=> volcado listo; cierra intervals/campos.py con esta salida")
        return 0
    except IntervalsError as exc:
        print(f"\n=> FALLA: {exc}", file=sys.stderr)
        return 1
    finally:
        cli.cerrar()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Verifica el acceso a intervals.icu")
    p.add_argument("dias", nargs="?", type=int, default=14, help="dias hacia atras")
    p.add_argument(
        "--volcar-claves",
        action="store_true",
        help="imprime las claves reales y que candidato de campos.py acerto",
    )
    p.add_argument("--json", metavar="ID", help="volcado JSON de una actividad")
    p.add_argument("--streams", metavar="ID", help="forma real de la respuesta de streams")
    args = p.parse_args(argv)

    logging_setup.setup("verificar_intervals")

    if args.json or args.streams:
        cli = IntervalsClient()
        try:
            if args.json:
                print(json.dumps(cli.actividad(args.json), indent=2, ensure_ascii=False))
            if args.streams:
                print(json.dumps(cli.streams(args.streams), indent=2)[:4000])
            return 0
        except IntervalsError as exc:
            print(f"=> FALLA: {exc}", file=sys.stderr)
            return 1
        finally:
            cli.cerrar()

    if args.volcar_claves:
        return volcar_claves(max(args.dias, 30))
    return revisar(args.dias)


if __name__ == "__main__":
    raise SystemExit(main())
