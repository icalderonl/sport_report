"""Acceso a SQLite. Unica capa que habla con la base."""
from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .. import config
from .models import BienestarDia, SesionReal, Vuelta

log = logging.getLogger(__name__)

RUTA_SCHEMA = Path(__file__).with_name("schema.sql")

_CAMPOS = (
    "fuente",
    "id_externo",
    "fecha_utc",
    "fecha_local",
    "dia_semana",
    "tipo",
    "es_fuerza",
    "nombre",
    "distancia_km",
    "duracion_mov_s",
    "duracion_tot_s",
    "hr_promedio",
    "hr_maximo",
    "cadencia_spm",
    "potencia_w",
    "gct_ms",
    "oscilacion_vertical_cm",
    "ratio_vertical_pct",
    "decoupling_pct",
    "carga",
    "carga_impreciso",
    "streams_procesados",
    "vueltas_procesadas",
)

_BANDERAS = ("es_fuerza", "carga_impreciso", "streams_procesados", "vueltas_procesadas")

_CAMPOS_BIENESTAR = (
    "fecha_local",
    "hrv",
    "hr_reposo",
    "sueno_h",
    "sueno_score",
    "readiness",
    "body_battery",
    "fuente",
    "crudo",
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
    (
        "vueltas_procesadas",
        "INTEGER NOT NULL DEFAULT 0",
        # Sin relleno: ninguna fila vieja tiene vueltas guardadas todavia, asi
        # que todas hay que pedirlas una vez.
        "",
    ),
)

# Reconstruccion de tabla: lo que `_MIGRACIONES` no puede hacer. `strava_id
# INTEGER PRIMARY KEY` es un alias de rowid y no acepta el id textual de
# intervals.icu, asi que la tabla se crea de nuevo y se copia. Cada entrada es
# (columna centinela, tabla, script); si la centinela ya existe, no se hace
# nada, y por eso reabrir la base no vuelve a migrar.
#
# Todo el historico queda con fuente='strava': era la unica fuente que existia.
_SQL_SESIONES_V2 = """
CREATE TABLE sesiones_v2 (
    fuente TEXT NOT NULL, id_externo TEXT NOT NULL,
    fecha_utc TEXT NOT NULL, fecha_local TEXT NOT NULL, dia_semana TEXT NOT NULL,
    tipo TEXT NOT NULL, es_fuerza INTEGER NOT NULL DEFAULT 0, nombre TEXT,
    distancia_km REAL, duracion_mov_s INTEGER, duracion_tot_s INTEGER,
    hr_promedio REAL, hr_maximo REAL, cadencia_spm REAL, potencia_w REAL,
    gct_ms REAL, oscilacion_vertical_cm REAL, ratio_vertical_pct REAL,
    decoupling_pct REAL, carga REAL, carga_impreciso INTEGER NOT NULL DEFAULT 0,
    streams_procesados INTEGER NOT NULL DEFAULT 0,
    vueltas_procesadas INTEGER NOT NULL DEFAULT 0,
    ingerido_en TEXT NOT NULL,
    PRIMARY KEY (fuente, id_externo)
);
INSERT INTO sesiones_v2 (
    fuente, id_externo, fecha_utc, fecha_local, dia_semana, tipo, es_fuerza,
    nombre, distancia_km, duracion_mov_s, duracion_tot_s, hr_promedio,
    hr_maximo, cadencia_spm, potencia_w, decoupling_pct, carga, carga_impreciso,
    streams_procesados, vueltas_procesadas, ingerido_en)
SELECT
    'strava', CAST(strava_id AS TEXT), fecha_utc, fecha_local, dia_semana,
    tipo_strava, es_fuerza, nombre, distancia_km, duracion_mov_s,
    duracion_tot_s, hr_promedio, hr_maximo, cadencia_spm, potencia_w,
    decoupling_pct, carga, carga_impreciso, streams_procesados,
    vueltas_procesadas, ingerido_en
FROM sesiones;
DROP TABLE sesiones;
ALTER TABLE sesiones_v2 RENAME TO sesiones;
"""

_SQL_VUELTAS_V2 = """
CREATE TABLE vueltas_v2 (
    fuente TEXT NOT NULL, id_externo TEXT NOT NULL, indice INTEGER NOT NULL,
    distancia_km REAL NOT NULL, duracion_mov_s INTEGER NOT NULL,
    PRIMARY KEY (fuente, id_externo, indice)
);
INSERT INTO vueltas_v2 (fuente, id_externo, indice, distancia_km, duracion_mov_s)
SELECT 'strava', CAST(strava_id AS TEXT), indice, distancia_km, duracion_mov_s
FROM vueltas;
DROP TABLE vueltas;
ALTER TABLE vueltas_v2 RENAME TO vueltas;
"""

_MIGRACIONES_TABLA = (
    ("id_externo", "sesiones", _SQL_SESIONES_V2),
    ("id_externo", "vueltas", _SQL_VUELTAS_V2),
)

# Con dos fuentes, la misma actividad puede estar dos veces. La regla es: por
# dia local manda una sola fuente, la de mayor rango, y las filas de la otra no
# se suman. Sin esto el volumen y la carga de una semana ingerida por ambas
# fuentes saldrian al doble.
#
# El orden es fijo y no sale de la configuracion a proposito: intervals.icu
# gana porque su registro es el completo (trae GCT, oscilacion y ratio
# vertical). Que alguien cambie FUENTE_PRINCIPAL no debe reinterpretar el
# historico ya guardado.
_ORDEN_FUENTES = "CASE s2.fuente WHEN 'intervals' THEN 0 WHEN 'strava' THEN 1 ELSE 2 END"


def _preferencia(alias: str = "sesiones") -> str:
    """Condicion SQL que deja pasar solo la fuente que manda ese dia."""
    return (
        f"{alias}.fuente = (SELECT s2.fuente FROM sesiones s2 "
        f"WHERE s2.fecha_local = {alias}.fecha_local "
        f"ORDER BY {_ORDEN_FUENTES}, s2.fuente LIMIT 1)"
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
        # El orden importa: la reconstruccion va ANTES del schema porque
        # `CREATE INDEX ... ON sesiones(fuente, ...)` no puede correr sobre una
        # tabla vieja que todavia no tiene esa columna.
        self._migrar_tablas()
        self.con.executescript(RUTA_SCHEMA.read_text(encoding="utf-8"))
        self._migrar()
        self.con.commit()

    def _columnas(self, tabla: str) -> set[str]:
        """Columnas de `tabla`, o vacio si la tabla no existe."""
        return {f["name"] for f in self.con.execute(f"PRAGMA table_info({tabla})").fetchall()}

    def _migrar(self) -> None:
        """Agrega columnas nuevas a una base que ya existia. Idempotente."""
        existentes = self._columnas("sesiones")
        if not existentes:
            return
        for columna, definicion, relleno in _MIGRACIONES:
            if columna in existentes:
                continue
            log.info("migrando la base: agregando sesiones.%s", columna)
            self.con.execute(f"ALTER TABLE sesiones ADD COLUMN {columna} {definicion}")
            if relleno:
                self.con.execute(relleno)

    def _migrar_tablas(self) -> None:
        """Reconstruye las tablas cuya clave primaria cambio. Idempotente.

        Es la unica operacion destructiva del esquema, asi que va en una sola
        transaccion: o queda la tabla nueva completa, o no se toca nada. El
        runbook exige un respaldo antes de desplegar esto.
        """
        pendientes = [
            (tabla, script)
            for centinela, tabla, script in _MIGRACIONES_TABLA
            if (cols := self._columnas(tabla)) and centinela not in cols
        ]
        if not pendientes:
            return
        # Las columnas que `_MIGRACIONES` agrega tienen que existir antes de
        # copiar: una base anterior a ellas no las trae y el SELECT fallaria.
        self._migrar()
        for tabla, script in pendientes:
            log.warning("migrando la base: reconstruyendo %s con (fuente, id_externo)", tabla)
            self.con.executescript(f"BEGIN;\n{script}\nCOMMIT;")

    def cerrar(self) -> None:
        self.con.close()

    def __enter__(self) -> "Repo":
        return self

    def __exit__(self, *exc) -> None:
        self.cerrar()

    # -- sesiones --------------------------------------------------------

    def guardar_sesion(self, s: SesionReal) -> None:
        """Upsert por (fuente, id_externo): reingerir no duplica ni pierde."""
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
        d["id_externo"] = str(d["id_externo"])
        for bandera in _BANDERAS:
            d[bandera] = bool(d[bandera])
        return SesionReal(**d)

    def sesion(self, fuente: str, id_externo: Any) -> SesionReal | None:
        f = self.con.execute(
            "SELECT * FROM sesiones WHERE fuente = ? AND id_externo = ?",
            (fuente, str(id_externo)),
        ).fetchone()
        return self._fila_a_sesion(f) if f else None

    def sesiones_entre(self, desde: date, hasta: date) -> list[SesionReal]:
        """Sesiones con fecha_local en [desde, hasta], ambos inclusive.

        Solo las de la fuente que manda cada dia (ver `_preferencia`).
        """
        filas = self.con.execute(
            "SELECT * FROM sesiones WHERE fecha_local BETWEEN ? AND ? "
            f"AND {_preferencia()} "
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
            f"AND {_preferencia()} "
            "GROUP BY fecha_local",
            (desde.isoformat(), hasta.isoformat()),
        ).fetchall()
        return {date.fromisoformat(f["fecha_local"]): float(f["total"]) for f in filas}

    def dias_con_datos(self, desde: date, hasta: date) -> int:
        f = self.con.execute(
            "SELECT COUNT(DISTINCT fecha_local) AS n FROM sesiones "
            "WHERE fecha_local BETWEEN ? AND ? AND carga IS NOT NULL "
            f"AND {_preferencia()}",
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
            f"AND tipo IN ({marcas}) AND {_preferencia()}",
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

    # -- vueltas ---------------------------------------------------------

    def guardar_vueltas(
        self, fuente: str, id_externo: Any, vueltas: Iterable[Vuelta]
    ) -> int:
        """Reemplaza las vueltas de una actividad. Idempotente.

        Se borran primero las que hubiera: si la fuente devuelve menos vueltas
        que antes (el atleta edito la actividad) dejar las viejas mezclaria dos
        versiones de la misma sesion.
        """
        id_externo = str(id_externo)
        filas = [
            (v.fuente, v.id_externo, v.indice, v.distancia_km, v.duracion_mov_s)
            for v in vueltas
        ]
        self.con.execute(
            "DELETE FROM vueltas WHERE fuente = ? AND id_externo = ?",
            (fuente, id_externo),
        )
        self.con.executemany(
            "INSERT INTO vueltas (fuente, id_externo, indice, distancia_km, duracion_mov_s) "
            "VALUES (?, ?, ?, ?, ?)",
            filas,
        )
        self.con.commit()
        return len(filas)

    def marcar_vueltas_procesadas(
        self, fuente: str, id_externo: Any, valor: bool = True
    ) -> None:
        """Marca la bandera sin volver a escribir la sesion entera.

        Cuando a una fila ya ingerida solo le faltaban las vueltas no hay que
        re-normalizarla: hacerlo exigiria volver a bajar los streams para no
        perder la carga, que es justo la llamada cara que se quiere evitar.
        """
        self.con.execute(
            "UPDATE sesiones SET vueltas_procesadas = ? "
            "WHERE fuente = ? AND id_externo = ?",
            (int(valor), fuente, str(id_externo)),
        )
        self.con.commit()

    def _fila_a_vuelta(self, f: sqlite3.Row) -> Vuelta:
        return Vuelta(
            fuente=f["fuente"],
            id_externo=str(f["id_externo"]),
            indice=int(f["indice"]),
            distancia_km=float(f["distancia_km"]),
            duracion_mov_s=int(f["duracion_mov_s"]),
        )

    def vueltas(self, fuente: str, id_externo: Any) -> list[Vuelta]:
        filas = self.con.execute(
            "SELECT * FROM vueltas WHERE fuente = ? AND id_externo = ? ORDER BY indice",
            (fuente, str(id_externo)),
        ).fetchall()
        return [self._fila_a_vuelta(f) for f in filas]

    def vueltas_entre(
        self, desde: date, hasta: date
    ) -> dict[tuple[str, str], list[Vuelta]]:
        """Vueltas de todas las sesiones de la ventana, agrupadas por actividad.

        La clave es `SesionReal.clave`, o sea (fuente, id_externo). Una sola
        consulta: el motor de adherencia las necesita todas de golpe y
        preguntar sesion por sesion son N viajes a la base por reporte.
        """
        filas = self.con.execute(
            "SELECT v.* FROM vueltas v JOIN sesiones s "
            "ON s.fuente = v.fuente AND s.id_externo = v.id_externo "
            "WHERE s.fecha_local BETWEEN ? AND ? "
            f"AND {_preferencia('s')} "
            "ORDER BY v.fuente, v.id_externo, v.indice",
            (desde.isoformat(), hasta.isoformat()),
        ).fetchall()
        salida: dict[tuple[str, str], list[Vuelta]] = {}
        for f in filas:
            v = self._fila_a_vuelta(f)
            salida.setdefault(v.clave, []).append(v)
        return salida

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
        if not f or f["origen"] not in config.ORIGENES_ZONAS_CONFIABLES:
            return None, (f["origen"] if f else None)
        maximos = [f[f"z{i}_max"] for i in range(1, 6)]
        if any(m is None for m in maximos):
            return None, f["origen"]
        zonas: list[dict[str, int]] = []
        anterior = 0
        for m in maximos:
            zonas.append({"min": anterior, "max": int(m)})
            anterior = int(m)
        return zonas, f["origen"]

    # -- bienestar -------------------------------------------------------

    def guardar_bienestar(self, dias: Iterable[BienestarDia]) -> int:
        """Upsert por fecha local. Solo intervals.icu llena esta tabla."""
        filas = [
            [getattr(d, c) for c in _CAMPOS_BIENESTAR] + [_ahora_utc()] for d in dias
        ]
        if not filas:
            return 0
        columnas = ", ".join(_CAMPOS_BIENESTAR)
        marcas = ", ".join("?" * len(_CAMPOS_BIENESTAR))
        self.con.executemany(
            f"INSERT OR REPLACE INTO bienestar ({columnas}, actualizado) "
            f"VALUES ({marcas}, ?)",
            filas,
        )
        self.con.commit()
        return len(filas)

    def bienestar_entre(self, desde: date, hasta: date) -> list[BienestarDia]:
        filas = self.con.execute(
            "SELECT * FROM bienestar WHERE fecha_local BETWEEN ? AND ? "
            "ORDER BY fecha_local",
            (desde.isoformat(), hasta.isoformat()),
        ).fetchall()
        return [
            BienestarDia(**{c: f[c] for c in _CAMPOS_BIENESTAR}) for f in filas
        ]

    # -- fuente por semana -----------------------------------------------

    def guardar_fuente_semana(
        self, semana: str, fuente: str, fallback: bool = False, detalle: str = ""
    ) -> None:
        self.con.execute(
            "INSERT OR REPLACE INTO fuentes_semana "
            "(semana, fuente, fallback, detalle, actualizado) VALUES (?, ?, ?, ?, ?)",
            (semana, fuente, int(fallback), detalle[:4000], _ahora_utc()),
        )
        self.con.commit()

    def fuente_semana(self, semana: str) -> dict[str, Any] | None:
        f = self.con.execute(
            "SELECT * FROM fuentes_semana WHERE semana = ?", (semana,)
        ).fetchone()
        if not f:
            return None
        d = dict(f)
        d["fallback"] = bool(d["fallback"])
        return d

    def fuentes_entre(self, desde: str, hasta: str) -> list[dict[str, Any]]:
        """Filas de `fuentes_semana` con clave (lunes ISO) en [desde, hasta]."""
        filas = self.con.execute(
            "SELECT * FROM fuentes_semana WHERE semana BETWEEN ? AND ? ORDER BY semana",
            (desde, hasta),
        ).fetchall()
        salida = []
        for f in filas:
            d = dict(f)
            d["fallback"] = bool(d["fallback"])
            salida.append(d)
        return salida

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
