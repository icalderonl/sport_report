"""Chequeo manual de la conexion con Strava.

    python -m sport_report.strava.verificar [dias]

No escribe nada en la base: solo confirma que las credenciales, los scopes y los
streams estan como el sistema los necesita.
"""
from __future__ import annotations

import sys
from datetime import timedelta

from ..fechas import ahora_local
from ..logging_setup import setup
from .client import StravaClient
from .errors import StravaError


def main(argv: list[str] | None = None) -> int:
    setup("verificar")
    argv = argv if argv is not None else sys.argv[1:]
    dias = int(argv[0]) if argv else 7

    cli = StravaClient()
    try:
        atleta = cli.atleta()
        print(f"Atleta: {atleta.get('firstname')} {atleta.get('lastname')} (id {atleta.get('id')})")

        zonas = cli.zonas_hr()
        if zonas:
            print("Zonas de HR:", " ".join(f"Z{i+1}:{z['min']}-{z['max']}" for i, z in enumerate(zonas)))
        else:
            print("Zonas de HR: NO configuradas o sin scope profile:read_all.")
            print("  -> la carga usara el peso fallback y quedara marcada como imprecisa.")

        hasta = ahora_local()
        desde = hasta - timedelta(days=dias)
        acts = cli.actividades(desde, hasta)
        print(f"\nActividades de los ultimos {dias} dias: {len(acts)}")
        for a in acts:
            km = (a.get("distance") or 0) / 1000
            print(
                f"  {a.get('start_date_local', '')[:16]}  {a.get('type', '?'):<14} "
                f"{km:6.2f}km  hr={a.get('average_heartrate') or '-'}  "
                f"watts={a.get('average_watts') or '-'}  id={a.get('id')}"
            )

        corridas = [a for a in acts if a.get("type") in ("Run", "TrailRun", "VirtualRun")]
        if corridas:
            aid = corridas[0]["id"]
            s = cli.streams(aid)
            print(f"\nStreams de la actividad {aid}:")
            for k, v in s.items():
                print(f"  {k:<16} {len(v)} puntos")
            if "heartrate" not in s:
                print("  ADVERTENCIA: sin stream de HR -> esa sesion no tendra carga ni deriva.")

        print(f"\nUso de cuota (15min/dia): {cli.ultimo_uso}")
        return 0
    except StravaError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        cli.cerrar()


if __name__ == "__main__":
    raise SystemExit(main())
