"""Tests del motor de calculo: ACWR, Foster, adherencia y el JSON final."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from sport_report.config import Umbrales
from sport_report.db.models import SesionReal, Vuelta
from sport_report.db.repo import Repo
from sport_report.engine import acwr, adherencia, foster, report
from sport_report.fechas import semana_de
from sport_report.plan.grammar import parse_plan
from sport_report.plan.store import PlanStore
from tests.test_grammar import PLAN_SPEC

SEMANA = semana_de(date(2026, 9, 7))  # lunes 7 a domingo 13
LUNES, DOMINGO = SEMANA.inicio, SEMANA.fin
U = Umbrales()


def carga_uniforme(fin: date, dias: int, valor: float, salto: int = 1) -> dict[date, float]:
    return {fin - timedelta(days=i): valor for i in range(0, dias, salto)}


# --------------------------------------------------------------------------
# ACWR
# --------------------------------------------------------------------------


def test_acwr_con_historial_suficiente():
    cargas = carga_uniforme(DOMINGO, 28, 100.0)
    r = acwr.calcular(cargas, DOMINGO, primera_fecha=DOMINGO - timedelta(days=60))
    assert (r.aguda, r.cronica, r.ratio) == (100.0, 100.0, 1.0)
    assert r.confiable is True and r.motivo == ""


def test_acwr_detecta_pico_de_carga():
    cargas = carga_uniforme(DOMINGO, 28, 50.0)
    for i in range(7):
        cargas[DOMINGO - timedelta(days=i)] = 150.0
    r = acwr.calcular(cargas, DOMINGO, primera_fecha=DOMINGO - timedelta(days=60))
    assert r.ratio > U.acwr_alto
    assert "sobre el umbral" in r.alerta


def test_acwr_no_confiable_con_menos_de_28_dias():
    """Primeras semanas del sistema: no confiable, no una cifra enganosa."""
    cargas = carga_uniforme(DOMINGO, 10, 100.0)
    r = acwr.calcular(cargas, DOMINGO, primera_fecha=DOMINGO - timedelta(days=9))
    assert r.confiable is False
    assert "10 dias de historico real" in r.motivo
    assert r.ratio is not None  # el numero existe, pero va marcado


def test_acwr_no_confiable_con_pocas_sesiones():
    cargas = {DOMINGO - timedelta(days=i * 7): 100.0 for i in range(4)}
    r = acwr.calcular(cargas, DOMINGO, primera_fecha=DOMINGO - timedelta(days=60))
    assert r.confiable is False
    assert "dias con carga registrada" in r.motivo


def test_acwr_sin_datos_no_divide_por_cero():
    r = acwr.calcular({}, DOMINGO, primera_fecha=None)
    assert r.ratio is None and r.confiable is False
    assert "cronica en cero" in r.motivo


def test_acwr_marca_las_sesiones_sin_hr():
    cargas = carga_uniforme(DOMINGO, 28, 100.0)
    r = acwr.calcular(
        cargas, DOMINGO, primera_fecha=DOMINGO - timedelta(days=60), sesiones_sin_carga=2
    )
    assert r.confiable is False
    assert "2 sesion(es) sin HR" in r.motivo


def test_acwr_alerta_por_carga_baja():
    cargas = carga_uniforme(DOMINGO, 28, 100.0)
    for i in range(7):
        cargas[DOMINGO - timedelta(days=i)] = 20.0
    r = acwr.calcular(cargas, DOMINGO, primera_fecha=DOMINGO - timedelta(days=60))
    assert "bajo el umbral" in r.alerta


# --------------------------------------------------------------------------
# Foster
# --------------------------------------------------------------------------


def test_monotony_y_strain():
    cargas = {LUNES + timedelta(days=i): v for i, v in enumerate([100, 0, 80, 0, 120, 0, 200])}
    r = foster.calcular(cargas, LUNES)
    assert r.carga_semanal == 500.0
    assert r.monotony == pytest.approx(1.02, abs=0.02)
    assert r.strain == pytest.approx(r.carga_semanal * r.monotony, abs=1)
    assert r.confiable is True


def test_monotony_alta_dispara_alerta():
    cargas = {LUNES + timedelta(days=i): v for i, v in enumerate([100, 98, 102, 99, 101, 100, 99])}
    r = foster.calcular(cargas, LUNES)
    assert r.monotony > U.monotony_alta
    assert "sobre el umbral" in r.alerta


def test_desviacion_cero_no_divide_por_cero():
    """Semana perfectamente plana: no confiable, no infinito."""
    cargas = {LUNES + timedelta(days=i): 100.0 for i in range(7)}
    r = foster.calcular(cargas, LUNES)
    assert r.monotony is None and r.strain is None
    assert r.confiable is False and "desviacion estandar 0" in r.motivo


def test_semana_sin_carga_no_es_confiable():
    r = foster.calcular({}, LUNES)
    assert r.confiable is False and "sin carga registrada" in r.motivo


def test_foster_solo_mira_su_ventana_de_7_dias():
    cargas = {LUNES - timedelta(days=1): 9999.0, LUNES: 100.0}
    assert foster.calcular(cargas, LUNES).carga_semanal == 100.0


# --------------------------------------------------------------------------
# Adherencia
# --------------------------------------------------------------------------


def sesion(dia_offset: int, **kw) -> SesionReal:
    f = LUNES + timedelta(days=dia_offset)
    base = dict(
        fuente="strava",
        id_externo=str(1000 + dia_offset),
        fecha_utc=f"{f}T12:00:00+00:00",
        fecha_local=f.isoformat(),
        dia_semana="LMWJVSD"[dia_offset],
        tipo="Run",
        es_fuerza=False,
        distancia_km=10.0,
        duracion_mov_s=3000,
    )
    base.update(kw)
    return SesionReal(**base)


# El jueves del PLAN_SPEC tal como lo marcaria el reloj: cada segmento
# declarado en su vuelta y las recuperaciones sueltas entre medio. Suman los
# 10.0km reales, de los cuales 8.6 son los declarados en `estructura=`.
VUELTAS_SERIES = [
    Vuelta("strava", "1003", 1, 2.0, 800),                                    # calentamiento
    Vuelta("strava", "1003", 2, 1.0, 240), Vuelta("strava", "1003", 3, 0.2, 90),
    Vuelta("strava", "1003", 4, 1.0, 242), Vuelta("strava", "1003", 5, 0.2, 92),
    Vuelta("strava", "1003", 6, 1.0, 238), Vuelta("strava", "1003", 7, 0.4, 150),  # trote largo, mide igual que una rep
    Vuelta("strava", "1003", 8, 0.4, 84), Vuelta("strava", "1003", 9, 0.2, 78),
    Vuelta("strava", "1003", 10, 0.4, 85), Vuelta("strava", "1003", 11, 0.2, 80),
    Vuelta("strava", "1003", 12, 0.4, 83), Vuelta("strava", "1003", 13, 0.2, 79),
    Vuelta("strava", "1003", 14, 0.4, 82),
    Vuelta("strava", "1003", 15, 2.0, 820),                                   # enfriamiento
]


def _adh(sesiones, plan=None, fuerza=None, vueltas=None):
    p = plan or parse_plan(PLAN_SPEC)
    if fuerza:
        from dataclasses import replace

        p = replace(p, fuerza_completada=fuerza)
    return adherencia.calcular(p, SEMANA, sesiones, vueltas=vueltas)


def test_dia_cumplido():
    # M: easy 8km
    r = _adh([sesion(1, distancia_km=8.0)])
    d = r.dias[1]
    assert (d.objetivo, d.real, d.pct, d.estado) == (8.0, 8.0, 100.0, adherencia.CUMPLIDA)


def test_dia_sin_sesion_es_cero_por_ciento():
    r = _adh([])
    assert r.dias[1].pct == 0.0
    assert r.dias[1].estado == adherencia.SIN_SESION


def test_sesion_sin_el_dato_pedido_no_es_cero():
    """Indoor sin GPS cuando el plan pedia km: dato faltante, no incumplimiento."""
    r = _adh([sesion(1, distancia_km=None)])
    d = r.dias[1]
    assert d.pct is None and d.real is None
    assert d.estado == adherencia.DATO_FALTANTE
    assert "no es lo mismo que no entrenar" in d.nota
    assert r.dias_sin_dato == 1


def test_series_sin_vueltas_se_comparan_contra_el_total_y_la_nota_lo_dice():
    """Caso real de la spec: 8.6km duros, 10.0km reales por la recuperacion.

    Sin vueltas no hay forma de separar la recuperacion, asi que se compara
    contra el total y el porcentaje sale alto. Lo que no se puede es callarlo.
    """
    r = _adh([sesion(3, distancia_km=10.0)])
    d = r.dias[3]
    assert d.objetivo == 8.6
    assert d.real == 10.0
    assert d.pct == pytest.approx(116.3, abs=0.1)
    assert "el porcentaje sale alto" in d.nota
    assert "no trae vueltas marcadas" in d.nota


def test_series_con_vueltas_se_comparan_declarado_contra_declarado():
    """Con las vueltas del reloj se descuenta la recuperacion y sale 100%."""
    r = _adh([sesion(3, distancia_km=10.0)], vueltas={("strava", "1003"): VUELTAS_SERIES})
    d = r.dias[3]
    assert d.objetivo == 8.6
    assert d.real == 8.6
    assert d.pct == pytest.approx(100.0, abs=0.1)
    assert d.estado == adherencia.CUMPLIDA
    assert "1.4km restantes son recuperacion" in d.nota


def test_el_volumen_real_si_cuenta_la_recuperacion():
    r = _adh([sesion(3, distancia_km=10.0)])
    # La adherencia del jueves se mide contra 8.6, pero los 10 km completos
    # entran al volumen de la semana.
    assert r.volumen_real_km == 10.0


def test_dia_de_descanso_con_actividad_se_senala():
    r = _adh([sesion(0, distancia_km=5.0)])  # L: rest
    assert r.dias[0].estado == adherencia.DESCANSO_ROTO
    assert "actividad no planificada" in r.dias[0].nota
    assert r.no_planificadas == ("L",)


def test_dia_de_descanso_respetado():
    assert _adh([]).dias[0].estado == adherencia.DESCANSO_OK


def test_fuerza_pendiente_y_cumplida():
    assert _adh([]).dias[2].estado == adherencia.FUERZA_PENDIENTE
    r = _adh([], fuerza={"W": True})
    assert r.dias[2].estado == adherencia.FUERZA_OK
    assert (r.fuerza_planificadas, r.fuerza_cumplidas) == (1, 1)


def test_sesion_en_minutos_se_compara_en_minutos():
    plan = parse_plan(PLAN_SPEC.replace("M: easy 8km Z2", "M: easy 50min Z2"))
    r = adherencia.calcular(plan, SEMANA, [sesion(1, duracion_mov_s=3000, distancia_km=None)])
    d = r.dias[1]
    assert (d.unidad, d.objetivo, d.real, d.pct) == ("min", 50.0, 50.0, 100.0)


def test_varias_sesiones_el_mismo_dia_se_suman():
    r = _adh([sesion(1, distancia_km=4.0), sesion(1, fuente="strava", id_externo="999", distancia_km=4.0)])
    assert r.dias[1].real == 8.0 and r.dias[1].estado == adherencia.CUMPLIDA


def test_la_fuerza_del_dia_no_contamina_la_comparacion_de_carrera():
    reales = [
        sesion(1, distancia_km=8.0),
        sesion(1, fuente="strava", id_externo="999", tipo="WeightTraining", es_fuerza=True, distancia_km=None),
    ]
    assert _adh(reales).dias[1].pct == 100.0


def test_resumen_global():
    reales = [sesion(1, distancia_km=8.0), sesion(3, distancia_km=8.6), sesion(4, distancia_km=10.0)]
    r = _adh(reales, fuerza={"W": True})
    # Evaluables: M, J, V, D (4). Cumplidas: M, J, V (D no tuvo sesion).
    assert (r.sesiones_evaluables, r.sesiones_cumplidas) == (4, 3)
    assert r.pct_global == 75.0
    assert r.volumen_planificado_km == 42.6


def test_sin_plan_solo_reporta_lo_real():
    r = adherencia.calcular(None, SEMANA, [sesion(1, distancia_km=8.0)])
    assert r.dias == () and r.volumen_real_km == 8.0
    assert "no habia plan cargado" in r.avisos[0]


# --------------------------------------------------------------------------
# JSON final
# --------------------------------------------------------------------------


@pytest.fixture
def entorno(tmp_path):
    repo = Repo(tmp_path / "t.db")
    store = PlanStore(actual=tmp_path / "plan.json", archivo=tmp_path / "planes")
    store.guardar(parse_plan(PLAN_SPEC), SEMANA)
    yield repo, store
    repo.cerrar()


def test_json_tiene_todas_las_secciones(entorno):
    repo, store = entorno
    repo.guardar_sesion(sesion(1, distancia_km=8.0, carga=90.0))
    j = report.construir(SEMANA, repo, store)

    esperadas = {
        "version", "generado_en", "semana", "volumen", "carga", "acwr", "monotony",
        "deriva_cardiaca", "cadencia", "adherencia", "sesiones", "alertas",
        "avisos_datos", "umbrales",
    }
    assert esperadas <= set(j)
    assert j["semana"]["numero_plan"] == 5
    assert j["volumen"]["planificado_km"] == 42.6


def test_json_marca_acwr_no_confiable_al_arrancar(entorno):
    repo, store = entorno
    repo.guardar_sesion(sesion(1, distancia_km=8.0, carga=90.0))
    j = report.construir(SEMANA, repo, store)
    assert j["acwr"]["confiable"] is False
    assert j["acwr"]["motivo"]


def test_json_propaga_la_carga_imprecisa(entorno):
    repo, store = entorno
    repo.guardar_sesion(sesion(1, distancia_km=8.0, carga=90.0, carga_impreciso=True))
    j = report.construir(SEMANA, repo, store)
    assert j["carga"]["impreciso"] is True
    assert any("peso de zona fijo" in a for a in j["avisos_datos"])


def test_json_avisa_de_sesiones_sin_hr(entorno):
    repo, store = entorno
    repo.guardar_sesion(sesion(1, distancia_km=8.0, carga=None))
    j = report.construir(SEMANA, repo, store)
    assert j["carga"]["sesiones_sin_carga"] == 1
    assert any("sin HR" in a for a in j["avisos_datos"])


def test_json_tendencia_de_cadencia(entorno):
    repo, store = entorno
    previa = LUNES - timedelta(days=7)
    repo.guardar_sesion(sesion(1, cadencia_spm=176.0, carga=90.0))
    repo.guardar_sesion(
        SesionReal(
            fuente="strava", id_externo="1",
            fecha_utc=f"{previa}T12:00:00+00:00",
            fecha_local=previa.isoformat(),
            dia_semana="L",
            tipo="Run",
            es_fuerza=False,
            distancia_km=10.0,
            cadencia_spm=170.0,
        )
    )
    j = report.construir(SEMANA, repo, store)
    assert j["cadencia"]["valor"] == 176.0
    assert j["cadencia"]["semana_anterior"] == 170.0
    assert j["cadencia"]["delta"] == 6.0


def test_json_deriva_promedio_y_maximo(entorno):
    repo, store = entorno
    repo.guardar_sesion(sesion(1, decoupling_pct=3.0, carga=90.0))
    repo.guardar_sesion(sesion(3, decoupling_pct=9.0, carga=90.0))
    j = report.construir(SEMANA, repo, store)
    assert j["deriva_cardiaca"]["promedio_pct"] == 6.0
    assert j["deriva_cardiaca"]["maximo_pct"] == 9.0
    assert "sobre el umbral" in j["deriva_cardiaca"]["alerta"]
    assert j["deriva_cardiaca"]["alerta"] in j["alertas"]


def test_json_sin_plan_cargado(entorno):
    repo, _ = entorno
    vacio = PlanStore(actual=repo.path.parent / "no.json", archivo=repo.path.parent / "nada")
    j = report.construir(SEMANA, repo, vacio)
    assert j["semana"]["plan_cargado"] is False
    assert any("no habia plan cargado" in a for a in j["avisos_datos"])


def test_json_es_serializable(entorno):
    import json

    repo, store = entorno
    repo.guardar_sesion(sesion(1, distancia_km=8.0, carga=90.0))
    texto = json.dumps(report.construir(SEMANA, repo, store), ensure_ascii=False)
    assert json.loads(texto)["version"] == report.VERSION_REPORTE
