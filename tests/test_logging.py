"""El log no puede llevarse credenciales al disco.

El token del bot de Telegram viaja DENTRO de la ruta de su API, y httpx
registra cada peticion con la URL completa en INFO. En la primera corrida real
de la fase 2 (2026-09-09) el token aparecio entero en la salida, asi que quedaba
en logs/*.log, en la salida del cron y en cualquier pantallazo.
"""
from __future__ import annotations

import logging

from sport_report.logging_setup import _SinSecretos

TOKEN = "123456789:AA-DUMMY-TOKEN-SOLO-PARA-TESTS-0000"


def _filtrado(mensaje: str, *args) -> str:
    registro = logging.LogRecord(
        "httpx", logging.INFO, __file__, 1, mensaje, args, None
    )
    assert _SinSecretos().filter(registro) is True
    return registro.getMessage()


def test_el_token_de_telegram_no_llega_al_log():
    salida = _filtrado(
        'HTTP Request: POST https://api.telegram.org/bot%s/sendMessage "HTTP/1.1 200 OK"',
        TOKEN,
    )
    assert TOKEN not in salida
    assert "AA-DUMMY" not in salida, "no basta con tapar la parte numerica"
    assert "/bot<TOKEN>/sendMessage" in salida
    # El resto de la linea sigue sirviendo para diagnosticar.
    assert "200 OK" in salida


def test_un_token_en_query_string_tambien_se_tapa():
    salida = _filtrado("POST https://www.strava.com/oauth/token?refresh_token=abc123&x=1")
    assert "abc123" not in salida
    assert "refresh_token=<REDACTADO>" in salida
    assert "x=1" in salida, "solo se tapa el valor del parametro sensible"


def test_una_linea_sin_secretos_se_deja_intacta():
    """El filtro no puede tocar lo que se lee todas las semanas."""
    original = (
        "HTTP Request: GET https://intervals.icu/api/v1/athlete/i705272/"
        'activities?oldest=2026-09-06&newest=2026-09-14 "HTTP/1.1 200 OK"'
    )
    assert _filtrado(original) == original


def test_los_args_no_se_reformatean_dos_veces():
    """Al redactar se consumen los args; formatear de nuevo reventaria."""
    registro = logging.LogRecord(
        "httpx", logging.INFO, __file__, 1, "bot%s ok", (TOKEN,), None
    )
    _SinSecretos().filter(registro)
    assert registro.getMessage() == registro.getMessage()
