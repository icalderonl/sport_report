"""Escritura atomica de JSON con lock entre procesos.

El bot (systemd, corriendo 24/7) y el cron semanal escriben los mismos archivos:
`plan_actual.json` (el cron marca fuerza detectada en Strava, el bot responde a
/fuerza) y `tokens.json` (refresh_token rotado por Strava). Una escritura a
medias ahi rompe el sistema en silencio, que es exactamente lo que la spec pide
evitar.
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

TIMEOUT_LOCK_S = 10.0
_ESPERA_S = 0.05


class LockOcupado(RuntimeError):
    pass


@contextmanager
def lock(destino: Path, timeout: float = TIMEOUT_LOCK_S) -> Iterator[None]:
    """Lock por archivo centinela. Portable (Windows y Linux) y suficiente aca:
    la contencion real es de dos procesos que escriben pocas veces al dia."""
    candado = destino.with_suffix(destino.suffix + ".lock")
    candado.parent.mkdir(parents=True, exist_ok=True)
    limite = time.monotonic() + timeout
    fd = None
    while True:
        try:
            fd = os.open(candado, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            # Un candado huerfano (proceso muerto) no debe bloquear para siempre.
            try:
                if time.time() - candado.stat().st_mtime > timeout * 3:
                    candado.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= limite:
                raise LockOcupado(f"no se pudo tomar el lock de {destino.name}")
            time.sleep(_ESPERA_S)
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        fd = None
        yield
    finally:
        if fd is not None:
            os.close(fd)
        candado.unlink(missing_ok=True)


def escribir_json(destino: Path, datos: Any, *, con_lock: bool = True) -> None:
    """Escribe JSON de forma atomica: tmp en el mismo directorio + os.replace."""

    def _hacer() -> None:
        destino.parent.mkdir(parents=True, exist_ok=True)
        tmp = destino.with_suffix(destino.suffix + f".tmp{os.getpid()}")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(datos, fh, ensure_ascii=False, indent=2, sort_keys=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, destino)

    if con_lock:
        with lock(destino):
            _hacer()
    else:
        _hacer()


def leer_json(origen: Path) -> Any | None:
    if not origen.exists():
        return None
    with origen.open("r", encoding="utf-8") as fh:
        return json.load(fh)
