"""Fatiga y descanso: tendencia de bienestar cruzada con la carga.

El test mas importante de este archivo es el que comprueba que NO existe un
score que fusione bienestar y carga. Todo lo demas gira alrededor de la misma
idea: un dato que la fuente no dio se reporta como faltante, con su motivo, y
nunca como un cero que despues alguien va a leer como si significara algo.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from sport_report.config import Bienestar
from sport_report.db.models import BienestarDia
from sport_report.engine import fatiga

LUNES = date(2026, 9, 7)
CFG = Bienestar(dias_minimos=3)


class ACWR:
    def __init__(self, ratio=1.32, confiable=True):
        self.ratio = ratio
        self.confiable = confiable


class Foster:
    def __init__(self, monotony=1.81, confiable=True):
        self.monotony = monotony
        self.confiable = confiable


def dias(n: int, inicio: date = LUNES, **valores) -> list[BienestarDia]:
    """n dias consecutivos con los mismos valores."""
    return [
        BienestarDia(
            fecha_local=(inicio + timedelta(days=i)).isoformat(),
            fuente="intervals",
            **valores,
        )
        for i in range(n)
    ]


def _calc(actual, previa=(), **kw):
    kw.setdefault("cfg", CFG)
    return fatiga.calcular(actual, previa, **kw)


# --------------------------------------------------------------------------
# Lo que no se puede hacer: fusionar bienestar y carga
# --------------------------------------------------------------------------


def test_no_existe_ningun_score_que_combine_bienestar_y_carga():
    """La spec lo prohibe explicitamente: no hay respaldo metodologico.

    Si alguien agrega un campo asi, este test cae y hay que discutirlo, no
    ajustarlo.
    """
    r = _calc(
        dias(7, hrv=68.0, readiness=71.0, sueno_h=7.5),
        dias(7, hrv=74.0, readiness=80.0, sueno_h=8.0, inicio=LUNES - timedelta(days=7)),
        r_acwr=ACWR(),
        r_foster=Foster(),
    )
    j = r.to_json()

    # Las claves de primer nivel son exactamente estas: cada metrica por su
    # lado, el descanso, y el cruce que solo enuncia. Un campo nuevo aca tiene
    # que ser una decision consciente, no un agregado que se cuela.
    # (`sueno_score` es el sleep score del reloj, un dato de la fuente, no un
    # indice calculado por este sistema.)
    assert set(j) == {
        "disponible",
        "motivo",
        "hrv",
        "hr_reposo",
        "sueno_h",
        "sueno_score",
        "readiness",
        "body_battery",
        "descanso",
        "cruce_carga",
    }
    # Y dentro del cruce no hay ningun numero que combine los dos lados.
    assert set(j["cruce_carga"]) == {
        "acwr",
        "acwr_confiable",
        "monotony",
        "monotony_confiable",
        # Los dos textos son plantillas que enuncian; ninguno es una cifra
        # nueva. `carga_texto` es solo el lado de la carga, para el mensaje.
        "lectura",
        "carga_texto",
        "nota",
    }

    # Los dos hechos estan, por separado, cada uno con su cifra.
    cruce = j["cruce_carga"]
    assert cruce["acwr"] == 1.32
    assert cruce["monotony"] == 1.81
    assert "HRV 68.0 ms" in cruce["lectura"]
    assert "ACWR 1.32" in cruce["lectura"] and "Monotony 1.81" in cruce["lectura"]
    assert "no existe un indice que los combine" in cruce["nota"]


def test_el_cruce_marca_lo_que_no_es_confiable():
    r = _calc(
        dias(5, hrv=60.0),
        r_acwr=ACWR(ratio=1.4, confiable=False),
        r_foster=Foster(monotony=2.1, confiable=False),
    )
    cruce = r.to_json()["cruce_carga"]
    assert cruce["acwr_confiable"] is False
    assert "no confiable" in cruce["lectura"]


def test_sin_bienestar_el_cruce_lo_dice_en_vez_de_callarse():
    r = _calc([], r_acwr=ACWR(), r_foster=Foster())
    cruce = r.to_json()["cruce_carga"]
    assert "sin bienestar con que cruzar" in cruce["lectura"]
    assert "ACWR 1.32" in cruce["lectura"]


# --------------------------------------------------------------------------
# Tendencias
# --------------------------------------------------------------------------


def test_promedio_y_delta_contra_la_semana_anterior():
    r = _calc(
        dias(7, hrv=64.0, hr_reposo=50.0),
        dias(7, hrv=70.0, hr_reposo=48.0, inicio=LUNES - timedelta(days=7)),
    )
    j = r.to_json()
    assert (j["hrv"]["valor"], j["hrv"]["semana_anterior"], j["hrv"]["delta"]) == (64.0, 70.0, -6.0)
    assert j["hrv"]["unidad"] == "ms"
    assert (j["hr_reposo"]["valor"], j["hr_reposo"]["delta"]) == (50.0, 2.0)
    assert r.disponible is True


def test_los_dias_sin_dato_no_cuentan_como_ceros():
    """Tres dias a 60 y cuatro sin medir dan 60, no 25.7."""
    con_dato = dias(3, hrv=60.0)
    sin_dato = dias(4, inicio=LUNES + timedelta(days=3), hr_reposo=48.0)
    j = _calc(con_dato + sin_dato).to_json()
    assert j["hrv"]["valor"] == 60.0
    assert j["hrv"]["n_dias"] == 3


def test_pocos_dias_no_son_una_tendencia():
    j = _calc(dias(2, hrv=60.0)).to_json()
    assert j["hrv"]["disponible"] is False
    assert j["hrv"]["valor"] == 60.0, "el promedio se muestra igual, marcado"
    assert "se necesitan 3" in j["hrv"]["motivo"]


def test_una_metrica_que_la_fuente_no_reporto_queda_en_null():
    j = _calc(dias(7, hrv=60.0)).to_json()
    assert j["body_battery"]["valor"] is None
    assert j["body_battery"]["disponible"] is False
    assert "no reporto este dato" in j["body_battery"]["motivo"]


def test_sin_semana_anterior_no_se_inventa_un_delta():
    j = _calc(dias(7, hrv=60.0)).to_json()
    assert j["hrv"]["semana_anterior"] is None
    assert j["hrv"]["delta"] is None
    assert "sin semana anterior" in _calc(dias(7, hrv=60.0), r_acwr=ACWR()).to_json()[
        "cruce_carga"
    ]["lectura"]


def test_el_sleep_score_se_reporta_aparte_de_las_horas():
    j = _calc(dias(7, sueno_h=7.5, sueno_score=82.0)).to_json()
    assert j["sueno_h"]["valor"] == 7.5 and j["sueno_h"]["unidad"] == "h"
    assert j["sueno_score"]["valor"] == 82.0


# --------------------------------------------------------------------------
# Semana degradada: el bienestar solo lo da intervals.icu
# --------------------------------------------------------------------------


def test_una_semana_por_el_respaldo_no_tiene_bienestar_y_lo_dice():
    r = _calc([], r_acwr=ACWR(), r_foster=Foster(), fuente_degradada=True)
    j = r.to_json()
    assert r.disponible is False
    assert "respaldo" in j["motivo"] and "Strava" in j["motivo"]
    # Todo en null, ni un cero.
    for clave in ("hrv", "hr_reposo", "sueno_h", "sueno_score", "readiness", "body_battery"):
        assert j[clave]["valor"] is None, clave
        assert j[clave]["disponible"] is False, clave


def test_aunque_hubiera_datos_viejos_la_semana_degradada_no_los_presenta_como_suyos():
    r = _calc(
        [], dias(7, hrv=70.0, inicio=LUNES - timedelta(days=7)), fuente_degradada=True
    )
    assert r.disponible is False
    assert r.to_json()["hrv"]["valor"] is None


def test_sin_bienestar_y_sin_respaldo_el_motivo_es_otro():
    """Distinguir "no lo pedimos" de "el reloj no lo midio" cambia que se revisa."""
    r = _calc([])
    assert r.disponible is False
    assert "sin datos de bienestar suficientes" in r.to_json()["motivo"]


# --------------------------------------------------------------------------
# Descanso
# --------------------------------------------------------------------------


def test_el_descanso_se_reporta_planificado_contra_tomado():
    j = _calc(
        dias(7, hrv=60.0),
        descanso_planificado=("L", "S"),
        descanso_tomado=("L",),
        descanso_roto=("S",),
    ).to_json()
    d = j["descanso"]
    assert (d["planificados"], d["tomados"]) == (2, 1)
    assert d["dias_rotos"] == ["S"]
    assert d["dias_extra"] == []


def test_el_descanso_extra_se_señala_porque_puede_ser_fatiga():
    r = _calc(
        dias(7, hrv=60.0),
        descanso_planificado=("L",),
        descanso_tomado=("L", "V"),
    )
    assert r.to_json()["descanso"]["dias_extra"] == ["V"]
    assert any("descanso no planificado" in a and "V" in a for a in r.alertas)


# --------------------------------------------------------------------------
# Alertas
# --------------------------------------------------------------------------


def test_una_caida_de_hrv_dispara_alerta():
    r = _calc(
        dias(7, hrv=62.0),
        dias(7, hrv=70.0, inicio=LUNES - timedelta(days=7)),
    )
    assert any("HRV bajo 8.0 ms" in a for a in r.alertas)


def test_una_caida_pequena_de_hrv_no_dispara_alerta():
    r = _calc(
        dias(7, hrv=68.0),
        dias(7, hrv=70.0, inicio=LUNES - timedelta(days=7)),
    )
    assert not any("HRV" in a for a in r.alertas)


def test_un_readiness_bajo_dispara_alerta_con_su_umbral():
    r = _calc(dias(7, readiness=32.0))
    assert any("Training Readiness promedio 32.0" in a and "40.0" in a for a in r.alertas)


def test_una_metrica_no_confiable_no_dispara_alerta():
    """Con dos dias de dato no hay tendencia de la que alertar."""
    r = _calc(
        dias(2, hrv=50.0),
        dias(7, hrv=70.0, inicio=LUNES - timedelta(days=7)),
    )
    assert not any("HRV" in a for a in r.alertas)


def test_los_umbrales_son_ajustables():
    cfg = Bienestar(dias_minimos=1, hrv_caida_ms=1.0, readiness_bajo=90.0)
    r = fatiga.calcular(dias(1, hrv=69.0, readiness=85.0), dias(1, hrv=70.0), cfg=cfg)
    assert any("HRV bajo 1.0 ms" in a for a in r.alertas)
    assert any("Readiness" in a for a in r.alertas)
