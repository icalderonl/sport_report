-- Historico de sesiones reales. Fuente unica para el motor de calculo.
PRAGMA journal_mode = WAL;

-- Una fila por actividad y por fuente. La clave es (fuente, id_externo) y no
-- un entero: los ids de intervals.icu son texto ('i12345678') y no caben en un
-- INTEGER PRIMARY KEY, que en SQLite es un alias de rowid.
CREATE TABLE IF NOT EXISTS sesiones (
    fuente          TEXT    NOT NULL,   -- 'intervals' | 'strava'
    id_externo      TEXT    NOT NULL,   -- id en esa fuente, siempre como texto
    fecha_utc       TEXT    NOT NULL,   -- ISO8601 UTC, inicio de la actividad
    fecha_local     TEXT    NOT NULL,   -- YYYY-MM-DD en TZ_LOCAL (define el dia)
    dia_semana      TEXT    NOT NULL,   -- L M W J V S D
    tipo            TEXT    NOT NULL,   -- vocabulario de la fuente ('Run', ...)
    es_fuerza       INTEGER NOT NULL DEFAULT 0,
    nombre          TEXT,
    distancia_km    REAL,               -- NULL si la actividad no reporta distancia
    duracion_mov_s  INTEGER,
    duracion_tot_s  INTEGER,
    hr_promedio     REAL,
    hr_maximo       REAL,
    cadencia_spm    REAL,               -- pasos/min (Strava reporta rpm de una pierna)
    potencia_w      REAL,               -- NULL si el dispositivo no la reporta
    -- Dinamica avanzada: SOLO la puebla intervals.icu. Con el respaldo de
    -- Strava activo quedan en NULL, que no es lo mismo que cero.
    gct_ms                 REAL,        -- tiempo de contacto con el suelo, ms
    oscilacion_vertical_cm REAL,        -- oscilacion vertical, cm
    ratio_vertical_pct     REAL,        -- oscilacion / largo de paso, %
    decoupling_pct  REAL,               -- NULL si el stream no alcanza el minimo
    carga           REAL,               -- TRIMP por zona; NULL si no hay stream de HR
    carga_impreciso INTEGER NOT NULL DEFAULT 0,  -- 1 si se uso peso de zona fallback
    -- 1 si ya se pidieron los streams y la fuente respondio (aunque no hubiera
    -- HR). Evita re-bajar para siempre los streams de una corrida sin carga.
    streams_procesados INTEGER NOT NULL DEFAULT 0,
    -- 1 si ya se pidieron las vueltas y la fuente respondio, aunque la
    -- actividad no tuviera ninguna. Mismo motivo que streams_procesados.
    vueltas_procesadas INTEGER NOT NULL DEFAULT 0,
    ingerido_en     TEXT    NOT NULL,
    PRIMARY KEY (fuente, id_externo)
);

CREATE INDEX IF NOT EXISTS idx_sesiones_fecha ON sesiones(fecha_local);
CREATE INDEX IF NOT EXISTS idx_sesiones_fuente ON sesiones(fuente, fecha_local);

-- Vueltas (`laps`) de una actividad, tal como las marco el reloj. Son lo que
-- permite separar el trabajo declarado en el plan de la recuperacion trotada
-- entre repeticiones (ver engine/vueltas.py).
--
-- Sin FOREIGN KEY a proposito: `guardar_sesion` usa INSERT OR REPLACE, que
-- borra la fila y la reinserta, y con ON DELETE CASCADE cada reingesta se
-- llevaria las vueltas por delante.
CREATE TABLE IF NOT EXISTS vueltas (
    fuente         TEXT    NOT NULL,
    id_externo     TEXT    NOT NULL,
    indice         INTEGER NOT NULL,   -- 1-based, como lo numera la fuente
    distancia_km   REAL    NOT NULL,
    duracion_mov_s INTEGER NOT NULL,
    PRIMARY KEY (fuente, id_externo, indice)
);

-- Zonas de HR del atleta, cacheadas.
-- origen = 'intervals' | 'strava' | 'fallback' (ver config.ORIGENES_ZONAS_CONFIABLES)
CREATE TABLE IF NOT EXISTS zonas_hr (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    z1_max      INTEGER,
    z2_max      INTEGER,
    z3_max      INTEGER,
    z4_max      INTEGER,
    z5_max      INTEGER,
    origen      TEXT NOT NULL,
    actualizado TEXT NOT NULL
);

-- Bienestar diario. SOLO lo provee intervals.icu: si su API falla, la semana
-- simplemente no tiene filas, que es distinto de tenerlas en cero. Por eso
-- todas las columnas son anulables y el reporte las declara no disponibles.
--
-- `crudo` guarda el registro entero tal como llego: varios nombres de campo de
-- intervals.icu no estan confirmados, y esto permite rellenar una columna
-- despues sin volver a pedirle nada a la API.
CREATE TABLE IF NOT EXISTS bienestar (
    fecha_local  TEXT PRIMARY KEY,   -- YYYY-MM-DD
    hrv          REAL,
    hr_reposo    REAL,
    sueno_h      REAL,
    sueno_score  REAL,
    readiness    REAL,               -- Training Readiness
    body_battery REAL,
    fuente       TEXT NOT NULL,
    crudo        TEXT,               -- JSON del registro tal cual llego
    actualizado  TEXT NOT NULL
);

-- Que fuente sirvio cada semana. La lee el reporte para marcar la semana
-- degradada, incluso cuando la corrida va con --sin-ingesta.
CREATE TABLE IF NOT EXISTS fuentes_semana (
    semana      TEXT PRIMARY KEY,   -- lunes ISO, la clave de RangoSemana
    fuente      TEXT NOT NULL,
    fallback    INTEGER NOT NULL DEFAULT 0,
    detalle     TEXT,
    actualizado TEXT NOT NULL
);

-- Bitacora de corridas del cron semanal.
CREATE TABLE IF NOT EXISTS corridas (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    inicio_utc   TEXT NOT NULL,
    fin_utc      TEXT,
    estado       TEXT NOT NULL,   -- ok | error | parcial | en_curso | interrumpida
    detalle      TEXT
);
