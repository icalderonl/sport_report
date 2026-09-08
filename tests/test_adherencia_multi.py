"""Adherencia cuando un dia tiene mas de una sesion planificada.

El caso que motiva todo esto es `J: easy 8km Z2` + `J: fuerza` el mismo jueves.
Lo delicado no es sumar: es no romper el comportamiento del dia con una sola
sesion (que es la enorme mayoria), no mezclar un check binario de fuerza con un
porcentaje de kilometros, y no inventar un objetivo cuando el dia mezcla km con
minutos y no hay con que estimarlos.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from sport_report.engine import adherencia
from sport_report.fechas import semana_de
from sport_report.plan.grammar import parse_plan
from tests.test_engine import VUELTAS_SERIES, sesion

LUNES = date(2026, 9, 7)
SEMANA = semana_de(LUNES)

# Base con los 7 dias; cada test le agrega las lineas que necesita.
BASE = """semana: 5
L: rest
M: easy 8km Z2
W: fuerza
J: series 8.6km @Z4 estructura=2km+3x1000m+4x400m+2km
V: easy 6km Z2
S: rest
D: long 16km @6:15/5:45 Z2
"""


def _adh(reales, extra: str = "", fuerza=None, vueltas=None, hasta=None):
    plan = parse_plan(BASE + extra)
    if fuerza:
        from dataclasses import replace

        plan = replace(plan, fuerza_completada=fuerza)
    return adherencia.calcular(plan, SEMANA, reales, vueltas=vueltas, hasta=hasta)


def _dia(r, letra: str):
    return next(d for d in r.dias if d.dia == letra)


# --------------------------------------------------------------------------
# Un dia con corrida + fuerza
# --------------------------------------------------------------------------


def test_la_corrida_manda_y_la_fuerza_va_en_su_propio_bloque(fuerza_extra="J: fuerza\n"):
    r = _adh([sesion(3, distancia_km=8.6)], extra=fuerza_extra, fuerza={"W": True, "J": True})
    d = _dia(r, "J")

    # El dia se sigue evaluando en km contra la corrida.
    assert (d.tipo_plan, d.unidad, d.objetivo, d.real) == ("series", "km", 8.6, 8.6)
    assert d.estado == adherencia.CUMPLIDA
    # Y la fuerza no se mezcla con ese porcentaje: va aparte.
    assert d.tipos_plan == ("series", "fuerza")
    assert d.fuerza == {"estado": adherencia.FUERZA_OK, "nota": ""}


def test_la_fuerza_pendiente_no_baja_el_porcentaje_de_la_corrida():
    r = _adh([sesion(3, distancia_km=8.6)], extra="J: fuerza\n", fuerza={"W": True})
    d = _dia(r, "J")
    assert d.estado == adherencia.CUMPLIDA
    assert d.fuerza["estado"] == adherencia.FUERZA_PENDIENTE
    # Pero si se cuenta como fuerza planificada no cumplida.
    assert (r.fuerza_planificadas, r.fuerza_cumplidas) == (2, 1)


def test_la_fuerza_de_un_dia_doble_cuenta_en_el_total_de_fuerza():
    r = _adh([sesion(3, distancia_km=8.6)], extra="J: fuerza\n", fuerza={"W": True, "J": True})
    assert (r.fuerza_planificadas, r.fuerza_cumplidas) == (2, 2)


def test_un_dia_solo_de_fuerza_sigue_reportandose_como_antes():
    r = _adh([], fuerza={"W": True})
    d = _dia(r, "W")
    assert (d.tipo_plan, d.estado) == ("fuerza", adherencia.FUERZA_OK)
    assert d.fuerza is None, "la fuerza sola va en `estado`, no duplicada en `fuerza`"


# --------------------------------------------------------------------------
# Dos corridas el mismo dia
# --------------------------------------------------------------------------


def test_dos_corridas_planificadas_se_suman_en_km():
    """4 + 4 reales contra 8 + 5 planificados."""
    r = _adh(
        [sesion(1, distancia_km=8.0), sesion(1, id_externo="999", distancia_km=5.0)],
        extra="M: easy 5km Z2\n",
    )
    d = _dia(r, "M")
    assert (d.objetivo, d.real, d.unidad) == (13.0, 13.0, "km")
    assert d.estado == adherencia.CUMPLIDA
    assert "2 sesiones planificadas, sumadas" in d.nota


def test_dos_corridas_en_minutos_se_suman_en_minutos():
    r = _adh(
        [
            sesion(1, duracion_mov_s=1800, distancia_km=None),
            sesion(1, id_externo="999", duracion_mov_s=1200, distancia_km=None),
        ],
        extra="M: easy 20min\n",
    )
    # El plan del martes en la BASE esta en km, asi que se reemplaza por minutos.
    r = adherencia.calcular(
        parse_plan(BASE.replace("M: easy 8km Z2", "M: easy 30min") + "M: easy 20min\n"),
        SEMANA,
        [
            sesion(1, duracion_mov_s=1800, distancia_km=None),
            sesion(1, id_externo="999", duracion_mov_s=1200, distancia_km=None),
        ],
    )
    d = _dia(r, "M")
    assert (d.unidad, d.objetivo, d.real) == ("min", 50.0, 50.0)
    assert d.estado == adherencia.CUMPLIDA


def test_un_dia_que_mezcla_km_y_minutos_se_evalua_en_km_y_lo_dice():
    """El de minutos se estima con el ritmo por defecto de `easy` (7:00)."""
    r = _adh(
        [sesion(1, distancia_km=8.0), sesion(1, id_externo="999", distancia_km=4.3)],
        extra="M: easy 30min\n",
    )
    d = _dia(r, "M")
    assert d.unidad == "km"
    # 8 km escritos + 30min a 7:00 = 4.29 km estimados.
    assert d.objetivo == pytest.approx(12.29, abs=0.01)
    assert "unidades distintas" in d.nota and "estimando" in d.nota


def test_si_una_sesion_no_se_puede_estimar_el_dia_es_dato_faltante():
    """Nunca incumplimiento: no hay con que construir el objetivo del dia."""
    # `tempo` no tiene ritmo por defecto, asi que 30min no se convierte a km.
    r = _adh(
        [sesion(1, distancia_km=8.0)],
        extra="M: tempo 30min\n",
    )
    d = _dia(r, "M")
    assert d.estado == adherencia.DATO_FALTANTE
    assert d.objetivo is None
    assert "no se puede estimar en km" in d.nota
    assert "no se compara para no inventar" in d.nota


def test_el_volumen_real_del_dia_suma_las_dos_corridas():
    r = _adh(
        [sesion(1, distancia_km=8.0), sesion(1, id_externo="999", distancia_km=5.0)],
        extra="M: easy 5km Z2\n",
    )
    assert r.volumen_real_km == pytest.approx(13.0)


def test_sin_ninguna_corrida_real_el_dia_doble_es_sin_sesion():
    r = _adh([], extra="M: easy 5km Z2\n")
    d = _dia(r, "M")
    assert (d.estado, d.pct) == (adherencia.SIN_SESION, 0.0)
    assert d.objetivo == 13.0


# --------------------------------------------------------------------------
# El porcentaje global cuenta sesiones, no dias
# --------------------------------------------------------------------------


def test_un_dia_con_dos_corridas_pesa_por_dos():
    r = _adh(
        [
            sesion(1, distancia_km=8.0),
            sesion(1, id_externo="999", distancia_km=5.0),
            sesion(3, distancia_km=8.6),
            sesion(4, distancia_km=6.0),
            sesion(6, distancia_km=16.0),
        ],
        extra="M: easy 5km Z2\n",
    )
    # Evaluables: M (x2), J, V, D = 5 sesiones, todas cumplidas.
    assert (r.sesiones_evaluables, r.sesiones_cumplidas) == (5, 5)
    assert r.pct_global == 100.0


def test_con_una_sesion_por_dia_el_conteo_es_el_de_siempre():
    """La red de seguridad del cambio de semantica."""
    r = _adh([sesion(1, distancia_km=8.0), sesion(3, distancia_km=8.6)])
    # Evaluables: M, J, V, D (4). Cumplidas: M, J.
    assert (r.sesiones_evaluables, r.sesiones_cumplidas) == (4, 2)
    assert r.pct_global == 50.0


def test_un_dia_doble_incumplido_pesa_por_dos_en_contra():
    r = _adh(
        [sesion(3, distancia_km=8.6), sesion(4, distancia_km=6.0), sesion(6, distancia_km=16.0)],
        extra="M: easy 5km Z2\n",
    )
    # M no se corrio: son 2 sesiones evaluables no cumplidas.
    assert (r.sesiones_evaluables, r.sesiones_cumplidas) == (5, 3)
    assert r.pct_global == 60.0


# --------------------------------------------------------------------------
# El descuento de recuperacion solo cuando hay con que emparejar
# --------------------------------------------------------------------------


def test_con_una_sola_sesion_estructurada_se_descuenta_como_antes():
    r = _adh(
        [sesion(3, distancia_km=10.0)],
        vueltas={("strava", "1003"): VUELTAS_SERIES},
    )
    d = _dia(r, "J")
    assert (d.objetivo, d.real) == (8.6, 8.6)
    assert d.recuperacion_km == pytest.approx(1.4, abs=0.01)


def test_la_fuerza_el_mismo_dia_no_estorba_el_descuento():
    r = _adh(
        [sesion(3, distancia_km=10.0), sesion(3, id_externo="998", tipo="WeightTraining", es_fuerza=True, distancia_km=None)],
        extra="J: fuerza\n",
        fuerza={"J": True},
        vueltas={("strava", "1003"): VUELTAS_SERIES},
    )
    d = _dia(r, "J")
    assert (d.objetivo, d.real) == (8.6, 8.6)
    assert d.fuerza["estado"] == adherencia.FUERZA_OK


def test_con_dos_corridas_planificadas_no_se_descuenta_y_se_dice():
    """No se sabe que vuelta pertenece a que sesion; adivinar seria peor."""
    r = _adh(
        [sesion(3, distancia_km=10.0), sesion(3, id_externo="998", distancia_km=5.0)],
        extra="J: easy 5km Z2\n",
        vueltas={("strava", "1003"): VUELTAS_SERIES},
    )
    d = _dia(r, "J")
    assert d.real == pytest.approx(15.0), "se descontó recuperacion sin poder emparejar"
    assert d.recuperacion_km is None
    assert "no se pudo separar la recuperacion" in d.nota


# --------------------------------------------------------------------------
# Semana en curso
# --------------------------------------------------------------------------


def test_un_dia_doble_pendiente_muestra_el_objetivo_agregado():
    r = _adh([], extra="M: easy 5km Z2\n", hasta=LUNES)
    d = _dia(r, "M")
    assert (d.estado, d.objetivo, d.unidad) == (adherencia.PENDIENTE, 13.0, "km")
    assert d.tipos_plan == ("easy", "easy")


def test_un_dia_pendiente_de_solo_fuerza_no_trae_objetivo():
    r = _adh([], hasta=LUNES)
    d = _dia(r, "W")
    assert (d.estado, d.objetivo, d.unidad) == (adherencia.PENDIENTE, None, None)


# --------------------------------------------------------------------------
# El descanso sigue siendo exclusivo
# --------------------------------------------------------------------------


def test_el_descanso_respetado_no_cambia():
    r = _adh([])
    assert _dia(r, "L").estado == adherencia.DESCANSO_OK


def test_una_actividad_en_dia_de_descanso_se_sigue_senalando():
    r = _adh([sesion(0, distancia_km=5.0)])
    d = _dia(r, "L")
    assert d.estado == adherencia.DESCANSO_ROTO
    assert r.no_planificadas == ("L",)
