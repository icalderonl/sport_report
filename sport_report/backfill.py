"""Carga historica inicial, desde la fuente que se elija.

    python -m sport_report.backfill [dias] [--fuente intervals|strava]

ACWR necesita 28 dias de historico para ser confiable. Sin esto, las primeras
cuatro semanas del sistema reportarian ACWR/Monotony como no confiables aunque
los datos ya existan en la fuente (spec 7 y 10).

Contra Strava corre con pausa entre requests: 35 dias pueden ser ~40 llamadas y
la cuota es de 100 cada 15 minutos.

No registra la fuente por semana: el rango de un backfill no es una semana, y
anotar una fuente semanal a partir de el diria algo falso del reporte semanal.
"""
from __future__ import annotations

import argparse
import sys
from datetime import timedelta

from . import config, fuentes
from .db.repo import Repo
from .fechas import hoy_local
from .logging_setup import setup

DIAS_POR_DEFECTO = 35
PAUSA_S = 1.5


def _dias(crudo: str) -> int:
    """Valida los dias. Un traceback no le sirve a nadie a las 7 de la manana."""
    try:
        dias = int(crudo)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{crudo}' no es un numero de dias") from None
    if dias < 1:
        raise argparse.ArgumentTypeError(
            f"los dias a recuperar deben ser 1 o mas, se recibio {dias}"
        )
    return dias


def _args(argv: list[str] | None):
    p = argparse.ArgumentParser(
        prog="python -m sport_report.backfill", description="Carga historica inicial"
    )
    p.add_argument(
        "dias", nargs="?", type=_dias, default=DIAS_POR_DEFECTO, help="dias hacia atras"
    )
    p.add_argument(
        "--fuente",
        choices=(config.INTERVALS, config.STRAVA),
        help="fuente a usar (por defecto, FUENTE_PRINCIPAL del .env)",
    )
    p.add_argument(
        "--reingerir",
        action="store_true",
        help=(
            "volver a bajar sesiones ya guardadas. Necesario cuando cambia el "
            "mapa de campos: una sesion ya procesada se salta, asi que un campo "
            "que antes no se sabia leer se quedaria nulo para siempre"
        ),
    )
    return p.parse_args(argv)


def _ingesta(nombre: str, repo: Repo):
    """Como `fuentes.abrir`, pero con la pausa de cuota que pide Strava."""
    if nombre == config.STRAVA:
        from .strava.client import StravaClient
        from .strava.ingest import Ingesta

        return Ingesta(cliente=StravaClient(pausa_entre_requests=PAUSA_S), repo=repo)
    return fuentes.abrir(nombre, repo)


def main(argv: list[str] | None = None) -> int:
    log = setup("backfill")
    try:
        a = _args(argv)
    except SystemExit as exc:
        # Devolver el codigo en vez de propagarlo: `main` es la interfaz que
        # usan los tests y el runbook, y un argumento mal escrito no debe
        # llegar a abrir la base (ni a imprimir un traceback).
        return int(exc.code or 2)

    nombre = a.fuente or config.FUENTE_PRINCIPAL
    hasta = hoy_local()
    desde = hasta - timedelta(days=a.dias)
    print(f"Backfill de {a.dias} dias desde {nombre}: {desde} a {hasta}")
    if a.reingerir:
        print("(--reingerir: se vuelven a bajar tambien las sesiones ya guardadas)")
    if nombre == config.STRAVA:
        print(f"(pausa de {PAUSA_S}s entre requests para no vaciar la cuota)")
        print("(Strava no expone GCT, oscilacion ni ratio vertical: quedan nulos)")
    print()

    repo = Repo()
    corrida = repo.abrir_corrida()
    ingesta = None
    try:
        ingesta = _ingesta(nombre, repo)
        resumen = ingesta.sincronizar(desde, hasta, forzar=a.reingerir)
    except Exception as exc:
        # Un fallo de la fuente (o una credencial que falta) es un error de
        # operacion, no un traceback: aca no hay respaldo al que caer, porque
        # mezclar dos fuentes en un backfill dejaria un historico a parches.
        repo.cerrar_corrida(corrida, "error", str(exc))
        log.error("backfill fallido: %s", exc)
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    except BaseException as exc:
        # Cualquier otra cosa (Ctrl-C incluido) tambien tiene que cerrar la
        # fila: si no, queda `en_curso` para siempre y el diagnostico no la
        # distingue de un error real.
        repo.cerrar_corrida(corrida, "error", f"{type(exc).__name__}: {exc}")
        raise
    finally:
        cliente = getattr(ingesta, "cliente", None)
        if cliente is not None and hasattr(cliente, "cerrar"):
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
