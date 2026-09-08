"""Reescribe los planes guardados al formato v2 (una lista de sesiones por dia).

    python -m sport_report.plan.migrar [--dry-run]

Un archivo v1 se lee igual sin migrarlo, asi que esto no es obligatorio para
que el sistema funcione: lo que evita es que el plan vigente se quede en v1
hasta que alguien mande un comando, y que un archivo viejo se reescriba a
medias en un momento que no eligio nadie. Es un paso del despliegue.

No toca la base ni la red. Nunca borra un plan: lo reescribe con el mismo
contenido en el formato nuevo.
"""
from __future__ import annotations

import argparse
import sys

from .. import config
from ..storage import escribir_json, leer_json
from .errors import PlanCorrupto
from .store import VERSION_FORMATO, PlanAnclado


def _rutas() -> list:
    """El plan vigente primero, despues los archivados por fecha."""
    rutas = [config.PLAN_ACTUAL_PATH]
    if config.PLANES_DIR.exists():
        rutas.extend(sorted(config.PLANES_DIR.glob("*.json")))
    return [r for r in rutas if r.exists()]


def migrar(dry_run: bool = False) -> tuple[int, int, list[str]]:
    """(migrados, ya_al_dia, problemas)."""
    migrados = 0
    al_dia = 0
    problemas: list[str] = []

    for ruta in _rutas():
        try:
            d = leer_json(ruta)
        except (OSError, ValueError) as exc:
            problemas.append(f"{ruta.name}: ilegible ({exc})")
            continue
        if not d:
            problemas.append(f"{ruta.name}: vacio")
            continue

        version = d.get("version", 1)
        if version == VERSION_FORMATO:
            al_dia += 1
            print(f"  [ya en v{VERSION_FORMATO}] {ruta.name}")
            continue

        try:
            anclado = PlanAnclado.from_json(d)
        except PlanCorrupto as exc:
            # No se toca: un plan que no se entiende es mejor dejarlo como esta
            # para poder mirarlo a mano que reescribirlo con lo que se adivino.
            problemas.append(f"{ruta.name}: corrupto, se deja sin tocar ({exc})")
            continue

        dias = {d_: len(anclado.plan.dia(d_)) for d_ in ("L", "M", "W", "J", "V", "S", "D")}
        detalle = " ".join(f"{k}:{v}" for k, v in dias.items())
        if dry_run:
            print(f"  [v{version} -> v{VERSION_FORMATO}] {ruta.name}  ({detalle})")
        else:
            escribir_json(ruta, anclado.to_json())
            print(f"  [migrado] {ruta.name}  ({detalle})")
        migrados += 1

    return migrados, al_dia, problemas


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m sport_report.plan.migrar",
        description="Reescribe los planes guardados al formato v2",
    )
    p.add_argument(
        "--dry-run", action="store_true", help="dice que haria, sin escribir nada"
    )
    a = p.parse_args(argv)

    print(f"Planes en {config.DATA_DIR}" + ("  (dry-run)" if a.dry_run else ""))
    migrados, al_dia, problemas = migrar(dry_run=a.dry_run)

    if not migrados and not al_dia and not problemas:
        print("\nNo hay planes guardados todavia: nada que migrar.")
        return 0

    verbo = "se migrarian" if a.dry_run else "migrados"
    print(f"\n{verbo}: {migrados}   ya en v{VERSION_FORMATO}: {al_dia}")
    if problemas:
        print("\nProblemas:")
        for x in problemas:
            print(f"  - {x}")
        # Codigo distinto de 0: el despliegue tiene que enterarse.
        print("\n=> revisa los problemas de arriba", file=sys.stderr)
        return 1
    print("\n=> listo")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
