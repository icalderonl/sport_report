"""Tests del cliente y la verificacion de la API Personal de Garmin (sin red)."""
from __future__ import annotations

import base64
import io
import json
import zipfile

import httpx
import pytest

from sport_report import config
from sport_report.garmin import verificar
from sport_report.garmin.auth import RUTA_TOKEN, clave, resumen_clave, vencimiento_token
from sport_report.garmin.client import GarminClient
from sport_report.garmin.errors import GarminAuthError, GarminError, SinPermiso

CLAVE = "clave-garmin-de-prueba"


def _jwt(exp: int = 1_800_000_000) -> str:
    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'RS256'})}.{b64({'exp': exp})}.firma"


TOKEN = _jwt()


def _cliente(handler) -> GarminClient:
    return GarminClient(
        clave_api=CLAVE,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        dormir=lambda s: None,
        base="https://garmin.test",
    )


def _zip_fit() -> bytes:
    fit = b"\x0e\x10\x00\x00\x00\x00\x00\x00.FIT" + b"\x00" * 20
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("123_ACTIVITY.fit", fit)
    return buf.getvalue()


def _api(request: httpx.Request) -> httpx.Response:
    """Respuestas minimas con la forma de la especificacion."""
    ruta = request.url.path
    if ruta == RUTA_TOKEN:
        return httpx.Response(200, text=TOKEN)
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    if ruta.endswith("/userprofile/settings"):
        return httpx.Response(200, json={"timeZone": "America/Santiago"})
    if ruta.endswith("/search/activities"):
        return httpx.Response(200, json=[
            {"activityId": 123, "activityType": {"typeKey": "running"},
             "distance": 10000.0, "averageHR": 150, "avgGroundContactTime": 240.0},
            {"activityId": 124, "activityType": {"typeKey": "strength_training"}},
        ])
    if ruta.endswith("/files/activity/123"):
        return httpx.Response(200, content=_zip_fit())
    if ruta.endswith("/activity/123"):
        return httpx.Response(200, json={
            "summaryDTO": {"groundContactTime": 240.0},
            "metadataDTO": {"lapCount": 10},
            "splitSummaries": [{"splitType": "INTERVAL_ACTIVE"}],
        })
    if ruta.endswith("/heartRateZones/v2"):
        return httpx.Response(200, json=[{"sport": "RUNNING", "zone1Floor": 100}])
    if "/hrv/daily/" in ruta:
        return httpx.Response(200, json={"hrvSummaries": [{"lastNightAvg": 55, "status": "BALANCED"}]})
    if ruta.endswith("/dailySleepSummariesByDate"):
        return httpx.Response(200, json=[{"sleepTimeSeconds": 25000, "sleepScores": {"overall": {"value": 80}}}])
    if ruta.endswith("/v2/dailysummaries"):
        return httpx.Response(200, json={"data": [{"heartRate": {"restingValue": 48}}]})
    return httpx.Response(404)


# --------------------------------------------------------------------------
# Autenticacion: clave -> token de 24 h, despues Bearer
# --------------------------------------------------------------------------


def test_la_clave_se_cambia_por_token_y_los_pedidos_van_con_bearer():
    vistos = []

    def handler(request):
        vistos.append((request.method, request.url.path, request.headers.get("authorization")))
        return _api(request)

    cli = _cliente(handler)
    cli.ajustes_usuario()
    cli.zonas_hr()
    assert vistos[0] == ("POST", RUTA_TOKEN, f"ApiKey {CLAVE}")
    # Un solo token por corrida.
    assert [v for v in vistos if v[1] == RUTA_TOKEN] == [vistos[0]]
    assert all(v[2] == f"Bearer {TOKEN}" for v in vistos[1:])


def test_clave_rechazada_no_se_reintenta():
    llamadas = []

    def handler(request):
        llamadas.append(request.url.path)
        return httpx.Response(401)

    with pytest.raises(GarminAuthError, match="vencido"):
        _cliente(handler).ajustes_usuario()
    assert llamadas == [RUTA_TOKEN]


def test_sin_clave_falla_temprano(monkeypatch):
    monkeypatch.setattr(config, "GARMIN_API_KEY", "")
    with pytest.raises(GarminAuthError, match="GARMIN_API_KEY"):
        clave()
    assert resumen_clave() == "ausente"


def test_resumen_de_clave_no_la_muestra():
    r = resumen_clave(CLAVE)
    assert CLAVE not in r and str(len(CLAVE)) in r


def test_vencimiento_del_token():
    assert vencimiento_token(_jwt(1_800_000_000)).timestamp() == 1_800_000_000
    assert vencimiento_token("no-es-jwt") is None


def test_5xx_se_reintenta_y_404_es_sin_permiso():
    estados = iter([503, 200])

    def handler(request):
        if request.url.path == RUTA_TOKEN:
            return httpx.Response(200, text=TOKEN)
        if request.url.path.endswith("/settings"):
            return httpx.Response(next(estados), json={"ok": True})
        return httpx.Response(404)

    cli = _cliente(handler)
    assert cli.ajustes_usuario() == {"ok": True}
    with pytest.raises(SinPermiso):
        cli.zonas_hr()


def test_429_no_espera_a_ciegas():
    def handler(request):
        if request.url.path == RUTA_TOKEN:
            return httpx.Response(200, text=TOKEN)
        return httpx.Response(429)

    with pytest.raises(GarminError, match="429"):
        _cliente(handler).zonas_hr()


def test_resumenes_diarios_desenvuelve_data():
    from datetime import date

    cli = _cliente(_api)
    assert cli.resumenes_diarios(date(2026, 10, 1), date(2026, 10, 2)) == [
        {"heartRate": {"restingValue": 48}}
    ]


# --------------------------------------------------------------------------
# Verificacion
# --------------------------------------------------------------------------


def test_verificar_completo_y_sin_secretos(capsys, monkeypatch):
    monkeypatch.setattr(config, "GARMIN_API_KEY", CLAVE)
    assert verificar.revisar(14, volcar=True, cli=_cliente(_api)) == 0
    salida = capsys.readouterr().out
    assert CLAVE not in salida and TOKEN not in salida
    assert "1 corridas" in salida
    assert "avgGroundContactTime" in salida
    assert "firma FIT OK" in salida
    assert "'overall'" in salida
    assert "FC reposo=48" in salida


def test_verificar_sigue_si_falla_una_seccion(capsys):
    def handler(request):
        if request.url.path.endswith("/heartRateZones/v2"):
            return httpx.Response(500)
        return _api(request)

    assert verificar.revisar(14, cli=_cliente(handler)) == 2
    salida = capsys.readouterr().out
    assert "zonas de FC" in salida and "FC reposo=48" in salida


def test_verificar_token_rechazado(capsys):
    assert verificar.revisar(14, cli=_cliente(lambda r: httpx.Response(401))) == 1
