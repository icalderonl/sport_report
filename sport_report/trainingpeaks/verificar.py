"""Verificacion de solo lectura contra TrainingPeaks.

    python -m sport_report.trainingpeaks.verificar            # chequeo corto
    python -m sport_report.trainingpeaks.verificar --volcar   # + JSON crudo

Confirma que la cookie sirve, lista los entrenamientos de la semana en curso y
la siguiente, y muestra el borrador de `/setplan` que cargaria `/tp`, con sus
avisos y si pasa la validacion.

Imprime la LONGITUD de la cookie, nunca la cookie ni el token. No escribe
nada: ni plan, ni base, ni Telegram.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .. import logging_setup
from ..fechas import semana_actual, semana_siguiente
from ..plan.errors import PlanInvalido
from ..plan.grammar import parse_plan
from .client import TPClient, resumen_cookie
from .convertir import DEPORTES, a_setplan
from .errors import TPAuthError, TPError

_NOMBRES = {3: "corrida", 7: "dia libre", 9: "fuerza", **DEPORTES}


def _linea(w: dict[str, Any]) -> str:
    km = w.get("distancePlanned")
    horas = w.get("totalTimePlanned")
    partes = [
        str(w.get("workoutDay") or "")[:10],
        _NOMBRES.get(w.get("workoutTypeValueId"), f"tipo {w.get('workoutTypeValueId')}"),
        f"«{(w.get('title') or '').strip()}»",
        f"{km / 1000:.2f} km" if km else "",
        f"{round(horas * 60)} min" if horas else "",
        "con estructura" if w.get("structure") else "",
        "completado" if w.get("totalTime") else "",
    ]
    return "  " + "  ".join(p for p in partes if p)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--volcar", action="store_true", help="imprime el JSON crudo")
    args = ap.parse_args(argv)
    logging_setup.setup("tp_verificar")

    print(f"cookie         : {resumen_cookie()}")
    cli = TPClient()
    try:
        cli.token()
        print("token          : OK")
        print(f"atleta         : {cli.atleta_id()}")
        actual, siguiente = semana_actual(), semana_siguiente()
        todos = cli.entrenamientos(actual.inicio, siguiente.fin)
    except TPAuthError as exc:
        print(f"autenticacion  : FALLA ({exc})")
        return 2
    except TPError as exc:
        print(f"TrainingPeaks  : FALLA ({exc})")
        return 1
    finally:
        cli.cerrar()

    print(f"entrenamientos : {len(todos)} entre {actual.inicio} y {siguiente.fin}")
    for w in todos:
        print(_linea(w))
    if args.volcar:
        print("--- entrenamientos (crudo) ---")
        print(json.dumps(todos, indent=2, ensure_ascii=False, default=str)[:20000])

    for nombre, rango in (("en curso", actual), ("proxima", siguiente)):
        b = a_setplan(todos, rango)
        print(f"\n=== borrador semana {nombre} ({rango}) ===")
        print(b.texto)
        for a in b.avisos:
            print(f"  aviso: {a}")
        try:
            parse_plan(b.texto)
            print("  validacion: OK")
        except PlanInvalido as exc:
            print(f"  validacion: RECHAZADO\n{exc.render()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
