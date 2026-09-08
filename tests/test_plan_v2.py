"""El plan pasa a v2 (lista de sesiones por dia) sin perder lo que ya hay en disco.

En la Pi existe un `plan_actual.json` en v1 y varios archivados. Que se sigan
leyendo no es una cortesia: si no, el reporte del lunes diria "no habia plan" y
la adherencia de esa semana saldria vacia.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from sport_report import config
from sport_report.fechas import semana_de
from sport_report.plan import migrar as m_migrar
from sport_report.plan.errors import PlanCorrupto
from sport_report.plan.grammar import parse_plan
from sport_report.plan.store import VERSION_FORMATO, PlanAnclado, PlanStore
from tests.test_grammar import PLAN_SPEC

SEMANA = semana_de(date(2026, 9, 7))

# Plan v1 tal como quedo guardado antes de fase 2: cada dia, un objeto.
PLAN_V1 = {
    "version": 1,
    "rango": {"inicio": "2026-09-07", "fin": "2026-09-13"},
    "cargado_en": "2026-09-07T07:00:00-04:00",
    "plan": {
        "semana": 5,
        "sesiones": {
            "L": {"dia": "L", "tipo": "rest", "zonas": [], "crudo": "rest"},
            "M": {
                "dia": "M",
                "tipo": "easy",
                "cantidad": 8.0,
                "unidad": "km",
                "zonas": ["Z2"],
                "crudo": "easy 8km Z2",
            },
            "W": {"dia": "W", "tipo": "fuerza", "zonas": [], "crudo": "fuerza"},
            "J": {
                "dia": "J",
                "tipo": "series",
                "cantidad": 8.6,
                "unidad": "km",
                "zonas": ["Z4"],
                "estructura": {
                    "crudo": "2km+3x1000m+4x400m+2km",
                    "bloques": [
                        {"reps": 1, "distancia_km": 2.0, "crudo": "2km"},
                        {"reps": 3, "distancia_km": 1.0, "crudo": "3x1000m"},
                        {"reps": 4, "distancia_km": 0.4, "crudo": "4x400m"},
                        {"reps": 1, "distancia_km": 2.0, "crudo": "2km"},
                    ],
                },
                "crudo": "series 8.6km @Z4 estructura=2km+3x1000m+4x400m+2km",
            },
            "V": {
                "dia": "V",
                "tipo": "easy",
                "cantidad": 45.0,
                "unidad": "min",
                "zonas": [],
                "crudo": "easy 45min",
            },
            "S": {"dia": "S", "tipo": "rest", "zonas": [], "crudo": "rest"},
            "D": {
                "dia": "D",
                "tipo": "long",
                "cantidad": 16.0,
                "unidad": "km",
                "zonas": ["Z2"],
                "crudo": "long 16km Z2",
            },
        },
        "fuerza_completada": {"W": True},
        "crudo": "semana: 5\nL: rest\n...",
    },
}


@pytest.fixture()
def entorno(tmp_path, monkeypatch):
    """Rutas de plan apuntando a tmp, como las mira `plan.migrar`."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "PLAN_ACTUAL_PATH", tmp_path / "plan_actual.json")
    monkeypatch.setattr(config, "PLANES_DIR", tmp_path / "planes")
    return tmp_path


def _escribir(ruta, d) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(d), encoding="utf-8")


# --------------------------------------------------------------------------
# Lectura de v1
# --------------------------------------------------------------------------


def test_un_plan_v1_se_sigue_leyendo(entorno):
    _escribir(entorno / "plan_actual.json", PLAN_V1)
    store = PlanStore(actual=entorno / "plan_actual.json", archivo=entorno / "planes")

    anclado = store.cargar()
    assert anclado is not None, "un plan v1 dejo de leerse: el lunes no habria plan"
    p = anclado.plan
    assert p.semana == 5
    # Cada dia queda con una tupla de una sola sesion.
    assert [x.tipo for x in p.sesiones["M"]] == ["easy"]
    assert p.sesiones["M"][0].cantidad == 8.0
    assert p.dias_fuerza() == ("W",)
    assert p.fuerza_completada == {"W": True}
    assert p.sesiones["J"][0].estructura.distancia_dura_km() == 8.6
    assert p.es_descanso("L") is True


def test_los_derivados_de_un_plan_v1_no_cambian(entorno):
    _escribir(entorno / "plan_actual.json", PLAN_V1)
    store = PlanStore(actual=entorno / "plan_actual.json", archivo=entorno / "planes")
    p = store.cargar().plan
    # 8 + 8.6 + 16 = 32.6 km escritos, mas el V estimado (45min a 7:00 = 6.43).
    assert p.volumen_planificado_km() == pytest.approx(39.03, abs=0.01)
    assert p.volumen_estimado_km() == pytest.approx(6.43, abs=0.01)


def test_al_reescribirlo_queda_en_v2(entorno):
    ruta = entorno / "plan_actual.json"
    _escribir(ruta, PLAN_V1)
    store = PlanStore(actual=ruta, archivo=entorno / "planes")

    store.marcar_fuerza("W", True, SEMANA)

    d = json.loads(ruta.read_text(encoding="utf-8"))
    assert d["version"] == VERSION_FORMATO == 2
    assert isinstance(d["plan"]["sesiones"]["M"], list)


def test_un_plan_v2_trae_las_dos_sesiones_del_dia(entorno):
    ruta = entorno / "plan_actual.json"
    store = PlanStore(actual=ruta, archivo=entorno / "planes")
    store.guardar(parse_plan(PLAN_SPEC + "J: fuerza\n"), SEMANA)

    d = json.loads(ruta.read_text(encoding="utf-8"))
    assert [x["tipo"] for x in d["plan"]["sesiones"]["J"]] == ["series", "fuerza"]
    # Y vuelve a entrar igual.
    assert [x.tipo for x in store.cargar().plan.sesiones["J"]] == ["series", "fuerza"]


def test_un_formato_futuro_se_rechaza_en_vez_de_interpretarse(entorno):
    futuro = dict(PLAN_V1, version=VERSION_FORMATO + 1)
    with pytest.raises(PlanCorrupto):
        PlanAnclado.from_json(futuro)


def test_a_un_plan_guardado_le_siguen_faltando_dias_si_falta_uno(entorno):
    roto = json.loads(json.dumps(PLAN_V1))
    del roto["plan"]["sesiones"]["S"]
    with pytest.raises(PlanCorrupto) as exc:
        PlanAnclado.from_json(roto)
    assert "faltan los dias S" in str(exc.value)


def test_un_dia_con_lista_vacia_cuenta_como_faltante(entorno):
    roto = json.loads(json.dumps(PLAN_V1))
    roto["plan"]["sesiones"]["S"] = []
    with pytest.raises(PlanCorrupto):
        PlanAnclado.from_json(roto)


# --------------------------------------------------------------------------
# La CLI de migracion
# --------------------------------------------------------------------------


def test_la_migracion_reescribe_el_vigente_y_los_archivados(entorno, capsys):
    _escribir(entorno / "plan_actual.json", PLAN_V1)
    _escribir(entorno / "planes" / "2026-08-31.json", PLAN_V1)
    _escribir(entorno / "planes" / "2026-08-24.json", PLAN_V1)

    assert m_migrar.main([]) == 0

    for ruta in (
        entorno / "plan_actual.json",
        entorno / "planes" / "2026-08-31.json",
        entorno / "planes" / "2026-08-24.json",
    ):
        d = json.loads(ruta.read_text(encoding="utf-8"))
        assert d["version"] == 2
        assert isinstance(d["plan"]["sesiones"]["J"], list)
    assert "migrados: 3" in capsys.readouterr().out


def test_el_dry_run_no_escribe_nada(entorno, capsys):
    ruta = entorno / "plan_actual.json"
    _escribir(ruta, PLAN_V1)
    antes = ruta.read_text(encoding="utf-8")

    assert m_migrar.main(["--dry-run"]) == 0

    assert ruta.read_text(encoding="utf-8") == antes
    salida = capsys.readouterr().out
    assert "dry-run" in salida and "se migrarian: 1" in salida


def test_volver_a_migrar_no_hace_nada(entorno, capsys):
    _escribir(entorno / "plan_actual.json", PLAN_V1)
    m_migrar.main([])
    capsys.readouterr()

    assert m_migrar.main([]) == 0
    salida = capsys.readouterr().out
    assert "ya en v2" in salida and "migrados: 0" in salida


def test_un_plan_corrupto_no_se_reescribe_y_se_reporta(entorno, capsys):
    roto = json.loads(json.dumps(PLAN_V1))
    del roto["plan"]["sesiones"]["S"]
    ruta = entorno / "plan_actual.json"
    _escribir(ruta, roto)
    antes = ruta.read_text(encoding="utf-8")

    assert m_migrar.main([]) == 1

    assert ruta.read_text(encoding="utf-8") == antes, "se reescribio un plan corrupto"
    assert "corrupto, se deja sin tocar" in capsys.readouterr().out


def test_sin_planes_guardados_no_falla(entorno, capsys):
    assert m_migrar.main([]) == 0
    assert "nada que migrar" in capsys.readouterr().out
