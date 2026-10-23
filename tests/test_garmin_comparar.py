"""Tests de la comparacion Garmin vs intervals.icu (sin red)."""
from __future__ import annotations

from sport_report.garmin import comparar as c

INTERVALS = {
    "start_date_local": "2026-10-21T07:00:05",
    "name": "Fondo",
    "type": "Run",
    "distance": 10000.0,
    "moving_time": 3000,
    "average_heartrate": 150,
    "max_heartrate": 170,
    "average_cadence": 85,  # rpm de una pierna: se duplica
    "average_stance_time": 287.3,
    "average_vertical_oscillation": 103.6,  # mm: se pasa a cm
    "average_vertical_ratio": 12.91,
}
GARMIN = {
    "startTimeLocal": "2026-10-21 07:00:00",
    "activityType": {"typeKey": "running"},
    "distance": 10010.0,
    "movingDuration": 3002.0,
    "averageHR": 150.0,
    "maxHR": 170.0,
    "averageRunningCadenceInStepsPerMinute": 170.0,
    "avgGroundContactTime": 287.29998779296875,
    "avgVerticalOscillation": 10.35999984741211,
    "avgVerticalRatio": 12.90999984741211,
}


def test_empareja_por_hora_de_inicio_y_deja_los_sueltos():
    otra_g = {**GARMIN, "startTimeLocal": "2026-10-22 18:00:00"}
    otra_i = {**INTERVALS, "start_date_local": "2026-10-20T07:00:00"}
    pares, solo_i, solo_g = c.emparejar([INTERVALS, otra_i], [otra_g, GARMIN])
    assert pares == [(INTERVALS, GARMIN)]
    assert solo_i == [otra_i] and solo_g == [otra_g]


def test_misma_corrida_con_unidades_distintas_coincide():
    filas = {f.metrica: f for f in c.comparar_corrida(INTERVALS, GARMIN)}
    assert all(f.estado == c.OK for f in filas.values()), filas
    assert filas["cadencia_spm"].intervals == 170
    assert filas["oscilacion_cm"].intervals == 10.36


def test_diferencia_y_falta_se_distinguen():
    g = {**GARMIN, "averageHR": 155.0, "avgGroundContactTime": None}
    filas = {f.metrica: f for f in c.comparar_corrida(INTERVALS, g)}
    assert filas["fc_promedio"].estado == c.DIFERENCIA
    assert filas["gct_ms"].estado == c.FALTA


def test_cinta_cuenta_como_corrida():
    assert c.es_corrida_garmin({"activityType": {"typeKey": "treadmill_running"}})
    assert not c.es_corrida_garmin({"activityType": {"typeKey": "elliptical"}})


def test_bienestar_por_fecha():
    b_g = c.bienestar_garmin(
        {"hrvSummaries": [{"calendarDate": "2026-10-21", "lastNightAvg": 43}]},
        [{"calendarDate": "2026-10-21", "sleepTimeSeconds": 27900,
          "sleepScores": {"overall": {"value": 81}}}],
        [{"calendarDate": "2026-10-21", "heartRate": {"restingValue": 57},
          "bodyBattery": {"maxValue": 75}}],
    )
    assert b_g["2026-10-21"] == {
        "hrv": 43, "hr_reposo": 57, "sueno_h": 7.75, "sueno_score": 81, "body_battery": 75,
    }
    filas = c.comparar_bienestar(
        [{"id": "2026-10-21", "hrv": 43, "restingHR": 57, "sleepSecs": 27900,
          "sleepScore": 81, "bodyBattery": 60}],
        b_g,
    )["2026-10-21"]
    estados = {f.metrica: (f.estado, f.nota) for f in filas}
    assert estados["hrv"] == (c.OK, "")
    assert estados["body_battery"] == (c.DIFERENCIA, "referencial")


class _FalsoIntervals:
    def actividades(self, desde, hasta):
        return [INTERVALS, {"type": "Ride", "start_date_local": "2026-10-21T09:00:00"}]

    def bienestar(self, desde, hasta):
        return [{"id": "2026-10-21", "hrv": 43, "restingHR": 57}]

    def zonas_hr(self):
        return [{"min": 0, "max": 128}, {"min": 128, "max": 141}]

    def cerrar(self):
        pass


class _FalsoGarmin:
    def actividades(self, desde, hasta):
        return [GARMIN]

    def hrv(self, desde, hasta):
        return {"hrvSummaries": [{"calendarDate": "2026-10-21", "lastNightAvg": 43}]}

    def sueno(self, desde, hasta):
        return []

    def resumenes_diarios(self, desde, hasta):
        return [{"calendarDate": "2026-10-21", "heartRate": {"restingValue": 57}}]

    def zonas_hr(self):
        return [{"sport": "RUNNING", "zone1Floor": 91, "maxHeartRateUsed": 182}]

    def cerrar(self):
        pass


def test_comparar_de_punta_a_punta(capsys):
    assert c.comparar(30, cli_i=_FalsoIntervals(), cli_g=_FalsoGarmin()) == 0
    salida = capsys.readouterr().out
    assert "emparejadas=1" in salida
    assert "10/10 comparaciones dentro de tolerancia" in salida
    assert "Garmin RUNNING" in salida
