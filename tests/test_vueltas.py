"""Tests del alineador de vueltas contra los segmentos del plan.

Lo que se prueba aqui es una decision, no una formula: cual de las vueltas del
reloj es el trabajo declarado y cual es recuperacion. Los casos dificiles estan
todos en que la distancia sola no alcanza para decidirlo.
"""
from __future__ import annotations

import pytest

from sport_report.db.models import Vuelta
from sport_report.engine.vueltas import MIN_VUELTAS, alinear, tolerancia_km
from sport_report.strava.metricas import normalizar_vueltas

# `J: series 8.6km estructura=2km+3x1000m+4x400m+2km`
SERIES = (2.0, 1.0, 1.0, 1.0, 0.4, 0.4, 0.4, 0.4, 2.0)


def vs(*pares: tuple[float, int]) -> list[Vuelta]:
    """(km, segundos) -> vueltas numeradas desde 1, en orden."""
    return [Vuelta("strava", "1", i + 1, km, s) for i, (km, s) in enumerate(pares)]


# --------------------------------------------------------------------------
# El caso que motiva todo
# --------------------------------------------------------------------------


def test_separa_el_trabajo_declarado_de_la_recuperacion():
    reales = vs(
        (2.0, 800),                                          # calentamiento
        (1.0, 240), (0.2, 90), (1.0, 242), (0.2, 92), (1.0, 238), (0.2, 90),
        (0.4, 84), (0.2, 78), (0.4, 85), (0.2, 80), (0.4, 83), (0.2, 79), (0.4, 82),
        (2.0, 820),                                          # enfriamiento
    )

    a = alinear(SERIES, reales)

    assert a.ok
    assert a.declarada_km == 8.6
    assert a.recuperacion_km == 1.2
    assert a.indices == (1, 2, 4, 6, 8, 10, 12, 14, 15)


def test_la_alineacion_es_verdadera_en_un_if():
    """`if alinear(...)` tiene que leerse; quien llama lo usa asi."""
    assert alinear(SERIES, []) is not None
    assert not alinear(SERIES, [])


# --------------------------------------------------------------------------
# Donde la distancia no basta
# --------------------------------------------------------------------------


def test_con_recuperaciones_de_la_misma_medida_manda_el_ritmo():
    """4x400m con 400m de trote: las ocho vueltas miden lo mismo.

    Es el caso que obliga a mirar el tiempo. Emparejando solo por distancia
    cualquiera de las ocho sirve y la respuesta seria arbitraria.
    """
    reales = vs(
        (2.0, 800),
        (1.0, 240), (0.4, 150), (1.0, 242), (0.4, 152), (1.0, 238), (0.4, 150),
        (0.4, 84), (0.4, 155), (0.4, 85), (0.4, 156), (0.4, 83), (0.4, 154), (0.4, 82),
        (2.0, 820),
    )

    a = alinear(SERIES, reales)

    assert a.ok
    assert a.declarada_km == 8.6
    assert a.recuperacion_km == 2.4
    # Las rapidas: 84, 85, 83, 82. Nunca las de 150+.
    assert a.indices == (1, 2, 4, 6, 8, 10, 12, 14, 15)


def test_no_se_queda_con_la_primera_vuelta_que_calza():
    """Emparejar con la primera compatible daria las dos de trote."""
    reales = vs((0.4, 150), (0.4, 80), (0.4, 150), (0.4, 82))

    a = alinear((0.4, 0.4), reales)

    assert a.indices == (2, 4)
    assert a.recuperacion_km == 0.8


def test_las_vueltas_se_usan_en_orden_y_sin_repetir():
    """Una vuelta rapida no puede emparejarse con un segmento anterior a ella."""
    reales = vs((2.0, 800), (1.0, 240), (2.0, 700))

    a = alinear((2.0, 1.0, 2.0), reales)

    assert a.indices == (1, 2, 3)


# --------------------------------------------------------------------------
# Tolerancia
# --------------------------------------------------------------------------


def test_el_gps_no_clava_la_distancia():
    reales = vs((2.03, 800), (0.968, 240), (0.43, 84), (1.98, 820))

    a = alinear((2.0, 1.0, 0.4, 2.0), reales)

    assert a.ok
    assert a.recuperacion_km == 0.0


def test_una_vuelta_demasiado_lejos_no_es_ese_segmento():
    """0.5km no son 400m ni con el error del GPS mas generoso."""
    assert not alinear((0.4,), vs((0.5, 100), (0.5, 100)))


def test_la_tolerancia_es_relativa_pero_con_piso():
    # Sobre 400m manda el piso; sobre 2km manda el 12%.
    assert tolerancia_km(0.4) == pytest.approx(0.05)
    assert tolerancia_km(2.0) == pytest.approx(0.24)


# --------------------------------------------------------------------------
# Cuando no se puede: nunca un numero inventado
# --------------------------------------------------------------------------


def test_sin_vueltas_no_se_alinea():
    a = alinear(SERIES, [])
    assert not a.ok
    assert a.declarada_km is None
    assert "no trae vueltas" in a.motivo


def test_una_sola_vuelta_no_es_una_alineacion():
    """El reloj no se toco: la unica vuelta es la actividad entera."""
    a = alinear((10.0,), vs((10.0, 3000)))
    assert not a.ok
    assert "el reloj no se toco" in a.motivo
    assert MIN_VUELTAS == 2


def test_menos_vueltas_que_segmentos():
    a = alinear(SERIES, vs((2.0, 800), (8.0, 2000)))
    assert not a.ok
    assert "faltan vueltas" in a.motivo


def test_vueltas_que_no_corresponden_al_plan():
    """Auto-lap cada kilometro sobre una sesion de series."""
    a = alinear(SERIES, vs(*[(1.0, 300)] * 10))
    assert not a.ok
    assert "no calzan" in a.motivo


def test_un_plan_sin_segmentos_no_se_alinea():
    assert not alinear((), vs((1.0, 300), (1.0, 300))).ok


# --------------------------------------------------------------------------
# Sesiones sin recuperacion
# --------------------------------------------------------------------------


def test_una_progresiva_no_deja_recuperacion():
    """`V: prog 10km estructura=3km@6:00+3km@5:30+4km@5:00`: todo es declarado."""
    a = alinear((3.0, 3.0, 4.0), vs((3.0, 1080), (3.0, 990), (4.0, 1200)))

    assert a.ok
    assert (a.declarada_km, a.recuperacion_km) == (10.0, 0.0)


# --------------------------------------------------------------------------
# Normalizacion de lo que manda Strava
# --------------------------------------------------------------------------


def test_normalizar_descarta_las_vueltas_inservibles():
    """Una vuelta sin distancia o sin tiempo solo puede estorbar al alinear."""
    crudas = [
        {"lap_index": 1, "distance": 2000.0, "moving_time": 800},
        {"lap_index": 2, "distance": 0, "moving_time": 60},
        {"lap_index": 3, "distance": 400.0, "moving_time": 0},
        {"lap_index": 4, "distance": None, "moving_time": 90},
        {"lap_index": 5, "distance": 400.0, "moving_time": 84},
        "no es un dict",
    ]

    assert [v.indice for v in normalizar_vueltas("strava", "9", crudas)] == [1, 5]


def test_normalizar_cae_a_elapsed_time_si_no_hay_moving():
    v = normalizar_vueltas("strava", "9", [{"lap_index": 1, "distance": 400.0, "elapsed_time": 95}])

    assert v[0].duracion_mov_s == 95


def test_normalizar_ordena_por_indice():
    crudas = [
        {"lap_index": 3, "distance": 400.0, "moving_time": 84},
        {"lap_index": 1, "distance": 2000.0, "moving_time": 800},
    ]

    assert [v.indice for v in normalizar_vueltas("strava", "9", crudas)] == [1, 3]


def test_el_ritmo_de_una_vuelta():
    assert Vuelta("strava", "1", 1, 0.4, 84).ritmo_s_km == pytest.approx(210.0)
    assert Vuelta("strava", "1", 1, 0.0, 84).ritmo_s_km is None


# --------------------------------------------------------------------------
# Migracion de una base que ya existia
# --------------------------------------------------------------------------


def test_una_base_vieja_gana_la_tabla_y_la_bandera(tmp_path):
    """La Pi lleva meses con datos: el despliegue no puede exigir base nueva.

    Todas las filas quedan con `vueltas_procesadas` en 0, no en 1: ninguna tiene
    vueltas guardadas todavia y hay que pedirlas una vez.
    """
    import sqlite3

    from sport_report.db.repo import Repo

    vieja = tmp_path / "vieja.db"
    con = sqlite3.connect(vieja)
    con.executescript(
        """
        CREATE TABLE sesiones (
            strava_id INTEGER PRIMARY KEY, fecha_utc TEXT NOT NULL,
            fecha_local TEXT NOT NULL, dia_semana TEXT NOT NULL,
            tipo_strava TEXT NOT NULL, es_fuerza INTEGER NOT NULL DEFAULT 0,
            nombre TEXT, distancia_km REAL, duracion_mov_s INTEGER,
            duracion_tot_s INTEGER, hr_promedio REAL, hr_maximo REAL,
            cadencia_spm REAL, potencia_w REAL, decoupling_pct REAL, carga REAL,
            carga_impreciso INTEGER NOT NULL DEFAULT 0,
            streams_procesados INTEGER NOT NULL DEFAULT 1, ingerido_en TEXT NOT NULL);
        INSERT INTO sesiones (strava_id, fecha_utc, fecha_local, dia_semana,
            tipo_strava, carga, ingerido_en)
            VALUES (1,'2026-08-31T12:00:00+00:00','2026-08-31','L','Run',420.0,'x');
        """
    )
    con.commit()
    con.close()

    with Repo(vieja) as r:
        assert r.sesion("strava", "1").streams_procesados is True
        assert r.sesion("strava", "1").vueltas_procesadas is False
        r.guardar_vueltas("strava", "1", [Vuelta("strava", "1", 1, 2.0, 800)])
        assert len(r.vueltas("strava", "1")) == 1
    with Repo(vieja) as r:  # reabrir no vuelve a migrar ni pierde nada
        assert len(r.vueltas("strava", "1")) == 1


def test_reingerir_una_sesion_no_se_lleva_sus_vueltas(tmp_path):
    """`guardar_sesion` usa INSERT OR REPLACE. Con una FOREIGN KEY en cascada
    eso borraria las vueltas en cada sincronizacion, en silencio."""
    from sport_report.db.repo import Repo
    from tests.test_engine import sesion

    with Repo(tmp_path / "t.db") as r:
        r.guardar_sesion(sesion(3, fuente="strava", id_externo="1003", distancia_km=10.0))
        r.guardar_vueltas("strava", "1003", [Vuelta("strava", "1003", 1, 2.0, 800), Vuelta("strava", "1003", 2, 1.0, 240)])

        r.guardar_sesion(sesion(3, fuente="strava", id_externo="1003", distancia_km=10.1))

        assert len(r.vueltas("strava", "1003")) == 2
