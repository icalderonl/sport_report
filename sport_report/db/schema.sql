-- Historico de sesiones reales. Fuente unica para el motor de calculo.
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS sesiones (
    strava_id       INTEGER PRIMARY KEY,
    fecha_utc       TEXT    NOT NULL,   -- ISO8601 UTC, inicio de la actividad
    fecha_local     TEXT    NOT NULL,   -- YYYY-MM-DD en TZ_LOCAL (define el dia)
    dia_semana      TEXT    NOT NULL,   -- L M W J V S D
    tipo_strava     TEXT    NOT NULL,
    es_fuerza       INTEGER NOT NULL DEFAULT 0,
    nombre          TEXT,
    distancia_km    REAL,               -- NULL si la actividad no reporta distancia
    duracion_mov_s  INTEGER,
    duracion_tot_s  INTEGER,
    hr_promedio     REAL,
    hr_maximo       REAL,
    cadencia_spm    REAL,               -- pasos/min (Strava reporta rpm de una pierna)
    potencia_w      REAL,               -- NULL si el dispositivo no la reporta
    decoupling_pct  REAL,               -- NULL si el stream no alcanza el minimo
    carga           REAL,               -- TRIMP por zona; NULL si no hay stream de HR
    carga_impreciso INTEGER NOT NULL DEFAULT 0,  -- 1 si se uso peso de zona fallback
    -- 1 si ya se pidieron los streams y Strava respondio (aunque no hubiera
    -- HR). Evita re-bajar para siempre los streams de una corrida sin carga.
    streams_procesados INTEGER NOT NULL DEFAULT 0,
    ingerido_en     TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sesiones_fecha ON sesiones(fecha_local);

-- Zonas de HR del atleta, cacheadas. origen = 'strava' | 'fallback'
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

-- Bitacora de corridas del cron semanal.
CREATE TABLE IF NOT EXISTS corridas (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    inicio_utc   TEXT NOT NULL,
    fin_utc      TEXT,
    estado       TEXT NOT NULL,   -- ok | error | parcial | en_curso | interrumpida
    detalle      TEXT
);
