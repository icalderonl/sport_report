"""Narrativa mensual: DOS llamadas al mes, con un modelo mas capaz.

El reporte mensual llega en dos mensajes de Telegram, no uno:

- **Actividad** (siempre): volumen semana a semana y contra el mes anterior,
  adherencia, los largos de la semana, la comparativa de entrenamientos de
  calidad (tempo/series/fartlek) y el contexto de la carrera si hay una
  declarada.
- **Fatiga y dinamica** (solo si `revisar_fatiga_dinamica.relevante` es
  True): tendencia de bienestar, dinamica de carrera y ACWR/Monotony contra el
  mes anterior. El motor ya decidio si hubo algo que amerite mandarlo —esto no
  lo decide el modelo— asi que `run_weekly` ni siquiera llama al narrador
  cuando no hace falta.

Mismo contrato que el semanal en lo demas —el modelo narra y no calcula— y la
misma maquinaria de verificacion, extendida con las rutas del JSON mensual. Un
detalle que importa: al prompt semanal se le quita `volumen_historico` para
que el modelo no derive tendencias. Aca la serie semanal ES el contenido, asi
que se manda entera —y por eso `engine/mensual.py` precalcula todo lo
derivable. Si alguien agrega una cifra al texto que el motor no calculo,
`verificar_cifras` la marca.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from .. import config
from .claude import (  # se reusa a proposito: una sola implementacion
    Narrativa,
    _cliente,
    _texto_de,
    verificar_atribucion,
    verificar_cifras,
)

log = logging.getLogger(__name__)

MAX_TOKENS_ACTIVIDAD = 1536
MAX_TOKENS_FISIOLOGIA = 1024

REGLA_ABSOLUTA = """\
REGLA ABSOLUTA: no puedes introducir ninguna cifra que no este literalmente en
el JSON. No calcules, no promedies, no estimes, no conviertas unidades, no
infieras porcentajes, totales ni tendencias. La serie semanal viene entera y
todo lo derivable de ella ya esta calculado (progresion, semanas al alza,
promedios, maximos, deltas): tu trabajo es citar e interpretar, no derivar."""

ALCANCE_NOTA = """\
ALCANCE: este reporte mide SOLO CARRERA (calle, pista, cinta, trail). La fuerza
y cualquier otro deporte quedan fuera de todas las cifras. Dilo en la primera
linea, para que el texto no se lea como un balance de todo el entrenamiento."""

LECTURA_JSON_COMUN = """\
Como leer el JSON:
- Un bloque con "confiable": false NO es un hecho. Si lo mencionas, di que no
  es confiable y por que (esta en "motivo").
- Un bloque con "disponible": false no tiene dato. Su "motivo" dice por que. No
  lo presentes como un cero.
- "avisos_datos" describe huecos. Si hay alguno relevante, mencionalo.
- Los valores de "umbrales" son de literatura general, no estan calibrados a
  este atleta. No los presentes como verdad medica."""

SISTEMA_MENSUAL_ACTIVIDAD = f"""\
Eres el redactor del PRIMER mensaje del reporte MENSUAL de entrenamiento de
running de un atleta: el de actividad (volumen, adherencia y calidad). Un
segundo mensaje aparte —que no escribes tu— cubre fatiga y dinamica cuando
corresponde. Recibes un JSON con agregados de 4 o 5 semanas YA CALCULADOS.

{REGLA_ABSOLUTA}

{ALCANCE_NOTA}

{LECTURA_JSON_COMUN}
- "carrera" es el marco del texto cuando no es null: cuantas semanas faltan y
  en que fase esta el ciclo. Si es null, no hay carrera declarada y no inventes
  una.
- "largos"."por_semana" trae el largo de cada semana (o null si esa semana no
  tenia un 'largo' prescrito en el plan, o no habia plan). No conviertas ese
  null en un cero.
- "calidad" cuenta las sesiones de tempo, series y fartlek del mes: cuantas
  hubo y que kilometraje representan sobre el volumen total del mes
  ("pct_volumen_total"). Si "disponible" es false, dilo en vez de omitirlo.

Que cubrir:
- progresion del volumen semana a semana dentro del mes, y el mes contra el
  anterior;
- adherencia promedio del mes;
- los largos de la semana: como vinieron evolucionando, con cifras;
- la comparativa de entrenamientos de calidad (tempo, series, fartlek): cuantas
  sesiones y que porcentaje del volumen representan;
- el contexto de la carrera, si hay una declarada.

Formato de salida:
- 6 a 10 lineas, en espanol, tono directo y concreto. Es un reporte mensual:
  mas profundo que el semanal, con matices y prioridades, no un listado.
- Texto plano. Sin markdown, sin encabezados, sin vinetas, sin emojis.
- No des consejo medico ni prescribas entrenamientos.
- No hables de fatiga, bienestar, ACWR, Monotony ni dinamica de carrera (GCT,
  cadencia, oscilacion u ratio vertical): eso va en el otro mensaje.\
"""

SISTEMA_MENSUAL_FISIOLOGIA = f"""\
Eres el redactor del SEGUNDO mensaje del reporte MENSUAL de entrenamiento de
running de un atleta: el de fatiga y dinamica de carrera. Ya se mando un
primer mensaje —que no escribes tu— con volumen, adherencia y calidad; no lo
repitas. Este mensaje SOLO se envia cuando el motor de calculo ya detecto una
variacion relevante contra el mes anterior: la razon esta en
"revisar_fatiga_dinamica"."motivos". Tu trabajo es explicarla con las cifras
del resto del JSON, no repetir la lista de motivos tal cual.

{REGLA_ABSOLUTA}

{ALCANCE_NOTA}

{LECTURA_JSON_COMUN}
- "fatiga_descanso" es dato de cuerpo completo, no atribuible solo a la
  carrera.
- NUNCA combines el bienestar (HRV, HR de reposo, sueno, sleep score, Body
  Battery, readiness) y la carga (ACWR, Monotony) en un solo indicador,
  puntaje o juicio unico. Reportalos por separado; si dices algo de la relacion
  entre ambos, di los dos numeros.

Que cubrir:
- que variacion disparo este mensaje ("revisar_fatiga_dinamica"."motivos") y
  que significa, con las cifras que la respaldan;
- tendencia de ACWR y Monotony a lo largo del mes, no el dato de una semana;
- tendencia de la dinamica de carrera (cadencia, GCT, oscilacion y ratio
  vertical) mes contra mes anterior;
- fatiga y descanso, si hay datos;
- las alertas, si "alertas" trae alguna.

Formato de salida:
- 5 a 8 lineas, en espanol, tono directo y concreto.
- Despues, si "alertas" no esta vacio, una linea por alerta empezando con
  "Alerta:".
- Texto plano. Sin markdown, sin encabezados, sin vinetas, sin emojis.
- No des consejo medico ni prescribas entrenamientos.\
"""

INSTRUCCION_MENSUAL_ACTIVIDAD = (
    "Redacta el primer mensaje del reporte mensual (actividad) a partir del "
    "siguiente JSON. Recuerda: solo carrera, y ninguna cifra que no este en "
    "el JSON.\n\n"
)

INSTRUCCION_MENSUAL_FISIOLOGIA = (
    "Redacta el segundo mensaje del reporte mensual (fatiga y dinamica) a "
    "partir del siguiente JSON. Recuerda: ninguna cifra que no este en el "
    "JSON.\n\n"
)

#: Rutas del JSON mensual, con el mismo mecanismo que `claude.METRICAS`. Se
#: extienden aca en vez de tocar la tabla semanal: son otras rutas para los
#: mismos nombres de metrica. Se comparte entre los dos mensajes: cada uno solo
#: menciona las metricas de su propio tema, asi que las rutas de sobra no
#: generan falsos positivos.
METRICAS_MENSUALES: tuple[tuple[str, re.Pattern[str], tuple[tuple[str, ...], ...]], ...] = (
    # Cada metrica con serie declara tambien su `por_semana`. A diferencia del
    # semanal, aca la serie entera VA en el prompt y el modelo la cita: en la
    # primera prueba real escribio "el ACWR se movio entre 0.91 y 1.29", con
    # 0.91 tomado del minimo de las cinco semanas. Sin la ruta de la serie eso
    # se reportaba como cifra mal atribuida siendo correcto, y ese aviso sube al
    # reporte del atleta.
    (
        "ACWR",
        re.compile(r"ACWR|ratio agudo[- :/]*cronico", re.I),
        (
            ("acwr", "promedio"),
            ("acwr", "maximo"),
            ("acwr", "por_semana", "valor"),
            ("umbrales", "acwr_alto"),
            ("umbrales", "acwr_bajo"),
        ),
    ),
    (
        "Monotony",
        # Con acento, igual que en el semanal: el modelo escribe "monotonia".
        re.compile(r"monoton[iíy]a?", re.I),
        (
            ("monotony", "promedio"),
            ("monotony", "maximo"),
            ("monotony", "por_semana", "valor"),
            ("umbrales", "monotony_alta"),
        ),
    ),
    (
        "volumen",
        re.compile(r"volumen|kil[oó]metro", re.I),
        (
            ("volumen", "total_km"),
            ("volumen", "promedio_km"),
            ("volumen", "progresion_pct"),
            ("volumen", "mes_anterior_km"),
            ("volumen", "delta_km"),
            ("volumen", "semanas_al_alza"),
            ("volumen", "por_semana", "km"),
            # "un X% del volumen" es como se lee `calidad.pct_volumen_total`: la
            # palabra "volumen" queda mas cerca del numero que "calidad".
            ("calidad", "pct_volumen_total"),
        ),
    ),
    (
        "carga",
        re.compile(r"carga", re.I),
        (("carga", "total"), ("carga", "corridas"), ("carga", "por_semana", "carga")),
    ),
    (
        "adherencia",
        re.compile(r"adherencia", re.I),
        (
            ("adherencia", "promedio_pct"),
            ("adherencia", "semanas_con_plan"),
            ("adherencia", "semanas_sin_plan"),
            ("adherencia", "por_semana", "pct"),
        ),
    ),
    (
        "largo",
        # "a lo largo del mes" es una locucion comun y no habla del largo de la
        # semana: se excluye con el lookbehind negativo, no cualquier "largo".
        re.compile(r"(?<!lo )\blargo(?:s)?\b", re.I),
        (
            ("largos", "promedio_km"),
            ("largos", "maximo_km"),
            ("largos", "semanas_con_largo"),
            ("largos", "por_semana", "km"),
        ),
    ),
    (
        "calidad",
        re.compile(r"tempo|fartlek|\bseries\b|calidad", re.I),
        (
            ("calidad", "tempo_km"),
            ("calidad", "series_km"),
            ("calidad", "fartlek_km"),
            ("calidad", "km_total"),
            ("calidad", "n_sesiones"),
            ("calidad", "pct_volumen_total"),
        ),
    ),
    (
        "cadencia",
        re.compile(r"cadencia", re.I),
        (
            ("dinamica", "cadencia", "valor"),
            ("dinamica", "cadencia", "mes_anterior"),
            ("dinamica", "cadencia", "delta"),
            ("umbrales", "cadencia_cambio_spm"),
        ),
    ),
    (
        "GCT",
        re.compile(r"\bGCT\b|tiempo de contacto", re.I),
        (
            ("dinamica", "gct", "valor"),
            ("dinamica", "gct", "mes_anterior"),
            ("dinamica", "gct", "delta"),
            ("umbrales", "gct_cambio_ms"),
        ),
    ),
    (
        "oscilacion vertical",
        re.compile(r"oscilaci[oó]n vertical", re.I),
        (
            ("dinamica", "oscilacion_vertical", "valor"),
            ("dinamica", "oscilacion_vertical", "mes_anterior"),
            ("dinamica", "oscilacion_vertical", "delta"),
            ("umbrales", "oscilacion_vertical_cambio_cm"),
        ),
    ),
    (
        "ratio vertical",
        re.compile(r"ratio vertical", re.I),
        (
            ("dinamica", "ratio_vertical", "valor"),
            ("dinamica", "ratio_vertical", "mes_anterior"),
            ("dinamica", "ratio_vertical", "delta"),
            ("umbrales", "ratio_vertical_cambio_pct"),
        ),
    ),
    (
        "HRV",
        re.compile(r"\bHRV\b|variabilidad card[ií]aca", re.I),
        (
            ("fatiga_descanso", "hrv", "valor"),
            ("fatiga_descanso", "hrv", "semana_anterior"),
            ("fatiga_descanso", "hrv", "delta"),
        ),
    ),
    (
        "semanas restantes",
        re.compile(r"semanas? (restantes?|para la carrera)|faltan", re.I),
        (("carrera", "semanas_restantes"),),
    ),
)


def _redactar(
    datos: dict[str, Any],
    sistema: str,
    instruccion: str,
    max_tokens: int,
    cliente: Any | None,
    modelo: str | None,
    api_key: str | None,
) -> Narrativa:
    """Una llamada a Claude con el prompt mensual dado. Nunca lanza."""
    modelo = modelo or config.ANTHROPIC_MODEL_MENSUAL
    try:
        cliente = cliente or _cliente(api_key)
    except Exception as exc:  # falta el SDK o la clave
        log.warning("sin narrativa mensual: %s", exc)
        return Narrativa(texto=None, modelo=modelo, error=f"{type(exc).__name__}: {exc}")

    cuerpo = instruccion + json.dumps(datos, ensure_ascii=False, indent=None)
    try:
        respuesta = cliente.messages.create(
            model=modelo,
            max_tokens=max_tokens,
            system=sistema,
            messages=[{"role": "user", "content": cuerpo}],
        )
    except Exception as exc:
        log.warning("la API de Claude fallo en el mensual: %s", exc)
        return Narrativa(texto=None, modelo=modelo, error=f"{type(exc).__name__}: {exc}")

    if getattr(respuesta, "stop_reason", "") == "refusal":
        log.warning("el modelo rechazo redactar el mensual")
        return Narrativa(texto=None, modelo=modelo, error="el modelo rechazo la solicitud")

    texto = _texto_de(respuesta)
    if not texto:
        return Narrativa(texto=None, modelo=modelo, error="respuesta vacia")

    uso = getattr(respuesta, "usage", None)
    n = Narrativa(
        texto=texto,
        modelo=modelo,
        tokens_entrada=int(getattr(uso, "input_tokens", 0) or 0),
        tokens_salida=int(getattr(uso, "output_tokens", 0) or 0),
        numeros_no_verificados=verificar_cifras(texto, datos),
        cifras_mal_atribuidas=verificar_atribucion(texto, datos, METRICAS_MENSUALES),
    )
    if n.numeros_no_verificados:
        log.warning("cifras sin respaldo en el mensual: %s", n.numeros_no_verificados)
    if n.cifras_mal_atribuidas:
        log.warning("cifras mal atribuidas en el mensual: %s", n.cifras_mal_atribuidas)
    return n


def redactar_mensual_actividad(
    datos: dict[str, Any],
    cliente: Any | None = None,
    modelo: str | None = None,
    api_key: str | None = None,
) -> Narrativa:
    """Primer mensaje mensual: volumen, adherencia, largos y calidad.

    Siempre se manda cuando toca el mensual (spec del atleta): a diferencia
    del segundo, no depende de ninguna variacion.
    """
    return _redactar(
        datos,
        SISTEMA_MENSUAL_ACTIVIDAD,
        INSTRUCCION_MENSUAL_ACTIVIDAD,
        MAX_TOKENS_ACTIVIDAD,
        cliente,
        modelo,
        api_key,
    )


def redactar_mensual_fisiologia(
    datos: dict[str, Any],
    cliente: Any | None = None,
    modelo: str | None = None,
    api_key: str | None = None,
) -> Narrativa:
    """Segundo mensaje mensual: fatiga y dinamica de carrera.

    El llamador (`run_weekly`) es quien decide si corresponde llamar a esta
    funcion, mirando `datos["revisar_fatiga_dinamica"]["relevante"]`: el motor
    de calculo ya hizo esa evaluacion, no el modelo, y no tiene sentido gastar
    una llamada a la API para un mes sin nada que reportar aca.
    """
    return _redactar(
        datos,
        SISTEMA_MENSUAL_FISIOLOGIA,
        INSTRUCCION_MENSUAL_FISIOLOGIA,
        MAX_TOKENS_FISIOLOGIA,
        cliente,
        modelo,
        api_key,
    )
