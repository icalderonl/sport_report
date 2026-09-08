"""Tests del cliente de intervals.icu contra un transporte simulado (sin red).

Ademas del transporte, aca se prueba `campos.py`: es la tabla de nombres no
confirmados de la API, y su contrato —tomar el primero que exista, devolver
None si ninguno aparece, nunca cero— es lo que sostiene la promesa de que un
dato faltante se reporta como faltante.
"""
from __future__ import annotations

import base64

import httpx
import pytest

from sport_report import config
from sport_report.intervals import campos
from sport_report.intervals.auth import USUARIO, credenciales, resumen_clave
from sport_report.intervals.client import IntervalsClient
from sport_report.intervals.errors import (
    IntervalsAuthError,
    IntervalsError,
    SinPermiso,
)

LUNES = "2026-09-07"


def _cliente(handler, **kw) -> IntervalsClient:
    return IntervalsClient(
        clave="clave-de-prueba",
        atleta_id="i1",
        http=httpx.Client(
            transport=httpx.MockTransport(handler), auth=credenciales("clave-de-prueba")
        ),
        dormir=lambda s: None,
        **kw,
    )


def _fecha(dia: str):
    from datetime import datetime

    return datetime.fromisoformat(f"{dia}T00:00:00").replace(tzinfo=config.TZ)


# --------------------------------------------------------------------------
# Autenticacion: Basic con usuario literal API_KEY, sin rotacion
# --------------------------------------------------------------------------


def test_la_clave_viaja_como_basic_con_usuario_api_key():
    visto = {}

    def handler(request: httpx.Request) -> httpx.Response:
        visto["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"id": "i1", "name": "Atleta"})

    cli = _cliente(handler)
    cli.atleta()

    esquema, _, dato = visto["auth"].partition(" ")
    assert esquema == "Basic"
    usuario, _, clave = base64.b64decode(dato).decode().partition(":")
    assert (usuario, clave) == (USUARIO, "clave-de-prueba")


def test_sin_clave_falla_temprano_con_un_mensaje_accionable():
    with pytest.raises(IntervalsAuthError) as exc:
        credenciales("   ")
    assert "INTERVALS_API_KEY" in str(exc.value)
    # Y dice como ponerla, porque el teclado de la Pi pierde caracteres.
    assert "scp" in str(exc.value)


def test_el_resumen_de_la_clave_no_filtra_la_clave():
    r = resumen_clave("abcdefghij")
    assert "abcdefgh" not in r
    assert "10 caracteres" in r and r.endswith("...ij")
    assert resumen_clave("") == "ausente"


# --------------------------------------------------------------------------
# Transporte
# --------------------------------------------------------------------------


def test_un_401_no_se_reintenta_y_dispara_el_respaldo():
    """Insistir con una clave rechazada solo retrasa el respaldo."""
    llamadas = []

    def handler(request: httpx.Request) -> httpx.Response:
        llamadas.append(request.url.path)
        return httpx.Response(401, text="unauthorized")

    with pytest.raises(IntervalsAuthError):
        _cliente(handler).atleta()
    assert len(llamadas) == 1
    # Y es un IntervalsError, que es lo que el selector atrapa.
    assert issubclass(IntervalsAuthError, IntervalsError)


def test_un_5xx_se_reintenta_y_despues_se_rinde():
    llamadas = []

    def handler(request: httpx.Request) -> httpx.Response:
        llamadas.append(1)
        return httpx.Response(503, text="nope")

    with pytest.raises(IntervalsError):
        _cliente(handler).atleta()
    assert len(llamadas) == 4  # 1 + REINTENTOS_5XX


def test_un_5xx_pasajero_se_supera():
    respuestas = [httpx.Response(500), httpx.Response(200, json={"id": "i1"})]

    def handler(request: httpx.Request) -> httpx.Response:
        return respuestas.pop(0)

    assert _cliente(handler).atleta() == {"id": "i1"}


def test_un_429_espera_lo_que_dice_retry_after():
    esperas = []
    respuestas = [
        httpx.Response(429, headers={"retry-after": "7"}),
        httpx.Response(200, json={"id": "i1"}),
    ]
    cli = IntervalsClient(
        clave="k",
        atleta_id="i1",
        http=httpx.Client(transport=httpx.MockTransport(lambda r: respuestas.pop(0))),
        dormir=esperas.append,
    )
    assert cli.atleta() == {"id": "i1"}
    assert esperas == [7.0]


def test_un_403_es_falta_de_permiso_y_no_tumba_la_corrida():
    cli = _cliente(lambda r: httpx.Response(403, text="forbidden"))
    with pytest.raises(SinPermiso):
        cli.actividad("i9")
    # Los endpoints opcionales lo absorben: sin streams no es un error.
    assert cli.streams("i9") == {}
    assert cli.intervalos("i9") == []
    assert cli.bienestar(_fecha(LUNES), _fecha(LUNES)) == []


def test_un_fallo_de_red_se_traduce_a_error_de_la_fuente():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sin ruta al host")

    with pytest.raises(IntervalsError) as exc:
        _cliente(handler).atleta()
    assert "fallo de red" in str(exc.value)


def test_una_respuesta_que_no_es_json_no_se_confunde_con_datos():
    cli = _cliente(lambda r: httpx.Response(200, text="<html>mantenimiento</html>"))
    with pytest.raises(IntervalsError):
        cli.atleta()


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


def test_las_actividades_se_piden_por_rango_de_fechas():
    visto = {}

    def handler(request: httpx.Request) -> httpx.Response:
        visto["path"] = request.url.path
        visto["params"] = dict(request.url.params)
        return httpx.Response(200, json=[{"id": "i1", "type": "Run"}, "basura"])

    acts = _cliente(handler).actividades(_fecha("2026-09-07"), _fecha("2026-09-13"))

    assert visto["path"] == "/api/v1/athlete/i1/activities"
    assert visto["params"] == {"oldest": "2026-09-07", "newest": "2026-09-13"}
    # Lo que no es un objeto se descarta en vez de romper mas adelante.
    assert acts == [{"id": "i1", "type": "Run"}]


def test_pedir_actividades_sin_zona_horaria_es_un_error_de_programacion():
    from datetime import datetime

    cli = _cliente(lambda r: httpx.Response(200, json=[]))
    with pytest.raises(ValueError):
        cli.actividades(datetime(2026, 9, 7), datetime(2026, 9, 13))


def test_los_streams_llegan_como_lista_y_se_adaptan_a_dict():
    """La diferencia mas peligrosa con Strava: aca la respuesta es una lista.

    Si no se adapta, `metricas.carga_trimp` no encuentra 'heartrate' y la
    semana entera queda sin carga ni decoupling, en silencio.
    """
    cuerpo = [
        {"type": "time", "data": [0, 1, 2]},
        {"type": "heartrate", "data": [120, 130, 140]},
        {"type": "distance", "data": [0.0, 2.5, 5.0]},
    ]
    s = _cliente(lambda r: httpx.Response(200, json=cuerpo)).streams("i1")
    assert s == {
        "time": [0, 1, 2],
        "heartrate": [120, 130, 140],
        "distance": [0.0, 2.5, 5.0],
    }


def test_los_intervalos_envueltos_en_el_objeto_tambien_se_encuentran():
    cuerpo = {"icu_intervals": [{"distance": 1000.0, "moving_time": 240}]}
    v = _cliente(lambda r: httpx.Response(200, json=cuerpo)).intervalos("i1")
    assert v == [{"distance": 1000.0, "moving_time": 240}]


def test_el_bienestar_indexado_por_fecha_se_normaliza_a_lista():
    cuerpo = {"2026-09-07": {"hrv": 68.0}, "2026-09-08": {"hrv": 61.0}}
    d = _cliente(lambda r: httpx.Response(200, json=cuerpo)).bienestar(
        _fecha("2026-09-07"), _fecha("2026-09-08")
    )
    assert sorted(x["id"] for x in d) == ["2026-09-07", "2026-09-08"]


# --------------------------------------------------------------------------
# campos.py: la tabla de nombres no confirmados
# --------------------------------------------------------------------------


def test_se_toma_el_primer_candidato_presente():
    assert campos.primero({"average_gct": 232.0}, "gct_ms") == 232.0
    assert campos.primero({"ground_time": 240.0}, "gct_ms") == 240.0
    # El orden manda cuando estan los dos.
    assert campos.primero({"average_gct": 1.0, "gct": 2.0}, "gct_ms") == 1.0


def test_un_campo_ausente_o_nulo_es_none_y_nunca_cero():
    """Es la regla que hace honesto el reporte de una semana degradada."""
    assert campos.primero({}, "gct_ms") is None
    assert campos.primero({"average_gct": None}, "gct_ms") is None
    assert campos.numero({"average_gct": "no es un numero"}, "gct_ms") is None
    assert campos.numero(None, "ratio_vertical_pct") is None


def test_las_unidades_se_deciden_por_magnitud_en_un_solo_lugar():
    # Oscilacion: Garmin la da en mm, el reporte la quiere en cm.
    assert campos.oscilacion_cm(85.0) == 8.5
    assert campos.oscilacion_cm(8.5) == 8.5
    assert campos.oscilacion_cm(None) is None
    # GCT: en ms se deja, en segundos se convierte.
    assert campos.gct_ms(232.0) == 232.0
    assert campos.gct_ms(0.232) == 232.0
    assert campos.gct_ms(None) is None
    # Cadencia: rpm de una pierna se duplica, igual que en Strava.
    assert campos.cadencia_spm(88.0) == 176.0
    assert campos.cadencia_spm(176.0) == 176.0


def test_el_informe_de_candidatos_dice_cual_acerto_y_cual_no():
    lineas = "\n".join(
        campos.informe_candidatos({"average_gct": 232.0, "vertical_ratio": None}, ("gct_ms", "ratio_vertical_pct"))
    )
    assert "gct_ms" in lineas and "average_gct = 232.0" in lineas
    assert "NINGUN CANDIDATO" in lineas
    # Distingue "no vino la clave" de "vino en null", que se arreglan distinto.
    assert "presentes pero nulos: vertical_ratio" in lineas


def test_los_tipos_vistos_se_cuentan_para_detectar_la_cinta():
    cuenta = campos.tipos_vistos(
        [{"type": "Run"}, {"type": "Run"}, {"type": "VirtualRun"}, {"sport_type": "Ride"}, {}]
    )
    assert cuenta == {"Run": 2, "VirtualRun": 1, "Ride": 1, "Desconocido": 1}


def test_las_zonas_se_leen_como_topes_o_como_objetos():
    topes = campos.zonas_hr({"hr_zones": [120, 140, 155, 170, 200]})
    assert topes == [
        {"min": 0, "max": 120},
        {"min": 120, "max": 140},
        {"min": 140, "max": 155},
        {"min": 155, "max": 170},
        {"min": 170, "max": 200},
    ]
    objetos = campos.zonas_hr(
        {"hrZones": [{"min": m, "max": x} for m, x in [(0, 120), (120, 140), (140, 155), (155, 170), (170, 200)]]}
    )
    assert objetos == topes
    # Por deporte: manda la de running.
    porte = campos.zonas_hr(
        [{"types": ["Ride"], "hr_zones": [1, 2, 3, 4, 5]}, {"types": ["Run"], "hr_zones": [120, 140, 155, 170, 200]}]
    )
    assert porte == topes


def test_unas_zonas_inservibles_devuelven_none_para_marcar_carga_imprecisa():
    assert campos.zonas_hr(None) is None
    assert campos.zonas_hr({}) is None
    assert campos.zonas_hr({"hr_zones": [120, 140]}) is None, "menos de 5 zonas"
    assert campos.zonas_hr({"hr_zones": [0, 0, 0, 0, 0]}) is None


def test_los_streams_en_forma_de_dict_tambien_se_aceptan():
    """Por si la API cambia a la forma de Strava, o el detalle la trae asi."""
    assert campos.adaptar_streams({"heartrate": {"data": [1, 2]}}) == {"heartrate": [1, 2]}
    assert campos.adaptar_streams({"heartrate": [1, 2]}) == {"heartrate": [1, 2]}
    assert campos.adaptar_streams(None) == {}
    assert campos.adaptar_streams([{"type": "x"}, "basura", {"data": [1]}]) == {}
