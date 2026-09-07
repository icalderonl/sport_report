"""Tests de la capa narrativa. No tocan la red ni requieren el SDK instalado."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest


from sport_report.narrative import claude
from sport_report.narrative.claude import (
    Narrativa,
    redactar,
    verificar_atribucion,
    verificar_cifras,
)

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


# --------------------------------------------------------------------------
# Verificacion por metrica: la cifra intercambiada
# --------------------------------------------------------------------------

# JSON completo, con varias metricas a la vez: es justo la situacion en la que
# un modelo confunde una con otra.
COMPLETO = {
    "volumen": {"planificado_km": 42.6, "real_km": 43.7, "pct": 102.6},
    "carga": {"semanal": 792.0},
    "acwr": {"ratio": 1.72, "aguda": 113.1, "cronica": 65.6, "confiable": True},
    "monotony": {"monotony": 0.97, "strain": 768.2, "confiable": True},
    "deriva_cardiaca": {"promedio_pct": 3.6, "maximo_pct": 7.8, "n_sesiones": 4},
    "cadencia": {"valor": 175.8, "semana_anterior": 171.0, "delta": 4.8},
    "adherencia": {"pct_global": 75.0, "sesiones_cumplidas": 3, "sesiones_evaluables": 4},
    "umbrales": {"acwr_alto": 1.5, "acwr_bajo": 0.8, "monotony_alta": 2.0,
                 "decoupling_alto_pct": 5.0},
    "alertas": [],
    "avisos_datos": [],
}


@pytest.mark.parametrize(
    "texto",
    [
        "Semana solida: 43.7 km de los 42.6 planificados (102.6%).",
        "El ACWR quedo en 1.72, sobre el umbral de 1.5: hubo un pico de carga.",
        "Monotony 0.97 y Strain 768.2, ambos dentro de lo esperado.",
        "La deriva cardiaca promedio fue 3.6% con un maximo de 7.8% en 4 sesiones.",
        "La cadencia subio a 175.8 spm desde 171.0 la semana pasada.",
        "Adherencia 75.0%: cumpliste 3 de 4 sesiones evaluables.",
        "El ratio agudo:cronico es 1.72, con carga aguda 113.1 y cronica 65.6.",
        "Tu carga semanal fue 792.0.",
        "La deriva cardiaca maxima de 7.8% supera el umbral de 5.0%.",
        # Una metrica se nombra junto a varias cifras suyas legitimas: el valor
        # y su umbral. Las dos tienen que pasar.
        "Alerta: ACWR 1.72 sobre el umbral 1.5: pico de carga",
    ],
)
def test_una_narrativa_correcta_no_dispara_falsos_positivos(texto):
    assert verificar_atribucion(texto, COMPLETO) == []


@pytest.mark.parametrize(
    "texto,metrica",
    [
        ("El ACWR de la semana fue 0.97.", "ACWR"),          # es el Monotony
        ("La monotonia quedo en 1.72.", "Monotony"),          # es el ACWR
        ("La cadencia promedio fue 43.7 spm.", "cadencia"),   # son los km
        ("La deriva cardiaca promedio fue 1.72%.", "deriva"),  # es el ACWR
        ("Tu carga semanal fue 768.2.", "carga"),             # es el Strain
    ],
)
def test_detecta_una_cifra_intercambiada(texto, metrica):
    problemas = verificar_atribucion(texto, COMPLETO)

    assert problemas, f"no detecto la cifra mal atribuida a {metrica}"
    assert metrica.lower() in problemas[0].lower()


def test_la_comprobacion_global_no_ve_la_cifra_intercambiada():
    """El motivo de que exista la verificacion por metrica.

    0.97 esta en el JSON (es el Monotony), asi que la comprobacion global la da
    por buena aunque el texto la presente como el ACWR.
    """
    texto = "El ACWR de la semana fue 0.97."

    assert verificar_cifras(texto, COMPLETO) == []
    assert verificar_atribucion(texto, COMPLETO)


def test_citar_una_metrica_que_el_motor_dejo_en_null():
    """Si el ACWR no se pudo calcular, cualquier numero pegado a ACWR esta mal."""
    datos = dict(COMPLETO, acwr={"ratio": None, "aguda": None, "cronica": None})

    problemas = verificar_atribucion("El ACWR de esta semana fue 1.3.", datos)

    assert problemas == ["ACWR: el texto dice 1.3 y el JSON trae null"]


def test_decir_que_no_hay_dato_no_es_atribuir_mal():
    datos = dict(COMPLETO, acwr={"ratio": None})

    assert verificar_atribucion("El ACWR no se pudo calcular todavia.", datos) == []


def test_un_numero_de_otra_frase_no_se_atribuye_a_la_metrica():
    """La ventana es corta a proposito: sin eso, cualquier cifra posterior
    quedaba colgada del ultimo nombre de metrica que apareciera."""
    texto = "El ACWR no es confiable todavia. En total corriste 43.7 km."

    assert verificar_atribucion(texto, COMPLETO) == []


def test_tolera_coma_decimal_y_redondeo():
    assert verificar_atribucion("El ACWR fue 1,72.", COMPLETO) == []
    assert verificar_atribucion("El ACWR ronda 1.7.", COMPLETO) == []
    assert verificar_atribucion("El ACWR fue 0,97.", COMPLETO)  # sigue siendo el Monotony


def test_el_valor_absoluto_de_un_delta_negativo_es_legitimo():
    """El JSON trae delta -4.8 y el texto dice "bajo 4.8": es la misma cifra."""
    datos = dict(COMPLETO, cadencia={"valor": 171.0, "semana_anterior": 175.8, "delta": -4.8})

    assert verificar_atribucion("La cadencia bajo 4.8 spm.", datos) == []


def test_la_narrativa_registra_la_cifra_mal_atribuida():
    cliente = ClienteFalso(Respuesta("El ACWR de la semana fue 0.97."))

    n = redactar(COMPLETO, cliente=cliente)

    assert n.ok  # no se descarta el texto: el reporte tiene que llegar
    assert n.cifras_mal_atribuidas
    assert n.to_json()["cifras_mal_atribuidas"]


def test_el_cliente_se_construye_con_reintentos_explicitos():
    """L3: el SDK ya reintenta solo, pero con el default pensado para trafico
    interactivo. Aca un fallo cuesta la narrativa de la semana entera."""
    anthropic = pytest.importorskip("anthropic")
    cliente = claude._cliente("clave-de-prueba")

    assert isinstance(cliente, anthropic.Anthropic)
    assert cliente.max_retries == claude.REINTENTOS
    # No se compara contra el default del SDK: vive en `anthropic._constants`,
    # que es privado y no tiene por que seguir ahi en la proxima version. Lo
    # que importa es que este puesto a proposito y sea mas paciente que 2.
    assert claude.REINTENTOS > 2


# --------------------------------------------------------------------------
# Casos venidos de produccion (corrida real en la Pi, 2026-09-06)
# --------------------------------------------------------------------------

# JSON tal como lo produjo el motor esa semana.
PRODUCCION = {
    "volumen": {"real_km": 39.87, "planificado_km": 38.31, "pct": 104.1, "estimado_km": 5.7},
    "carga": {"semanal": 558.2},
    "acwr": {"ratio": 1.22, "aguda": 79.7, "cronica": 65.2},
    "monotony": {"monotony": 0.94, "strain": 524.7, "carga_semanal": 558.2},
    "deriva_cardiaca": {"promedio_pct": 0.7, "maximo_pct": 5.07, "n_sesiones": 4},
    "cadencia": {"valor": 165.0, "semana_anterior": 162.2, "delta": 2.8},
    "adherencia": {"pct_global": 75.0, "sesiones_cumplidas": 3, "sesiones_evaluables": 4},
    "umbrales": {
        "acwr_alto": 1.5,
        "acwr_bajo": 0.8,
        "monotony_alta": 2.0,
        "decoupling_alto_pct": 5.0,
    },
}

NARRATIVA_REAL = (
    "Semana completada con volumen por encima del plan. Recorriste 39.87 km frente a los "
    "38.31 km planificados, representando el 104.1% de adherencia. La carga semanal fue "
    "558.2 unidades distribuida en cuatro sesiones, con el mayor esfuerzo concentrado el "
    "domingo en la carrera larga de 16 km. Tu cadencia mejoro a 165.0 spm desde las 162.2 "
    "spm de la semana anterior. El ratio ACWR se ubico en 1.22, dentro de rangos "
    "controlados, y la monotonia fue 0.94 sin variabilidad excesiva.\n\n"
    "Alerta: deriva cardiaca maxima 5.07% sobre el umbral 5.0% registrada en la sesion de "
    "series del jueves."
)


def test_la_narrativa_real_delata_el_porcentaje_de_volumen_como_adherencia():
    """El caso que aparecio en la primera corrida real.

    El modelo escribio "el 104.1% de adherencia", pero 104.1 es el porcentaje de
    VOLUMEN; la adherencia de esa semana fue 75.0. Todo lo demas del texto esta
    bien citado, incluida la carga semanal 558.2 que va en la frase siguiente.
    """
    problemas = verificar_atribucion(NARRATIVA_REAL, PRODUCCION)

    assert problemas == ["adherencia: el texto dice 104.1 y el JSON trae 75.0"]


def test_el_numero_puede_ir_delante_del_nombre():
    """En espanol es la forma natural para los porcentajes, y mirando solo
    hacia adelante se escapaba entera."""
    assert verificar_atribucion("un 104.1% de adherencia", PRODUCCION)
    assert verificar_atribucion("un 75.0% de adherencia", PRODUCCION) == []


def test_cada_numero_es_de_la_metrica_que_tiene_MAS_cerca():
    """"Monotony 0.97 y Strain 768.2": el 0.97 es del Monotony que tiene pegado
    a la izquierda, no del Strain que queda tres caracteres a la derecha."""
    datos = dict(COMPLETO)

    assert verificar_atribucion("Monotony 0.97 y Strain 768.2, ambos normales.", datos) == []


def test_no_se_cruza_el_punto_que_separa_dos_frases():
    """Sin esto, la carga semanal correctamente citada en la frase siguiente se
    leia como si fuera la adherencia de la anterior."""
    texto = "un 75.0% de adherencia. La carga semanal fue 558.2 unidades."

    assert verificar_atribucion(texto, PRODUCCION) == []


def test_un_numero_sin_metrica_cerca_no_se_comprueba():
    """"la carrera larga de 16 km" no habla de ninguna metrica: no hay nada que
    contrastar y inventarse una atribucion daria falsos positivos."""
    assert verificar_atribucion("el domingo hiciste la carrera larga de 16 km", PRODUCCION) == []
