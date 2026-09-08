"""Chequeo de salud del sistema. Lo primero que hay que correr cuando algo falla.

    python -m sport_report.diagnostico

No toca la red ni modifica nada: mira configuracion, archivos y base local.
Nunca imprime el valor de un secreto, solo si esta presente.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta

from . import config
from .db.repo import Repo
from .fechas import ahora_local, semana_a_reportar
from .plan.carrera import CarreraStore
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


def describir_zonas(zonas: list[dict[str, int]]) -> str:
    """Zonas de HR legibles.

    Strava manda `max = -1` en la ultima zona para decir "sin tope". Imprimirlo
    en crudo daba `Z5<=-1`, que parece un dato corrupto. El calculo ya lo trata
    bien (ver `metricas.indice_zona`); esto es solo como se muestra.
    """
    partes = []
    for i, z in enumerate(zonas):
        tope = z.get("max", -1)
        if tope is None or tope <= 0:
            partes.append(f"Z{i + 1}>{z.get('min', 0)}")
        else:
            partes.append(f"Z{i + 1}<={tope}")
    return " ".join(partes)


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

    # `anthropic` y `matplotlib` degradan (reporte sin narrativa / sin imagen);
    # sin httpx o telegram no hay sistema.
    opcionales = ("anthropic", "matplotlib")
    for mod in ("httpx", "telegram", "anthropic", "matplotlib"):
        try:
            __import__(mod)
            linea(OK, f"dependencia {mod}", "instalada")
        except ImportError:
            linea(
                AVISO if mod in opcionales else FALLA,
                f"dependencia {mod}",
                "no instalada"
                + (" (el reporte llega sin grafico)" if mod == "matplotlib" else ""),
            )

    # Un chown mal puesto en el instalador no se nota hasta que el cron intenta
    # escribir, y ahi ya se perdio el reporte de la semana.
    for etiqueta, ruta in (("data/", config.DATA_DIR), ("logs/", config.LOG_DIR)):
        try:
            ruta.mkdir(parents=True, exist_ok=True)
            testigo = ruta / f".escritura-{os.getpid()}"
            testigo.write_text("x", encoding="utf-8")
            testigo.unlink()
            linea(OK, f"escritura en {etiqueta}", str(ruta))
        except OSError as exc:
            linea(FALLA, f"escritura en {etiqueta}", f"{ruta}: {exc}")

    # -- credenciales ----------------------------------------------------
    seccion("CREDENCIALES")
    if not (config.ROOT / ".env").exists():
        linea(FALLA, ".env", f"no existe en {config.ROOT}")
    else:
        linea(OK, ".env", "presente")
    # La clave de la fuente principal es obligatoria; la de Strava, no: sin
    # ella se pierde el respaldo, que es un aviso, no una falla.
    _secreto("INTERVALS_API_KEY", config.INTERVALS_API_KEY)
    _secreto("STRAVA_CLIENT_ID", config.STRAVA_CLIENT_ID, obligatorio=False)
    _secreto("STRAVA_CLIENT_SECRET", config.STRAVA_CLIENT_SECRET, obligatorio=False)
    _secreto("TELEGRAM_BOT_TOKEN", config.TELEGRAM_BOT_TOKEN)
    _secreto("TELEGRAM_CHAT_ID", config.TELEGRAM_CHAT_ID)
    _secreto("ANTHROPIC_API_KEY", config.ANTHROPIC_API_KEY, obligatorio=False)

    marca, detalle = estado_tokens(
        leer_json(config.TOKENS_PATH), config.STRAVA_REFRESH_TOKEN
    )
    # El respaldo casi nunca se ejercita, asi que su credencial puede podrirse
    # sin que nadie lo note hasta el dia que hace falta.
    linea(AVISO if marca == FALLA else marca, "tokens de Strava (respaldo)", detalle)

    # -- plan ------------------------------------------------------------
    seccion("PLAN")
    store = PlanStore()
    vigente = store.cargar()
    if vigente is None and config.PLAN_ACTUAL_PATH.exists():
        # `cargar()` devuelve None tanto si no hay plan como si el archivo esta
        # corrupto. Que el archivo exista y aun asi no se pueda leer es una
        # falla, no un "todavia no cargaste nada".
        linea(
            FALLA,
            "plan vigente",
            f"{config.PLAN_ACTUAL_PATH.name} existe pero no se pudo leer; "
            "el motivo esta en los logs. Vuelve a cargarlo con /setplan",
        )
    elif vigente is None:
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

    carrera = CarreraStore().cargar()
    if carrera is None:
        linea(OK, "carrera objetivo", "ninguna declarada (opcional)")
    else:
        c = carrera.contexto()
        linea(OK, "carrera objetivo", f"{c['fecha']} {c['nombre']}: {c['nota']}")

    objetivo = semana_a_reportar()
    if store.para_semana(objetivo) is None:
        linea(AVISO, "plan de la semana a reportar", f"no hay plan para {objetivo}")
    else:
        linea(OK, "plan de la semana a reportar", str(objetivo))

    # -- fuente de datos -------------------------------------------------
    seccion("FUENTE DE DATOS")
    if config.FUENTE_PRINCIPAL == config.INTERVALS:
        linea(OK, "fuente principal", "intervals.icu (respaldo: Strava)")
    elif config.FUENTE_PRINCIPAL == config.STRAVA:
        linea(
            AVISO,
            "fuente principal",
            "Strava forzada: sin GCT, oscilacion ni ratio vertical, sin bienestar "
            "y SIN respaldo",
        )
    else:
        linea(FALLA, "fuente principal", f"valor desconocido: {config.FUENTE_PRINCIPAL!r}")
    linea(OK, "atleta de intervals.icu", config.INTERVALS_ATHLETE_ID)

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
                linea(AVISO, "historico", "sin sesiones; corre python -m sport_report.backfill")
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
                linea(OK, "zonas de HR", describir_zonas(zonas))
            elif origen is None:
                linea(AVISO, "zonas de HR", "aun no se consultaron a la fuente")
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
                    marca = {
                        "ok": OK,
                        "parcial": AVISO,
                        "en_curso": AVISO,
                        # El proceso murio sin cerrarla; no es un fallo del
                        # calculo pero conviene verlo.
                        "interrumpida": AVISO,
                    }.get(c["estado"], FALLA)
                    linea(marca, f"corrida {c['inicio_utc']}", f"{c['estado']} {c['detalle'] or ''}")

            # De donde salieron los datos de las ultimas semanas. Una racha de
            # semanas por respaldo significa que intervals.icu lleva tiempo
            # fallando y nadie lo vio: son las semanas sin dinamica ni bienestar.
            objetivo_semana = semana_a_reportar()
            desde_semana = (objetivo_semana.inicio - timedelta(weeks=5)).isoformat()
            filas = repo.fuentes_entre(desde_semana, objetivo_semana.clave)
            if not filas:
                linea(AVISO, "fuente por semana", "sin registro todavia")
            for f in filas:
                linea(
                    AVISO if f["fallback"] else OK,
                    f"semana {f['semana']}",
                    f["fuente"] + (" (respaldo)" if f["fallback"] else ""),
                )
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

    # -- respaldo --------------------------------------------------------
    seccion("RESPALDO")
    from .respaldo import ultimo

    reciente = ultimo()
    if reciente is None:
        linea(
            AVISO,
            "ultimo respaldo",
            f"ninguno en {config.BACKUP_DIR}; corre python -m sport_report.respaldo",
        )
    else:
        dias = (datetime.now() - datetime.fromtimestamp(reciente.stat().st_mtime)).days
        # La corrida semanal deja uno cada lunes: mas de 8 dias significa que
        # hace al menos dos semanas que no corre, o que falla al respaldar.
        linea(
            OK if dias <= 8 else AVISO,
            "ultimo respaldo",
            f"{reciente.name} (hace {dias} dia(s))",
        )
        if config.BACKUP_DIR.is_relative_to(config.ROOT):
            linea(
                AVISO,
                "destino del respaldo",
                "esta en la misma maquina que el original: no protege contra la "
                "muerte de la SD. Apunta BACKUP_DIR a otro medio",
            )

    # -- veredicto -------------------------------------------------------
    veredicto = ("todo en orden", "funciona con advertencias", "hay fallas que impiden operar")
    print(f"\n=> {veredicto[_estado['peor']]}")
    return _estado["peor"]


if __name__ == "__main__":
    raise SystemExit(main())
