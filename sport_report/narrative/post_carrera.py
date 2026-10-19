"""Narrativa del informe post-carrera: una llamada, con el modelo del mensual.

Mismo contrato que el semanal y el mensual: el modelo narra y no calcula. Las
senales de fatiga ya vienen decididas por el motor (`fatiga.senales`): el
modelo las explica, no las diagnostica. La llamada, la verificacion de cifras
y la de atribucion son las del mensual (`mensual._redactar`), con una tabla de
rutas propia.
"""
from __future__ import annotations

import re
from typing import Any

from .claude import Narrativa
from .mensual import LECTURA_JSON_COMUN, REGLA_ABSOLUTA, _redactar

MAX_TOKENS = 1536

SISTEMA_POST_CARRERA = f"""\
Eres el redactor del informe POST-CARRERA de un atleta de running: llega el
dia siguiente a una carrera objetivo. Recibes un JSON con todo YA CALCULADO:
la actividad de la carrera, sus parciales, las senales de fatiga, el bienestar
alrededor de la carrera y, si era la carrera principal, el cierre del ciclo de
entrenamiento con su comparacion contra el ciclo anterior.

{REGLA_ABSOLUTA}

{LECTURA_JSON_COMUN}
- Los ritmos y tiempos vienen ya formateados ("ritmo", "tiempo_mov",
  "primera_mitad"...): citalos tal cual, no los conviertas.
- "fatiga"."senales" es la decision del motor, con sus umbrales. Explica cada
  senal con sus cifras. Si la lista esta vacia y "evaluadas" no, di que no se
  observaron senales de fatiga en lo que se pudo medir; si hay algo en
  "no_evaluables", di que no se pudo evaluar y por que. No agregues senales que
  el motor no marco.
- "bienestar" es contexto de cuerpo completo, no atribuible solo a la carrera.
  NUNCA lo combines con otras cifras en un puntaje o juicio unico.
- "ciclo" es null si la carrera no era la principal: entonces no hables de
  ciclo ni de afinamiento.
- En "carreras_similares", "delta_ritmo_s_km" negativo significa que esta
  carrera fue mas rapida; esta en segundos por kilometro.

Que cubrir, en este orden:
- el resultado: distancia, tiempo, ritmo y FC;
- como se distribuyo el esfuerzo: mitades, cuartos y regularidad;
- si se observa fatiga y en que;
- el bienestar de la manana de la carrera y la de despues, si hay datos;
- si hay ciclo: semanas, volumen total, pico, afinamiento y adherencia, y la
  comparacion con el ciclo anterior y con carreras similares si existen.

Formato de salida:
- 8 a 12 lineas, en espanol, tono directo y concreto.
- Texto plano. Sin markdown, sin encabezados, sin vinetas, sin emojis.
- No des consejo medico ni prescribas entrenamientos.\
"""

INSTRUCCION_POST_CARRERA = (
    "Redacta el informe post-carrera a partir del siguiente JSON. Recuerda: "
    "ninguna cifra que no este en el JSON.\n\n"
)

#: Solo metricas con valor numerico en el JSON: la atribucion compara numeros,
#: y una ruta a un texto ("5:02/km") no aporta valores legitimos. Los ritmos y
#: tiempos quedan cubiertos por `verificar_cifras`.
METRICAS_POST_CARRERA: tuple[tuple[str, re.Pattern[str], tuple[tuple[str, ...], ...]], ...] = (
    (
        "FC",
        re.compile(r"\bFC\b|frecuencia card[ií]aca|pulsaciones", re.I),
        (
            ("actividad", "hr_promedio"),
            ("actividad", "hr_maximo"),
            ("bienestar", "hr_reposo", "dia_carrera"),
            ("bienestar", "hr_reposo", "dia_siguiente"),
            ("bienestar", "hr_reposo", "promedio_28_dias_previos"),
            ("comparacion", "carreras_similares", "hr_promedio"),
        ),
    ),
    (
        "deriva cardiaca",
        re.compile(r"deriva card[ií]aca|decoupling", re.I),
        (("actividad", "decoupling_pct"), ("umbrales", "decoupling_alto_pct")),
    ),
    (
        "cadencia",
        re.compile(r"cadencia", re.I),
        (("actividad", "cadencia_spm"),),
    ),
    (
        "GCT",
        re.compile(r"\bGCT\b|tiempo de contacto", re.I),
        (("actividad", "gct_ms"),),
    ),
    (
        "oscilacion vertical",
        re.compile(r"oscilaci[oó]n vertical", re.I),
        (("actividad", "oscilacion_vertical_cm"),),
    ),
    (
        "split",
        re.compile(r"\bsplit\b", re.I),
        (
            ("parciales", "split_pct"),
            ("umbrales", "split_positivo_pct"),
            ("umbrales", "split_parejo_pct"),
        ),
    ),
    (
        "afinamiento",
        re.compile(r"afinamiento|\btaper\b", re.I),
        (
            ("ciclo", "afinamiento", "reduccion_vs_pico_pct"),
            ("ciclo", "afinamiento", "promedio_km"),
            ("ciclo", "afinamiento", "n_semanas"),
            ("ciclo", "afinamiento", "semanas", "km"),
        ),
    ),
    (
        "pico",
        re.compile(r"\bpico\b", re.I),
        (
            ("ciclo", "pico", "km"),
            ("comparacion", "ciclo_anterior", "pico_km"),
            ("comparacion", "ciclo_anterior", "delta_pico_km"),
        ),
    ),
    (
        "adherencia",
        re.compile(r"adherencia", re.I),
        (
            ("ciclo", "adherencia", "promedio_pct"),
            ("ciclo", "adherencia", "semanas_con_plan"),
            ("ciclo", "adherencia", "semanas_sin_plan"),
            ("ciclo", "por_semana", "adherencia_pct"),
            ("comparacion", "ciclo_anterior", "adherencia_pct"),
            ("comparacion", "ciclo_anterior", "delta_adherencia_pct"),
        ),
    ),
    (
        "volumen",
        re.compile(r"volumen", re.I),
        (
            ("ciclo", "total_km"),
            ("ciclo", "promedio_semanal_km"),
            ("ciclo", "por_semana", "km"),
            ("comparacion", "ciclo_anterior", "total_km"),
            ("comparacion", "ciclo_anterior", "delta_total_km"),
            ("comparacion", "ciclo_anterior", "promedio_semanal_km"),
            ("comparacion", "ciclo_anterior", "delta_promedio_semanal_km"),
        ),
    ),
    (
        "HRV",
        re.compile(r"\bHRV\b|variabilidad card[ií]aca", re.I),
        (
            ("bienestar", "hrv", "dia_carrera"),
            ("bienestar", "hrv", "dia_siguiente"),
            ("bienestar", "hrv", "promedio_28_dias_previos"),
        ),
    ),
)


def redactar_post_carrera(
    datos: dict[str, Any],
    cliente: Any | None = None,
    modelo: str | None = None,
    api_key: str | None = None,
) -> Narrativa:
    return _redactar(
        datos,
        SISTEMA_POST_CARRERA,
        INSTRUCCION_POST_CARRERA,
        MAX_TOKENS,
        cliente,
        modelo,
        api_key,
        metricas=METRICAS_POST_CARRERA,
    )
