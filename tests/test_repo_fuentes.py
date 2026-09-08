"""Dos fuentes en la misma base: preferencia por dia, bienestar y fuente semanal.

El riesgo que cubren estos tests es concreto: la misma corrida ingerida por
intervals.icu y por Strava son dos filas, y sin la regla de preferencia el
volumen y la carga de esa semana salen al doble. La regla vive en el repo (una
sola constante SQL) porque el motor de calculo no deberia saber que hay dos
fuentes.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from sport_report.db.models import BienestarDia, SesionReal, Vuelta
from sport_report.db.repo import Repo

LUNES = date(2026, 9, 7)


@pytest.fixture()
def repo(tmp_path):
    with Repo(tmp_path / "t.db") as r:
        yield r


def _sesion(fuente: str, id_externo: str, dia_offset: int = 0, **kw) -> SesionReal:
    f = LUNES + timedelta(days=dia_offset)
    base = dict(
        fuente=fuente,
        id_externo=id_externo,
        fecha_utc=f"{f}T12:00:00+00:00",
        fecha_local=f.isoformat(),
        dia_semana="LMWJVSD"[dia_offset],
        tipo="Run",
        es_fuerza=False,
        distancia_km=10.0,
        duracion_mov_s=3000,
        carga=400.0,
    )
    base.update(kw)
    return SesionReal(**base)


def _misma_corrida_en_las_dos_fuentes(repo, dia_offset: int = 0) -> None:
    repo.guardar_sesion(_sesion("intervals", "i1", dia_offset, gct_ms=232.0))
    repo.guardar_sesion(_sesion("strava", "1", dia_offset))


# --------------------------------------------------------------------------
# Preferencia de fuente por dia
# --------------------------------------------------------------------------


def test_el_mismo_dia_en_dos_fuentes_no_se_cuenta_dos_veces(repo):
    _misma_corrida_en_las_dos_fuentes(repo)

    assert len(repo.sesiones_entre(LUNES, LUNES)) == 1
    assert repo.carga_diaria(LUNES, LUNES) == {LUNES: 400.0}
    assert repo.dias_con_datos(LUNES, LUNES) == 1
    assert repo.volumen_semanal(LUNES, 1) == [(LUNES, 10.0)]


def test_manda_intervals_porque_es_el_registro_completo(repo):
    _misma_corrida_en_las_dos_fuentes(repo)

    (s,) = repo.sesiones_entre(LUNES, LUNES)
    assert s.fuente == "intervals"
    assert s.gct_ms == 232.0, "se quedo con la fila pobre y se perdio la dinamica"


def test_el_orden_de_ingesta_no_cambia_quien_manda(repo):
    """Guardar Strava despues de intervals no debe invertir la preferencia."""
    repo.guardar_sesion(_sesion("strava", "1"))
    assert repo.sesiones_entre(LUNES, LUNES)[0].fuente == "strava"
    repo.guardar_sesion(_sesion("intervals", "i1", gct_ms=232.0))
    assert repo.sesiones_entre(LUNES, LUNES)[0].fuente == "intervals"


def test_una_semana_de_respaldo_se_lee_completa(repo):
    """Si un dia solo lo tiene Strava, ese dia lo sirve Strava.

    Es el caso de la semana degradada: la preferencia no puede dejar el dia
    vacio solo porque intervals.icu no lo trajo.
    """
    repo.guardar_sesion(_sesion("strava", "1", 0))
    repo.guardar_sesion(_sesion("strava", "2", 1))

    assert [s.fuente for s in repo.sesiones_entre(LUNES, LUNES + timedelta(days=1))] == [
        "strava",
        "strava",
    ]
    assert sum(repo.carga_diaria(LUNES, LUNES + timedelta(days=1)).values()) == 800.0


def test_la_preferencia_es_por_dia_y_no_por_semana(repo):
    """Un lunes por intervals y un martes por respaldo suman los dos."""
    repo.guardar_sesion(_sesion("intervals", "i1", 0))
    repo.guardar_sesion(_sesion("strava", "2", 1))

    dias = repo.sesiones_entre(LUNES, LUNES + timedelta(days=1))
    assert [(s.fuente, s.fecha_local) for s in dias] == [
        ("intervals", "2026-09-07"),
        ("strava", "2026-09-08"),
    ]


def test_las_vueltas_siguen_a_la_fuente_que_manda(repo):
    _misma_corrida_en_las_dos_fuentes(repo)
    repo.guardar_vueltas("intervals", "i1", [Vuelta("intervals", "i1", 1, 2.0, 800)])
    repo.guardar_vueltas(
        "strava",
        "1",
        [Vuelta("strava", "1", 1, 2.0, 800), Vuelta("strava", "1", 2, 1.0, 240)],
    )

    mapa = repo.vueltas_entre(LUNES, LUNES)
    assert list(mapa) == [("intervals", "i1")]


def test_dos_actividades_distintas_el_mismo_dia_si_se_suman(repo):
    """La preferencia descarta duplicados entre fuentes, no sesiones dobles."""
    repo.guardar_sesion(_sesion("intervals", "i1", 0, distancia_km=6.0, carga=200.0))
    repo.guardar_sesion(_sesion("intervals", "i2", 0, distancia_km=4.0, carga=150.0))

    assert len(repo.sesiones_entre(LUNES, LUNES)) == 2
    assert repo.carga_diaria(LUNES, LUNES) == {LUNES: 350.0}
    assert repo.volumen_semanal(LUNES, 1) == [(LUNES, 10.0)]


def test_la_bici_sigue_fuera_del_volumen_aunque_venga_de_intervals(repo):
    repo.guardar_sesion(_sesion("intervals", "i9", 0, tipo="Ride", distancia_km=60.0))
    assert repo.volumen_semanal(LUNES, 1) == [(LUNES, 0.0)]


# --------------------------------------------------------------------------
# Bienestar: solo intervals.icu, y ausente no es cero
# --------------------------------------------------------------------------


def test_el_bienestar_se_guarda_y_se_lee_por_rango(repo):
    repo.guardar_bienestar(
        [
            BienestarDia(
                fecha_local=LUNES.isoformat(),
                fuente="intervals",
                hrv=68.0,
                hr_reposo=48.0,
                sueno_h=7.5,
                sueno_score=82.0,
                readiness=71.0,
                crudo='{"id": "2026-09-07"}',
            ),
            BienestarDia(
                fecha_local=(LUNES + timedelta(days=1)).isoformat(),
                fuente="intervals",
                hrv=61.0,
            ),
        ]
    )
    dias = repo.bienestar_entre(LUNES, LUNES + timedelta(days=6))
    assert [d.fecha_local for d in dias] == ["2026-09-07", "2026-09-08"]
    assert (dias[0].hrv, dias[0].sueno_score, dias[0].readiness) == (68.0, 82.0, 71.0)
    # Lo que el reloj no midio queda en None, no en 0.0.
    assert dias[1].hr_reposo is None and dias[1].body_battery is None


def test_una_semana_sin_bienestar_no_devuelve_ceros(repo):
    """Es la semana de respaldo: intervals.icu no respondio."""
    assert repo.bienestar_entre(LUNES, LUNES + timedelta(days=6)) == []
    assert repo.guardar_bienestar([]) == 0


def test_reingerir_el_bienestar_del_mismo_dia_actualiza_sin_duplicar(repo):
    dia = LUNES.isoformat()
    repo.guardar_bienestar([BienestarDia(fecha_local=dia, fuente="intervals", hrv=60.0)])
    repo.guardar_bienestar([BienestarDia(fecha_local=dia, fuente="intervals", hrv=65.0)])
    dias = repo.bienestar_entre(LUNES, LUNES)
    assert len(dias) == 1 and dias[0].hrv == 65.0


def test_un_dia_de_bienestar_sin_ninguna_metrica_se_reconoce_vacio():
    assert BienestarDia(fecha_local="2026-09-07", fuente="intervals").vacio is True
    assert BienestarDia(fecha_local="2026-09-07", fuente="intervals", hrv=60.0).vacio is False


# --------------------------------------------------------------------------
# Que fuente sirvio cada semana
# --------------------------------------------------------------------------


def test_la_fuente_de_la_semana_se_persiste_para_el_reporte(repo):
    repo.guardar_fuente_semana("2026-09-07", "strava", fallback=True, detalle="401")
    f = repo.fuente_semana("2026-09-07")
    assert (f["fuente"], f["fallback"], f["detalle"]) == ("strava", True, "401")
    # Sin fila no se inventa una fuente: el reporte decide que decir.
    assert repo.fuente_semana("2026-09-14") is None


def test_reejecutar_la_semana_reescribe_su_fuente(repo):
    repo.guardar_fuente_semana("2026-09-07", "strava", fallback=True)
    repo.guardar_fuente_semana("2026-09-07", "intervals", fallback=False)
    f = repo.fuente_semana("2026-09-07")
    assert (f["fuente"], f["fallback"]) == ("intervals", False)


def test_las_fuentes_de_un_rango_salen_ordenadas(repo):
    repo.guardar_fuente_semana("2026-09-14", "intervals")
    repo.guardar_fuente_semana("2026-09-07", "strava", fallback=True)
    repo.guardar_fuente_semana("2026-08-31", "intervals")
    filas = repo.fuentes_entre("2026-09-01", "2026-09-30")
    assert [f["semana"] for f in filas] == ["2026-09-07", "2026-09-14"]
    assert [f["fallback"] for f in filas] == [True, False]
