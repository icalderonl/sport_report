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


def proceso_vivo(pid: int) -> bool:
    """Si el proceso existe. Ante la duda devuelve True: quitarle el candado a
    un proceso vivo es peor que esperar de mas."""
    if pid <= 0:
        return False
    if os.name == "nt":
        # OJO: en Windows `os.kill(pid, 0)` no consulta, MATA (se traduce a
        # TerminateProcess). Hay que preguntar por el handle.
        import ctypes

        k32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        ERROR_ACCESS_DENIED = 5
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            k32.CloseHandle(handle)
            return True
        # Acceso denegado = existe pero es de otro usuario. Solo un PID que ya
        # no existe autoriza a reciclar el candado.
        return k32.GetLastError() == ERROR_ACCESS_DENIED
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # existe, pero no podemos señalizarlo
    return True


def _dueno_del_candado(candado: Path) -> int:
    """PID escrito dentro del candado, o 0 si todavia no se escribio."""
    try:
        contenido = candado.read_text(encoding="utf-8").strip()
    except OSError:
        return 0
    return int(contenido) if contenido.isdigit() else 0


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
            # Un candado huerfano (proceso muerto) no debe bloquear para
            # siempre. Se exigen las dos cosas —antiguedad Y dueño muerto—
            # porque solo por tiempo se le quitaba el candado a un proceso vivo
            # pero lento, y ahi se pierde una escritura.
            try:
                antiguo = time.time() - candado.stat().st_mtime > timeout * 3
            except FileNotFoundError:
                continue
            if antiguo and not proceso_vivo(_dueno_del_candado(candado)):
                candado.unlink(missing_ok=True)
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


def _fsync_directorio(directorio: Path) -> None:
    """Baja a disco la entrada de directorio del rename.

    Sin esto el `os.replace` puede quedarse en cache: en un corte de luz —el
    modo de fallo tipico de una Pi con SD— el archivo nuevo desaparece aunque
    su contenido si estuviera sincronizado. Importa sobre todo para
    `tokens.json`. En Windows no se puede abrir un directorio y no hay nada
    que hacer.
    """
    if os.name == "nt":
        return
    try:
        fd = os.open(directorio, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:  # algunos sistemas de archivos no lo soportan
        pass
    finally:
        os.close(fd)


def escribir_json(destino: Path, datos: Any, *, con_lock: bool = True) -> None:
    """Escribe JSON de forma atomica: tmp en el mismo directorio + os.replace."""

    def _hacer() -> None:
        destino.parent.mkdir(parents=True, exist_ok=True)
        tmp = destino.with_suffix(destino.suffix + f".tmp{os.getpid()}")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(datos, fh, ensure_ascii=False, indent=2, sort_keys=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, destino)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        _fsync_directorio(destino.parent)

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
