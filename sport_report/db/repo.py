"""Acceso a SQLite. Unica capa que habla con la base."""
from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .. import config
from .models import SesionReal

log = logging.getLogger(__name__)

RUTA_SCHEMA = Path(__file__).with_name("schema.sql")

_CAMPOS = (
    "strava_id",
    "fecha_utc",
    "fecha_local",
    "dia_semana",
    "tipo_strava",
    "es_fuerza",
    "nombre",
    "distancia_km",
    "duracion_mov_s",
    "duracion_tot_s",
    "hr_promedio",
    "hr_maximo",
    "cadencia_spm",
    "potencia_w",
    "decoupling_pct",
    "carga",
    "carga_impreciso",
    "streams_procesados",
)

# Columnas agregadas despues de la primera version del esquema. `CREATE TABLE
# IF NOT EXISTS` no las agrega a una base que ya existe, asi que hay que
# migrarlas a mano. Cada entrada es (columna, definicion, relleno para las
# filas viejas).
_MIGRACIONES = (
    (
        "streams_procesados",
        "INTEGER NOT NULL DEFAULT 0",
        # Una fila con carga ya paso por el stream: no hay que volver a bajarla.
        # Las que no la tienen se re-piden una vez y quedan marcadas.
        "UPDATE sesiones SET streams_procesados = 1 WHERE carga IS NOT NULL",
    ),
)

# Horas tras las cuales una corrida `en_curso` se da por muerta. El servicio
# semanal tiene TimeoutStartSec=30min, asi que 6 horas no puede pisar una viva.
_HORAS_CORRIDA_MUERTA = 6


def _ahora_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Repo:
    def __init__(self, path: Path | None = None):
        self.path = path or config.DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(self.path, timeout=30)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def _init_schema(self) -> None:
        self.con.executescript(RUTA_SCHEMA.read_text(encoding="utf-8"))
        self._migrar()
        self.con.commit()

    def _migrar(self) -> None:
        """Agrega columnas nuevas a una base que ya existia. Idempotente."""
        existentes = {
            f["name"] for f in self.con.execute("PRAGMA table_info(sesiones)").fetchall()
        }
        for columna, definicion, relleno in _MIGRACIONES:
            if columna in existentes:
                continue
            log.info("migrando la base: agregando sesiones.%s", columna)
            self.con.execute(f"ALTER TABLE sesiones ADD COLUMN {columna} {definicion}")
            if relleno:
                self.con.execute(relleno)

    def cerrar(self) -> None:
        self.con.close()

    def __enter__(self) -> "Repo":
        return self

    def __exit__(self, *exc) -> None:
        self.cerrar()

    # -- sesiones --------------------------------------------------------

    def guardar_sesion(self, s: SesionReal) -> None:
        """Upsert por strava_id: reingerir la misma semana no duplica ni pierde."""
        valores = [getattr(s, c) for c in _CAMPOS]
        valores = [int(v) if isinstance(v, bool) else v for v in valores]
        columnas = ", ".join(_CAMPOS)
        marcas = ", ".join("?" * len(_CAMPOS))
        self.con.execute(
            f"INSERT OR REPLACE INTO sesiones ({columnas}, ingerido_en) "
            f"VALUES ({marcas}, ?)",
            [*valores, _ahora_utc()],
        )
        self.con.commit()

    def guardar_sesiones(self, sesiones: Iterable[SesionReal]) -> int:
        n = 0
        for s in sesiones:
            self.guardar_sesion(s)
            n += 1
        return n

    def _fila_a_sesion(self, f: sqlite3.Row) -> SesionReal:
        d = {c: f[c] for c in _CAMPOS}
        for bandera in ("es_fuerza", "carga_impreciso", "streams_procesados"):
            d[bandera] = bool(d[bandera])
        return SesionReal(**d)

    def sesion(self, strava_id: int) -> SesionReal | None:
        f = self.con.execute(
            "SELECT * FROM sesiones WHERE strava_id = ?", (strava_id,)
        ).fetchone()
        return self._fila_a_sesion(f) if f else None

    def sesiones_entre(self, desde: date, hasta: date) -> list[SesionReal]:
        """Sesiones con fecha_local en [desde, hasta], ambos inclusive."""
        filas = self.con.execute(
            "SELECT * FROM sesiones WHERE fecha_local BETWEEN ? AND ? "
            "ORDER BY fecha_local, fecha_utc",
            (desde.isoformat(), hasta.isoformat()),
        ).fetchall()
        return [self._fila_a_sesion(f) for f in filas]

    def carga_diaria(self, desde: date, hasta: date) -> dict[date, float]:
        """Carga sumada por dia. Solo dias con al menos una sesion con carga.

        Los dias sin carga NO aparecen: quien calcula ACWR decide si eso es un
        cero real (descanso) o un hueco de datos.
        """
        filas = self.con.execute(
            "SELECT fecha_local, SUM(carga) AS total FROM sesiones "
            "WHERE fecha_local BETWEEN ? AND ? AND carga IS NOT NULL "
            "GROUP BY fecha_local",
            (desde.isoformat(), hasta.isoformat()),
        ).fetchall()
        return {date.fromisoformat(f["fecha_local"]): float(f["total"]) for f in filas}

    def dias_con_datos(self, desde: date, hasta: date) -> int:
        f = self.con.execute(
            "SELECT COUNT(DISTINCT fecha_local) AS n FROM sesiones "
            "WHERE fecha_local BETWEEN ? AND ? AND carga IS NOT NULL",
            (desde.isoformat(), hasta.isoformat()),
        ).fetchone()
        return int(f["n"])

    def volumen_semanal(self, hasta: date, semanas: int) -> list[tuple[date, float]]:
        """km de CARRERA por semana lunes-domingo, la ultima la que contiene `hasta`.

        Devuelve siempre `semanas` entradas, con 0.0 donde no hay datos. Esa
        distincion importa: una semana en cero puede ser descanso real o falta
        de historico, y quien dibuja el grafico no puede saberlo — por eso el
        reporte avisa aparte desde que fecha hay datos.

        Filtra por tipo: una salida en bici tambien trae `distancia_km` y sin
        esto entraba al volumen de running.
        """
        lunes_final = hasta - timedelta(days=hasta.weekday())
        inicio = lunes_final - timedelta(weeks=semanas - 1)
        marcas = ", ".join("?" * len(config.TIPOS_RUN))
        filas = self.con.execute(
            "SELECT fecha_local, distancia_km FROM sesiones "
            "WHERE fecha_local BETWEEN ? AND ? AND distancia_km IS NOT NULL "
            f"AND tipo_strava IN ({marcas})",
            (
                inicio.isoformat(),
                (lunes_final + timedelta(days=6)).isoformat(),
                *config.TIPOS_RUN,
            ),
        ).fetchall()

        acum: dict[date, float] = {
            inicio + timedelta(weeks=i): 0.0 for i in range(semanas)
        }
        for f in filas:
            d = date.fromisoformat(f["fecha_local"])
            lunes = d - timedelta(days=d.weekday())
            if lunes in acum:
                acum[lunes] += float(f["distancia_km"])
        return [(k, round(v, 1)) for k, v in sorted(acum.items())]

    def primera_fecha(self) -> date | None:
        f = self.con.execute("SELECT MIN(fecha_local) AS m FROM sesiones").fetchone()
        return date.fromisoformat(f["m"]) if f and f["m"] else None

    # -- zonas -----------------------------------------------------------

    def guardar_zonas(self, zonas: list[dict[str, int]] | None, origen: str) -> None:
        maximos = [z["max"] for z in zonas] if zonas else [None] * 5
        self.con.execute(
            "INSERT OR REPLACE INTO zonas_hr "
            "(id, z1_max, z2_max, z3_max, z4_max, z5_max, origen, actualizado) "
            "VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
            (*maximos[:5], origen, _ahora_utc()),
        )
        self.con.commit()

    def zonas(self) -> tuple[list[dict[str, int]] | None, str | None]:
        """Devuelve (zonas, origen). zonas es None si el origen fue el fallback."""
        f = self.con.execute("SELECT * FROM zonas_hr WHERE id = 1").fetchone()
        if not f or f["origen"] != "strava":
            return None, (f["origen"] if f else None)
        maximos = [f[f"z{i}_max"] for i in range(1, 6)]
        if any(m is None for m in maximos):
            return None, f["origen"]
        zonas: list[dict[str, int]] = []
        anterior = 0
        for m in maximos:
            zonas.append({"min": anterior, "max": int(m)})
            anterior = int(m)
        return zonas, "strava"

    # -- corridas --------------------------------------------------------

    def abrir_corrida(self) -> int:
        self.sanear_corridas()
        cur = self.con.execute(
            "INSERT INTO corridas (inicio_utc, estado) VALUES (?, 'en_curso')", (_ahora_utc(),)
        )
        self.con.commit()
        return int(cur.lastrowid)

    def sanear_corridas(self) -> int:
        """Cierra las corridas que quedaron `en_curso` porque el proceso murio.

        Un corte de luz, un OOM o el TimeoutStartSec de systemd dejan la fila
        abierta para siempre, y el diagnostico no puede distinguirla de un
        error real. Solo se tocan las viejas: una corrida reciente puede estar
        viva de verdad.
        """
        limite = (
            datetime.now(timezone.utc) - timedelta(hours=_HORAS_CORRIDA_MUERTA)
        ).isoformat(timespec="seconds")
        cur = self.con.execute(
            "UPDATE corridas SET estado = 'interrumpida', fin_utc = ?, "
            "detalle = 'el proceso murio sin cerrar la corrida' "
            "WHERE estado = 'en_curso' AND inicio_utc < ?",
            (_ahora_utc(), limite),
        )
        self.con.commit()
        if cur.rowcount:
            log.warning("%d corrida(s) quedaron sin cerrar y se marcaron interrumpidas", cur.rowcount)
        return int(cur.rowcount)

    def cerrar_corrida(self, corrida_id: int, estado: str, detalle: str = "") -> None:
        self.con.execute(
            "UPDATE corridas SET fin_utc = ?, estado = ?, detalle = ? WHERE id = ?",
            (_ahora_utc(), estado, detalle[:4000], corrida_id),
        )
        self.con.commit()

    def ultimas_corridas(self, n: int = 10) -> list[dict[str, Any]]:
        filas = self.con.execute(
            "SELECT * FROM corridas ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        return [dict(f) for f in filas]
