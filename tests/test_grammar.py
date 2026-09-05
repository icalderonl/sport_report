"""Tests del parser de /setplan. Cada caso mapea a una regla explicita de la spec."""
from __future__ import annotations

import pytest

from sport_report.plan.errors import PlanInvalido
from sport_report.plan.grammar import distancia_dura_km, parse_plan
from sport_report.plan.render import avisos, resumen

PLAN_SPEC = """/setplan
semana: 5
L: rest
M: easy 8km Z2
W: fuerza
J: series 8.6km @Z4 estructura=2km+3x1000m+4x400m+2km
V: prog 10km estructura=3km@6:00+3km@5:30+4km@5:00
S: rest
D: long 16km @6:15/5:45 Z2
"""


# --------------------------------------------------------------------------
# Plan de ejemplo de la spec
# --------------------------------------------------------------------------


def test_plan_de_la_spec_parsea():
    p = parse_plan(PLAN_SPEC)
    assert p.semana == 5
    assert set(p.sesiones) == {"L", "M", "W", "J", "V", "S", "D"}
    assert p.sesiones["L"].tipo == "rest"
    assert p.sesiones["W"].tipo == "fuerza"
    assert p.dias_fuerza() == ("W",)


def test_sesion_simple_con_zona():
    s = parse_plan(PLAN_SPEC).sesiones["M"]
    assert (s.tipo, s.cantidad, s.unidad, s.zonas) == ("easy", 8.0, "km", ("Z2",))
    assert s.ritmo is None
    assert s.objetivo_km() == 8.0


def test_ventana_de_ritmo():
    s = parse_plan(PLAN_SPEC).sesiones["D"]
    assert s.ritmo.min_s_km == 345  # 5:45
    assert s.ritmo.max_s_km == 375  # 6:15
    assert s.ritmo.es_ventana
    assert str(s.ritmo) == "6:15/5:45"


def test_zona_con_arroba_equivale_a_sin_arroba():
    a = parse_plan(PLAN_SPEC.replace("@Z4", "Z4")).sesiones["J"]
    b = parse_plan(PLAN_SPEC).sesiones["J"]
    assert a.zonas == b.zonas == ("Z4",)


def test_insensible_a_mayusculas():
    p = parse_plan(PLAN_SPEC.upper().replace("/SETPLAN", "/setplan"))
    assert p.semana == 5
    assert p.sesiones["M"].tipo == "easy"
    assert p.sesiones["J"].estructura.distancia_dura_km() == 8.6


# --------------------------------------------------------------------------
# Distancia dura (spec 3 y 7)
# --------------------------------------------------------------------------


def test_distancia_dura_series():
    # 2 + 3x1 + 4x0.4 + 2 = 8.6 km duros
    assert distancia_dura_km("2km+3x1000m+4x400m+2km") == 8.6


def test_distancia_dura_prog():
    assert distancia_dura_km("3km@6:00+3km@5:30+4km@5:00", "prog") == 10.0


def test_objetivo_usa_distancia_dura_no_la_cantidad_tecleada():
    # Caso real de la spec: plan de 8.6km duros; si se compara contra un
    # `cantidad` mal tecleado la adherencia sale distorsionada.
    p = parse_plan(PLAN_SPEC.replace("series 8.6km", "series 10km"))
    assert p.sesiones["J"].cantidad == 10.0
    assert p.sesiones["J"].objetivo_km() == 8.6


def test_aviso_si_cantidad_no_calza_con_estructura():
    p = parse_plan(PLAN_SPEC.replace("series 8.6km", "series 10km"))
    assert any("8.6km duros" in a for a in avisos(p))
    assert avisos(parse_plan(PLAN_SPEC)) == []


def test_volumen_planificado_se_deriva_de_las_sesiones():
    # 8 (M) + 8.6 (J, duros) + 10 (V) + 16 (D) = 42.6
    assert parse_plan(PLAN_SPEC).volumen_planificado_km() == 42.6


def test_alias_k_y_metros():
    assert distancia_dura_km("2k+5x800m") == 6.0


# --------------------------------------------------------------------------
# Rechazos (spec 3: rechazar el plan COMPLETO, con linea y dia)
# --------------------------------------------------------------------------


def _errores(texto: str) -> list:
    with pytest.raises(PlanInvalido) as exc:
        parse_plan(texto)
    return exc.value.errores


def test_estructura_sin_unidades_es_rechazada():
    errs = _errores(PLAN_SPEC.replace("2km+3x1000m+4x400m+2km", "2+4x600+2"))
    assert len(errs) == 1
    assert errs[0].dia == "J"
    assert errs[0].linea == 6
    assert "unidad" in errs[0].mensaje


def test_plan_incompleto_se_rechaza_entero():
    incompleto = "\n".join(l for l in PLAN_SPEC.splitlines() if not l.startswith("S:"))
    errs = _errores(incompleto)
    assert any("faltan dias" in e.mensaje and "S (sabado)" in e.mensaje for e in errs)


def test_no_se_asume_descanso_implicito():
    solo_dos = "semana: 1\nL: rest\nM: easy 5km\n"
    errs = _errores(solo_dos)
    assert any("faltan dias" in e.mensaje for e in errs)


def test_linea_basura_reporta_numero_de_linea():
    roto = PLAN_SPEC.replace("W: fuerza", "Mie: fuerza")
    errs = _errores(roto)
    assert errs[0].linea == 5
    assert "formato no reconocido" in errs[0].mensaje


def test_series_sin_estructura_es_rechazada():
    errs = _errores(PLAN_SPEC.replace(" estructura=2km+3x1000m+4x400m+2km", ""))
    assert errs[0].dia == "J"
    assert "requiere `estructura=`" in errs[0].mensaje


def test_simple_con_estructura_es_rechazada():
    errs = _errores(PLAN_SPEC.replace("M: easy 8km Z2", "M: easy 8km estructura=2km+2km"))
    assert errs[0].dia == "M"
    assert "no admite `estructura=`" in errs[0].mensaje


def test_prog_requiere_ritmo_por_bloque():
    errs = _errores(PLAN_SPEC.replace("3km@6:00+3km@5:30+4km@5:00", "3km+3km+4km"))
    assert errs[0].dia == "V"
    assert "su propio ritmo" in errs[0].mensaje


def test_ritmo_por_bloque_solo_en_prog():
    errs = _errores(PLAN_SPEC.replace("2km+3x1000m+4x400m+2km", "2km@5:00+3x1000m"))
    assert errs[0].dia == "J"
    assert "solo se admite en sesiones `prog`" in errs[0].mensaje


def test_cantidad_con_espacio_es_rechazada():
    errs = _errores(PLAN_SPEC.replace("easy 8km", "easy 8 km"))
    assert errs[0].dia == "M"
    assert "sin espacio" in errs[0].mensaje


def test_fuerza_no_admite_argumentos():
    errs = _errores(PLAN_SPEC.replace("W: fuerza", "W: fuerza 45min"))
    assert errs[0].dia == "W"


def test_rest_no_admite_argumentos():
    errs = _errores(PLAN_SPEC.replace("L: rest", "L: rest 5km"))
    assert errs[0].dia == "L"


def test_tipo_desconocido():
    errs = _errores(PLAN_SPEC.replace("M: easy 8km Z2", "M: cuesta 8km"))
    assert "no reconocido" in errs[0].mensaje


def test_volumen_objetivo_no_existe():
    errs = _errores(PLAN_SPEC.replace("semana: 5", "semana: 5\nvolumen_objetivo: 42km"))
    assert any("volumen" in e.mensaje for e in errs)


def test_dia_repetido():
    errs = _errores(PLAN_SPEC.replace("S: rest", "M: rest"))
    assert any("repetido" in e.mensaje for e in errs)


def test_sin_cabecera_semana():
    errs = _errores(PLAN_SPEC.replace("semana: 5\n", ""))
    assert "semana" in errs[0].mensaje


def test_zona_invalida():
    errs = _errores(PLAN_SPEC.replace("M: easy 8km Z2", "M: easy 8km Z9"))
    assert errs[0].dia == "M"


def test_ritmo_invalido():
    errs = _errores(PLAN_SPEC.replace("@6:15/5:45", "@6:75"))
    assert errs[0].dia == "D"


def test_se_reportan_todos_los_errores_juntos():
    roto = PLAN_SPEC.replace("easy 8km", "easy 8 km").replace("2km+3x1000m", "2+3x1000m")
    errs = _errores(roto)
    assert {e.dia for e in errs} == {"M", "J"}


def test_mensaje_de_error_incluye_linea_y_dia():
    with pytest.raises(PlanInvalido) as exc:
        parse_plan(PLAN_SPEC.replace("easy 8km", "easy 8 km"))
    texto = exc.value.render()
    assert "Linea 4" in texto and "Martes" in texto


# --------------------------------------------------------------------------
# Sesiones por tiempo y serializacion
# --------------------------------------------------------------------------


def test_sesion_en_minutos():
    p = parse_plan(PLAN_SPEC.replace("M: easy 8km Z2", "M: easy 45min Z2"))
    s = p.sesiones["M"]
    assert (s.unidad, s.objetivo_min(), s.objetivo_km()) == ("min", 45.0, None)


def test_roundtrip_json():
    d = parse_plan(PLAN_SPEC).to_json()
    assert d["semana"] == 5
    assert d["sesiones"]["J"]["estructura"]["distancia_dura_km"] == 8.6
    assert d["fuerza_completada"] == {"W": False}
    assert d["volumen_planificado_km"] == 42.6


def test_resumen_es_legible():
    texto = resumen(parse_plan(PLAN_SPEC), con_fuerza=True)
    assert "Semana 5" in texto
    assert "pendiente" in texto
    assert "8.6km duros" in texto
