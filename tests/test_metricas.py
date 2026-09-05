"""Tests de las metricas por sesion (carga TRIMP, deriva cardiaca, campos)."""
from __future__ import annotations

import pytest

from sport_report.strava import metricas
from sport_report.strava.metricas import (
    carga_trimp,
    cadencia_spm,
    deriva_cardiaca,
    distancia_km,
    indice_zona,
    potencia_w,
)

ZONAS = [
    {"min": 0, "max": 120},
    {"min": 120, "max": 140},
    {"min": 140, "max": 155},
    {"min": 155, "max": 170},
    {"min": 170, "max": -1},
]
PESOS = (1.0, 2.0, 3.0, 4.0, 5.0)


def stream_plano(n: int, hr: int, paso_s: int = 1, vel_ms: float = 3.0) -> dict:
    return {
        "time": list(range(0, n * paso_s, paso_s)),
        "heartrate": [hr] * n,
        "distance": [i * paso_s * vel_ms for i in range(n)],
        "moving": [True] * n,
    }


# --------------------------------------------------------------------------
# Zonas
# --------------------------------------------------------------------------


def test_indice_zona():
    assert indice_zona(100, ZONAS) == 0
    assert indice_zona(120, ZONAS) == 0  # el tope pertenece a la zona
    assert indice_zona(121, ZONAS) == 1
    assert indice_zona(155, ZONAS) == 2
    assert indice_zona(200, ZONAS) == 4  # la ultima zona no tiene tope


# --------------------------------------------------------------------------
# Carga TRIMP
# --------------------------------------------------------------------------


def test_carga_una_sola_zona():
    # 600 muestras a 1 Hz = 599 tramos de 1s ~ 9.98 min en Z2 (peso 2)
    r = carga_trimp(stream_plano(601, hr=130), ZONAS, PESOS, 2.5)
    assert r.carga == pytest.approx(20.0, abs=0.1)
    assert r.impreciso is False
    assert r.minutos_por_zona[1] == pytest.approx(10.0, abs=0.05)


def test_carga_mezcla_zonas():
    s = stream_plano(601, hr=130)
    s["heartrate"] = [130] * 301 + [175] * 300  # mitad Z2, mitad Z5
    r = carga_trimp(s, ZONAS, PESOS, 2.5)
    # ~5 min x2 + ~5 min x5 = ~35
    assert r.carga == pytest.approx(35.0, abs=0.2)


def test_carga_descuenta_pausas():
    """Un salto grande en el stream de tiempo es una pausa, no tiempo en zona."""
    s = stream_plano(301, hr=130)
    s["time"] = [t if i < 150 else t + 3600 for i, t in enumerate(s["time"])]
    r = carga_trimp(s, ZONAS, PESOS, 2.5)
    assert r.minutos_por_zona[1] == pytest.approx(299 / 60, abs=0.05)


def test_carga_descuenta_muestras_detenidas():
    s = stream_plano(601, hr=130)
    s["moving"] = [True] * 301 + [False] * 300
    r = carga_trimp(s, ZONAS, PESOS, 2.5)
    assert r.minutos_por_zona[1] == pytest.approx(5.0, abs=0.1)


def test_sin_stream_de_hr_la_carga_es_none():
    """Sin HR no hay carga: None, no 0. Un 0 hundiria el ACWR."""
    s = stream_plano(601, hr=130)
    del s["heartrate"]
    r = carga_trimp(s, ZONAS, PESOS, 2.5)
    assert r.carga is None and r.impreciso is False
    assert "sin stream de HR" in r.motivo


def test_sin_zonas_usa_peso_plano_y_marca_impreciso():
    r = carga_trimp(stream_plano(601, hr=130), None, PESOS, 2.5)
    assert r.carga == pytest.approx(10.0 * 2.5, abs=0.1)
    assert r.impreciso is True
    assert "zonas de HR no configuradas" in r.motivo


def test_carga_con_stream_a_baja_frecuencia():
    # Muchos relojes reportan cada 5s; el calculo usa el dt real, no el conteo
    # de muestras: 121 muestras cada 5s son 600s = 10 min, igual que 601 a 1 Hz.
    lento = carga_trimp(stream_plano(121, hr=130, paso_s=5), ZONAS, PESOS, 2.5)
    rapido = carga_trimp(stream_plano(601, hr=130, paso_s=1), ZONAS, PESOS, 2.5)
    assert lento.carga == pytest.approx(20.0, abs=0.1)
    assert lento.carga == pytest.approx(rapido.carga, abs=0.1)


# --------------------------------------------------------------------------
# Deriva cardiaca
# --------------------------------------------------------------------------


def test_sin_deriva_da_cero():
    assert deriva_cardiaca(stream_plano(1200, hr=140), 600) == pytest.approx(0.0, abs=0.1)


def test_deriva_positiva_cuando_sube_el_pulso_a_igual_ritmo():
    s = stream_plano(1200, hr=140)
    s["heartrate"] = [140] * 600 + [154] * 600  # +10% de HR, misma velocidad
    d = deriva_cardiaca(s, 600)
    assert d == pytest.approx(9.1, abs=0.5)


def test_deriva_negativa_cuando_mejora_la_eficiencia():
    s = stream_plano(1200, hr=140)
    s["heartrate"] = [154] * 600 + [140] * 600
    assert deriva_cardiaca(s, 600) < 0


def test_deriva_none_si_el_stream_es_corto():
    """Sesion corta o stream pobre -> None, nunca un numero inventado."""
    assert deriva_cardiaca(stream_plano(300, hr=140), 600) is None


def test_deriva_none_sin_distancia():
    s = stream_plano(1200, hr=140)
    del s["distance"]
    assert deriva_cardiaca(s, 600) is None


def test_deriva_none_sin_hr():
    s = stream_plano(1200, hr=140)
    del s["heartrate"]
    assert deriva_cardiaca(s, 600) is None


def test_deriva_none_si_no_hubo_desplazamiento():
    s = stream_plano(1200, hr=140, vel_ms=0.0)
    assert deriva_cardiaca(s, 600) is None


def test_deriva_ignora_muestras_nulas():
    s = stream_plano(1400, hr=140)
    s["heartrate"][700] = None
    assert deriva_cardiaca(s, 600) is not None


# --------------------------------------------------------------------------
# Campos de la actividad
# --------------------------------------------------------------------------


def test_cadencia_rpm_a_spm():
    assert cadencia_spm({"average_cadence": 87.5}) == 175.0
    assert cadencia_spm({}) is None
    assert cadencia_spm({"average_cadence": 0}) is None


def test_potencia_solo_si_la_reporta_el_dispositivo():
    assert potencia_w({"average_watts": 250, "device_watts": True}) == 250.0
    # Potencia estimada por Strava: no se usa, no se inventa.
    assert potencia_w({"average_watts": 250, "device_watts": False}) is None
    assert potencia_w({"device_watts": True}) is None


def test_distancia_cero_es_dato_faltante_no_cero():
    assert distancia_km({"distance": 10450}) == 10.45
    assert distancia_km({"distance": 0}) is None
    assert distancia_km({}) is None
