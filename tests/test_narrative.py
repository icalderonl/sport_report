"""Tests de la capa narrativa. No tocan la red ni requieren el SDK instalado."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


from sport_report.narrative import claude
from sport_report.narrative.claude import Narrativa, redactar, verificar_cifras

DATOS = {
    "semana": {"inicio": "2026-09-07", "fin": "2026-09-13", "numero_plan": 5},
    "volumen": {"planificado_km": 42.6, "real_km": 43.7, "pct": 102.6},
    "acwr": {"ratio": 1.72, "confiable": True, "motivo": "", "alerta": "ACWR 1.72 alto"},
    "monotony": {"monotony": 0.97, "strain": 768.2, "confiable": True},
    "alertas": ["ACWR 1.72 sobre el umbral 1.5: pico de carga"],
    "avisos_datos": [],
}


# --------------------------------------------------------------------------
# Dobles del SDK
# --------------------------------------------------------------------------


@dataclass
class Bloque:
    text: str
    type: str = "text"


@dataclass
class Uso:
    input_tokens: int = 1200
    output_tokens: int = 90


class Respuesta:
    def __init__(self, texto="", stop_reason="end_turn", stop_details=None, bloques=None):
        self.content = bloques if bloques is not None else [Bloque(texto)]
        self.stop_reason = stop_reason
        self.stop_details = stop_details
        self.usage = Uso()
        self.model = "claude-haiku-4-5"


class ClienteFalso:
    def __init__(self, respuesta=None, excepcion=None):
        self._respuesta = respuesta
        self._excepcion = excepcion
        self.llamadas: list[dict[str, Any]] = []

    @property
    def messages(self):
        return self

    def create(self, **kw):
        self.llamadas.append(kw)
        if self._excepcion:
            raise self._excepcion
        return self._respuesta


# --------------------------------------------------------------------------
# Verificacion de cifras (el LLM narra, no calcula)
# --------------------------------------------------------------------------


def test_cifras_del_json_pasan():
    texto = "Semana 5: 43.7 km reales contra 42.6 planificados. ACWR 1.72."
    assert verificar_cifras(texto, DATOS) == []


def test_detecta_una_cifra_inventada():
    texto = "Corriste 43.7 km, un 18% mas que el mes pasado."
    assert "18" in verificar_cifras(texto, DATOS)


def test_tolera_el_redondeo():
    """Si el JSON trae 1.72 y el modelo escribe 1.7, no es una cifra inventada."""
    assert verificar_cifras("El ACWR quedo en 1.7", DATOS) == []
    assert verificar_cifras("Volumen 43.7 km, cerca de 44", DATOS) == []


def test_acepta_numeros_dentro_de_strings_del_json():
    # 1.5 aparece solo dentro del texto de la alerta.
    assert verificar_cifras("El umbral es 1.5", DATOS) == []


def test_acepta_fechas_del_json():
    assert verificar_cifras("Semana del 2026-09-07 al 2026-09-13", DATOS) == []


def test_acepta_coma_decimal():
    assert verificar_cifras("Volumen real 43,7 km", DATOS) == []


# --------------------------------------------------------------------------
# Llamada
# --------------------------------------------------------------------------


def test_redaccion_exitosa():
    cli = ClienteFalso(Respuesta("Semana solida: 43.7 km.\nAlerta: ACWR 1.72 alto."))
    n = redactar(DATOS, cliente=cli)
    assert n.ok and n.texto.startswith("Semana solida")
    assert (n.tokens_entrada, n.tokens_salida) == (1200, 90)
    assert n.numeros_no_verificados == []


def test_el_prompt_lleva_el_json_y_la_regla():
    cli = ClienteFalso(Respuesta("ok"))
    redactar(DATOS, cliente=cli, modelo="claude-haiku-4-5")
    kw = cli.llamadas[0]
    assert kw["model"] == "claude-haiku-4-5"
    assert kw["max_tokens"] == claude.MAX_TOKENS
    assert "no puedes introducir ninguna cifra" in kw["system"]
    assert "42.6" in kw["messages"][0]["content"]
    # Sin prefill de assistant: los modelos actuales lo rechazan.
    assert [m["role"] for m in kw["messages"]] == ["user"]


def test_falla_de_api_no_rompe_la_corrida():
    """Spec 10: el reporte debe poder enviarse igual, sin la parte narrativa."""
    cli = ClienteFalso(excepcion=RuntimeError("503 overloaded"))
    n = redactar(DATOS, cliente=cli)
    assert n.ok is False and n.texto is None
    assert "503 overloaded" in n.error


def test_timeout_tampoco_rompe():
    cli = ClienteFalso(excepcion=TimeoutError("se agoto el tiempo"))
    assert redactar(DATOS, cliente=cli).ok is False


def test_sin_api_key_no_intenta_llamar():
    n = redactar(DATOS, api_key="")
    assert n.ok is False
    assert "ANTHROPIC_API_KEY" in n.error


def test_respuesta_vacia():
    assert redactar(DATOS, cliente=ClienteFalso(Respuesta(""))).ok is False


def test_rechazo_del_modelo():
    class Detalle:
        category = "cyber"

    cli = ClienteFalso(Respuesta("", stop_reason="refusal", stop_details=Detalle()))
    n = redactar(DATOS, cliente=cli)
    assert n.ok is False and "refusal: cyber" in n.error


def test_ignora_bloques_que_no_son_texto():
    bloques = [Bloque("", type="thinking"), Bloque("resumen real")]
    n = redactar(DATOS, cliente=ClienteFalso(Respuesta(bloques=bloques)))
    assert n.texto == "resumen real"


def test_una_cifra_inventada_se_reporta_pero_no_descarta_el_texto():
    cli = ClienteFalso(Respuesta("Subiste 18% respecto del mes pasado."))
    n = redactar(DATOS, cliente=cli)
    assert n.ok is True
    assert "18" in n.numeros_no_verificados


def test_narrativa_serializable():
    n = Narrativa(texto="hola", modelo="claude-haiku-4-5")
    assert n.to_json()["texto"] == "hola"
