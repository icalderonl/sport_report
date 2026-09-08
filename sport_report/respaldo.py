"""Respaldo de `data/`: la base, los tokens y los planes.

    python -m sport_report.respaldo [destino]

En `data/` viven tres cosas irreemplazables y todas en la misma SD de la Pi:

  - `tokens.json`, con el refresh_token rotado de Strava. Perderlo obliga a
    rehacer el flujo OAuth con navegador.
  - `sport_report.db`, con todo el historico. Rehacerlo con backfill solo llega
    hasta donde Strava conserve, y las cargas ya calculadas se pierden.
  - `planes/` y `reportes/`, que no se pueden regenerar: el plan de una semana
    vieja y la narrativa que se envio no estan en ninguna otra parte.

La base se copia con la API de backup de SQLite, no con `cp`: el bot puede estar
escribiendo en ese momento y, con WAL activo, copiar el archivo suelto da una
base inconsistente o a medias.

IMPORTANTE: por defecto los respaldos quedan en la misma maquina, lo que sirve
contra un borrado accidental pero NO contra la muerte de la tarjeta, que es el
fallo tipico de una Pi. Apunta `BACKUP_DIR` a otro medio (un pendrive montado,
un NAS, un directorio sincronizado) para que el respaldo valga de verdad.
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from . import config
from .fechas import ahora_local

log = logging.getLogger(__name__)

# Subdirectorios de data/ que se copian enteros.
DIRECTORIOS = ("planes", "reportes")
# Archivos sueltos. El que importa de verdad es tokens.json.
# La carrera objetivo se respalda como el plan: se declaro una vez y no
# esta en ninguna otra parte.
ARCHIVOS = ("tokens.json", "plan_actual.json", "carrera.json")


def _marca_de_tiempo(momento: datetime | None = None) -> str:
    """Nombre de carpeta ordenable alfabeticamente (los ':' no valen en FAT)."""
    return (momento or ahora_local()).strftime("%Y-%m-%dT%H-%M-%S")


def _copiar_base(origen: Path, destino: Path) -> int:
    """Copia la base con la API de backup de SQLite. Devuelve bytes escritos.

    `sqlite3.Connection.backup` toma una instantanea coherente aunque haya otro
    proceso escribiendo, que es justo el caso: el bot corre 24/7.
    """
    con_origen = sqlite3.connect(f"file:{origen}?mode=ro", uri=True)
    try:
        con_destino = sqlite3.connect(destino)
        try:
            con_origen.backup(con_destino)
        finally:
            con_destino.close()
    finally:
        con_origen.close()
    return destino.stat().st_size


def rotar(directorio: Path, conservar: int) -> list[Path]:
    """Borra los respaldos mas viejos. Devuelve los que se borraron."""
    if conservar < 1:
        return []
    existentes = sorted(p for p in directorio.glob("*") if p.is_dir())
    sobran = existentes[:-conservar] if len(existentes) > conservar else []
    for viejo in sobran:
        shutil.rmtree(viejo, ignore_errors=True)
        log.info("respaldo viejo borrado: %s", viejo.name)
    return sobran


def respaldar(
    destino: Path | None = None,
    origen: Path | None = None,
    conservar: int | None = None,
    momento: datetime | None = None,
) -> tuple[Path, dict[str, int]]:
    """Deja una copia de `data/` en `destino`. Devuelve (carpeta, {pieza: bytes}).

    Lo que no existe se omite en silencio: una instalacion nueva todavia no
    tiene `tokens.json` ni planes archivados, y eso no es un error.
    """
    origen = origen or config.DATA_DIR
    destino = destino or config.BACKUP_DIR
    conservar = config.RESPALDOS_CONSERVAR if conservar is None else conservar

    carpeta = destino / _marca_de_tiempo(momento)
    carpeta.mkdir(parents=True, exist_ok=True)
    piezas: dict[str, int] = {}

    base = origen / config.DB_PATH.name
    if base.is_file():
        piezas[base.name] = _copiar_base(base, carpeta / base.name)

    for nombre in ARCHIVOS:
        archivo = origen / nombre
        if archivo.is_file():
            shutil.copy2(archivo, carpeta / nombre)
            piezas[nombre] = archivo.stat().st_size

    for nombre in DIRECTORIOS:
        sub = origen / nombre
        if sub.is_dir():
            shutil.copytree(sub, carpeta / nombre, dirs_exist_ok=True)
            piezas[f"{nombre}/"] = sum(
                p.stat().st_size for p in (carpeta / nombre).rglob("*") if p.is_file()
            )

    rotar(destino, conservar)
    log.info("respaldo en %s (%d piezas, %d bytes)", carpeta, len(piezas), sum(piezas.values()))
    return carpeta, piezas


def ultimo(destino: Path | None = None) -> Path | None:
    """Respaldo mas reciente, o None si no hay ninguno."""
    destino = destino or config.BACKUP_DIR
    if not destino.is_dir():
        return None
    carpetas = sorted(p for p in destino.glob("*") if p.is_dir())
    return carpetas[-1] if carpetas else None


def main(argv: list[str] | None = None) -> int:
    from .logging_setup import setup

    setup("respaldo")
    argv = argv if argv is not None else sys.argv[1:]
    destino = Path(argv[0]).expanduser() if argv else config.BACKUP_DIR

    try:
        carpeta, piezas = respaldar(destino=destino)
    except (OSError, sqlite3.Error) as exc:
        print(f"ERROR: no se pudo respaldar: {exc}", file=sys.stderr)
        return 1

    if not piezas:
        print(f"No habia nada que respaldar en {config.DATA_DIR}")
        return 0

    print(f"Respaldo en {carpeta}")
    for nombre, tam in piezas.items():
        print(f"  {nombre:<20} {tam / 1024:8.1f} KB")
    print(f"\nSe conservan los ultimos {config.RESPALDOS_CONSERVAR} respaldos.")
    if not str(destino).startswith(("/mnt", "/media")) and destino.is_relative_to(config.ROOT):
        print(
            "\nOJO: este respaldo esta en la misma maquina que el original, asi que\n"
            "no protege contra la muerte de la SD. Apunta BACKUP_DIR a otro medio."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
