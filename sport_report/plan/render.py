"""Renderizado del plan para las respuestas de /setplan y /plan."""
from __future__ import annotations

from .models import ORDEN_DIAS, PlanSemanal, Sesion

# Diferencia tolerada entre el `cantidad` tecleado y la distancia dura derivada
# antes de avisar. No invalida el plan: solo advierte (spec 3).
TOLERANCIA_CANTIDAD = 0.05


def describir_sesion(s: Sesion) -> str:
    if s.tipo == "rest":
        return "descanso"
    if s.tipo == "fuerza":
        return "fuerza"

    partes = [s.tipo]
    if s.cantidad is not None:
        cant = f"{s.cantidad:g}{s.unidad}"
        partes.append(cant)
    if s.ritmo:
        partes.append(f"@{s.ritmo}")
    if s.zonas:
        partes.append("/".join(s.zonas))
    if s.estructura:
        partes.append(f"[{s.estructura.crudo}]")
        partes.append(f"-> {s.estructura.distancia_dura_km():g}km duros")
    return " ".join(partes)


def avisos(plan: PlanSemanal) -> list[str]:
    """Advertencias no bloqueantes detectadas al cargar el plan."""
    out: list[str] = []
    for d in plan.dias_sin_estimar():
        s = plan.sesiones[d]
        out.append(
            f"{s.nombre_dia}: prescrito en minutos y sin ritmo con que estimar km. "
            "Queda fuera del volumen planificado; agrega un ritmo (@5:30) si quieres "
            "que cuente."
        )
    for d in ORDEN_DIAS:
        s = plan.sesiones[d]
        if s.estructura is None or s.unidad != "km" or s.cantidad is None:
            continue
        dura = s.estructura.distancia_dura_km()
        if dura <= 0:
            continue
        if abs(dura - s.cantidad) / s.cantidad > TOLERANCIA_CANTIDAD:
            out.append(
                f"{s.nombre_dia}: tecleaste {s.cantidad:g}km pero `estructura=` suma "
                f"{dura:g}km duros. Se usara {dura:g}km para la adherencia."
            )
    return out


def resumen(plan: PlanSemanal, con_fuerza: bool = False) -> str:
    lineas = [f"Semana {plan.semana}"]
    for d in ORDEN_DIAS:
        s = plan.sesiones[d]
        texto = describir_sesion(s)
        if con_fuerza and s.es_fuerza:
            estado = "cumplida" if plan.fuerza_completada.get(d) else "pendiente"
            marca = "OK" if plan.fuerza_completada.get(d) else "..."
            texto = f"{texto}  [{marca} {estado}]"
        lineas.append(f"{d}  {texto}")
    lineas.append("")

    total = plan.volumen_planificado_km()
    estimado = plan.volumen_estimado_km()
    linea_vol = f"Volumen planificado: {total:g} km"
    if estimado:
        # El total incluye km que no estan escritos en ninguna linea del plan:
        # decirlo aca evita que el numero parezca salido de la nada.
        linea_vol += f"  ({estimado:g} estimados de sesiones por tiempo)"
    lineas.append(linea_vol)

    av = avisos(plan)
    if av:
        lineas.append("")
        lineas.append("Avisos:")
        lineas.extend(f"  - {a}" for a in av)
    return "\n".join(lineas)
