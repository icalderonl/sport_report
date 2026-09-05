"""Prueba manual de la capa narrativa.

    python -m sport_report.narrative.probar [reporte.json]

Sin argumento usa un JSON de ejemplo, asi se puede probar la API antes de tener
datos reales en la base. Imprime el texto, los tokens usados y cualquier cifra
que el modelo haya introducido sin respaldo en el JSON.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .. import config
from ..logging_setup import setup
from .claude import redactar

EJEMPLO = {
    "semana": {"inicio": "2026-09-07", "fin": "2026-09-13", "numero_plan": 5},
    "volumen": {"planificado_km": 42.6, "real_km": 43.7, "pct": 102.6},
    "carga": {"semanal": 792.0, "impreciso": False, "sesiones_sin_carga": 0},
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
    "cadencia": {"valor": 175.8, "semana_anterior": 171.0, "delta": 4.8},
    "adherencia": {
        "pct_global": 75.0,
        "sesiones_evaluables": 4,
        "sesiones_cumplidas": 3,
        "fuerza": {"planificadas": 1, "cumplidas": 1},
    },
    "alertas": [
        "ACWR 1.72 sobre el umbral 1.5: pico de carga",
        "deriva cardiaca maxima 7.8% sobre el umbral 5.0%",
    ],
    "avisos_datos": [],
    "umbrales": {"nota": "valores de literatura general, no calibrados a este atleta"},
}


def main(argv: list[str] | None = None) -> int:
    setup("narrativa")
    argv = argv if argv is not None else sys.argv[1:]
    datos = json.loads(Path(argv[0]).read_text(encoding="utf-8")) if argv else EJEMPLO

    print(f"Modelo: {config.ANTHROPIC_MODEL}")
    print(f"JSON de entrada: {len(json.dumps(datos))} bytes\n")

    n = redactar(datos)
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
