"""Logica de los comandos, sin dependencia de la libreria de Telegram.

Cada funcion recibe el texto crudo del usuario y devuelve el texto de respuesta.
Asi los comandos se testean enteros sin red ni bot token.
"""
from __future__ import annotations

import unicodedata

from ..fechas import semana_a_reportar
from ..plan.errors import PlanInvalido
from ..plan.grammar import parse_plan
from ..plan.models import DIAS, ORDEN_DIAS
from ..plan.render import resumen
from ..plan.store import DiaSinFuerza, PlanStore, resolver_rango

AYUDA = """Comandos:

/setplan — carga el plan de la semana en curso.
  Escribe el plan en las lineas siguientes al comando:

  /setplan
  semana: 5
  L: rest
  M: easy 8km Z2
  W: fuerza
  J: series 8.6km @Z4 estructura=2km+3x1000m+4x400m+2km
  V: prog 10km estructura=3km@6:00+3km@5:30+4km@5:00
  S: rest
  D: long 16km @6:15/5:45 Z2

  Los 7 dias son obligatorios (usa `rest` si no hay sesion).
  `/setplan proxima` ancla el plan a la semana siguiente.

/plan — muestra el plan cargado y el estado de las sesiones de fuerza.
/fuerza <dia> — marca una fuerza como cumplida (L M W J V S D o el nombre).
  Solo hace falta si la sesion no quedo registrada en Strava.
"""


def _sin_tildes(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if not unicodedata.combining(c))


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def _separar(texto: str) -> tuple[str, str]:
    """Separa `/setplan [sufijo]` de las lineas del plan."""
    lineas = texto.splitlines()
    primera = lineas[0].strip() if lineas else ""
    sufijo = ""
    if primera.lower().startswith("/setplan"):
        sufijo = primera[len("/setplan") :].strip()
        # Telegram agrega @nombre_del_bot en grupos.
        if sufijo.startswith("@"):
            sufijo = sufijo.split(None, 1)[1] if " " in sufijo else ""
        lineas = lineas[1:]
    return sufijo, "\n".join(lineas)


def parse_dia(arg: str) -> str:
    """Acepta la letra del dia o su nombre (con o sin tildes, en cualquier caso)."""
    a = _sin_tildes(arg.strip().lower())
    if not a:
        raise ValueError("falta el dia: `/fuerza W` o `/fuerza miercoles`")
    if len(a) == 1 and a.upper() in ORDEN_DIAS:
        return a.upper()
    for letra, nombre in DIAS.items():
        if a == nombre or a == nombre[:3]:
            return letra
    validos = ", ".join(f"{l}={DIAS[l]}" for l in ORDEN_DIAS)
    raise ValueError(f"dia '{arg}' no reconocido. Validos: {validos}")


# --------------------------------------------------------------------------


def cmd_setplan(store: PlanStore, texto: str) -> str:
    sufijo, cuerpo = _separar(texto)
    try:
        rango = resolver_rango(sufijo)
    except ValueError as exc:
        return _cap(str(exc))

    if not cuerpo.strip():
        return "Falta el plan. Escribelo en las lineas siguientes al comando.\n\n" + AYUDA

    try:
        plan = parse_plan(cuerpo)
    except PlanInvalido as exc:
        return exc.render()

    anclado = store.guardar(plan, rango)
    return (
        f"Plan cargado para la semana {anclado.rango}.\n\n"
        f"{resumen(anclado.plan, con_fuerza=True)}"
    )


def cmd_plan(store: PlanStore) -> str:
    anclado = store.cargar()
    if anclado is None:
        return "No hay plan cargado. Usa /setplan.\n\n" + AYUDA
    cabecera = f"Plan vigente — semana {anclado.rango}"
    if anclado.cargado_en:
        cabecera += f"\n(cargado el {anclado.cargado_en})"
    return f"{cabecera}\n\n{resumen(anclado.plan, con_fuerza=True)}"


def cmd_fuerza(store: PlanStore, arg: str) -> str:
    try:
        dia = parse_dia(arg)
    except ValueError as exc:
        return _cap(str(exc))
    try:
        anclado = store.marcar_fuerza(dia)
    except FileNotFoundError:
        return "No hay plan cargado. Usa /setplan."
    except DiaSinFuerza as exc:
        return _cap(str(exc))
    return f"Fuerza del {DIAS[dia]} marcada como cumplida.\n\n{resumen(anclado.plan, con_fuerza=True)}"


def cmd_estado(store: PlanStore) -> str:
    """Diagnostico rapido: que semana reportaria el cron ahora y con que plan."""
    objetivo = semana_a_reportar()
    anclado = store.para_semana(objetivo)
    if anclado is None:
        return f"Semana a reportar: {objetivo}\nNo hay plan guardado para esa semana."
    return (
        f"Semana a reportar: {objetivo}\n"
        f"Plan encontrado: semana {anclado.plan.semana} "
        f"({anclado.plan.volumen_planificado_km():g} km planificados)"
    )
