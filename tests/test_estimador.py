"""Estimador de tiempo de carrera y la distancia en /carrera. Sin red."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from sport_report import run_weekly
from sport_report.db.models import Vuelta
from sport_report.db.repo import Repo
from sport_report.engine import estimador as m_est
from sport_report.engine import post_carrera as m_post
from sport_report.fechas import semana_de
from sport_report.plan.carrera import (
    MARATON_KM,
    MEDIA_MARATON_KM,
    Carrera,
    CarreraStore,
    distancia_del_nombre,
    parse_distancia,
)
from sport_report.plan.store import PlanStore
from sport_report.telegram import comandos
from tests.test_post_carrera import corrida, vueltas

HOY = date(2026, 11, 2)  # lunes
OBJETIVO = Carrera(fecha=date(2026, 11, 29), nombre="Fedachi", distancia_km=MEDIA_MARATON_KM)


@pytest.fixture()
def repo(tmp_path):
    r = Repo(tmp_path / "t.db")
    yield r
    r.cerrar()


# --------------------------------------------------------------------------
# Distancia de la carrera
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token,km",
    [("10k", 10.0), ("10K", 10.0), ("5km", 5.0), ("21k", MEDIA_MARATON_KM),
     ("21.1k", MEDIA_MARATON_KM), ("42k", MARATON_KM), ("42,195k", 42.195),
     ("Fedachi", None), ("0k", None), ("2026k", None)],
)
def test_parse_distancia(token, km):
    assert parse_distancia(token) == km


def test_distancia_del_nombre():
    assert distancia_del_nombre("Media Maratón de Santiago") == MEDIA_MARATON_KM
    assert distancia_del_nombre("Maraton de Santiago") == MARATON_KM
    assert distancia_del_nombre("Fedachi") is None


def test_carrera_con_distancia_por_comando(tmp_path):
    store = CarreraStore(tmp_path / "c.json")
    r = comandos.cmd_carrera(store, "2026-11-29 21k Fedachi", hoy=HOY)
    c = store.principal()
    assert c.nombre == "Fedachi" and c.distancia_km == MEDIA_MARATON_KM
    assert "(21.0975 km) — Fedachi" in r


def test_redeclarar_sin_distancia_no_la_borra(tmp_path):
    store = CarreraStore(tmp_path / "c.json")
    comandos.cmd_carrera(store, "2026-11-29 21k Fedachi", hoy=HOY)
    comandos.cmd_carrera(store, "2026-11-29 Fedachi 2026", hoy=HOY)
    c = store.principal()
    assert c.nombre == "Fedachi 2026" and c.distancia_km == MEDIA_MARATON_KM


def test_la_distancia_persiste(tmp_path):
    store = CarreraStore(tmp_path / "c.json")
    store.agregar(OBJETIVO)
    assert CarreraStore(tmp_path / "c.json").principal().distancia_km == MEDIA_MARATON_KM


# --------------------------------------------------------------------------
# Piezas
# --------------------------------------------------------------------------


def test_riegel_y_tanda():
    # 10K en 50:00 -> media con k=1.06: 3000 * 2.10975^1.06 = 6619 s.
    assert round(m_est.riegel(3000, 10, MEDIA_MARATON_KM, 1.06)) == 6619
    # Tanda devuelve ritmo (s/km): 100 km/sem a 4:30 -> ~4:08/km.
    assert round(m_est.tanda_ritmo(100, 270)) == 248


def test_mejor_tramo_reparte_la_ultima_vuelta():
    vs = [Vuelta("i", "x", i + 1, 1.0, r) for i, r in enumerate([300, 280, 280, 280, 300])]
    assert m_est.mejor_tramo(vs, 3) == 840
    assert m_est.mejor_tramo(vs, 2.5) == 700
    assert m_est.mejor_tramo(vs, 6) is None


# --------------------------------------------------------------------------
# Estimar
# --------------------------------------------------------------------------


def test_sin_distancia_lo_pide(repo):
    est = m_est.estimar(Carrera(fecha=OBJETIVO.fecha), repo, HOY)
    assert est["disponible"] is False and "21k" in est["motivo"]


def test_sin_datos_no_inventa(repo):
    est = m_est.estimar(OBJETIVO, repo, HOY)
    assert est["disponible"] is False and est["estimaciones"] == []


def test_desde_una_carrera_reciente(repo):
    previa = Carrera(fecha=HOY - timedelta(days=15), nombre="10K Vitacura", distancia_km=10)
    corrida(repo, previa.fecha, 10.0, seg_km=300)
    est = m_est.estimar(OBJETIVO, repo, HOY, carreras=[previa, OBJETIVO])
    p = est["principal"]
    # Factor 2.1 (de 10 a 21.1 km): reciente, pero mas de x2 -> media.
    assert p["origen"] == "carrera" and p["confianza"] == m_est.MEDIA
    assert p["central"] == "1:50:19"
    lo, hi = p["rango_s"]
    assert lo < p["central_s"] < hi
    assert p["referencia"]["detalle"] == "10K Vitacura"


def test_carrera_reciente_y_cercana_es_confianza_alta(repo):
    previa = Carrera(fecha=HOY - timedelta(days=15), distancia_km=15)
    corrida(repo, previa.fecha, 15.0, seg_km=300)
    p = m_est.estimar(OBJETIVO, repo, HOY, carreras=[previa])["principal"]
    assert p["confianza"] == m_est.ALTA


def test_una_carrera_vieja_no_cuenta(repo):
    previa = Carrera(fecha=HOY - timedelta(weeks=20), distancia_km=10)
    corrida(repo, previa.fecha, 10.0)
    est = m_est.estimar(OBJETIVO, repo, HOY, carreras=[previa])
    assert all(e["origen"] != "carrera" for e in est["estimaciones"])


def test_desde_entrenamiento_es_baja_y_conservador(repo):
    s = corrida(repo, HOY - timedelta(days=5), 12.0, seg_km=320)
    vueltas(repo, s, [320.0] * 2 + [290.0] * 8 + [320.0] * 2)
    est = m_est.estimar(OBJETIVO, repo, HOY)
    p = est["principal"]
    assert p["origen"] == "entrenamiento" and p["confianza"] == m_est.BAJA
    assert "conservador" in p["nota"]
    # El mejor tramo de 10 km: 8 vueltas a 4:50 y 2 a 5:20, el mas cercano a la media.
    assert p["referencia"]["distancia_km"] == 10.0 and p["referencia"]["ritmo"] == "4:56/km"


def test_no_extrapola_mas_alla_del_factor(repo):
    s = corrida(repo, HOY - timedelta(days=5), 3.0)
    vueltas(repo, s, [240.0] * 3)
    maraton = Carrera(fecha=OBJETIVO.fecha, distancia_km=MARATON_KM)
    est = m_est.estimar(maraton, repo, HOY)
    assert all(e["metodo"] != "riegel" for e in est["estimaciones"])


def test_la_cinta_no_es_referencia(repo):
    s = corrida(repo, HOY - timedelta(days=5), 10.0, tipo="VirtualRun")
    vueltas(repo, s, [270.0] * 10)
    assert m_est.estimar(OBJETIVO, repo, HOY)["disponible"] is False


def test_tanda_solo_para_maraton(repo):
    lunes = semana_de(HOY).inicio
    for i in range(1, 9):
        for d in (1, 3, 5, 6):
            corrida(repo, lunes - timedelta(weeks=i) + timedelta(days=d), 12.5, seg_km=330)
    maraton = Carrera(fecha=OBJETIVO.fecha, nombre="Maraton", distancia_km=MARATON_KM)
    t = next(e for e in m_est.estimar(maraton, repo, HOY)["estimaciones"] if e["metodo"] == "tanda")
    assert t["referencia"]["km_semana"] == 50 and t["referencia"]["ritmo_medio"] == "5:30/km"
    # 17.1 + 140*exp(-0.265) + 0.55*330 = 306.0 s/km -> 3:35:12
    assert t["central"] == "3:35:12" and t["confianza"] == m_est.MEDIA
    media = m_est.estimar(OBJETIVO, repo, HOY)
    assert all(e["metodo"] != "tanda" for e in media["estimaciones"])


def test_la_carrera_va_antes_que_el_entrenamiento(repo):
    previa = Carrera(fecha=HOY - timedelta(days=15), distancia_km=10)
    corrida(repo, previa.fecha, 10.0, seg_km=300)
    s = corrida(repo, HOY - timedelta(days=5), 10.0, seg_km=280)
    vueltas(repo, s, [280.0] * 10)
    est = m_est.estimar(OBJETIVO, repo, HOY, carreras=[previa])
    assert [e["origen"] for e in est["estimaciones"]] == ["carrera", "entrenamiento"]


def test_comparar():
    est = {
        "disponible": True,
        "principal": {
            "metodo": "riegel", "origen": "carrera", "central_s": 6600,
            "central": "1:50:00", "rango": "1:48:00-1:53:00", "rango_s": [6480, 6780],
        },
    }
    c = m_est.comparar(est, 6540)
    assert c["diferencia"] == "-1:00" and c["diferencia_pct"] == -0.9
    assert c["dentro_del_rango"]


# --------------------------------------------------------------------------
# Donde aparece
# --------------------------------------------------------------------------


def test_comando_estimar(repo, tmp_path):
    store = CarreraStore(tmp_path / "c.json")
    assert "No hay carrera principal" in comandos.cmd_estimar(store, repo=repo, hoy=HOY)
    store.agregar(OBJETIVO)
    s = corrida(repo, HOY - timedelta(days=5), 10.0, seg_km=300)
    vueltas(repo, s, [300.0] * 10)
    r = comandos.cmd_estimar(store, repo=repo, hoy=HOY)
    assert r.startswith("Estimado para Fedachi (21.0975 km, 2026-11-29)")
    assert "confianza baja" in r


def test_el_semanal_trae_el_estimado_fuera_de_la_narrativa(repo, tmp_path):
    store = CarreraStore(tmp_path / "c.json")
    store.agregar(OBJETIVO)
    s = corrida(repo, HOY - timedelta(days=5), 10.0, seg_km=300)
    vueltas(repo, s, [300.0] * 10)
    vistos = []

    def narrador(datos):
        vistos.append("estimacion_carrera" in datos)
        from sport_report.narrative.claude import Narrativa

        return Narrativa(texto="ok", modelo="m")

    r = run_weekly.ejecutar(
        semana_de(HOY - timedelta(days=1)),
        repo,
        PlanStore(actual=tmp_path / "p.json", archivo=tmp_path / "planes"),
        narrador=narrador,
        enviador=None,
        guardar=False,
        carrera_store=store,
    )
    assert vistos == [False]
    assert r.datos["estimacion_carrera"]["disponible"]
    assert "Estimado para Fedachi" in r.mensaje


def test_el_post_carrera_compara_con_el_estimado_de_la_vispera(repo, tmp_path):
    previa = Carrera(fecha=OBJETIVO.fecha - timedelta(days=20), distancia_km=10)
    corrida(repo, previa.fecha, 10.0, seg_km=300)
    corrida(repo, OBJETIVO.fecha, MEDIA_MARATON_KM, seg_km=310)
    store = PlanStore(actual=tmp_path / "p.json", archivo=tmp_path / "planes")
    j = m_post.construir(OBJETIVO, True, repo, store, carreras=[previa, OBJETIVO])
    ev = j["estimado_vs_real"]
    assert ev["disponible"] and ev["origen"] == "carrera"
    # Real 21.0975 km a 5:10 = 1:49:00, 1.2% mas rapido que lo estimado.
    assert ev["estimado"] == "1:50:19" and ev["diferencia_pct"] == -1.2


def test_tanda_fuera_de_la_muestra_baja_la_confianza(repo):
    lunes = semana_de(HOY).inicio
    for i in range(1, 9):
        for d in (1, 3, 5, 6):
            corrida(repo, lunes - timedelta(weeks=i) + timedelta(days=d), 12.5, seg_km=360)
    maraton = Carrera(fecha=OBJETIVO.fecha, distancia_km=MARATON_KM)
    t = next(e for e in m_est.estimar(maraton, repo, HOY)["estimaciones"] if e["metodo"] == "tanda")
    assert t["confianza"] == m_est.BAJA
    assert "6:00/km" in t["fuera_de_muestra"][0]
    # Rango: +-2 errores estandar de 5.77 s/km sobre 42.195 km = +-487 s.
    lo, hi = t["rango_s"]
    assert round((hi - lo) / 2) == 487
