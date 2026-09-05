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
    "pendiente": " ",
}

# Bloque lleno U+2588. Telegram lo renderiza bien; en consola Windows hace falta
# stdout en UTF-8 (run_weekly lo fuerza al arrancar).
BARRA = "█"
ANCHO_GRAFICO = 16

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


def _dia_mes(iso: str) -> str:
    a, m, d = iso.split("-")
    return f"{d}/{m}"


def _lunes_iso(iso: str) -> str:
    """Lunes de la semana que contiene esa fecha, en ISO."""
    from datetime import date, timedelta

    d = date.fromisoformat(iso)
    return (d - timedelta(days=d.weekday())).isoformat()


def grafico_volumen(historico: dict, ancho: int = ANCHO_GRAFICO) -> str:
    """Barras de km por semana, escaladas a la semana de mayor volumen.

    Sin dependencias y sin imagenes: el sistema entrega texto por Telegram y un
    grafico de barras en caracteres se lee igual de bien en el telefono.
    """
    semanas = historico.get("semanas") or []
    if not semanas:
        return ""
    maximo = max(s["km"] for s in semanas)
    if maximo <= 0:
        return ""

    ultima_en_curso = bool(historico.get("ultima_en_curso"))
    L = [f"VOLUMEN ULTIMAS {len(semanas)} SEMANAS (km)"]
    for i, s in enumerate(semanas):
        km = s["km"]
        largo = int(round(km / maximo * ancho))
        # Una semana con kilometros no puede dibujarse vacia: se confundiria
        # con una de descanso total.
        if km > 0 and largo == 0:
            largo = 1
        # Sin esta marca, la semana a medio correr parece un desplome de volumen.
        cola = " (en curso)" if ultima_en_curso and i == len(semanas) - 1 else ""
        L.append(
            f"  {_dia_mes(s['lunes'])} {BARRA * largo}{' ' * (ancho - largo)} {km:g}{cola}"
        )

    # Se compara el LUNES de la primera fecha con datos, no la fecha suelta: si
    # el historico empieza un martes, esa semana igual tiene datos y avisar
    # sobraria.
    desde = historico.get("primera_fecha_con_datos")
    if desde and _lunes_iso(desde) > semanas[0]["lunes"]:
        L.append(f"  (sin historico antes del {_dia_mes(desde)}: esas semanas van en cero)")
    return "\n".join(L)


def _linea_dia(d: dict) -> str:
    marca = MARCAS.get(d["estado"], "?")
    dia = NOMBRE_DIA.get(d["dia"], d["dia"])
    tipo = d["tipo_plan"]

    if d["estado"] == "pendiente":
        u = d["unidad"] or ""
        objetivo = f"{tipo} {_num(d['objetivo'], u)}" if d["objetivo"] is not None else tipo
        return f"[{marca}] {dia}  {objetivo} (pendiente)"

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


def formatear_reporte(
    datos: dict, narrativa: str | None = None, con_grafico: bool = True
) -> str:
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

    if con_grafico and datos.get("volumen_historico"):
        g = grafico_volumen(datos["volumen_historico"])
        if g:
            L.append(g)

    if datos["alertas"]:
        L.append("\n".join(["ALERTAS"] + [f"  ! {x}" for x in datos["alertas"]]))

    if datos["avisos_datos"]:
        L.append("\n".join(["SOBRE LOS DATOS"] + [f"  - {x}" for x in datos["avisos_datos"]]))

    return "\n\n".join(L)
