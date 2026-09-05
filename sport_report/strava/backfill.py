"""Carga historica inicial.

    python -m sport_report.strava.backfill [dias]

ACWR necesita 28 dias de historico para ser confiable. Sin esto, las primeras
cuatro semanas del sistema reportarian ACWR/Monotony como no confiables aunque
los datos ya existan en Strava (spec 7 y 10).

Corre con pausa entre requests: 35 dias pueden ser ~40 llamadas y la cuota es de
100 cada 15 minutos.
"""
from __future__ import annotations

import sys
from datetime import timedelta

from ..db.repo import Repo
from ..fechas import hoy_local
from ..logging_setup import setup
from .client import StravaClient
from .errors import StravaError
from .ingest import Ingesta

DIAS_POR_DEFECTO = 35
PAUSA_S = 1.5


def main(argv: list[str] | None = None) -> int:
    log = setup("backfill")
    argv = argv if argv is not None else sys.argv[1:]
    dias = int(argv[0]) if argv else DIAS_POR_DEFECTO

    hasta = hoy_local()
    desde = hasta - timedelta(days=dias)
    print(f"Backfill de {dias} dias: {desde} a {hasta}")
    print(f"(pausa de {PAUSA_S}s entre requests para no vaciar la cuota)\n")

    repo = Repo()
    cliente = StravaClient(pausa_entre_requests=PAUSA_S)
    corrida = repo.abrir_corrida()
    try:
        resumen = Ingesta(cliente=cliente, repo=repo).sincronizar(desde, hasta)
    except StravaError as exc:
        repo.cerrar_corrida(corrida, "error", str(exc))
        log.error("backfill fallido: %s", exc)
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        cliente.cerrar()

    repo.cerrar_corrida(corrida, "ok", str(resumen.to_json()))
    print("Resultado:")
    for k, v in resumen.to_json().items():
        print(f"  {k:<18} {v}")
    print(f"\nDias con carga registrada: {repo.dias_con_datos(desde, hasta)}")
    print(f"Primera fecha en la base:  {repo.primera_fecha()}")
    repo.cerrar()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
