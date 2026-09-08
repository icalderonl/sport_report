"""Narrativa mensual: una llamada al mes, con un modelo mas capaz.

Mismo contrato que la semanal —el modelo narra y no calcula— y la misma
maquinaria de verificacion, extendida con las rutas del JSON mensual. Lo unico
que cambia es el modelo y la longitud: es una llamada al mes, el costo es
irrelevante a esa frecuencia, y la ventaja de un modelo mayor esta en sostener
un texto sobre cuatro semanas de datos, no en calcular nada.

Un detalle que importa: al prompt semanal se le quita `volumen_historico` para
que el modelo no derive tendencias. Aca la serie semanal ES el contenido, asi
que se manda entera — y por eso `engine/mensual.py` precalcula todo lo
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
    MAX_TOKENS,
    REINTENTOS,
    TIMEOUT_S,
    Narrativa,
    _cliente,
    _texto_de,
    verificar_atribucion,
    verificar_cifras,
)

log = logging.getLogger(__name__)

MAX_TOKENS_MENSUAL = 2048

SISTEMA_MENSUAL = """\
Eres el redactor del reporte MENSUAL de entrenamiento de running de un atleta.
Recibes un JSON con agregados de 4 o 5 semanas YA CALCULADOS y escribes el
resumen del mes.

REGLA ABSOLUTA: no puedes introducir ninguna cifra que no este literalmente en
el JSON. No calcules, no promedies, no estimes, no conviertas unidades, no
infieras porcentajes, totales ni tendencias. La serie semanal viene entera y
todo lo derivable de ella ya esta calculado (progresion, semanas al alza,
promedios, maximos, deltas): tu trabajo es citar e interpretar, no derivar.

ALCANCE: este reporte mide SOLO CARRERA (calle, pista, cinta, trail). La fuerza
y cualquier otro deporte quedan fuera de todas las cifras. Dilo en la primera
linea, para que el texto no se lea como un balance de todo el entrenamiento.

Como leer el JSON:
- Un bloque con "confiable": false NO es un hecho. Si lo mencionas, di que no
  es confiable y por que (esta en "motivo").
- Un bloque con "disponible": false no tiene dato. Su "motivo" dice por que. No
  lo presentes como un cero.
- "avisos_datos" describe huecos. Si hay alguno relevante, mencionalo.
- "carrera" es el marco del texto cuando no es null: cuantas semanas faltan y
  en que fase esta el ciclo. Si es null, no hay carrera declarada y no inventes
  una.
- "fatiga_descanso" es dato de cuerpo completo, no atribuible solo a la
  carrera.
- NUNCA combines el bienestar (HRV, HR de reposo, sueno, sleep score, Body
  Battery, readiness) y la carga (ACWR, Monotony) en un solo indicador,
  puntaje o juicio unico. Reportalos por separado; si dices algo de la relacion
  entre ambos, di los dos numeros.
- Los valores de "umbrales" son de literatura general, no estan calibrados a
  este atleta. No los presentes como verdad medica.

Que cubrir:
- progresion del volumen semana a semana dentro del mes, y el mes contra el
  anterior;
- tendencia de ACWR y Monotony a lo largo del mes, no el dato de una semana;
- adherencia promedio del mes;
- tendencia de la dinamica de carrera (cadencia, GCT, oscilacion y ratio
  vertical) mes contra mes anterior;
- fatiga y descanso, si hay datos;
- el contexto de la carrera, si hay una declarada;
- las alertas, si "alertas" trae alguna.

Formato de salida:
- 8 a 12 lineas, en espanol, tono directo y concreto. Es un reporte mensual:
  mas profundo que el semanal, con matices y prioridades, no un listado.
- Despues, si "alertas" no esta vacio, una linea por alerta empezando con
  "Alerta:".
- Texto plano. Sin markdown, sin encabezados, sin vinetas, sin emojis.
- No des consejo medico ni prescribas entrenamientos.\
"""

INSTRUCCION_MENSUAL = (
    "Redacta el resumen mensual a partir del siguiente JSON. Recuerda: solo "
    "carrera, y ninguna cifra que no este en el JSON.\n\n"
)

#: Rutas del JSON mensual, con el mismo mecanismo que `claude.METRICAS`. Se
#: extienden aca en vez de tocar la tabla semanal: son otras rutas para los
#: mismos nombres de metrica.
METRICAS_MENSUALES: tuple[tuple[str, re.Pattern[str], tuple[tuple[str, ...], ...]], ...] = (
    (
        "ACWR",
        re.compile(r"ACWR|ratio agudo[- :/]*cronico", re.I),
        (
            ("acwr", "promedio"),
            ("acwr", "maximo"),
            ("umbrales", "acwr_alto"),
            ("umbrales", "acwr_bajo"),
        ),
    ),
    (
        "Monotony",
        re.compile(r"monoton[iy]a?", re.I),
        (("monotony", "promedio"), ("monotony", "maximo"), ("umbrales", "monotony_alta")),
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
        ),
    ),
    (
        "adherencia",
        re.compile(r"adherencia", re.I),
        (
            ("adherencia", "promedio_pct"),
            ("adherencia", "semanas_con_plan"),
            ("adherencia", "semanas_sin_plan"),
        ),
    ),
    (
        "cadencia",
        re.compile(r"cadencia", re.I),
        (
            ("dinamica", "cadencia", "valor"),
            ("dinamica", "cadencia", "mes_anterior"),
            ("dinamica", "cadencia", "delta"),
        ),
    ),
    (
        "GCT",
        re.compile(r"\bGCT\b|tiempo de contacto", re.I),
        (
            ("dinamica", "gct", "valor"),
            ("dinamica", "gct", "mes_anterior"),
            ("dinamica", "gct", "delta"),
        ),
    ),
    (
        "oscilacion vertical",
        re.compile(r"oscilaci[oó]n vertical", re.I),
        (
            ("dinamica", "oscilacion_vertical", "valor"),
            ("dinamica", "oscilacion_vertical", "mes_anterior"),
            ("dinamica", "oscilacion_vertical", "delta"),
        ),
    ),
    (
        "ratio vertical",
        re.compile(r"ratio vertical", re.I),
        (
            ("dinamica", "ratio_vertical", "valor"),
            ("dinamica", "ratio_vertical", "mes_anterior"),
            ("dinamica", "ratio_vertical", "delta"),
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


def redactar_mensual(
    datos: dict[str, Any],
    cliente: Any | None = None,
    modelo: str | None = None,
    api_key: str | None = None,
) -> Narrativa:
    """Narra el JSON mensual. Nunca lanza: si falla, el mensual no se envia."""
    modelo = modelo or config.ANTHROPIC_MODEL_MENSUAL
    try:
        cliente = cliente or _cliente(api_key)
    except Exception as exc:  # falta el SDK o la clave
        log.warning("sin narrativa mensual: %s", exc)
        return Narrativa(texto=None, modelo=modelo, error=f"{type(exc).__name__}: {exc}")

    cuerpo = INSTRUCCION_MENSUAL + json.dumps(datos, ensure_ascii=False, indent=None)
    try:
        respuesta = cliente.messages.create(
            model=modelo,
            max_tokens=MAX_TOKENS_MENSUAL,
            system=SISTEMA_MENSUAL,
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
