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
        if largo + len(linea) + 1 > limite:
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


def _linea_dia(d: dict) -> str:
    marca = MARCAS.get(d["estado"], "?")
    dia = NOMBRE_DIA.get(d["dia"], d["dia"])
    tipo = d["tipo_plan"]

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
        return f"[{marca}] {dia}  {tipo} {obj} -> {real}"
    return f"[{marca}] {dia}  {tipo} {obj} -> {_num(d['real'], u)} ({_num(d['pct'], '%')})"


def formatear_reporte(datos: dict, narrativa: str | None = None) -> str:
    """Arma el mensaje de Telegram a partir del JSON del motor de calculo.

    No calcula nada: todo lo que aparece aca sale del JSON.
    """
    s = datos["semana"]
    L: list[str] = []

    cabecera = f"REPORTE SEMANAL - {_fecha_larga(s['inicio'])} al {_fecha_larga(s['fin'])}"
    if s.get("numero_plan") is not None:
        cabecera += f"\nPlan: semana {s['numero_plan']}"
    L.append(cabecera)

    if narrativa:
        L.append(narrativa)

    vol = datos["volumen"]
    carga = datos["carga"]
    v = [
        "VOLUMEN",
        f"  {_num(vol['real_km'], ' km')} reales / {_num(vol['planificado_km'], ' km')} "
        f"planificados ({_num(vol['pct'], '%')})",
        f"  carga semanal {_num(carga['semanal'])}"
        + (" (imprecisa)" if carga.get("impreciso") else ""),
    ]
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
