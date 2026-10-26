"""Entrenamientos de TrainingPeaks -> texto de `/setplan`.

La salida es TEXTO y no un `PlanSemanal`: asi el plan importado pasa por el
mismo parser y la misma validacion que uno tecleado, y el plan sigue teniendo
una sola puerta de entrada. Si la conversion produce algo que la gramatica no
acepta, el usuario recibe el borrador para corregirlo a mano con `/setplan`.

Lo que se traduce y lo que no:

- Corrida -> `easy`/`tempo`/`long`/`series`/`fartlek` con la cantidad
  planificada (km si TP la trae, si no minutos).
- Estructura -> `estructura=` solo si TODOS los tramos de trabajo estan en
  distancia: la gramatica no tiene tramos por tiempo. La recuperacion entre
  repeticiones se omite, como pide la gramatica.
- Fuerza -> `fuerza`; dia libre o dia sin nada -> `rest`.
- Ritmos y zonas NO se traducen: TP los guarda como % del umbral y convertirlos
  exige un umbral que el reporte no tiene. Se avisa.
- Otros deportes (bici, natacion...) se omiten con aviso: el plan es de correr.
"""
from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from ..fechas import RangoSemana
from ..plan.models import ORDEN_DIAS

# workoutTypeValueId de TrainingPeaks (GET /fitness/v6/workouttypes).
CORRIDA = 3
DIA_LIBRE = 7
FUERZA = 9
DEPORTES = {
    1: "natacion",
    2: "bici",
    4: "brick",
    5: "crosstraining",
    6: "carrera",
    8: "MTB",
    10: "personalizado",
    11: "esqui",
    12: "remo",
    13: "caminata",
    100: "otro",
}

_RECUPERACION = ("rest", "recovery")
_METROS = {"meter": 1.0, "kilometer": 1000.0, "mile": 1609.344}

# Palabras del titulo que deciden el tipo cuando la estructura no lo dice.
# El orden importa: "fondo largo progresivo" es largo antes que progresivo.
_PALABRAS_TIPO = (
    ("fartlek", ("fartlek",)),
    ("long", ("largo", "larga", "fondo", "long")),
    ("series", ("series", "serie", "intervalo", "repeticion", "pista", "interval")),
    ("tempo", ("tempo", "umbral", "threshold", "ritmo", "progresiv", "cambio")),
    ("easy", ("suave", "rodaje", "trote", "easy", "recuperacion", "regenerativ", "base")),
)


@dataclass
class Borrador:
    """El plan convertido, listo para `parse_plan`, mas lo que hubo que decidir."""

    texto: str
    avisos: list[str] = field(default_factory=list)
    #: Entrenamientos que realmente entraron (sin contar los `rest` de relleno).
    importados: int = 0


def _sin_tildes(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if not unicodedata.combining(c)
    ).lower()


def _num(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".")


def _dia(w: dict[str, Any]) -> date | None:
    crudo = str(w.get("workoutDay") or "")[:10]
    try:
        return date.fromisoformat(crudo)
    except ValueError:
        return None


def _positivo(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _es_planificado(w: dict[str, Any]) -> bool:
    """Un completado sin nada planificado es una actividad suelta, no plan."""
    return any(
        _positivo(w.get(k)) for k in ("distancePlanned", "totalTimePlanned")
    ) or bool(w.get("structure")) or w.get("workoutTypeValueId") in (DIA_LIBRE, FUERZA)


def _estructura(w: dict[str, Any]) -> dict[str, Any] | None:
    s = w.get("structure")
    if isinstance(s, str):
        try:
            s = json.loads(s)
        except ValueError:
            return None
    return s if isinstance(s, dict) and s.get("structure") else None


def _metros(paso: dict[str, Any]) -> float | None:
    largo = paso.get("length") or {}
    factor = _METROS.get(str(largo.get("unit")))
    valor = _positivo(largo.get("value"))
    return valor * factor if factor and valor else None


def _tramo(metros: float, repeticion: bool = False) -> str:
    """`2km` para un tramo suelto, `1000m` para una repeticion (como se escribe)."""
    if not repeticion and metros >= 1000 and metros % 100 == 0:
        return f"{_num(metros / 1000)}km"
    return f"{round(metros)}m"


@dataclass
class _Estructura:
    texto: str | None  # `estructura=` o None si no se puede expresar
    total_m: float | None  # distancia total, recuperacion incluida
    con_repeticiones: bool
    motivo: str = ""


def _leer_estructura(s: dict[str, Any]) -> _Estructura:
    tokens: list[str] = []
    total = 0.0
    total_valido = True
    con_reps = False
    motivo = ""

    for bloque in s.get("structure") or []:
        largo = bloque.get("length") or {}
        reps = int(largo.get("value") or 1) if largo.get("unit") == "repetition" else 1
        pasos = bloque.get("steps") or []
        trabajo = [p for p in pasos if p.get("intensityClass") not in _RECUPERACION]

        for p in pasos:
            m = _metros(p)
            if m is None:
                total_valido = False
            else:
                total += m * reps

        if any(_metros(p) is None for p in trabajo):
            motivo = "tiene tramos por tiempo y `estructura=` solo admite distancia"
            continue
        if not trabajo:
            continue
        if reps > 1:
            con_reps = True
            if len(trabajo) == 1:
                tokens.append(f"{reps}x{_tramo(_metros(trabajo[0]), repeticion=True)}")
            else:
                # Dos tramos de trabajo que se alternan no tienen notacion
                # propia: se despliegan en orden, que es lo que se alinea
                # contra las vueltas reales.
                tokens.extend(
                    _tramo(_metros(p), repeticion=True) for _ in range(reps) for p in trabajo
                )
        else:
            tokens.extend(_tramo(_metros(p)) for p in trabajo)

    return _Estructura(
        texto=None if motivo or not tokens else "+".join(tokens),
        total_m=total if total_valido and total > 0 else None,
        con_repeticiones=con_reps,
        motivo=motivo,
    )


def _tipo_por_titulo(titulo: str) -> str | None:
    t = _sin_tildes(titulo)
    for tipo, palabras in _PALABRAS_TIPO:
        if any(p in t for p in palabras):
            return tipo
    return None


def _corrida(w: dict[str, Any], letra: str, avisos: list[str]) -> str:
    titulo = (w.get("title") or "").strip()
    nombre = f"{letra} «{titulo}»" if titulo else letra

    est = _estructura(w)
    leida = _leer_estructura(est) if est else None
    if leida and leida.motivo:
        avisos.append(f"{nombre}: la estructura no se importo porque {leida.motivo}")

    tipo_titulo = _tipo_por_titulo(titulo)
    estructura = leida.texto if leida else None
    if estructura and leida.con_repeticiones:
        tipo = "fartlek" if tipo_titulo == "fartlek" else "series"
    else:
        # Sin repeticiones, una estructura (calentamiento + bloque + vuelta a
        # la calma) es una sesion continua: los tipos simples no admiten
        # `estructura=`, asi que se descarta y queda la cantidad total.
        estructura = None
        tipo = tipo_titulo if tipo_titulo in ("easy", "tempo", "long") else None
        if tipo is None:
            tipo = "easy"
            if tipo_titulo in ("series", "fartlek"):
                avisos.append(
                    f"{nombre}: parece {tipo_titulo} pero no trae repeticiones en "
                    "distancia; se cargo como easy"
                )
            else:
                avisos.append(f"{nombre}: el titulo no dice el tipo; se cargo como easy")

    km = _positivo(w.get("distancePlanned"))
    km = km / 1000 if km else (leida.total_m / 1000 if leida and leida.total_m else None)
    horas = _positivo(w.get("totalTimePlanned"))
    if km:
        cantidad = f"{_num(km)}km"
    elif horas:
        cantidad = f"{round(horas * 60)}min"
    else:
        avisos.append(f"{nombre}: TrainingPeaks no trae distancia ni duracion planificada")
        cantidad = ""

    partes = [tipo, cantidad]
    if estructura:
        partes.append(f"estructura={estructura}")
    return " ".join(p for p in partes if p)


def _comentario(w: dict[str, Any]) -> str:
    """Titulo y descripcion como comentario `#`, que el parser ignora."""
    texto = " — ".join(
        x for x in ((w.get("title") or "").strip(), (w.get("description") or "").strip()) if x
    )
    return " ".join(texto.split())[:200]


def a_setplan(entrenamientos: list[dict[str, Any]], rango: RangoSemana) -> Borrador:
    """Convierte la semana `rango` al texto de `/setplan`."""
    avisos: list[str] = []
    lineas: list[str] = []
    importados = 0
    hubo_objetivos = False

    por_dia: dict[date, list[dict[str, Any]]] = {}
    for w in entrenamientos:
        d = _dia(w)
        if d is not None and rango.inicio <= d <= rango.fin and _es_planificado(w):
            por_dia.setdefault(d, []).append(w)

    for i, letra in enumerate(ORDEN_DIAS):
        del_dia = por_dia.get(rango.inicio + timedelta(days=i), [])
        sesiones: list[str] = []
        fuerza = False
        for w in del_dia:
            tipo = w.get("workoutTypeValueId")
            if tipo == CORRIDA:
                if comentario := _comentario(w):
                    lineas.append(f"# {letra}: {comentario}")
                sesiones.append(_corrida(w, letra, avisos))
                est = _estructura(w)
                hubo_objetivos = hubo_objetivos or bool(est and est.get("primaryIntensityMetric"))
            elif tipo == FUERZA:
                if not fuerza:
                    sesiones.append("fuerza")
                    fuerza = True
            elif tipo == DIA_LIBRE:
                continue
            else:
                deporte = DEPORTES.get(tipo, f"tipo {tipo}")
                titulo = (w.get("title") or "").strip()
                avisos.append(
                    f"{letra}: se omitio {deporte}"
                    + (f" «{titulo}»" if titulo else "")
                    + " (el plan solo lleva corridas y fuerza)"
                )
        importados += len(sesiones)
        lineas.extend(f"{letra}: {s}" for s in sesiones or ["rest"])

    if hubo_objetivos:
        avisos.append(
            "Los ritmos y zonas de TrainingPeaks no se importan (vienen como % del "
            "umbral); agregalos con /corregir si los quieres en el reporte"
        )
    return Borrador(texto="\n".join(lineas), avisos=avisos, importados=importados)
