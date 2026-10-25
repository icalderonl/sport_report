"""Garmin como respaldo: FIT -> streams/vueltas, actividades, zonas y bienestar."""
from __future__ import annotations

import io
import zipfile
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from sport_report import config, fuentes
from sport_report.db.models import BienestarDia
from sport_report.db.repo import Repo
from sport_report.diagnostico import AVISO, FALLA, OK, estado_clave_garmin
from sport_report.garmin import fit
from sport_report.garmin.auth import RUTA_TOKEN, dias_para_vencer
from sport_report.garmin.client import GarminClient
from sport_report.garmin.errors import GarminError
from sport_report.garmin.ingest import (
    Ingesta,
    normalizar_bienestar,
    tipo_de,
    zonas_desde_garmin,
)
from sport_report.plan.store import PlanStore

garmin_fit_sdk = pytest.importorskip("garmin_fit_sdk")

T0 = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
TOKEN = "a.b.c"


def _fit(n: int = 600, hr: bool = True) -> bytes:
    """Un FIT real (del SDK oficial): n segundos a 3 m/s y dos vueltas."""
    M = garmin_fit_sdk.Profile["mesg_num"]
    e = garmin_fit_sdk.Encoder()
    e.on_mesg(M["FILE_ID"], {
        "type": "activity", "manufacturer": "garmin", "product": 1,
        "serial_number": 1, "time_created": T0,
    })
    for i in range(n):
        r = {
            "timestamp": T0 + timedelta(seconds=i), "distance": 3.0 * i,
            "cadence": 85, "fractional_cadence": 0.5, "enhanced_speed": 3.0,
        }
        if hr:
            r["heart_rate"] = 150
        e.on_mesg(M["RECORD"], r)
    mitad = n // 2
    for k, (ini, fin) in enumerate([(0, mitad), (mitad, n)]):
        e.on_mesg(M["LAP"], {
            "message_index": k, "timestamp": T0 + timedelta(seconds=fin),
            "start_time": T0 + timedelta(seconds=ini),
            "total_distance": 3.0 * (fin - ini),
            "total_timer_time": float(fin - ini), "total_elapsed_time": float(fin - ini),
        })
    return e.close()


def _zip(datos: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("999_ACTIVITY.fit", datos)
    return buf.getvalue()


# --------------------------------------------------------------------------
# FIT
# --------------------------------------------------------------------------


def test_el_fit_da_streams_y_vueltas_con_el_vocabulario_de_strava():
    a = fit.leer(_zip(_fit(10)))

    assert a.streams["time"][:3] == [0.0, 1.0, 2.0]
    assert a.streams["heartrate"][0] == 150
    assert a.streams["distance"][-1] == 27.0
    # rpm de una pierna + fraccion, por dos.
    assert a.streams["cadence"][0] == 171.0
    assert "watts" not in a.streams  # el reloj no midio potencia
    assert [v["lap_index"] for v in a.vueltas] == [1, 2]
    assert a.vueltas[0]["distance"] == 15.0 and a.vueltas[0]["moving_time"] == 5.0


def test_el_fit_suelto_tambien_se_lee():
    assert fit.leer(_fit(5)).streams["time"][-1] == 4.0


def test_un_archivo_que_no_es_fit_es_error_de_la_fuente():
    with pytest.raises(GarminError):
        fit.leer(b"<html>no</html>" * 4)
    with pytest.raises(GarminError):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("leeme.txt", "x")
        fit.leer(buf.getvalue())


# --------------------------------------------------------------------------
# Traducciones
# --------------------------------------------------------------------------


def test_tipos_de_garmin_al_vocabulario_de_intervals():
    assert tipo_de({"activityType": {"typeKey": "running"}}) == "Run"
    assert tipo_de({"activityType": {"typeKey": "treadmill_running"}}) == "VirtualRun"
    assert tipo_de({"activityType": {"typeKey": "trail_running"}}) == "TrailRun"
    assert tipo_de({"activityType": {"typeKey": "strength_training"}}) == "WeightTraining"
    assert tipo_de({"activityType": {"typeKey": "elliptical"}}) == "Elliptical"
    assert tipo_de({"activityType": {"typeKey": "pilates"}}) == "pilates"
    assert tipo_de({}) == "Desconocido"
    for clave in ("running", "treadmill_running", "trail_running"):
        assert tipo_de({"activityType": {"typeKey": clave}}) in config.TIPOS_RUN
    assert "WeightTraining" in config.TIPOS_FUERZA


def test_zonas_de_garmin_pisos_a_techos():
    crudo = [
        {"sport": "DEFAULT", "zone1Floor": 1, "zone2Floor": 2, "zone3Floor": 3,
         "zone4Floor": 4, "zone5Floor": 5, "maxHeartRateUsed": 6},
        {"sport": "RUNNING", "zone1Floor": 91, "zone2Floor": 128, "zone3Floor": 141,
         "zone4Floor": 155, "zone5Floor": 168, "maxHeartRateUsed": 182},
    ]
    z = zonas_desde_garmin(crudo)
    # Lo que quedo cargado en intervals.icu: los mismos techos.
    assert [x["max"] for x in z] == [127, 140, 154, 167, 182]
    assert z[0]["min"] == 0 and z[1]["min"] == 127

    assert [x["max"] for x in zonas_desde_garmin(crudo[:1])] == [1, 2, 3, 4, 6]
    assert zonas_desde_garmin([]) is None
    assert zonas_desde_garmin([{"sport": "RUNNING", "zone1Floor": 90}]) is None


def test_bienestar_suma_el_sueno_sin_medir_como_intervals():
    d = normalizar_bienestar(
        "2026-09-27",
        {"lastNightAvg": 45},
        {"sleepTimeSeconds": 25200, "unmeasurableSleepSeconds": 1800,
         "sleepScores": {"overall": {"value": 81}}},
        {"heartRate": {"restingValue": 47}, "bodyBattery": {"highestValue": 90}},
    )
    assert (d.hrv, d.hr_reposo, d.sueno_h, d.sueno_score) == (45.0, 47.0, 7.5, 81.0)
    assert d.fuente == config.GARMIN
    assert d.body_battery is None and d.readiness is None

    vacio = normalizar_bienestar("2026-09-28", None, {"unmeasurableSleepSeconds": 600}, None)
    assert vacio.sueno_h is None and vacio.vacio


# --------------------------------------------------------------------------
# La ingesta contra la API (simulada)
# --------------------------------------------------------------------------


ACTIVIDADES = [
    {"activityId": 1, "activityName": "Rodaje", "activityType": {"typeKey": "running"},
     "startTimeLocal": "2026-10-13 09:00:00", "distance": 10000.0,
     "movingDuration": 3000.0, "elapsedDuration": 3100.0, "averageHR": 150.0,
     "maxHR": 170.0, "averageRunningCadenceInStepsPerMinute": 172.0,
     "avgGroundContactTime": 240.5, "avgVerticalOscillation": 8.7,
     "avgVerticalRatio": 7.456},
    {"activityId": 2, "activityName": "Cinta", "activityType": {"typeKey": "treadmill_running"},
     "startTimeLocal": "2026-10-14 19:00:00", "distance": 39.0, "movingDuration": 60.0,
     "averageRunningCadenceInStepsPerMinute": 10.7},
    {"activityId": 3, "activityName": "Gym", "activityType": {"typeKey": "strength_training"},
     "startTimeLocal": "2026-10-15 19:00:00", "movingDuration": 1800.0},
    # Sin GPS: Garmin no da movingDuration, solo el cronometro.
    {"activityId": 5, "activityName": "Ciclismo en sala", "activityType": {"typeKey": "indoor_cycling"},
     "startTimeLocal": "2026-10-16 19:00:00", "duration": 2701.4, "elapsedDuration": 2750.0},
    # Fuera de la semana: el buscador por fecha la puede devolver igual.
    {"activityId": 4, "activityType": {"typeKey": "running"},
     "startTimeLocal": "2026-10-20 07:00:00", "distance": 5000.0},
]


def _api(pedidos: list[str], bienestar_falla: bool = False):
    def handler(request: httpx.Request) -> httpx.Response:
        ruta = request.url.path
        pedidos.append(ruta)
        if ruta == RUTA_TOKEN:
            return httpx.Response(200, text=TOKEN)
        if ruta.endswith("/search/activities"):
            inicio = int(request.url.params.get("start", 0))
            return httpx.Response(200, json=ACTIVIDADES[inicio:])
        if "/files/activity/" in ruta:
            return httpx.Response(200, content=_zip(_fit(600, hr=not ruta.endswith("/2"))))
        if ruta.endswith("/heartRateZones/v2"):
            return httpx.Response(200, json=[
                {"sport": "RUNNING", "zone1Floor": 91, "zone2Floor": 128, "zone3Floor": 141,
                 "zone4Floor": 155, "zone5Floor": 168, "maxHeartRateUsed": 182},
            ])
        if bienestar_falla and ("/hrv/" in ruta or "sleep" in ruta or "dailysummaries" in ruta):
            return httpx.Response(500)
        if "/hrv/daily/" in ruta:
            return httpx.Response(200, json={"hrvSummaries": [
                {"calendarDate": "2026-10-13", "lastNightAvg": 50},
                {"calendarDate": "2026-10-14", "lastNightAvg": 52},
            ]})
        if ruta.endswith("/dailySleepSummariesByDate"):
            return httpx.Response(200, json=[
                {"calendarDate": "2026-10-13", "sleepTimeSeconds": 25200,
                 "unmeasurableSleepSeconds": 0, "sleepScores": {"overall": {"value": 80}}},
            ])
        if ruta.endswith("/v2/dailysummaries"):
            return httpx.Response(200, json={"data": [
                {"calendarDate": "2026-10-13", "heartRate": {"restingValue": 46}},
            ]})
        return httpx.Response(404)

    return handler


def _ingesta(tmp_path, repo, pedidos, **kw) -> Ingesta:
    cliente = GarminClient(
        clave_api="clave",
        http=httpx.Client(transport=httpx.MockTransport(_api(pedidos, **kw))),
        dormir=lambda s: None,
        base="https://garmin.test",
    )
    return Ingesta(
        cliente=cliente, repo=repo,
        plan_store=PlanStore(tmp_path / "plan.json", tmp_path / "planes"),
    )


@pytest.fixture()
def repo(tmp_path):
    with Repo(tmp_path / "t.db") as r:
        yield r


def test_la_semana_por_garmin_trae_todo_lo_que_trae_intervals(tmp_path, repo):
    pedidos: list[str] = []
    r = _ingesta(tmp_path, repo, pedidos).sincronizar(date(2026, 10, 12), date(2026, 10, 18))

    assert (r.actividades, r.corridas, r.fuerza, r.otras) == (4, 2, 1, 1)
    assert r.zonas_origen == "garmin" and not r.carga_imprecisa
    assert r.sin_dinamica == 1  # la cinta

    s = {x.id_externo: x for x in repo.sesiones_entre(date(2026, 10, 12), date(2026, 10, 18))}
    assert set(s) == {"1", "2", "3", "5"}  # la del 20 quedo fuera
    rodaje = s["1"]
    assert rodaje.fuente == config.GARMIN and rodaje.tipo == "Run"
    assert rodaje.fecha_local == "2026-10-13" and rodaje.dia_semana == "M"
    assert (rodaje.distancia_km, rodaje.duracion_mov_s, rodaje.duracion_tot_s) == (10.0, 3000, 3100)
    assert (rodaje.gct_ms, rodaje.oscilacion_vertical_cm, rodaje.ratio_vertical_pct) == (240.5, 8.7, 7.46)
    assert rodaje.cadencia_spm == 172.0
    assert rodaje.carga and rodaje.carga > 0 and not rodaje.carga_impreciso
    assert len(repo.vueltas(config.GARMIN, "1")) == 2
    # Una corrida corta no se filtra (decision de Ignacio), y sin HR no hay carga.
    assert s["2"].distancia_km == 0.039 and s["2"].carga is None
    # La cadencia de Garmin ya es spm: una baja no se duplica.
    assert s["2"].cadencia_spm == 10.7
    assert s["3"].es_fuerza and s["3"].distancia_km is None
    assert (s["5"].tipo, s["5"].duracion_mov_s, s["5"].duracion_tot_s) == ("VirtualRide", 2701, 2750)

    # El FIT se baja una sola vez por actividad, aunque de streams y vueltas.
    assert pedidos.count("/download-service/files/activity/1") == 1

    b = {d.fecha_local: d for d in repo.bienestar_entre(date(2026, 10, 5), date(2026, 10, 18))}
    assert b["2026-10-13"].hrv == 50.0 and b["2026-10-13"].hr_reposo == 46.0
    assert b["2026-10-13"].sueno_h == 7.0 and b["2026-10-14"].hrv == 52.0
    assert r.bienestar_dias == 2


def test_garmin_no_pisa_el_bienestar_de_intervals(tmp_path, repo):
    repo.guardar_bienestar([BienestarDia(
        fecha_local="2026-10-13", fuente=config.INTERVALS, hrv=60.0, readiness=70.0,
    )])
    r = _ingesta(tmp_path, repo, []).sincronizar(date(2026, 10, 12), date(2026, 10, 18))

    b = {d.fecha_local: d for d in repo.bienestar_entre(date(2026, 10, 12), date(2026, 10, 18))}
    assert (b["2026-10-13"].fuente, b["2026-10-13"].hrv) == (config.INTERVALS, 60.0)
    assert b["2026-10-14"].fuente == config.GARMIN
    assert r.bienestar_dias == 1


def test_si_falla_el_bienestar_las_actividades_quedan(tmp_path, repo):
    r = _ingesta(tmp_path, repo, [], bienestar_falla=True).sincronizar(
        date(2026, 10, 12), date(2026, 10, 18)
    )
    assert r.corridas == 2
    assert any("bienestar de Garmin" in a for a in r.avisos)


def test_el_dia_lo_gana_intervals_si_las_dos_guardaron(repo):
    from sport_report.db.models import SesionReal

    def ses(fuente, id_):
        return SesionReal(
            fuente=fuente, id_externo=id_, fecha_utc="2026-10-13T12:00:00+00:00",
            fecha_local="2026-10-13", dia_semana="M", tipo="Run", es_fuerza=False,
            distancia_km=10.0, duracion_mov_s=3000,
        )

    repo.guardar_sesiones([ses(config.GARMIN, "g1"), ses(config.STRAVA, "s1")])
    assert [s.fuente for s in repo.sesiones_entre(date(2026, 10, 13), date(2026, 10, 13))] == [config.GARMIN]
    repo.guardar_sesiones([ses(config.INTERVALS, "i1")])
    assert [s.fuente for s in repo.sesiones_entre(date(2026, 10, 13), date(2026, 10, 13))] == [config.INTERVALS]


# --------------------------------------------------------------------------
# Vencimiento de la clave
# --------------------------------------------------------------------------


HOY = date(2026, 10, 23)


def test_dias_para_vencer():
    assert dias_para_vencer(HOY, "2026-10-30") == 7
    assert dias_para_vencer(HOY, "2026-10-20") == -3
    assert dias_para_vencer(HOY, "") is None
    assert dias_para_vencer(HOY, "30/10/2026") is None


def test_diagnostico_de_la_clave(monkeypatch):
    monkeypatch.setattr(config, "GARMIN_API_KEY", "x")
    assert estado_clave_garmin(HOY, "2027-01-21")[0] == OK
    assert estado_clave_garmin(HOY, "2026-11-01")[0] == AVISO
    assert estado_clave_garmin(HOY, "2026-10-01")[0] == FALLA
    assert estado_clave_garmin(HOY, "")[0] == AVISO
    monkeypatch.setattr(config, "GARMIN_API_KEY", "")
    assert "sin clave" in estado_clave_garmin(HOY, "2027-01-21")[2]


def test_el_reporte_avisa_solo_cerca_del_vencimiento(monkeypatch):
    monkeypatch.setattr(config, "FUENTE_PRINCIPAL", config.INTERVALS)
    monkeypatch.setattr(config, "GARMIN_API_KEY_VENCE", "2026-10-28")
    assert "vence en 5 dias" in fuentes.aviso_vencimiento_garmin(HOY)
    monkeypatch.setattr(config, "GARMIN_API_KEY_VENCE", "2026-10-20")
    assert "vencio hace 3 dias" in fuentes.aviso_vencimiento_garmin(HOY)
    monkeypatch.setattr(config, "GARMIN_API_KEY_VENCE", "2026-12-31")
    assert fuentes.aviso_vencimiento_garmin(HOY) is None
    # Si Garmin no esta en la cadena, no importa.
    monkeypatch.setattr(config, "GARMIN_API_KEY_VENCE", "2026-10-20")
    monkeypatch.setattr(config, "FUENTE_PRINCIPAL", config.STRAVA)
    assert fuentes.aviso_vencimiento_garmin(HOY) is None
