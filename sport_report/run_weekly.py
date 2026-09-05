"""Orquestador semanal. Es lo que dispara el cron los lunes a las 07:00.

    python -m sport_report.run_weekly [opciones]

Encadena: ingesta Strava -> motor de calculo -> narrativa -> formateo -> Telegram.

Politica de fallos (spec 10): el objetivo es que SIEMPRE llegue un mensaje.
  - Si Strava falla, se reporta igual con lo que ya hay en la base, avisando.
  - Si Claude falla, se reporta igual sin la parte narrativa.
  - Solo un fallo del motor de calculo o del envio hace fracasar la corrida.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from . import config
from .db.repo import Repo
from .engine import report
from .fechas import RangoSemana, semana_a_reportar, semana_de
from .logging_setup import setup
from .narrative.claude import redactar
from .plan.store import PlanStore
from .strava.client import StravaClient
from .strava.errors import StravaError
from .strava.ingest import Ingesta
from .telegram.formato import formatear_reporte
from .telegram.sender import enviar

log = logging.getLogger(__name__)

OK = "ok"
PARCIAL = "parcial"
ERROR = "error"


@dataclass
class Resultado:
    estado: str
    rango: RangoSemana
    mensaje: str = ""
    datos: dict[str, Any] = field(default_factory=dict)
    enviado: bool = False
    problemas: list[str] = field(default_factory=list)

    @property
    def codigo_salida(self) -> int:
        return 1 if self.estado == ERROR else 0


def _guardar_reporte(rango: RangoSemana, datos: dict[str, Any]) -> None:
    """Deja el JSON en disco para poder auditar que vio el sistema esa semana."""
    from .storage import escribir_json

    escribir_json(config.DATA_DIR / "reportes" / f"{rango.clave}.json", datos)


def ejecutar(
    rango: RangoSemana,
    repo: Repo,
    plan_store: PlanStore | None = None,
    ingesta: Ingesta | None = None,
    narrador: Callable[[dict], Any] | None = redactar,
    enviador: Callable[[str], bool] | None = enviar,
    guardar: bool = True,
) -> Resultado:
    """Corre el pipeline completo. No lanza salvo un fallo del motor de calculo."""
    plan_store = plan_store or PlanStore()
    problemas: list[str] = []

    # 1. Ingesta -----------------------------------------------------------
    if ingesta is not None:
        try:
            resumen = ingesta.sincronizar(rango.inicio, rango.fin)
            log.info("ingesta ok: %s", resumen.to_json())
        except StravaError as exc:
            # No es fatal: la base ya tiene lo de corridas anteriores.
            log.error("la ingesta de Strava fallo: %s", exc)
            problemas.append(f"no se pudo sincronizar con Strava ({exc}); se reporta lo ya guardado")
    else:
        log.info("ingesta omitida")

    # 2. Motor de calculo --------------------------------------------------
    datos = report.construir(rango, repo, plan_store)
    if problemas:
        datos["avisos_datos"] = list(datos["avisos_datos"]) + problemas

    # 3. Narrativa ---------------------------------------------------------
    texto_narrativa = None
    if narrador is not None:
        n = narrador(datos)
        if n.ok:
            texto_narrativa = n.texto
            datos["narrativa"] = n.to_json()
            if n.numeros_no_verificados:
                log.warning("cifras sin respaldo en la narrativa: %s", n.numeros_no_verificados)
        else:
            log.warning("sin narrativa: %s", n.error)
            problemas.append(f"resumen automatico no disponible ({n.error})")
            datos["narrativa"] = n.to_json()

    if guardar:
        _guardar_reporte(rango, datos)

    # 4. Formateo y envio --------------------------------------------------
    mensaje = formatear_reporte(datos, texto_narrativa)
    enviado = True
    if enviador is not None:
        enviado = bool(enviador(mensaje))
        if not enviado:
            problemas.append("fallo el envio por Telegram")

    if not enviado:
        estado = ERROR
    elif problemas:
        estado = PARCIAL
    else:
        estado = OK

    return Resultado(
        estado=estado,
        rango=rango,
        mensaje=mensaje,
        datos=datos,
        enviado=enviado,
        problemas=problemas,
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _args(argv: list[str] | None):
    p = argparse.ArgumentParser(description="Reporte semanal de entrenamiento")
    p.add_argument(
        "--semana",
        metavar="YYYY-MM-DD",
        help="cualquier fecha dentro de la semana a reportar (por defecto, la que cerro)",
    )
    p.add_argument("--dry-run", action="store_true", help="imprime el mensaje en vez de enviarlo")
    p.add_argument("--sin-ingesta", action="store_true", help="no consulta Strava")
    p.add_argument("--sin-narrativa", action="store_true", help="no llama a la API de Claude")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    setup("run_weekly")
    a = _args(argv)
    rango = semana_de(date.fromisoformat(a.semana)) if a.semana else semana_a_reportar()
    log.info("corrida para la semana %s", rango)

    repo = Repo()
    corrida = repo.abrir_corrida()
    cliente = None
    try:
        ingesta = None
        if not a.sin_ingesta:
            cliente = StravaClient()
            ingesta = Ingesta(cliente=cliente, repo=repo)

        r = ejecutar(
            rango,
            repo=repo,
            ingesta=ingesta,
            narrador=None if a.sin_narrativa else redactar,
            enviador=None if a.dry_run else enviar,
        )
    except Exception as exc:  # el motor de calculo o algo imprevisto
        log.exception("la corrida fallo")
        repo.cerrar_corrida(corrida, ERROR, f"{type(exc).__name__}: {exc}")
        repo.cerrar()
        return 1
    finally:
        if cliente is not None:
            cliente.cerrar()

    repo.cerrar_corrida(corrida, r.estado, json.dumps(r.problemas, ensure_ascii=False))
    repo.cerrar()

    if a.dry_run:
        print(r.mensaje)
    log.info("corrida terminada: %s %s", r.estado, r.problemas)
    if r.problemas:
        print(f"[{r.estado}] " + "; ".join(r.problemas), file=sys.stderr)
    return r.codigo_salida


if __name__ == "__main__":
    raise SystemExit(main())
