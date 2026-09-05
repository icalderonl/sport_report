"""Tests de persistencia del plan, anclaje a semana y comandos."""
from __future__ import annotations

from datetime import date, datetime

import pytest

from sport_report import fechas
from sport_report.config import TZ
from sport_report.fechas import semana_a_reportar, semana_de
from sport_report.plan.grammar import parse_plan
from sport_report.plan.store import DiaSinFuerza, PlanStore, resolver_rango
from sport_report.storage import escribir_json, leer_json, lock
from sport_report.telegram import comandos
from sport_report.telegram.formato import trozos
from tests.test_grammar import PLAN_SPEC


@pytest.fixture
def store(tmp_path) -> PlanStore:
    return PlanStore(actual=tmp_path / "plan_actual.json", archivo=tmp_path / "planes")


@pytest.fixture
def semana_fija(monkeypatch):
    """Congela 'hoy' en miercoles 2026-09-09 (semana 2026-09-07 a 2026-09-13)."""
    fijo = datetime(2026, 9, 9, 12, 0, tzinfo=TZ)
    monkeypatch.setattr(fechas, "ahora_local", lambda: fijo)
    monkeypatch.setattr(fechas, "hoy_local", lambda: fijo.date())
    return semana_de(fijo.date())


# --------------------------------------------------------------------------
# Semanas
# --------------------------------------------------------------------------


def test_semana_va_de_lunes_a_domingo():
    r = semana_de(date(2026, 9, 9))  # miercoles
    assert (r.inicio, r.fin) == (date(2026, 9, 7), date(2026, 9, 13))
    assert r.clave == "2026-09-07"


def test_el_lunes_se_reporta_la_semana_anterior():
    lunes = datetime(2026, 9, 14, 7, 0, tzinfo=TZ)
    assert semana_a_reportar(lunes) == semana_de(date(2026, 9, 7))


def test_fuera_del_lunes_se_reporta_la_semana_en_curso():
    jueves = datetime(2026, 9, 10, 20, 0, tzinfo=TZ)
    assert semana_a_reportar(jueves) == semana_de(date(2026, 9, 7))


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


def test_guardar_y_cargar(store, semana_fija):
    plan = parse_plan(PLAN_SPEC)
    guardado = store.guardar(plan, semana_fija)
    leido = store.cargar()
    assert leido.rango == semana_fija
    assert leido.plan.to_json() == guardado.plan.to_json()
    assert leido.cargado_en


def test_sin_plan_cargado_devuelve_none(store):
    assert store.cargar() is None


def test_cargar_plan_nuevo_archiva_el_anterior(store, semana_fija):
    anterior = fechas.semana_anterior(semana_fija)
    store.guardar(parse_plan(PLAN_SPEC), anterior)
    store.guardar(parse_plan(PLAN_SPEC.replace("semana: 5", "semana: 6")), semana_fija)

    assert store.cargar().plan.semana == 6
    # El plan de la semana cerrada sigue disponible para el reporte del lunes.
    recuperado = store.para_semana(anterior)
    assert recuperado is not None and recuperado.plan.semana == 5


def test_recargar_la_misma_semana_no_archiva(store, semana_fija):
    store.guardar(parse_plan(PLAN_SPEC), semana_fija)
    store.guardar(parse_plan(PLAN_SPEC.replace("semana: 5", "semana: 7")), semana_fija)
    assert store.cargar().plan.semana == 7
    assert not list((store.archivo).glob("*.json")) if store.archivo.exists() else True


def test_para_semana_sin_plan_devuelve_none(store, semana_fija):
    assert store.para_semana(semana_fija) is None


def test_setplan_proxima_ancla_a_la_semana_siguiente(semana_fija):
    assert resolver_rango("proxima") == fechas.semana_siguiente(semana_fija)
    assert resolver_rango("") == semana_fija


def test_setplan_con_sufijo_invalido():
    with pytest.raises(ValueError):
        resolver_rango("la que viene")


# --------------------------------------------------------------------------
# Fuerza
# --------------------------------------------------------------------------


def test_marcar_fuerza(store, semana_fija):
    store.guardar(parse_plan(PLAN_SPEC), semana_fija)
    assert store.cargar().plan.fuerza_completada.get("W") is not True
    a = store.marcar_fuerza("W")
    assert a.plan.fuerza_completada["W"] is True
    assert store.cargar().plan.fuerza_completada["W"] is True


def test_marcar_fuerza_en_dia_sin_fuerza(store, semana_fija):
    store.guardar(parse_plan(PLAN_SPEC), semana_fija)
    with pytest.raises(DiaSinFuerza):
        store.marcar_fuerza("L")


def test_marcar_fuerza_sin_plan(store):
    with pytest.raises(FileNotFoundError):
        store.marcar_fuerza("W")


def test_parse_dia_acepta_letra_y_nombre():
    assert comandos.parse_dia("W") == "W"
    assert comandos.parse_dia("miercoles") == "W"
    assert comandos.parse_dia("Miércoles") == "W"
    assert comandos.parse_dia("mie") == "W"
    assert comandos.parse_dia(" d ") == "D"


def test_parse_dia_invalido():
    with pytest.raises(ValueError):
        comandos.parse_dia("lunez")
    with pytest.raises(ValueError):
        comandos.parse_dia("")


# --------------------------------------------------------------------------
# Comandos
# --------------------------------------------------------------------------


def test_cmd_setplan_ok(store, semana_fija):
    r = comandos.cmd_setplan(store, PLAN_SPEC)
    assert "Plan cargado para la semana 2026-09-07 a 2026-09-13" in r
    assert "Semana 5" in r
    assert store.cargar() is not None


def test_cmd_setplan_rechaza_y_no_persiste(store, semana_fija):
    r = comandos.cmd_setplan(store, PLAN_SPEC.replace("easy 8km", "easy 8 km"))
    assert "Plan rechazado" in r and "Martes" in r
    assert store.cargar() is None


def test_cmd_setplan_no_pisa_el_plan_vigente_si_falla(store, semana_fija):
    comandos.cmd_setplan(store, PLAN_SPEC)
    comandos.cmd_setplan(store, PLAN_SPEC.replace("semana: 5", "semana: 9").replace("L: rest", ""))
    assert store.cargar().plan.semana == 5


def test_cmd_setplan_sin_cuerpo(store, semana_fija):
    assert "Falta el plan" in comandos.cmd_setplan(store, "/setplan")


def test_cmd_setplan_proxima(store, semana_fija):
    r = comandos.cmd_setplan(store, "/setplan proxima\n" + PLAN_SPEC.split("\n", 1)[1])
    assert "2026-09-14 a 2026-09-20" in r


def test_cmd_setplan_ignora_mencion_del_bot(store, semana_fija):
    r = comandos.cmd_setplan(store, "/setplan@mi_bot\n" + PLAN_SPEC.split("\n", 1)[1])
    assert "Plan cargado" in r


def test_cmd_plan_sin_plan(store):
    assert "No hay plan cargado" in comandos.cmd_plan(store)


def test_cmd_plan_muestra_estado_de_fuerza(store, semana_fija):
    comandos.cmd_setplan(store, PLAN_SPEC)
    assert "pendiente" in comandos.cmd_plan(store)
    comandos.cmd_fuerza(store, "miercoles")
    assert "cumplida" in comandos.cmd_plan(store)


def test_cmd_fuerza_dia_sin_fuerza(store, semana_fija):
    comandos.cmd_setplan(store, PLAN_SPEC)
    assert "no tiene fuerza" in comandos.cmd_fuerza(store, "L").lower()


def test_cmd_estado(store, semana_fija, monkeypatch):
    objetivo = semana_a_reportar(fechas.ahora_local())
    store.guardar(parse_plan(PLAN_SPEC), objetivo)
    assert "Plan encontrado: semana 5" in comandos.cmd_estado(store)


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


def test_escritura_atomica_no_deja_temporales(tmp_path):
    destino = tmp_path / "x.json"
    escribir_json(destino, {"a": 1})
    assert leer_json(destino) == {"a": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]


def test_leer_json_inexistente(tmp_path):
    assert leer_json(tmp_path / "nada.json") is None


def test_lock_se_libera(tmp_path):
    destino = tmp_path / "x.json"
    with lock(destino):
        assert destino.with_suffix(".json.lock").exists()
    assert not destino.with_suffix(".json.lock").exists()


def test_trozos_respeta_el_limite():
    texto = "\n".join(f"linea {i}" * 20 for i in range(200))
    partes = trozos(texto, limite=500)
    assert all(len(p) <= 500 for p in partes)
    assert "\n".join(partes) == texto


def test_trozos_corta_linea_muy_larga():
    partes = trozos("x" * 1200, limite=500)
    assert [len(p) for p in partes] == [500, 500, 200]


def test_trozos_mensaje_corto():
    assert trozos("hola") == ["hola"]
