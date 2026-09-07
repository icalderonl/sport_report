"""Tests del cliente de Strava contra un transporte simulado (sin red)."""
from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx
import pytest

from sport_report.strava.auth import StravaAuth, TokenStore, Tokens
from sport_report.strava.client import POR_PAGINA, StravaClient
from sport_report.strava.errors import SinPermiso, StravaAuthError, StravaError, StravaRateLimit


@pytest.fixture
def store(tmp_path) -> TokenStore:
    return TokenStore(path=tmp_path / "tokens.json", refresh_inicial="")


def _auth(store, respuestas, registro=None) -> StravaAuth:
    """respuestas: lista de httpx.Response para las llamadas a /oauth/token."""
    cola = list(respuestas)

    def handler(request: httpx.Request) -> httpx.Response:
        if registro is not None:
            registro.append(dict(x.split("=", 1) for x in request.content.decode().split("&")))
        return cola.pop(0)

    return StravaAuth(
        client_id="123",
        client_secret="sec",
        store=store,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _token_ok(access="acc", refresh="ref", en=3600) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": access,
            "refresh_token": refresh,
            "expires_at": int(time.time()) + en,
        },
    )


# --------------------------------------------------------------------------
# Rotacion y persistencia del refresh_token (spec 5 y 10)
# --------------------------------------------------------------------------


def test_el_refresh_token_rotado_se_persiste(store):
    store.guardar(Tokens("viejo_acc", "ref_v1", 0))
    auth = _auth(store, [_token_ok(access="acc_nuevo", refresh="ref_v2")])

    assert auth.access_token() == "acc_nuevo"
    # Lo importante: en disco, no solo en memoria.
    assert store.cargar().refresh_token == "ref_v2"


def test_se_persiste_aunque_el_refresh_token_no_cambie(store):
    store.guardar(Tokens("", "ref_v1", 0))
    auth = _auth(store, [_token_ok(access="acc", refresh="ref_v1")])
    auth.access_token()
    guardado = store.cargar()
    assert guardado.access_token == "acc" and guardado.expires_at > time.time()


def test_un_segundo_proceso_ve_el_token_rotado(store):
    """El bot y el cron comparten tokens.json: el segundo no debe reusar el viejo.

    El primer refresh entrega un access_token ya vencido para forzar que el
    segundo proceso tenga que refrescar de verdad."""
    store.guardar(Tokens("", "ref_v1", 0))
    registro: list[dict] = []
    _auth(store, [_token_ok(refresh="ref_v2", en=0)], registro).access_token()

    otro_store = TokenStore(path=store.path, refresh_inicial="")
    _auth(otro_store, [_token_ok(refresh="ref_v3")], registro).access_token()

    assert [r["refresh_token"] for r in registro] == ["ref_v1", "ref_v2"]


def test_token_vigente_no_gatilla_refresh(store):
    store.guardar(Tokens("acc_vigente", "ref", int(time.time()) + 7200))
    auth = _auth(store, [])  # cualquier POST reventaria con IndexError
    assert auth.access_token() == "acc_vigente"


def test_token_por_vencer_gatilla_refresh(store):
    # Dentro del margen de 5 minutos: se refresca aunque no haya vencido.
    store.guardar(Tokens("acc_viejo", "ref", int(time.time()) + 60))
    auth = _auth(store, [_token_ok(access="acc_fresco")])
    assert auth.access_token() == "acc_fresco"


def test_bootstrap_desde_env(tmp_path):
    store = TokenStore(path=tmp_path / "tokens.json", refresh_inicial="ref_de_env")
    auth = _auth(store, [_token_ok(refresh="ref_rotado")])
    auth.access_token()
    # Una vez creado el archivo, .env deja de mandar.
    assert store.cargar().refresh_token == "ref_rotado"


def test_sin_credenciales_falla_con_mensaje_accionable(store):
    with pytest.raises(StravaAuthError, match="flujo inicial"):
        StravaAuth(client_id="1", client_secret="2", store=store).access_token()


def test_refresh_rechazado_pide_reautorizar(store):
    store.guardar(Tokens("", "ref_muerto", 0))
    auth = _auth(store, [httpx.Response(400, text='{"message":"Bad Request"}')])
    with pytest.raises(StravaAuthError, match="volver a autorizar"):
        auth.access_token()


def test_respuesta_de_token_incompleta(store):
    store.guardar(Tokens("", "ref", 0))
    auth = _auth(store, [httpx.Response(200, json={"access_token": "a"})])
    with pytest.raises(StravaAuthError, match="incompleta"):
        auth.access_token()


# --------------------------------------------------------------------------
# Cliente
# --------------------------------------------------------------------------


class AuthFalsa:
    def __init__(self):
        self.refrescos = 0

    def access_token(self, forzar: bool = False) -> str:
        if forzar:
            self.refrescos += 1
        return f"tok{self.refrescos}"


def _cliente(handler, **kw) -> tuple[StravaClient, AuthFalsa, list]:
    dormidas: list[float] = []
    auth = AuthFalsa()
    cli = StravaClient(
        auth=auth,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        dormir=dormidas.append,
        **kw,
    )
    return cli, auth, dormidas


DESDE = datetime(2026, 8, 31, tzinfo=timezone.utc)
HASTA = datetime(2026, 9, 7, tzinfo=timezone.utc)


def test_actividades_pagina():
    llamadas: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        pagina = int(req.url.params["page"])
        llamadas.append(pagina)
        cuerpo = [{"id": pagina * 1000 + i} for i in range(POR_PAGINA if pagina == 1 else 3)]
        return httpx.Response(200, json=cuerpo)

    cli, _, _ = _cliente(handler)
    acts = cli.actividades(DESDE, HASTA)
    assert llamadas == [1, 2]
    assert len(acts) == POR_PAGINA + 3


def test_actividades_manda_el_rango_como_epoch():
    vistos = {}

    def handler(req: httpx.Request) -> httpx.Response:
        vistos.update(req.url.params)
        return httpx.Response(200, json=[])

    cli, _, _ = _cliente(handler)
    cli.actividades(DESDE, HASTA)
    assert int(vistos["after"]) == int(DESDE.timestamp())
    assert int(vistos["before"]) == int(HASTA.timestamp())


def test_actividades_exige_datetime_con_zona():
    cli, _, _ = _cliente(lambda r: httpx.Response(200, json=[]))
    with pytest.raises(ValueError):
        cli.actividades(datetime(2026, 8, 31), HASTA)


def test_401_fuerza_refresh_y_reintenta_una_vez():
    intentos: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        intentos.append(req.headers["Authorization"])
        if len(intentos) == 1:
            return httpx.Response(401, text="Unauthorized")
        return httpx.Response(200, json=[])

    cli, auth, _ = _cliente(handler)
    cli.actividades(DESDE, HASTA)
    assert auth.refrescos == 1
    assert intentos == ["Bearer tok0", "Bearer tok1"]


def test_401_persistente_no_hace_bucle():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Unauthorized")

    cli, _, _ = _cliente(handler)
    with pytest.raises(StravaError):
        cli.actividades(DESDE, HASTA)


def test_429_espera_y_reintenta():
    n = {"i": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        n["i"] += 1
        if n["i"] == 1:
            return httpx.Response(429, headers={"Retry-After": "42"})
        return httpx.Response(200, json=[])

    cli, _, dormidas = _cliente(handler)
    cli.actividades(DESDE, HASTA)
    assert dormidas == [42.0]


def test_429_indefinido_se_rinde():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "600"})

    cli, _, _ = _cliente(handler)
    with pytest.raises(StravaRateLimit):
        cli.actividades(DESDE, HASTA)


def test_5xx_reintenta_con_backoff():
    n = {"i": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        n["i"] += 1
        return httpx.Response(200, json=[]) if n["i"] > 2 else httpx.Response(502)

    cli, _, dormidas = _cliente(handler)
    cli.actividades(DESDE, HASTA)
    assert dormidas == [2.0, 4.0]


def test_5xx_persistente_falla():
    cli, _, _ = _cliente(lambda r: httpx.Response(503))
    with pytest.raises(StravaError, match="persistente"):
        cli.actividades(DESDE, HASTA)


def test_pausa_entre_requests_para_el_backfill():
    cli, _, dormidas = _cliente(
        lambda r: httpx.Response(200, json=[]), pausa_entre_requests=0.5
    )
    cli.actividades(DESDE, HASTA)
    assert dormidas == [0.5]


# --------------------------------------------------------------------------
# Streams y zonas
# --------------------------------------------------------------------------


def test_streams_se_aplanan_a_listas():
    cuerpo = {
        "time": {"data": [0, 1, 2], "series_type": "time"},
        "heartrate": {"data": [120, 130, 140]},
    }
    cli, _, _ = _cliente(lambda r: httpx.Response(200, json=cuerpo))
    assert cli.streams(1) == {"time": [0, 1, 2], "heartrate": [120, 130, 140]}


def test_actividad_sin_streams_devuelve_vacio_no_error():
    cli, _, _ = _cliente(lambda r: httpx.Response(404, text="Record Not Found"))
    assert cli.streams(1) == {}


def test_vueltas_llegan_como_lista():
    cuerpo = [
        {"lap_index": 1, "distance": 2000.0, "moving_time": 800},
        {"lap_index": 2, "distance": 1000.0, "moving_time": 240},
    ]
    cli, _, _ = _cliente(lambda r: httpx.Response(200, json=cuerpo))
    assert cli.vueltas(1) == cuerpo


def test_actividad_sin_vueltas_devuelve_lista_vacia_no_error():
    """Una subida manual no tiene vueltas: es un caso normal, no un fallo."""
    cli, _, _ = _cliente(lambda r: httpx.Response(404, text="Record Not Found"))
    assert cli.vueltas(1) == []


def test_vueltas_ignora_una_respuesta_que_no_es_lista():
    cli, _, _ = _cliente(lambda r: httpx.Response(200, json={"error": "?"}))
    assert cli.vueltas(1) == []


def test_zonas_hr_ok():
    cuerpo = {
        "heart_rate": {
            "custom_zones": True,
            "zones": [
                {"min": 0, "max": 120},
                {"min": 120, "max": 140},
                {"min": 140, "max": 155},
                {"min": 155, "max": 170},
                {"min": 170, "max": -1},
            ],
        }
    }
    cli, _, _ = _cliente(lambda r: httpx.Response(200, json=cuerpo))
    zonas = cli.zonas_hr()
    assert len(zonas) == 5 and zonas[4] == {"min": 170, "max": -1}


def test_zonas_hr_sin_configurar_devuelve_none():
    """None es la senal de que hay que usar el peso fallback y marcar impreciso."""
    cli, _, _ = _cliente(lambda r: httpx.Response(200, json={"heart_rate": {"zones": []}}))
    assert cli.zonas_hr() is None


def test_zonas_hr_sin_scope_devuelve_none():
    cli, _, _ = _cliente(lambda r: httpx.Response(403, text="Forbidden"))
    assert cli.zonas_hr() is None


def test_403_en_otro_endpoint_si_es_error():
    cli, _, _ = _cliente(lambda r: httpx.Response(403, text="Forbidden"))
    with pytest.raises(SinPermiso):
        cli.actividad(999)
