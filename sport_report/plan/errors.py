"""Errores de parseo. Siempre con linea y dia exactos; nunca fallar en silencio."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorPlan:
    linea: int
    dia: str | None
    mensaje: str

    def __str__(self) -> str:
        from .models import DIAS

        ubic = f"Linea {self.linea}"
        if self.dia:
            ubic += f", dia {DIAS[self.dia].capitalize()}"
        return f"{ubic}: {self.mensaje}"


class PlanInvalido(Exception):
    """El plan completo se rechaza. Nunca se acepta parcialmente."""

    def __init__(self, errores: list[ErrorPlan]):
        self.errores = errores
        super().__init__(self.render())

    def render(self) -> str:
        cabecera = "Plan rechazado. No se cargo nada." if len(self.errores) else "Plan rechazado."
        cuerpo = "\n".join(f"  - {e}" for e in self.errores)
        return f"{cabecera}\n{cuerpo}"
