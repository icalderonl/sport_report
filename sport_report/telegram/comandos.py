"""Logica de los comandos, sin dependencia de la libreria de Telegram.

Cada funcion recibe el texto crudo del usuario y devuelve el texto de respuesta.
Asi los comandos se testean enteros sin red ni bot token.
"""
from __future__ import annotations

import unicodedata
from datetime import date, timedelta
from typing import Any

from .. import fechas
from ..fechas import RangoSemana, semana_a_reportar
from ..plan.errors import PlanInvalido
from ..plan.carrera import Carrera, CarreraInvalida, CarreraStore, parse_fecha
from ..plan.grammar import parse_dias, parse_plan
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

  Los 7 dias son obligatorios (usa `rest` si no hay sesion). Un dia puede
  tener mas de una linea (por ejemplo `J: easy 8km Z2` y `J: fuerza`).
  `/setplan proxima` ancla el plan a la semana siguiente.

/corregir <dia>: <sesion> — arregla un dia sin reenviar la semana entera.
  Reemplaza TODAS las sesiones de ese dia:

  /corregir J: series 8.6km @Z4 estructura=2km+3x1000m+4x400m+2km

  Para dejar dos sesiones ese dia, manda las dos lineas en el mismo comando.

/carrera <YYYY-MM-DD> [nombre] — declara la carrera objetivo, que se conserva
  entre semanas (`/setplan` no la borra). `/carrera` la consulta y
  `/carrera borrar` la quita.

/plan — muestra el plan cargado y el estado de las sesiones de fuerza.
/fuerza <dia> — marca una fuerza como cumplida (L M W J V S D o el nombre).
  Solo hace falta si la sesion no quedo registrada en la fuente de datos.
  Se aplica a la semana en curso; agrega `anterior` o `proxima` para otra
  (`/fuerza D anterior` el lunes, para el domingo que acaba de pasar).
/progreso — resumen corto de la semana en curso: cuantos km llevas de los
  programados y que entrenamientos te quedan. Sincroniza los datos primero.
/volumen — grafico de los kilometros por semana de las ultimas 16 semanas.
/estado — que semana reportaria el cron ahora y con que plan.
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


_SUFIJO_PROXIMA = ("proxima", "siguiente", "next")
_SUFIJO_ANTERIOR = ("anterior", "pasada", "previa")


def partir_fuerza(arg: str, hoy: date) -> tuple[str, RangoSemana]:
    """Separa `/fuerza <dia> [anterior|proxima]` en (dia, semana destino).

    Por defecto la semana en curso, NO el plan vigente: si el domingo se cargo
    el plan de la semana siguiente con `/setplan proxima`, el vigente ya es
    otro y marcar sobre el ponia la fuerza en una semana que aun no empieza.
    """
    tokens = arg.split()
    destino = fechas.semana_de(hoy)
    if len(tokens) >= 2:
        cola = _sin_tildes(tokens[-1].lower())
        if cola in _SUFIJO_PROXIMA:
            return " ".join(tokens[:-1]), fechas.semana_siguiente(destino)
        if cola in _SUFIJO_ANTERIOR:
            return " ".join(tokens[:-1]), fechas.semana_anterior(destino)
    return arg, destino


def cmd_fuerza(store: PlanStore, arg: str, hoy: date | None = None) -> str:
    dia_texto, rango = partir_fuerza(arg, hoy or fechas.hoy_local())
    try:
        dia = parse_dia(dia_texto)
    except ValueError as exc:
        return _cap(str(exc))
    try:
        anclado = store.marcar_fuerza(dia, True, rango)
    except FileNotFoundError:
        return f"No hay plan guardado para la semana {rango}. Usa /setplan."
    except DiaSinFuerza as exc:
        return _cap(str(exc))
    return (
        f"Fuerza del {DIAS[dia]} ({anclado.rango}) marcada como cumplida.\n\n"
        f"{resumen(anclado.plan, con_fuerza=True)}"
    )


def cmd_corregir(
    store: PlanStore, texto: str, hoy: date | None = None
) -> str:
    """Reemplaza las sesiones de uno o mas dias del plan vigente.

    Usa `parse_dias`, que es el mismo parser y los mismos mensajes de error que
    `/setplan`: no hay una segunda ruta de validacion que se pueda
    desincronizar de la primera.
    """
    dia_hoy = hoy or fechas.hoy_local()
    anclado = store.cargar()
    if anclado is None:
        return "No hay plan cargado que corregir. Usa /setplan.\n\n" + AYUDA

    # Corregir un dia de una semana que no es la cargada casi siempre significa
    # que el plan vigente es otro: mejor decirlo que escribir sobre el plan
    # equivocado.
    if not (anclado.rango.inicio <= dia_hoy <= anclado.rango.fin):
        return (
            f"El plan vigente cubre {anclado.rango} y hoy es {dia_hoy}: no es la "
            "semana que estarias corrigiendo. Carga la semana en curso con "
            "/setplan."
        )

    try:
        dias = parse_dias(texto)
    except PlanInvalido as exc:
        return exc.render()

    for dia, sesiones in dias.items():
        try:
            anclado = store.reemplazar_dia(dia, sesiones, anclado.rango)
        except (FileNotFoundError, ValueError) as exc:
            return _cap(str(exc))

    nombres = ", ".join(DIAS[d] for d in ORDEN_DIAS if d in dias)
    return (
        f"Corregido: {nombres} ({anclado.rango}).\n\n"
        f"{resumen(anclado.plan, con_fuerza=True)}"
    )


_BORRAR = ("borrar", "quitar", "eliminar", "none", "ninguna")


def cmd_carrera(store: CarreraStore, arg: str, hoy: date | None = None) -> str:
    """Declara, consulta o borra la carrera objetivo."""
    dia = hoy or fechas.hoy_local()
    arg = (arg or "").strip()

    if not arg:
        carrera = store.cargar()
        if carrera is None:
            return (
                "No hay carrera declarada. Se declara asi:\n"
                "  /carrera 2026-11-15 Maraton de Santiago"
            )
        return _describir_carrera(carrera, dia)

    if _sin_tildes(arg.lower()) in _BORRAR:
        if store.borrar():
            return "Carrera borrada."
        return "No habia ninguna carrera declarada."

    partes = arg.split(None, 1)
    try:
        fecha = parse_fecha(partes[0], dia)
    except CarreraInvalida as exc:
        return _cap(str(exc))

    nombre = partes[1].strip() if len(partes) > 1 else ""
    carrera = store.guardar(Carrera(fecha=fecha, nombre=nombre))
    return "Carrera declarada.\n" + _describir_carrera(carrera, dia)


def _describir_carrera(carrera: Carrera, hoy: date) -> str:
    c = carrera.contexto(hoy)
    etiqueta = f"{c['fecha']}" + (f" — {c['nombre']}" if c["nombre"] else "")
    return f"{etiqueta}\n  {_cap(c['nota'])}"


def cmd_progreso(
    store: PlanStore,
    repo: Any = None,
    ingesta: Any = None,
    hoy: date | None = None,
    sincronizar: bool = True,
) -> str:
    """Resumen ejecutivo de la semana en curso.

    Usa el mismo motor de calculo que el reporte del lunes con `hasta=hoy` —una
    sola fuente de cifras— pero lo formatea corto: km contra lo programado y
    que queda por hacer. Las metricas de carga se quedan en el reporte semanal.

    Los imports pesados van adentro para que este modulo se siga pudiendo
    importar (y testear) sin httpx ni el SDK de Strava.
    """
    from ..engine import report
    from ..fechas import hoy_local, semana_de
    from .formato import formatear_progreso

    dia = hoy or hoy_local()
    rango = semana_de(dia)

    repo_propio = repo is None
    if repo_propio:
        from ..db.repo import Repo

        repo = Repo()

    resultado = None
    avisos: list[str] = []
    try:
        if ingesta is None and sincronizar:
            # El selector elige la fuente y cae al respaldo por su cuenta; aca
            # solo interesa que la respuesta diga con que datos se armo.
            from .. import fuentes

            try:
                resultado = fuentes.sincronizar(
                    rango.inicio, dia, repo, store, semana=rango.clave
                )
                if not resultado.ok:
                    avisos.append(
                        f"no se pudo sincronizar con ninguna fuente ({resultado.motivo}); "
                        "se muestra lo ya guardado"
                    )
                elif resultado.fallback:
                    avisos.append(
                        f"datos desde {resultado.fuente} (respaldo): sin GCT, "
                        "oscilacion vertical, ratio vertical ni bienestar"
                    )
            except Exception as exc:  # sin credenciales, sin red, sin SDK
                avisos.append(f"no se pudo abrir la conexion con la fuente de datos ({exc})")
        elif ingesta is not None:
            try:
                ingesta.sincronizar(rango.inicio, dia)
            except Exception as exc:
                # Igual que la corrida semanal: se responde con lo que hay.
                avisos.append(
                    f"no se pudo sincronizar ({exc}); se muestra lo ya guardado"
                )

        datos = report.construir(rango, repo, store, hasta=dia)
        texto = formatear_progreso(datos)
        if avisos:
            texto += "\n\n" + "\n".join(f"({a})" for a in avisos)
        return texto
    finally:
        if resultado is not None:
            resultado.cerrar()
        if repo_propio:
            repo.cerrar()


def cmd_volumen(
    repo: Any = None,
    hoy: date | None = None,
    semanas: int | None = None,
    destino: Any = None,
) -> tuple[Any, str]:
    """Grafico de km por semana. Devuelve (ruta_png | None, texto)."""
    from .. import config
    from ..fechas import hoy_local

    dia = hoy or hoy_local()
    n = semanas or config.SEMANAS_GRAFICO

    repo_propio = repo is None
    if repo_propio:
        from ..db.repo import Repo

        repo = Repo()
    try:
        serie = repo.volumen_semanal(dia, n)
        primera = repo.primera_fecha()
    finally:
        if repo_propio:
            repo.cerrar()

    historico = {
        "semanas": [{"lunes": d.isoformat(), "km": v} for d, v in serie],
        "primera_fecha_con_datos": primera.isoformat() if primera else None,
        # La ultima barra va marcada si esa semana todavia no termina. `<=`,
        # no `<`: el domingo la semana sigue en curso (misma regla que
        # `report.construir`), y con `<` la barra se pintaba como cerrada.
        "ultima_en_curso": bool(serie) and dia <= serie[-1][0] + timedelta(days=6),
    }
    if not any(s["km"] for s in historico["semanas"]):
        return None, (
            "Todavia no hay kilometros registrados.\n"
            "Corre: python -m sport_report.backfill 120"
        )

    from ..grafico import volumen_png

    try:
        ruta = volumen_png(historico, destino)
    except Exception as exc:  # matplotlib ausente, o fallo al dibujar
        return None, f"No se pudo generar el grafico: {exc}"

    total = sum(s["km"] for s in historico["semanas"])
    return ruta, f"Volumen de las ultimas {n} semanas ({total:g} km en total)."


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
