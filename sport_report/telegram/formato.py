"""Utilidades de texto para mensajes de Telegram. Sin dependencias externas."""
from __future__ import annotations

LIMITE_TELEGRAM = 4096


def trozos(texto: str, limite: int = LIMITE_TELEGRAM) -> list[str]:
    """Parte un mensaje largo respetando saltos de linea."""
    if len(texto) <= limite:
        return [texto]
    partes: list[str] = []
    actual: list[str] = []
    largo = 0
    for linea in texto.split("\n"):
        # Una sola linea mas larga que el limite se corta duro.
        while len(linea) > limite:
            if actual:
                partes.append("\n".join(actual))
                actual, largo = [], 0
            partes.append(linea[:limite])
            linea = linea[limite:]
        # `actual` puede estar vacio si la linea mide exactamente `limite`:
        # cerrar el trozo ahi metia un "" en la lista y Telegram rechaza un
        # mensaje vacio con un 400, tumbando el envio entero.
        if actual and largo + len(linea) + 1 > limite:
            partes.append("\n".join(actual))
            actual, largo = [], 0
        actual.append(linea)
        largo += len(linea) + 1
    if actual:
        partes.append("\n".join(actual))
    return partes


# --------------------------------------------------------------------------
# Reporte semanal
# --------------------------------------------------------------------------

# Telegram renderiza en tipografia proporcional, asi que no se intenta alinear
# columnas: cada dia va en una linea autocontenida que se lee bien en el telefono.
MARCAS = {
    "cumplida": "OK",
    "bajo_plan": "-",
    "sobre_plan": "+",
    "sin_sesion": "NO",
    "dato_faltante": "?",
    "descanso_respetado": ".",
    "actividad_no_planificada": "!",
    "fuerza_cumplida": "OK",
    "fuerza_pendiente": "..",
    "pendiente": " ",
}

MESES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)

NOMBRE_DIA = {
    "L": "lun", "M": "mar", "W": "mie", "J": "jue", "V": "vie", "S": "sab", "D": "dom",
}


def _fecha_larga(iso: str) -> str:
    a, m, d = (int(x) for x in iso.split("-"))
    return f"{d} de {MESES[m - 1]}"


def _num(v, sufijo: str = "", dec: int = 1) -> str:
    if v is None:
        return "s/d"
    if isinstance(v, float) and v == int(v):
        return f"{int(v)}{sufijo}"
    return f"{round(v, dec)}{sufijo}" if isinstance(v, (int, float)) else f"{v}{sufijo}"


def _bloque_confiable(datos: dict, lineas: list[str]) -> None:
    if not datos.get("confiable", True) and datos.get("motivo"):
        lineas.append(f"  (no confiable: {datos['motivo']})")


def _cola_fuerza(d: dict) -> str:
    """Sufijo cuando el dia tiene fuerza ADEMAS de una corrida.

    Con la fuerza sola en su dia, el estado ya la describe y no hace falta.
    """
    fz = d.get("fuerza")
    if not fz:
        return ""
    estado = "cumplida" if fz.get("estado") == "fuerza_cumplida" else "pendiente"
    return f" + fuerza ({estado})"


def _linea_dia(d: dict) -> str:
    marca = MARCAS.get(d["estado"], "?")
    dia = NOMBRE_DIA.get(d["dia"], d["dia"])
    tipo = d["tipo_plan"]

    if d["estado"] == "pendiente":
        u = d["unidad"] or ""
        objetivo = f"{tipo} {_num(d['objetivo'], u)}" if d["objetivo"] is not None else tipo
        # El dia entero esta pendiente, asi que la fuerza tambien: repetir
        # "(pendiente)" dos veces en la misma linea solo estorba.
        cola = " + fuerza" if d.get("fuerza") else ""
        return f"[{marca}] {dia}  {objetivo} (pendiente){cola}"

    if tipo == "rest":
        cola = d["nota"] if d["nota"] else "descanso"
        return f"[{marca}] {dia}  {cola}"
    if tipo == "fuerza":
        estado = "fuerza cumplida" if d["estado"] == "fuerza_cumplida" else "fuerza pendiente"
        return f"[{marca}] {dia}  {estado}"

    u = d["unidad"] or ""
    obj = _num(d["objetivo"], u)
    if d["real"] is None:
        real = "sin dato" if d["estado"] == "dato_faltante" else "no registrada"
        return f"[{marca}] {dia}  {tipo} {obj} -> {real}{_cola_fuerza(d)}"
    linea = f"[{marca}] {dia}  {tipo} {obj} -> {_num(d['real'], u)} ({_num(d['pct'], '%')})"
    # La comparacion de una sesion con `estructura=` deja fuera la recuperacion
    # trotada. Sin decirlo, la linea parece contradecir el volumen de la semana.
    # Va pegada a la cifra de la corrida, antes de la fuerza, que es de otra cosa.
    if d.get("recuperacion_km"):
        linea += f" +{_num(d['recuperacion_km'], 'km')} rec"
    return linea + _cola_fuerza(d)


def formatear_reporte(datos: dict, narrativa: str | None = None) -> str:
    """Arma el mensaje de Telegram a partir del JSON del motor de calculo.

    No calcula nada: todo lo que aparece aca sale del JSON.
    """
    s = datos["semana"]
    en_curso = bool(s.get("en_curso"))
    L: list[str] = []

    titulo = "AVANCE DE LA SEMANA" if en_curso else "REPORTE SEMANAL"
    cabecera = f"{titulo} - {_fecha_larga(s['inicio'])} al {_fecha_larga(s['fin'])}"
    if en_curso:
        cabecera += (
            f"\nAl {_fecha_larga(s['hasta'])}: {s['dias_transcurridos']} de 7 dias"
        )
    if s.get("numero_plan") is not None:
        cabecera += f"\nPlan: semana {s['numero_plan']}"
    L.append(cabecera)

    if narrativa:
        L.append(narrativa)

    vol = datos["volumen"]
    carga = datos["carga"]
    sufijo_plan = " planificados hasta hoy" if en_curso else " planificados"
    v = [
        "VOLUMEN",
        f"  {_num(vol['real_km'], ' km')} reales / {_num(vol['planificado_km'], ' km')}"
        f"{sufijo_plan} ({_num(vol['pct'], '%')})",
    ]
    if vol.get("estimado_km"):
        v.append(
            f"  ({_num(vol['estimado_km'], ' km')} del plan estimados de sesiones "
            "prescritas en minutos)"
        )
    v.append(
        f"  carga semanal {_num(carga['semanal'])}"
        + (" (imprecisa)" if carga.get("impreciso") else "")
    )
    L.append("\n".join(v))

    a = datos["acwr"]
    f = datos["monotony"]
    c = ["CARGA"]
    c.append(f"  ACWR {_num(a['ratio'], '', 2)}  (aguda {_num(a['aguda'])} / cronica {_num(a['cronica'])})")
    _bloque_confiable(a, c)
    c.append(f"  Monotony {_num(f['monotony'], '', 2)}  Strain {_num(f['strain'])}")
    _bloque_confiable(f, c)
    L.append("\n".join(c))

    dc = datos["deriva_cardiaca"]
    if dc["n_sesiones"]:
        L.append(
            "DERIVA CARDIACA\n"
            f"  promedio {_num(dc['promedio_pct'], '%')}  maximo {_num(dc['maximo_pct'], '%')}"
            f"  ({dc['n_sesiones']} sesiones)"
        )

    cad = datos["cadencia"]
    if cad["valor"] is not None:
        linea = f"CADENCIA\n  {_num(cad['valor'], ' spm')}"
        if cad["delta"] is not None:
            signo = "+" if cad["delta"] >= 0 else ""
            linea += f"  ({signo}{_num(cad['delta'])} vs semana anterior)"
        L.append(linea)

    adh = datos["adherencia"]
    if adh["dias"]:
        cab = "ADHERENCIA"
        if adh["pct_global"] is not None:
            cab += (
                f"  {_num(adh['pct_global'], '%')} "
                f"({adh['sesiones_cumplidas']}/{adh['sesiones_evaluables']} sesiones)"
            )
        L.append("\n".join([cab] + [f"  {_linea_dia(d)}" for d in adh["dias"]]))

        fz = adh["fuerza"]
        if fz["planificadas"]:
            L.append(f"FUERZA\n  {fz['cumplidas']}/{fz['planificadas']} cumplidas")

    if datos["alertas"]:
        L.append("\n".join(["ALERTAS"] + [f"  ! {x}" for x in datos["alertas"]]))

    if datos["avisos_datos"]:
        L.append("\n".join(["SOBRE LOS DATOS"] + [f"  - {x}" for x in datos["avisos_datos"]]))

    return "\n\n".join(L)


# --------------------------------------------------------------------------
# Resumen ejecutivo de la semana en curso (/progreso)
# --------------------------------------------------------------------------

# Hubo actividad ese dia, se ajustara mas o menos al plan.
_CON_ACTIVIDAD = ("cumplida", "bajo_plan", "sobre_plan", "dato_faltante", "fuerza_cumplida")
# El dia paso y no hubo nada.
_PERDIDOS = ("sin_sesion", "fuerza_pendiente")


def _que_falta(d: dict) -> str:
    """Una linea por sesion pendiente: que es y de que tamano."""
    dia = NOMBRE_DIA.get(d["dia"], d["dia"])
    if d["tipo_plan"] == "fuerza":
        return f"  {dia}  fuerza"
    u = d["unidad"] or ""
    if d["objetivo"] is None:
        return f"  {dia}  {d['tipo_plan']}"
    return f"  {dia}  {d['tipo_plan']} {_num(d['objetivo'], u)}"


def formatear_progreso(datos: dict) -> str:
    """Resumen corto de como va la semana. No es el reporte semanal.

    Responde dos preguntas y nada mas: cuantos km llevo de los programados y
    que me queda por hacer. Las metricas de carga viven en el reporte del lunes.
    """
    s = datos["semana"]
    vol = datos["volumen"]
    adh = datos["adherencia"]

    llevo = vol["real_km"] or 0.0
    programado = vol.get("planificado_semana_km") or 0.0
    L = [
        f"SEMANA {_fecha_larga(s['inicio'])} al {_fecha_larga(s['fin'])}",
        f"Al {_fecha_larga(s['hasta'])}",
    ]

    if not adh["dias"]:
        return "\n".join(L + ["", f"{_num(llevo, ' km')} corridos.", "No hay plan cargado para esta semana."])

    pct = round(llevo / programado * 100) if programado else None
    linea = f"{_num(llevo, ' km')} de {_num(programado, ' km')} programados"
    if pct is not None:
        linea += f" ({pct}%)"
    L.append("")
    L.append(linea)

    # Un descanso por venir no es algo "por hacer": no entra en la lista.
    pendientes = [
        d for d in adh["dias"] if d["estado"] == "pendiente" and d["tipo_plan"] != "rest"
    ]
    # Lo que falta se mide contra el plan, no contra la resta de totales: si ya
    # te pasaste en una sesion, eso no descuenta de lo que queda por correr.
    faltan_km = sum(
        d["objetivo"] for d in pendientes if d["unidad"] == "km" and d["objetivo"]
    )
    entrenamientos = [d for d in adh["dias"] if d["tipo_plan"] != "rest"]
    hechos = [d for d in entrenamientos if d["estado"] in _CON_ACTIVIDAD]
    perdidos = [d for d in entrenamientos if d["estado"] in _PERDIDOS]

    L.append(f"{len(hechos)} de {len(entrenamientos)} entrenamientos hechos")
    if faltan_km:
        L.append(f"Quedan {_num(round(faltan_km, 1), ' km')} en el plan.")
    if perdidos:
        dias = ", ".join(NOMBRE_DIA.get(d["dia"], d["dia"]) for d in perdidos)
        L.append(f"Sin registrar: {dias}.")

    if pendientes:
        L.append("")
        L.append("QUEDA ESTA SEMANA")
        L.extend(_que_falta(d) for d in pendientes)
    else:
        L.append("")
        L.append("La semana esta completa: no queda nada por hacer.")

    fz = adh["fuerza"]
    if fz["planificadas"] and fz["cumplidas"] < fz["planificadas"]:
        L.append("")
        L.append(
            f"Fuerza: {fz['cumplidas']}/{fz['planificadas']} cumplidas "
            "(marca con /fuerza <dia> si no quedo en Strava)."
        )

    return "\n".join(L)
