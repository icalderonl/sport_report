"""GCT, oscilacion vertical y ratio vertical en el reporte semanal.

Son las tres metricas por las que se cambio de fuente: Strava no las expone.
Todo el archivo prueba la misma promesa desde varios angulos — cuando no estan,
el reporte dice por que no estan, y nunca las presenta como cero.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from sport_report import config
from sport_report.db.models import BienestarDia, SesionReal
from sport_report.db.repo import Repo
from sport_report.engine import report
from sport_report.fechas import semana_de
from sport_report.plan.store import PlanStore
from sport_report.telegram.formato import formatear_reporte

LUNES = date(2026, 9, 7)
SEMANA = semana_de(LUNES)


@pytest.fixture()
def entorno(tmp_path):
    repo = Repo(tmp_path / "t.db")
    store = PlanStore(actual=tmp_path / "plan.json", archivo=tmp_path / "planes")
    yield repo, store
    repo.cerrar()


def corrida(
    repo: Repo,
    dia_offset: int = 0,
    fuente: str = "intervals",
    id_externo: str | None = None,
    semanas_atras: int = 0,
    **kw,
) -> None:
    f = LUNES + timedelta(days=dia_offset) - timedelta(weeks=semanas_atras)
    base = dict(
        fuente=fuente,
        id_externo=id_externo or f"a{f.isoformat()}{dia_offset}",
        fecha_utc=f"{f}T12:00:00+00:00",
        fecha_local=f.isoformat(),
        dia_semana="LMWJVSD"[f.weekday()],
        tipo="Run",
        es_fuerza=False,
        distancia_km=10.0,
        duracion_mov_s=3000,
        carga=400.0,
        cadencia_spm=176.0,
    )
    base.update(kw)
    repo.guardar_sesion(SesionReal(**base))


def _json(entorno, fuente: str = "intervals", fallback: bool = False, motivo: str = ""):
    repo, store = entorno
    repo.guardar_fuente_semana(SEMANA.clave, fuente, fallback, motivo)
    return report.construir(SEMANA, repo, store)


# --------------------------------------------------------------------------
# Con datos
# --------------------------------------------------------------------------


def test_las_tres_metricas_salen_con_su_unidad(entorno):
    repo, _ = entorno
    corrida(repo, 1, gct_ms=232.0, oscilacion_vertical_cm=8.4, ratio_vertical_pct=7.9)
    corrida(repo, 3, gct_ms=228.0, oscilacion_vertical_cm=8.0, ratio_vertical_pct=7.5)

    j = _json(entorno)
    assert (j["gct"]["valor"], j["gct"]["unidad"]) == (230.0, "ms")
    assert (j["oscilacion_vertical"]["valor"], j["oscilacion_vertical"]["unidad"]) == (8.2, "cm")
    assert (j["ratio_vertical"]["valor"], j["ratio_vertical"]["unidad"]) == (7.7, "%")
    for clave in ("gct", "oscilacion_vertical", "ratio_vertical"):
        assert j[clave]["disponible"] is True
        assert j[clave]["motivo"] == ""
        assert j[clave]["n_sesiones"] == 2


def test_la_tendencia_compara_contra_la_semana_anterior(entorno):
    repo, _ = entorno
    corrida(repo, 1, gct_ms=232.0)
    corrida(repo, 1, semanas_atras=1, gct_ms=240.0)

    j = _json(entorno)
    assert (j["gct"]["valor"], j["gct"]["semana_anterior"], j["gct"]["delta"]) == (
        232.0,
        240.0,
        -8.0,
    )


def test_la_cadencia_se_sigue_reportando_como_antes(entorno):
    """No gana `disponible` ni `motivo`: Strava tambien la da."""
    repo, _ = entorno
    corrida(repo, 1, cadencia_spm=176.0)
    j = _json(entorno)
    assert j["cadencia"]["valor"] == 176.0
    assert "disponible" not in j["cadencia"]


def test_la_dinamica_por_sesion_va_en_el_detalle(entorno):
    repo, _ = entorno
    corrida(repo, 1, gct_ms=232.0, oscilacion_vertical_cm=8.4, ratio_vertical_pct=7.9)
    s = _json(entorno)["sesiones"][0]
    assert (s["gct_ms"], s["oscilacion_vertical_cm"], s["ratio_vertical_pct"]) == (
        232.0,
        8.4,
        7.9,
    )


# --------------------------------------------------------------------------
# Sin datos: tres motivos distintos, ningun cero
# --------------------------------------------------------------------------


def test_una_semana_por_el_respaldo_lo_dice_con_ese_motivo(entorno):
    repo, _ = entorno
    corrida(repo, 1, fuente="strava")

    j = _json(entorno, fuente="strava", fallback=True, motivo="intervals: 401")
    for clave in ("gct", "oscilacion_vertical", "ratio_vertical"):
        assert j[clave]["valor"] is None, clave
        assert j[clave]["disponible"] is False, clave
        assert "respaldo" in j[clave]["motivo"], clave
        assert "strava" in j[clave]["motivo"], clave


def test_si_el_reloj_no_la_midio_el_motivo_es_otro(entorno):
    """Distinguirlo del respaldo cambia que hay que ir a revisar."""
    repo, _ = entorno
    corrida(repo, 1, gct_ms=None)
    corrida(repo, 3, gct_ms=None)

    j = _json(entorno)
    assert j["gct"]["valor"] is None
    assert "el reloj no reporto" in j["gct"]["motivo"]
    assert "2 corridas" in j["gct"]["motivo"]


def test_una_semana_sin_corridas_no_culpa_al_reloj(entorno):
    j = _json(entorno)
    assert j["gct"]["valor"] is None
    assert "no hubo corridas" in j["gct"]["motivo"]


def test_nunca_se_reporta_cero(entorno):
    """Un GCT de 0 ms diria que el pie no toco el suelo."""
    repo, _ = entorno
    corrida(repo, 1, gct_ms=None, oscilacion_vertical_cm=None, ratio_vertical_pct=None)
    j = _json(entorno)
    for clave in ("gct", "oscilacion_vertical", "ratio_vertical"):
        valor = j[clave]["valor"]
        assert valor is None, f"{clave} salio {valor!r} en vez de None"


def test_una_metrica_puede_faltar_sin_arrastrar_a_las_otras(entorno):
    """El reloj puede dar GCT y no ratio vertical."""
    repo, _ = entorno
    corrida(repo, 1, gct_ms=232.0, oscilacion_vertical_cm=8.4, ratio_vertical_pct=None)
    j = _json(entorno)
    assert j["gct"]["disponible"] is True
    assert j["oscilacion_vertical"]["disponible"] is True
    assert j["ratio_vertical"]["disponible"] is False
    assert j["ratio_vertical"]["valor"] is None


# --------------------------------------------------------------------------
# Version y compatibilidad del formateo
# --------------------------------------------------------------------------


def test_el_reporte_declara_la_version_2(entorno):
    assert _json(entorno)["version"] == 2


def test_el_mensaje_muestra_la_dinamica(entorno):
    repo, _ = entorno
    corrida(repo, 1, gct_ms=232.0, oscilacion_vertical_cm=8.4, ratio_vertical_pct=7.9)
    texto = formatear_reporte(_json(entorno))
    assert "DINAMICA DE CARRERA" in texto
    assert "GCT 232" in texto and "oscilacion vertical 8.4" in texto
    assert "ratio vertical 7.9" in texto


def test_el_mensaje_explica_la_dinamica_que_falta(entorno):
    repo, _ = entorno
    corrida(repo, 1, fuente="strava")
    texto = formatear_reporte(_json(entorno, fuente="strava", fallback=True))
    assert "GCT: sin dato" in texto
    assert "respaldo" in texto


def test_el_mensaje_muestra_el_bloque_de_fatiga(entorno):
    repo, _ = entorno
    corrida(repo, 1)
    for i in range(5):
        repo.guardar_bienestar(
            [
                BienestarDia(
                    fecha_local=(LUNES + timedelta(days=i)).isoformat(),
                    fuente="intervals",
                    hrv=68.0,
                    hr_reposo=48.0,
                    sueno_h=7.5,
                    sueno_score=82.0,
                    readiness=71.0,
                )
            ]
        )
    texto = formatear_reporte(_json(entorno))
    assert "FATIGA Y DESCANSO" in texto
    assert "HRV 68" in texto and "sleep score 82" in texto
    # El cruce enuncia los dos lados, sin fusionarlos.
    assert "Al mismo tiempo" in texto


def test_un_reporte_v1_archivado_se_sigue_formateando():
    """Los JSON en data/reportes/ de antes de fase 2 no traen los bloques nuevos."""
    v1 = {
        "version": 1,
        "semana": {"inicio": "2026-08-31", "fin": "2026-09-06", "numero_plan": 4,
                   "plan_cargado": True, "en_curso": False},
        "volumen": {"planificado_km": 40.0, "real_km": 38.0, "pct": 95.0,
                    "estimado_km": 0.0, "planificado_semana_km": 40.0},
        "carga": {"semanal": 1200.0, "por_dia": {}, "impreciso": False,
                  "sesiones_sin_carga": 0},
        "acwr": {"ratio": 1.1, "aguda": 170.0, "cronica": 155.0, "confiable": True,
                 "motivo": "", "alerta": ""},
        "monotony": {"monotony": 1.5, "strain": 1800.0, "confiable": True,
                     "motivo": "", "alerta": ""},
        "deriva_cardiaca": {"promedio_pct": 3.0, "maximo_pct": 4.0, "n_sesiones": 2,
                            "sin_dato": 0, "alerta": ""},
        "cadencia": {"valor": 175.0, "semana_anterior": None, "delta": None,
                     "n_sesiones": 2},
        "adherencia": {"dias": [], "pct_global": None, "sesiones_cumplidas": 0,
                       "sesiones_evaluables": 0, "fuerza": {"planificadas": 0,
                       "cumplidas": 0}, "avisos": []},
        "sesiones": [],
        "alertas": [],
        "avisos_datos": [],
        "umbrales": {"acwr_alto": 1.5, "acwr_bajo": 0.8, "monotony_alta": 2.0,
                     "decoupling_alto_pct": 5.0, "nota": "x"},
    }
    texto = formatear_reporte(v1)
    assert "REPORTE SEMANAL" in texto and "cadencia 175" in texto
    # Sin inventar secciones que ese reporte no tenia.
    assert "FATIGA Y DESCANSO" not in texto
    assert "GCT" not in texto
