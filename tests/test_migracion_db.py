"""La reconstruccion de `sesiones` y `vueltas` con clave (fuente, id_externo).

Es la unica operacion destructiva del esquema: copia el historico a una tabla
nueva y borra la vieja. Si pierde una fila, un valor o una bandera, se pierde
el historico real de la Pi, que no se puede volver a bajar (Strava no regala
cuota infinita y las semanas viejas ya no traen dinamica). De ahi el detalle de
estos tests.
"""
from __future__ import annotations

import sqlite3

from sport_report.db.models import SesionReal, Vuelta
from sport_report.db.repo import Repo

# Esquema tal como quedo en produccion antes de fase 2: strava_id como
# INTEGER PRIMARY KEY (alias de rowid, que no acepta el id textual de
# intervals.icu) y tipo_strava en vez de tipo.
DDL_V1 = """
CREATE TABLE sesiones (
    strava_id INTEGER PRIMARY KEY, fecha_utc TEXT NOT NULL,
    fecha_local TEXT NOT NULL, dia_semana TEXT NOT NULL,
    tipo_strava TEXT NOT NULL, es_fuerza INTEGER NOT NULL DEFAULT 0,
    nombre TEXT, distancia_km REAL, duracion_mov_s INTEGER,
    duracion_tot_s INTEGER, hr_promedio REAL, hr_maximo REAL,
    cadencia_spm REAL, potencia_w REAL, decoupling_pct REAL, carga REAL,
    carga_impreciso INTEGER NOT NULL DEFAULT 0,
    streams_procesados INTEGER NOT NULL DEFAULT 0,
    vueltas_procesadas INTEGER NOT NULL DEFAULT 0,
    ingerido_en TEXT NOT NULL);
CREATE TABLE vueltas (
    strava_id INTEGER NOT NULL, indice INTEGER NOT NULL,
    distancia_km REAL NOT NULL, duracion_mov_s INTEGER NOT NULL,
    PRIMARY KEY (strava_id, indice));
CREATE TABLE zonas_hr (
    id INTEGER PRIMARY KEY CHECK (id = 1), z1_max INTEGER, z2_max INTEGER,
    z3_max INTEGER, z4_max INTEGER, z5_max INTEGER,
    origen TEXT NOT NULL, actualizado TEXT NOT NULL);
CREATE TABLE corridas (
    id INTEGER PRIMARY KEY AUTOINCREMENT, inicio_utc TEXT NOT NULL,
    fin_utc TEXT, estado TEXT NOT NULL, detalle TEXT);

INSERT INTO sesiones (strava_id, fecha_utc, fecha_local, dia_semana,
    tipo_strava, es_fuerza, nombre, distancia_km, duracion_mov_s,
    duracion_tot_s, hr_promedio, hr_maximo, cadencia_spm, potencia_w,
    decoupling_pct, carga, carga_impreciso, streams_procesados,
    vueltas_procesadas, ingerido_en)
VALUES (1234, '2026-08-31T12:00:00+00:00', '2026-08-31', 'L', 'Run', 0,
    'Series de martes', 10.0, 3000, 3200, 152.0, 178.0, 176.0, 290.0,
    4.2, 420.5, 1, 1, 1, '2026-09-01T07:00:00+00:00');
INSERT INTO sesiones (strava_id, fecha_utc, fecha_local, dia_semana,
    tipo_strava, es_fuerza, carga, ingerido_en)
VALUES (5678, '2026-09-02T12:00:00+00:00', '2026-09-02', 'W',
    'WeightTraining', 1, NULL, '2026-09-03T07:00:00+00:00');
INSERT INTO vueltas (strava_id, indice, distancia_km, duracion_mov_s)
VALUES (1234, 1, 2.0, 800), (1234, 2, 1.0, 240);
INSERT INTO zonas_hr VALUES (1, 120, 140, 155, 170, 200, 'strava', 'x');
"""


def _base_v1(tmp_path, ddl: str = DDL_V1):
    ruta = tmp_path / "vieja.db"
    con = sqlite3.connect(ruta)
    con.executescript(ddl)
    con.commit()
    con.close()
    return ruta


def test_el_historico_sobrevive_intacto(tmp_path):
    ruta = _base_v1(tmp_path)
    with Repo(ruta) as r:
        s = r.sesion("strava", "1234")
        assert s is not None, "la sesion se perdio en la migracion"
        # Toda la fila, campo por campo: un typo en el INSERT ... SELECT
        # desplaza una columna y el dato queda mal en silencio.
        assert (s.fuente, s.id_externo, s.tipo) == ("strava", "1234", "Run")
        assert s.fecha_utc == "2026-08-31T12:00:00+00:00"
        assert (s.fecha_local, s.dia_semana) == ("2026-08-31", "L")
        assert (s.nombre, s.es_fuerza) == ("Series de martes", False)
        assert (s.distancia_km, s.duracion_mov_s, s.duracion_tot_s) == (10.0, 3000, 3200)
        assert (s.hr_promedio, s.hr_maximo) == (152.0, 178.0)
        assert (s.cadencia_spm, s.potencia_w) == (176.0, 290.0)
        assert (s.decoupling_pct, s.carga, s.carga_impreciso) == (4.2, 420.5, True)
        assert (s.streams_procesados, s.vueltas_procesadas) == (True, True)
        # La fuerza tambien, con su carga en NULL (nunca convertida a 0.0).
        f = r.sesion("strava", "5678")
        assert (f.tipo, f.es_fuerza, f.carga) == ("WeightTraining", True, None)


def test_lo_que_solo_da_intervals_queda_en_null(tmp_path):
    """El historico es de Strava, que no expone dinamica avanzada.

    Rellenar con 0.0 diria "no oscilo verticalmente", que es falso.
    """
    with Repo(_base_v1(tmp_path)) as r:
        s = r.sesion("strava", "1234")
        assert s.gct_ms is None
        assert s.oscilacion_vertical_cm is None
        assert s.ratio_vertical_pct is None
        assert r.bienestar_entre(s.fecha, s.fecha) == []


def test_las_vueltas_sobreviven_y_quedan_indexadas_por_clave(tmp_path):
    with Repo(_base_v1(tmp_path)) as r:
        vs = r.vueltas("strava", "1234")
        assert [(v.indice, v.distancia_km, v.duracion_mov_s) for v in vs] == [
            (1, 2.0, 800),
            (2, 1.0, 240),
        ]
        assert all(v.clave == ("strava", "1234") for v in vs)
        # Y el mapa que consume el motor de adherencia queda indexado por
        # (fuente, id_externo), no por un entero.
        fecha = r.sesion("strava", "1234").fecha
        mapa = r.vueltas_entre(fecha, fecha)
        assert list(mapa) == [("strava", "1234")]
        assert len(mapa[("strava", "1234")]) == 2


def test_reabrir_no_vuelve_a_migrar_ni_duplica(tmp_path):
    ruta = _base_v1(tmp_path)
    with Repo(ruta) as r:
        antes = r.con.execute("SELECT COUNT(*) FROM sesiones").fetchone()[0]
    for _ in range(3):
        with Repo(ruta) as r:
            assert r.con.execute("SELECT COUNT(*) FROM sesiones").fetchone()[0] == antes
            assert r.sesion("strava", "1234") is not None
    # La tabla intermedia no queda de recuerdo.
    with Repo(ruta) as r:
        tablas = {
            f[0]
            for f in r.con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert "sesiones_v2" not in tablas and "vueltas_v2" not in tablas
    assert {"bienestar", "fuentes_semana"} <= tablas


def test_una_base_v1_sin_las_columnas_de_banderas_tambien_migra(tmp_path):
    """Hay instalaciones anteriores a streams_procesados/vueltas_procesadas.

    Primero hay que agregar esas columnas y solo despues copiar: si no, el
    INSERT ... SELECT de la reconstruccion falla por columna inexistente.
    """
    ddl = """
    CREATE TABLE sesiones (
        strava_id INTEGER PRIMARY KEY, fecha_utc TEXT NOT NULL,
        fecha_local TEXT NOT NULL, dia_semana TEXT NOT NULL,
        tipo_strava TEXT NOT NULL, es_fuerza INTEGER NOT NULL DEFAULT 0,
        nombre TEXT, distancia_km REAL, duracion_mov_s INTEGER,
        duracion_tot_s INTEGER, hr_promedio REAL, hr_maximo REAL,
        cadencia_spm REAL, potencia_w REAL, decoupling_pct REAL, carga REAL,
        carga_impreciso INTEGER NOT NULL DEFAULT 0, ingerido_en TEXT NOT NULL);
    INSERT INTO sesiones (strava_id, fecha_utc, fecha_local, dia_semana,
        tipo_strava, carga, ingerido_en)
        VALUES (1,'2026-08-31T12:00:00+00:00','2026-08-31','L','Run',420.0,'x');
    INSERT INTO sesiones (strava_id, fecha_utc, fecha_local, dia_semana,
        tipo_strava, carga, ingerido_en)
        VALUES (2,'2026-09-01T12:00:00+00:00','2026-09-01','M','Run',NULL,'x');
    """
    ruta = _base_v1(tmp_path, ddl)
    with Repo(ruta) as r:
        # El relleno de la migracion de columnas se conserva a traves de la
        # reconstruccion: la que ya tenia carga no se re-baja, la otra si.
        assert r.sesion("strava", "1").streams_procesados is True
        assert r.sesion("strava", "2").streams_procesados is False
        assert r.sesion("strava", "1").vueltas_procesadas is False


def test_una_base_v1_sin_tabla_de_vueltas_migra_igual(tmp_path):
    """Forma real de la base de desarrollo: `sesiones` vieja y `vueltas` inexistente.

    La reconstruccion tiene que saltarse la tabla que no esta (y dejar que el
    schema la cree en v2) en vez de fallar al copiarla.
    """
    ddl = """
    CREATE TABLE sesiones (
        strava_id INTEGER PRIMARY KEY, fecha_utc TEXT NOT NULL,
        fecha_local TEXT NOT NULL, dia_semana TEXT NOT NULL,
        tipo_strava TEXT NOT NULL, es_fuerza INTEGER NOT NULL DEFAULT 0,
        nombre TEXT, distancia_km REAL, duracion_mov_s INTEGER,
        duracion_tot_s INTEGER, hr_promedio REAL, hr_maximo REAL,
        cadencia_spm REAL, potencia_w REAL, decoupling_pct REAL, carga REAL,
        carga_impreciso INTEGER NOT NULL DEFAULT 0, ingerido_en TEXT NOT NULL,
        streams_procesados INTEGER NOT NULL DEFAULT 0);
    INSERT INTO sesiones (strava_id, fecha_utc, fecha_local, dia_semana,
        tipo_strava, nombre, distancia_km, duracion_mov_s, carga,
        streams_procesados, ingerido_en)
        VALUES (15881234567,'2026-08-31T12:00:00+00:00','2026-08-31','L','Run',
        'Largo', 21.1, 7200, 980.5, 1, 'x');
    """
    ruta = _base_v1(tmp_path, ddl)
    with Repo(ruta) as r:
        # El id de Strava no cabe en un int de 32 bits; como texto da igual.
        s = r.sesion("strava", "15881234567")
        assert (s.distancia_km, s.carga, s.streams_procesados) == (21.1, 980.5, True)
        assert s.vueltas_procesadas is False
        assert r.con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        # La tabla de vueltas la creo el schema, ya en v2.
        assert {"fuente", "id_externo"} <= r._columnas("vueltas")
        assert r.vueltas("strava", "15881234567") == []


def test_una_base_nueva_nace_en_v2_sin_pasar_por_la_reconstruccion(tmp_path):
    with Repo(tmp_path / "nueva.db") as r:
        cols = r._columnas("sesiones")
        assert {"fuente", "id_externo", "tipo", "gct_ms", "ratio_vertical_pct"} <= cols
        assert "strava_id" not in cols and "tipo_strava" not in cols
        # Y acepta un id textual, que es el motivo de todo el cambio.
        r.guardar_sesion(
            SesionReal(
                fuente="intervals",
                id_externo="i98765432",
                fecha_utc="2026-09-07T12:00:00+00:00",
                fecha_local="2026-09-07",
                dia_semana="L",
                tipo="Run",
                es_fuerza=False,
                gct_ms=232.0,
            )
        )
        s = r.sesion("intervals", "i98765432")
        assert (s.id_externo, s.gct_ms) == ("i98765432", 232.0)
        r.guardar_vueltas("intervals", "i98765432", [Vuelta("intervals", "i98765432", 1, 2.0, 800)])
        assert len(r.vueltas("intervals", "i98765432")) == 1
