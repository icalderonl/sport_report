"""Chequeo de salud del sistema. Lo primero que hay que correr cuando algo falla.

    python -m sport_report.diagnostico

No toca la red ni modifica nada: mira configuracion, archivos y base local.
Nunca imprime el valor de un secreto, solo si esta presente.
"""
from __future__ import annotations

import sys
import time
from datetime import timedelta
from pathlib import Path

from . import config
from .db.repo import Repo
from .fechas import ahora_local, semana_a_reportar
from .plan.store import PlanStore
from .storage import leer_json

OK, AVISO, FALLA = "OK  ", "AVISO", "FALLA"
_estado = {"peor": 0}
_RANGO = {OK: 0, AVISO: 1, FALLA: 2}


def linea(marca: str, titulo: str, detalle: str = "") -> None:
    _estado["peor"] = max(_estado["peor"], _RANGO[marca])
    print(f"  [{marca}] {titulo}" + (f"  {detalle}" if detalle else ""))


def seccion(nombre: str) -> None:
    print(f"\n{nombre}")


def _secreto(nombre: str, valor: str, obligatorio: bool = True) -> None:
    if valor:
        linea(OK, nombre, f"presente ({len(valor)} caracteres)")
    else:
        linea(FALLA if obligatorio else AVISO, nombre, "sin configurar")


def estado_tokens(
    tokens: dict | None, refresh_env: str = "", ahora: float | None = None
) -> tuple[str, str]:
    """(marca, detalle) del estado de los tokens de Strava.

    Funcion aparte para poder testearla: la version anterior miraba solo el
    archivo y daba FALLA cuando el sistema en realidad podia arrancar con el
    STRAVA_REFRESH_TOKEN sembrado en .env.
    """
    if not tokens:
        if refresh_env:
            return AVISO, (
                f"todavia no existe {config.TOKENS_PATH.name}, pero hay "
                "STRAVA_REFRESH_TOKEN en .env: la primera corrida lo usa y crea el "
                "archivo. Desde ahi manda el archivo y .env deja de leerse"
            )
        return FALLA, (
            f"falta {config.TOKENS_PATH.name} y no hay STRAVA_REFRESH_TOKEN en .env; "
            "corre python -m sport_report.strava.autorizar"
        )

    if not tokens.get("refresh_token"):
        return FALLA, "sin refresh_token: hay que re-autorizar"

    restante = int(tokens.get("expires_at", 0)) - (ahora if ahora is not None else time.time())
    if restante > 0:
        return OK, f"access_token vigente {int(restante / 60)} min mas"
    return OK, "access_token vencido (se refresca solo en la corrida)"


def main() -> int:
    print(f"Diagnostico de sport_report  -  {ahora_local().isoformat(timespec='seconds')}")
    print(f"Raiz: {config.ROOT}")

    # -- entorno ---------------------------------------------------------
    seccion("ENTORNO")
    v = sys.version_info
    if v >= (3, 11):
        linea(OK, "Python", f"{v.major}.{v.minor}.{v.micro}")
    elif v >= (3, 10):
        linea(AVISO, "Python", f"{v.major}.{v.minor}: el proyecto declara 3.11+")
    else:
        linea(FALLA, "Python", f"{v.major}.{v.minor}: el SDK anthropic necesita 3.10+")
    linea(OK, "Zona horaria", str(config.TZ))
    linea(OK, "Semana a reportar", str(semana_a_reportar()))

    for nombre, mod in (("httpx", "httpx"), ("telegram", "telegram"), ("anthropic", "anthropic")):
        try:
            __import__(mod)
            linea(OK, f"dependencia {nombre}", "instalada")
        except ImportError:
            linea(AVISO if mod == "anthropic" else FALLA, f"dependencia {nombre}", "no instalada")

    # -- credenciales ----------------------------------------------------
    seccion("CREDENCIALES")
    if not (config.ROOT / ".env").exists():
        linea(FALLA, ".env", f"no existe en {config.ROOT}")
    else:
        linea(OK, ".env", "presente")
    _secreto("STRAVA_CLIENT_ID", config.STRAVA_CLIENT_ID)
    _secreto("STRAVA_CLIENT_SECRET", config.STRAVA_CLIENT_SECRET)
    _secreto("TELEGRAM_BOT_TOKEN", config.TELEGRAM_BOT_TOKEN)
    _secreto("TELEGRAM_CHAT_ID", config.TELEGRAM_CHAT_ID)
    _secreto("ANTHROPIC_API_KEY", config.ANTHROPIC_API_KEY, obligatorio=False)

    marca, detalle = estado_tokens(
        leer_json(config.TOKENS_PATH), config.STRAVA_REFRESH_TOKEN
    )
    linea(marca, "tokens de Strava", detalle)

    # -- plan ------------------------------------------------------------
    seccion("PLAN")
    store = PlanStore()
    vigente = store.cargar()
    if vigente is None:
        linea(AVISO, "plan vigente", "no hay plan cargado; usa /setplan en Telegram")
    else:
        p = vigente.plan
        linea(
            OK,
            "plan vigente",
            f"semana {p.semana} para {vigente.rango} ({p.volumen_planificado_km():g} km)",
        )
        pendientes = [d for d in p.dias_fuerza() if not p.fuerza_completada.get(d)]
        if pendientes:
            linea(AVISO, "fuerza pendiente", ", ".join(pendientes))

    objetivo = semana_a_reportar()
    if store.para_semana(objetivo) is None:
        linea(AVISO, "plan de la semana a reportar", f"no hay plan para {objetivo}")
    else:
        linea(OK, "plan de la semana a reportar", str(objetivo))

    # -- base de datos ---------------------------------------------------
    seccion("BASE DE DATOS")
    if not config.DB_PATH.exists():
        linea(AVISO, "SQLite", "todavia no existe; se crea en la primera corrida")
    else:
        repo = Repo()
        try:
            hoy = ahora_local().date()
            primera = repo.primera_fecha()
            tam = config.DB_PATH.stat().st_size / 1024
            linea(OK, "SQLite", f"{config.DB_PATH.name}, {tam:.0f} KB")
            if primera is None:
                linea(AVISO, "historico", "sin sesiones; corre python -m sport_report.strava.backfill")
            else:
                dias = (hoy - primera).days
                sesiones = len(repo.sesiones_entre(primera, hoy))
                linea(OK, "historico", f"{sesiones} sesiones desde {primera} ({dias} dias)")
                con_carga = repo.dias_con_datos(hoy - timedelta(days=27), hoy)
                marca = OK if dias >= 28 and con_carga >= config.UMBRALES.acwr_sesiones_minimas else AVISO
                linea(
                    marca,
                    "ventana de ACWR",
                    f"{con_carga} dias con carga en los ultimos 28 "
                    f"(se necesitan {config.UMBRALES.acwr_sesiones_minimas})",
                )
            zonas, origen = repo.zonas()
            if zonas:
                linea(OK, "zonas de HR", " ".join(f"Z{i+1}<={z['max']}" for i, z in enumerate(zonas)))
            elif origen is None:
                linea(AVISO, "zonas de HR", "aun no se consultaron a Strava")
            else:
                linea(
                    AVISO,
                    "zonas de HR",
                    f"origen '{origen}': la carga se calcula con peso fijo y es imprecisa",
                )

            corridas = repo.ultimas_corridas(5)
            if not corridas:
                linea(AVISO, "corridas", "ninguna registrada todavia")
            else:
                for c in corridas:
                    marca = {"ok": OK, "parcial": AVISO}.get(c["estado"], FALLA)
                    linea(marca, f"corrida {c['inicio_utc']}", f"{c['estado']} {c['detalle'] or ''}")
        finally:
            repo.cerrar()

    # -- archivos --------------------------------------------------------
    seccion("ARCHIVOS")
    reportes = sorted((config.DATA_DIR / "reportes").glob("*.json")) if (
        config.DATA_DIR / "reportes"
    ).exists() else []
    if reportes:
        linea(OK, "ultimo reporte", reportes[-1].name)
    else:
        linea(AVISO, "reportes", "ninguno generado todavia")

    for log in sorted(config.LOG_DIR.glob("*.log")) if config.LOG_DIR.exists() else []:
        linea(OK, f"log {log.name}", f"{log.stat().st_size / 1024:.0f} KB")

    huerfanos = list(config.DATA_DIR.glob("*.lock")) if config.DATA_DIR.exists() else []
    if huerfanos:
        linea(AVISO, "locks huerfanos", ", ".join(p.name for p in huerfanos))

    # -- veredicto -------------------------------------------------------
    veredicto = ("todo en orden", "funciona con advertencias", "hay fallas que impiden operar")
    print(f"\n=> {veredicto[_estado['peor']]}")
    return _estado["peor"]


if __name__ == "__main__":
    raise SystemExit(main())
