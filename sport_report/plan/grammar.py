"""Parser del formato de /setplan.

Reglas duras (spec 3):
  - Los 7 dias deben aparecer. Un plan incompleto se rechaza completo.
  - Cualquier linea que no matchee rechaza el plan completo, senalando linea y dia.
  - Nunca se asume descanso implicito ni se adivina la intencion.
  - Todo el parseo es insensible a mayusculas/minusculas.
  - No existe campo `volumen_objetivo`: el volumen se deriva de las sesiones.
"""
from __future__ import annotations

import re

from .errors import ErrorPlan, PlanInvalido
from .models import (
    DIAS,
    ORDEN_DIAS,
    TIPOS_ESTRUCTURADOS,
    TIPOS_SIMPLES,
    TIPOS_SIN_CANTIDAD,
    TIPOS_VALIDOS,
    Bloque,
    Estructura,
    PlanSemanal,
    Ritmo,
    Sesion,
)

RE_SEMANA = re.compile(r"^semana\s*:\s*(\d{1,3})$", re.I)
RE_LINEA_DIA = re.compile(r"^([lmwjvsd])\s*:\s*(.*)$", re.I)

RE_CANTIDAD = re.compile(r"^(\d+(?:\.\d+)?)(km|k|min)$", re.I)
RE_ZONA = re.compile(r"^@?z([1-5])(?:/z([1-5]))?$", re.I)
_MMSS = r"\d{1,2}:[0-5]\d"
RE_RITMO = re.compile(rf"^@({_MMSS})(?:/({_MMSS}))?$", re.I)
RE_ESTRUCTURA = re.compile(r"^estructura\s*=\s*(.+)$", re.I)

RE_DIST = re.compile(r"^(\d+(?:\.\d+)?)(km|k|m)$", re.I)
RE_REP = re.compile(r"^(\d+)x(\d+(?:\.\d+)?)(km|k|m)$", re.I)
RE_BLOQUE_PROG = re.compile(rf"^(\d+(?:\.\d+)?)(km|k|m)@({_MMSS})$", re.I)


def _a_segundos(mmss: str) -> int:
    m, s = mmss.split(":")
    return int(m) * 60 + int(s)


def _dist_a_km(valor: str, unidad: str) -> float:
    n = float(valor)
    return n / 1000.0 if unidad.lower() == "m" else n


def _parse_ritmo(m: re.Match[str]) -> Ritmo:
    a = _a_segundos(m.group(1))
    b = _a_segundos(m.group(2)) if m.group(2) else a
    return Ritmo(min_s_km=min(a, b), max_s_km=max(a, b))


# --------------------------------------------------------------------------
# estructura=
# --------------------------------------------------------------------------


def parse_estructura(crudo: str, tipo: str) -> Estructura:
    """Parsea `estructura=`. Lanza ValueError con un mensaje accionable."""
    crudo = crudo.strip()
    if not crudo:
        raise ValueError("`estructura=` esta vacia")

    bloques: list[Bloque] = []
    for token in crudo.split("+"):
        token = token.strip()
        if not token:
            raise ValueError(f"bloque vacio en `estructura={crudo}` (revisa los '+')")

        if tipo == "prog":
            m = RE_BLOQUE_PROG.match(token)
            if not m:
                raise ValueError(
                    f"bloque '{token}' invalido en `prog`: cada bloque necesita "
                    "distancia con unidad y su propio ritmo (ej. 3km@5:30)"
                )
            seg = _a_segundos(m.group(3))
            bloques.append(
                Bloque(
                    reps=1,
                    distancia_km=_dist_a_km(m.group(1), m.group(2)),
                    ritmo=Ritmo(seg, seg),
                    crudo=token,
                )
            )
            continue

        m = RE_REP.match(token)
        if m:
            reps = int(m.group(1))
            if reps < 1:
                raise ValueError(f"bloque '{token}': las repeticiones deben ser >= 1")
            bloques.append(
                Bloque(reps=reps, distancia_km=_dist_a_km(m.group(2), m.group(3)), crudo=token)
            )
            continue

        m = RE_DIST.match(token)
        if m:
            bloques.append(
                Bloque(reps=1, distancia_km=_dist_a_km(m.group(1), m.group(2)), crudo=token)
            )
            continue

        if RE_BLOQUE_PROG.match(token):
            raise ValueError(
                f"bloque '{token}': el ritmo por bloque solo se admite en sesiones `prog`"
            )
        raise ValueError(
            f"bloque '{token}' invalido: falta la unidad o el formato no se reconoce "
            "(usa 2km, 600m o 3x1000m; las unidades son obligatorias en cada bloque)"
        )

    return Estructura(bloques=tuple(bloques), crudo=crudo)


def distancia_dura_km(crudo: str, tipo: str = "series") -> float:
    """Atajo publico: distancia dura a partir del string de `estructura=`."""
    return parse_estructura(crudo, tipo).distancia_dura_km()


# --------------------------------------------------------------------------
# linea de dia
# --------------------------------------------------------------------------


def _parse_sesion(dia: str, cuerpo: str) -> Sesion:
    """Parsea el cuerpo de una linea de dia. Lanza ValueError."""
    tokens = cuerpo.split()
    if not tokens:
        raise ValueError("linea sin contenido; usa `rest` si no hay sesion")

    tipo = tokens[0].lower()
    if tipo not in TIPOS_VALIDOS:
        raise ValueError(
            f"tipo de sesion '{tokens[0]}' no reconocido "
            f"(validos: {', '.join(TIPOS_VALIDOS)})"
        )

    resto = tokens[1:]

    if tipo in TIPOS_SIN_CANTIDAD:
        if resto:
            raise ValueError(f"`{tipo}` no admite cantidad, ritmo, zona ni estructura")
        return Sesion(dia=dia, tipo=tipo, crudo=cuerpo)

    if not resto:
        raise ValueError(f"`{tipo}` requiere una cantidad (ej. 8km, 45min)")

    m = RE_CANTIDAD.match(resto[0])
    if not m:
        raise ValueError(
            f"cantidad '{resto[0]}' invalida: numero y unidad pegados, sin espacio "
            "(km, k o min; ej. 8km, 10.5km, 45min)"
        )
    cantidad = float(m.group(1))
    if cantidad <= 0:
        raise ValueError("la cantidad debe ser mayor que 0")
    unidad = "min" if m.group(2).lower() == "min" else "km"

    ritmo: Ritmo | None = None
    zonas: tuple[str, ...] = ()
    estructura: Estructura | None = None

    for token in resto[1:]:
        me = RE_ESTRUCTURA.match(token)
        if me:
            if estructura is not None:
                raise ValueError("`estructura=` aparece mas de una vez")
            estructura = parse_estructura(me.group(1), tipo)
            continue

        mz = RE_ZONA.match(token)
        if mz:
            if zonas:
                raise ValueError("la zona se declara una sola vez (usa Z3/Z4 para mixta)")
            zonas = tuple(f"Z{g}" for g in mz.groups() if g)
            continue

        mr = RE_RITMO.match(token)
        if mr:
            if ritmo is not None:
                raise ValueError("el ritmo se declara una sola vez")
            ritmo = _parse_ritmo(mr)
            continue

        bajo = token.lower()
        if bajo.startswith("estructura"):
            raise ValueError(
                f"'{token}': la estructura se escribe `estructura=...` sin espacios "
                "alrededor del '=' ni dentro de los bloques"
            )
        if "volumen" in bajo:
            raise ValueError(
                "no existe un campo de volumen objetivo: el volumen se deriva "
                "sumando las sesiones"
            )
        raise ValueError(
            f"'{token}' no se reconoce (esperaba @ritmo, zona Z1-Z5 o estructura=...)"
        )

    if tipo in TIPOS_ESTRUCTURADOS and estructura is None:
        raise ValueError(f"`{tipo}` requiere `estructura=`")
    if tipo in TIPOS_SIMPLES and estructura is not None:
        raise ValueError(f"`{tipo}` es una sesion simple y no admite `estructura=`")
    if tipo == "prog" and ritmo is not None:
        raise ValueError(
            "`prog` lleva el ritmo dentro de cada bloque de `estructura=`, "
            "no un @ritmo global"
        )

    return Sesion(
        dia=dia,
        tipo=tipo,
        cantidad=cantidad,
        unidad=unidad,
        ritmo=ritmo,
        zonas=zonas,
        estructura=estructura,
        crudo=cuerpo,
    )


# --------------------------------------------------------------------------
# plan completo
# --------------------------------------------------------------------------


def _numerar(texto: str, comandos: tuple[str, ...] = ("/setplan",)) -> list[tuple[int, str]]:
    """Lineas utiles con su numero: sin vacias, sin comentarios, sin el comando."""
    lineas: list[tuple[int, str]] = []
    for i, bruta in enumerate(texto.splitlines(), start=1):
        linea = bruta.strip()
        if not linea or linea.startswith("#"):
            continue
        bajo = linea.lower()
        for comando in comandos:
            if bajo.startswith(comando):
                resto = linea[len(comando) :].strip()
                if resto:
                    lineas.append((i, resto))
                break
        else:
            lineas.append((i, linea))
    return lineas


def _parse_dias(
    cuerpo: list[tuple[int, str]]
) -> tuple[dict[str, list[Sesion]], list[ErrorPlan], dict[str, list[int]]]:
    """Parsea lineas `<dia>: <sesion>`. Devuelve lo entendido y los errores.

    Un dia puede tener varias lineas (una corrida y fuerza el mismo dia, o dos
    corridas). Las restricciones que si se aplican:

    - `rest` es exclusivo: declarar descanso y ademas una sesion es una
      contradiccion, no dos sesiones.
    - como maximo una `fuerza` por dia: es lo que permite que el cumplimiento
      de fuerza siga siendo un booleano por dia.

    Devuelve tambien las lineas vistas por dia, incluidas las que no parsearon:
    un dia con una linea rota ya tiene su error y no hay que acusarlo ademas de
    faltar.

    Es el UNICO camino de validacion de lineas de dia: lo usan `parse_plan` y
    `parse_dias` (el que llama /corregir), para que los mensajes de error sean
    identicos por construccion.
    """
    sesiones: dict[str, list[Sesion]] = {}
    errores: list[ErrorPlan] = []
    lineas_por_dia: dict[str, list[int]] = {}

    for nro_linea, linea in cuerpo:
        md = RE_LINEA_DIA.match(linea)
        if not md:
            if RE_SEMANA.match(linea):
                errores.append(ErrorPlan(nro_linea, None, "`semana:` declarada mas de una vez"))
            elif "volumen" in linea.lower():
                errores.append(
                    ErrorPlan(
                        nro_linea,
                        None,
                        "no existe un campo de volumen objetivo: el volumen se deriva "
                        "sumando las sesiones",
                    )
                )
            else:
                errores.append(
                    ErrorPlan(
                        nro_linea,
                        None,
                        f"formato no reconocido: '{linea}'. Se esperaba `<dia>: <sesion>` "
                        "con dia en L M W J V S D",
                    )
                )
            continue

        dia = md.group(1).upper()
        anteriores = lineas_por_dia.setdefault(dia, [])
        try:
            sesion = _parse_sesion(dia, md.group(2).strip())
        except ValueError as exc:
            errores.append(ErrorPlan(nro_linea, dia, str(exc)))
            anteriores.append(nro_linea)
            continue

        previas = sesiones.setdefault(dia, [])
        if previas and (sesion.tipo == "rest" or any(p.tipo == "rest" for p in previas)):
            errores.append(
                ErrorPlan(
                    nro_linea,
                    dia,
                    "`rest` no puede convivir con otra sesion el mismo dia (la otra "
                    f"esta en la linea {anteriores[0]}); si entrenaste, quita el `rest`",
                )
            )
            anteriores.append(nro_linea)
            continue
        if sesion.es_fuerza and any(p.es_fuerza for p in previas):
            errores.append(
                ErrorPlan(
                    nro_linea,
                    dia,
                    "solo se admite una sesion de `fuerza` por dia (ya hay una en la "
                    f"linea {anteriores[0]})",
                )
            )
            anteriores.append(nro_linea)
            continue

        previas.append(sesion)
        anteriores.append(nro_linea)

    return sesiones, errores, lineas_por_dia


def parse_dias(texto: str) -> dict[str, tuple[Sesion, ...]]:
    """Lineas de dia sueltas, sin cabecera `semana:` ni los 7 dias. Para /corregir.

    Mismo parser y mismos mensajes que `/setplan`: no hay una ruta de
    validacion paralela que se pueda desincronizar.
    """
    lineas = _numerar(texto, comandos=("/corregir", "/setplan"))
    if not lineas:
        raise PlanInvalido([ErrorPlan(1, None, "no hay ninguna linea que corregir")])

    sesiones, errores, _ = _parse_dias(lineas)
    if errores:
        raise PlanInvalido(errores)
    return {d: tuple(v) for d, v in sesiones.items()}


def parse_plan(texto: str) -> PlanSemanal:
    """Parsea el plan completo. Lanza PlanInvalido con TODOS los errores hallados."""
    lineas = _numerar(texto)

    if not lineas:
        raise PlanInvalido([ErrorPlan(1, None, "el plan esta vacio")])

    errores: list[ErrorPlan] = []

    # --- cabecera ---
    nro_linea, primera = lineas[0]
    ms = RE_SEMANA.match(primera)
    if ms:
        semana = int(ms.group(1))
        cuerpo = lineas[1:]
    else:
        errores.append(
            ErrorPlan(
                nro_linea,
                None,
                f"se esperaba `semana: <N>` como primera linea, se encontro '{primera}'",
            )
        )
        semana = 0
        cuerpo = lineas

    # --- dias ---
    sesiones, errores_dias, vistos = _parse_dias(cuerpo)
    errores.extend(errores_dias)

    # "Al menos una linea", no "exactamente una": la unicidad por dia dejo de
    # ser un requisito cuando el plan admitio dos entrenamientos el mismo dia.
    # Se mira lo VISTO y no lo parseado: un dia con una linea rota ya tiene su
    # error, y decir ademas que falta manda a buscar el problema al lugar
    # equivocado.
    faltantes = [d for d in ORDEN_DIAS if d not in vistos]
    if faltantes:
        nombres = ", ".join(f"{d} ({DIAS[d]})" for d in faltantes)
        errores.append(
            ErrorPlan(
                lineas[-1][0],
                None,
                f"faltan dias: {nombres}. Los 7 dias son obligatorios; usa `rest` "
                "si no hay sesion (no se asume descanso implicito)",
            )
        )

    if errores:
        raise PlanInvalido(errores)

    return PlanSemanal(
        semana=semana,
        sesiones={d: tuple(sesiones.get(d, ())) for d in ORDEN_DIAS},
        crudo=texto.strip(),
    )
