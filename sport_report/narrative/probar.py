"""Prueba manual de la capa narrativa.

    python -m sport_report.narrative.probar [reporte.json]
    python -m sport_report.narrative.probar --mensual [mensual.json]

Sin argumento usa un JSON de ejemplo, asi se puede probar la API antes de tener
datos reales en la base. Imprime el texto, los tokens usados y las dos
verificaciones: cifras sin respaldo en el JSON y cifras mal atribuidas.

Los ejemplos traen a proposito los casos que mas facil se le escapan al modelo:
un bloque `disponible: false` (Body Battery), un `confiable: false`, y un
bienestar a la baja junto a una carga alta — que el prompt prohibe fusionar en
un solo juicio.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .. import config
from ..logging_setup import setup
from .claude import redactar
from .mensual import redactar_mensual

EJEMPLO = {
    "version": 2,
    "semana": {"inicio": "2026-09-07", "fin": "2026-09-13", "numero_plan": 5},
    "volumen": {"planificado_km": 42.6, "real_km": 43.7, "pct": 102.6},
    "carga": {"semanal": 792.0, "impreciso": False, "sesiones_sin_carga": 0},
    "fuente": {
        "principal": "intervals",
        "usada": "intervals",
        "fallback": False,
        "motivo": "",
        "no_disponibles": [],
    },
    "acwr": {
        "aguda": 113.1,
        "cronica": 65.6,
        "ratio": 1.72,
        "confiable": True,
        "motivo": "",
        "alerta": "ACWR 1.72 sobre el umbral 1.5: pico de carga",
    },
    "monotony": {"monotony": 0.97, "strain": 768.2, "confiable": True, "motivo": ""},
    "deriva_cardiaca": {"promedio_pct": 3.6, "maximo_pct": 7.8, "n_sesiones": 4},
    "cadencia": {"valor": 175.8, "semana_anterior": 171.0, "delta": 4.8, "n_sesiones": 4},
    "gct": {
        "valor": 232.0,
        "semana_anterior": 240.0,
        "delta": -8.0,
        "n_sesiones": 4,
        "unidad": "ms",
        "disponible": True,
        "motivo": "",
    },
    "oscilacion_vertical": {
        "valor": 8.4,
        "semana_anterior": 8.1,
        "delta": 0.3,
        "n_sesiones": 4,
        "unidad": "cm",
        "disponible": True,
        "motivo": "",
    },
    "ratio_vertical": {
        "valor": 7.9,
        "semana_anterior": 7.6,
        "delta": 0.3,
        "n_sesiones": 4,
        "unidad": "%",
        "disponible": True,
        "motivo": "",
    },
    "fatiga_descanso": {
        "disponible": True,
        "motivo": "",
        "hrv": {"valor": 62.0, "semana_anterior": 70.0, "delta": -8.0, "n_dias": 6,
                "unidad": "ms", "disponible": True, "motivo": ""},
        "hr_reposo": {"valor": 50.0, "semana_anterior": 48.0, "delta": 2.0, "n_dias": 6,
                      "unidad": "lpm", "disponible": True, "motivo": ""},
        "sueno_h": {"valor": 6.9, "semana_anterior": 7.6, "delta": -0.7, "n_dias": 6,
                    "unidad": "h", "disponible": True, "motivo": ""},
        "sueno_score": {"valor": 74.0, "semana_anterior": 82.0, "delta": -8.0,
                        "n_dias": 6, "unidad": "", "disponible": True, "motivo": ""},
        "readiness": {"valor": 38.0, "semana_anterior": 66.0, "delta": -28.0,
                      "n_dias": 6, "unidad": "", "disponible": True, "motivo": ""},
        # El caso que hay que ver como se narra: sin dato, no en cero.
        "body_battery": {"valor": None, "semana_anterior": None, "delta": None,
                         "n_dias": 0, "unidad": "", "disponible": False,
                         "motivo": "la fuente no reporto este dato en la semana"},
        "descanso": {"planificados": 2, "tomados": 1, "dias_planificados": ["L", "S"],
                     "dias_tomados": ["L"], "dias_extra": [], "dias_rotos": ["S"]},
        "cruce_carga": {
            "acwr": 1.72,
            "acwr_confiable": True,
            "monotony": 0.97,
            "monotony_confiable": True,
            "lectura": (
                "HRV 62.0 ms (-8.0 vs la semana anterior); sueno 6.9 h (-0.7 vs la "
                "semana anterior); readiness 38.0 (-28.0 vs la semana anterior). "
                "Al mismo tiempo: ACWR 1.72, Monotony 0.97"
            ),
            "carga_texto": "ACWR 1.72, Monotony 0.97",
            "nota": (
                "los dos datos se reportan por separado a proposito: no existe un "
                "indice que los combine"
            ),
        },
    },
    "adherencia": {
        "pct_global": 75.0,
        "sesiones_evaluables": 4,
        "sesiones_cumplidas": 3,
        "fuerza": {"planificadas": 1, "cumplidas": 1},
    },
    "alertas": [
        "ACWR 1.72 sobre el umbral 1.5: pico de carga",
        "deriva cardiaca maxima 7.8% sobre el umbral 5.0%",
        "HRV bajo 8.0 ms respecto a la semana anterior (umbral 5.0 ms)",
        "Training Readiness promedio 38.0, bajo el umbral 40.0",
    ],
    "avisos_datos": [],
    "umbrales": {
        "acwr_alto": 1.5,
        "acwr_bajo": 0.8,
        "monotony_alta": 2.0,
        "decoupling_alto_pct": 5.0,
        "hrv_caida_ms": 5.0,
        "readiness_bajo": 40.0,
        "nota": "valores de literatura general, no calibrados a este atleta",
    },
}

EJEMPLO_MENSUAL = {
    "version": 1,
    "mes": {
        "clave": "2026-09",
        "inicio": "2026-09-01",
        "fin": "2026-09-30",
        "semanas": 4,
        "semanas_incompletas": ["2026-09-28"],
        "alcance": "solo carrera",
        "tipos_incluidos": ["Run", "TrailRun", "VirtualRun"],
        "nota_alcance": (
            "este reporte mide solo carrera (calle, pista, cinta, trail). La "
            "fuerza y cualquier otro deporte quedan fuera de todas las cifras"
        ),
    },
    "volumen": {
        "total_km": 150.0,
        "por_semana": [
            {"lunes": "2026-09-07", "km": 30.0},
            {"lunes": "2026-09-14", "km": 35.0},
            {"lunes": "2026-09-21", "km": 40.0},
            {"lunes": "2026-09-28", "km": 45.0},
        ],
        "promedio_km": 37.5,
        "progresion_pct": 50.0,
        "semanas_al_alza": 3,
        "semanas": 4,
        "mes_anterior_km": 120.0,
        "delta_km": 30.0,
        "confiable": True,
        "motivo": "",
    },
    "carga": {"total": 6120.0, "por_semana": [], "corridas": 16},
    "acwr": {"por_semana": [], "promedio": 1.14, "maximo": 1.31,
             "confiable": True, "motivo": ""},
    # A proposito no confiable: hay que ver como lo dice el texto.
    "monotony": {"por_semana": [], "promedio": None, "maximo": None,
                 "confiable": False,
                 "motivo": "ninguna de las 4 semanas del mes tiene un valor confiable"},
    "adherencia": {"por_semana": [], "promedio_pct": 92.0, "semanas_con_plan": 4,
                   "semanas_sin_plan": 0, "confiable": True, "motivo": "",
                   "nota": "cuenta solo sesiones de carrera; la fuerza no entra en el mensual"},
    "dinamica": {
        "cadencia": {"valor": 176.0, "mes_anterior": 174.0, "delta": 2.0,
                     "n_sesiones": 16, "unidad": "spm", "disponible": True, "motivo": ""},
        "gct": {"valor": 232.0, "mes_anterior": 240.0, "delta": -8.0, "n_sesiones": 16,
                "unidad": "ms", "disponible": True, "motivo": ""},
        "oscilacion_vertical": {"valor": 8.4, "mes_anterior": 8.1, "delta": 0.3,
                                "n_sesiones": 16, "unidad": "cm", "disponible": True,
                                "motivo": ""},
        "ratio_vertical": {"valor": 7.9, "mes_anterior": 7.6, "delta": 0.3,
                           "n_sesiones": 16, "unidad": "%", "disponible": True,
                           "motivo": ""},
        "semanas_con_dinamica": 4,
    },
    "fatiga_descanso": dict(
        EJEMPLO["fatiga_descanso"],
        nota=(
            "dato de cuerpo completo, no atribuible solo a la carrera; se incluye "
            "como contexto de fatiga"
        ),
    ),
    "carrera": {
        "fecha": "2026-11-15",
        "nombre": "Maraton de Santiago",
        "semanas_restantes": 6,
        "fase": "construccion",
        "nota": "a 6 semana(s): fase de construccion",
    },
    "fuentes_usadas": [
        {"semana": "2026-09-07", "fuente": "intervals", "fallback": False},
        {"semana": "2026-09-14", "fuente": "intervals", "fallback": False},
    ],
    "alertas": [],
    "avisos_datos": [],
    "umbrales": {"acwr_alto": 1.5, "acwr_bajo": 0.8, "monotony_alta": 2.0,
                 "nota": "valores de literatura general, no calibrados a este atleta"},
}


def main(argv: list[str] | None = None) -> int:
    setup("narrativa")
    p = argparse.ArgumentParser(
        prog="python -m sport_report.narrative.probar",
        description="Prueba manual de la capa narrativa",
    )
    p.add_argument("json", nargs="?", help="reporte a narrar (por defecto, un ejemplo)")
    p.add_argument(
        "--mensual",
        action="store_true",
        help="prueba la narrativa mensual (modelo mayor, prompt propio)",
    )
    a = p.parse_args(argv)

    ejemplo = EJEMPLO_MENSUAL if a.mensual else EJEMPLO
    datos = json.loads(Path(a.json).read_text(encoding="utf-8")) if a.json else ejemplo
    modelo = config.ANTHROPIC_MODEL_MENSUAL if a.mensual else config.ANTHROPIC_MODEL

    print(f"Reporte: {'mensual' if a.mensual else 'semanal'}")
    print(f"Modelo: {modelo}")
    print(f"JSON de entrada: {len(json.dumps(datos))} bytes\n")

    n = redactar_mensual(datos) if a.mensual else redactar(datos)
    if not n.ok:
        print(f"Sin narrativa: {n.error}")
        print("(el reporte se enviaria igual, solo con las cifras)")
        return 1

    print("-" * 60)
    print(n.texto)
    print("-" * 60)
    print(f"\ntokens: {n.tokens_entrada} entrada / {n.tokens_salida} salida")
    if n.numeros_no_verificados:
        print(f"CIFRAS SIN RESPALDO EN EL JSON: {n.numeros_no_verificados}")
    else:
        print("todas las cifras del texto estan en el JSON")
    if n.cifras_mal_atribuidas:
        print(f"CIFRAS MAL ATRIBUIDAS: {n.cifras_mal_atribuidas}")
    else:
        print("ninguna cifra atribuida a la metrica equivocada")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
