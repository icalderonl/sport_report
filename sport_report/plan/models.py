"""Modelo del plan semanal. Inmutable y serializable a JSON."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Sequence

from ..config import ESTIMACION, Estimacion

DIAS: dict[str, str] = {
    "L": "lunes",
    "M": "martes",
    "W": "miercoles",
    "J": "jueves",
    "V": "viernes",
    "S": "sabado",
    "D": "domingo",
}
ORDEN_DIAS: tuple[str, ...] = ("L", "M", "W", "J", "V", "S", "D")

TIPOS_SIMPLES = ("easy", "tempo", "long")
TIPOS_ESTRUCTURADOS = ("series", "fartlek", "prog")
TIPOS_SIN_CANTIDAD = ("fuerza", "rest")
TIPOS_VALIDOS = TIPOS_SIMPLES + TIPOS_ESTRUCTURADOS + TIPOS_SIN_CANTIDAD

Unidad = Literal["km", "min"]


def mmss(segundos: int) -> str:
    return f"{segundos // 60}:{segundos % 60:02d}"


@dataclass(frozen=True)
class Ritmo:
    """Un ritmo puntual (min == max) o una ventana a mantener."""

    min_s_km: int
    max_s_km: int

    @property
    def es_ventana(self) -> bool:
        return self.min_s_km != self.max_s_km

    def __str__(self) -> str:
        if not self.es_ventana:
            return mmss(self.min_s_km)
        # Se muestra como se escribe: del mas lento al mas rapido.
        return f"{mmss(self.max_s_km)}/{mmss(self.min_s_km)}"

    def to_json(self) -> dict[str, Any]:
        return {"min_s_km": self.min_s_km, "max_s_km": self.max_s_km}


@dataclass(frozen=True)
class Bloque:
    """Un tramo de la estructura. reps=1 para un tramo simple.

    distancia_km es la distancia de UNA repeticion; dura() da el total del bloque.
    ritmo solo se usa en sesiones `prog`.
    """

    reps: int
    distancia_km: float
    ritmo: Ritmo | None = None
    crudo: str = ""

    def dura(self) -> float:
        return self.reps * self.distancia_km

    def __str__(self) -> str:
        return self.crudo

    def to_json(self) -> dict[str, Any]:
        return {
            "reps": self.reps,
            "distancia_km": self.distancia_km,
            "ritmo": self.ritmo.to_json() if self.ritmo else None,
            "crudo": self.crudo,
        }


@dataclass(frozen=True)
class Estructura:
    bloques: tuple[Bloque, ...]
    crudo: str

    def distancia_dura_km(self) -> float:
        """Suma de calentamiento + reps + enfriamiento, SIN recuperacion.

        La recuperacion entre repeticiones nunca se declara en el plan, asi que
        no aparece en los bloques: la suma de bloques ES la distancia dura.
        Esta cifra es independiente del `cantidad` tecleado a mano y es la que
        usa la comparacion de adherencia (spec 3 y 7).
        """
        return round(sum(b.dura() for b in self.bloques), 3)

    def to_json(self) -> dict[str, Any]:
        return {
            "crudo": self.crudo,
            "distancia_dura_km": self.distancia_dura_km(),
            "bloques": [b.to_json() for b in self.bloques],
        }


@dataclass(frozen=True)
class Sesion:
    dia: str
    tipo: str
    cantidad: float | None = None
    unidad: Unidad | None = None
    ritmo: Ritmo | None = None
    zonas: tuple[str, ...] = ()
    estructura: Estructura | None = None
    crudo: str = ""

    @property
    def nombre_dia(self) -> str:
        return DIAS[self.dia]

    @property
    def es_descanso(self) -> bool:
        return self.tipo == "rest"

    @property
    def es_fuerza(self) -> bool:
        return self.tipo == "fuerza"

    def objetivo_km(self) -> float | None:
        """Distancia a comparar contra lo real para adherencia.

        Con estructura= manda la distancia dura derivada, no el `cantidad`
        tecleado a mano: la sesion real siempre incluye la recuperacion trotada
        entre reps, que el plan nunca declara (spec 7).
        """
        if self.estructura is not None:
            return self.estructura.distancia_dura_km()
        if self.unidad == "km":
            return self.cantidad
        return None

    def objetivo_min(self) -> float | None:
        return self.cantidad if self.unidad == "min" else None

    def ritmo_estimacion_s_km(self, est: Estimacion = ESTIMACION) -> int | None:
        """Ritmo con el que convertir minutos a km. None si no hay con que.

        El ritmo escrito en el plan manda sobre el ritmo por defecto: si el
        atleta se tomo el trabajo de prescribirlo, es mas especifico que
        cualquier valor de configuracion. De una ventana (`@6:15/5:45`) se toma
        el punto medio.
        """
        if self.ritmo is not None:
            return round((self.ritmo.min_s_km + self.ritmo.max_s_km) / 2)
        if self.tipo in est.tipos_con_ritmo_por_defecto:
            return est.ritmo_easy_s_km
        return None

    def objetivo_km_estimado(self, est: Estimacion = ESTIMACION) -> float | None:
        """km derivados de una sesion prescrita en minutos. None si no aplica."""
        if self.objetivo_km() is not None:
            return None  # ya viene en km, no hay nada que estimar
        minutos = self.objetivo_min()
        if minutos is None:
            return None
        ritmo = self.ritmo_estimacion_s_km(est)
        if not ritmo or ritmo <= 0:
            return None
        return round(minutos * 60.0 / ritmo, 2)

    def km_para_volumen(self, est: Estimacion = ESTIMACION) -> tuple[float | None, bool]:
        """(km, es_estimado) para el agregado semanal de volumen.

        Distinto de `objetivo_km()`, que es lo que se compara dia a dia: la
        adherencia diaria sigue evaluandose en la unidad nativa de la sesion.
        """
        km = self.objetivo_km()
        if km is not None:
            return km, False
        estimado = self.objetivo_km_estimado(est)
        return (estimado, True) if estimado is not None else (None, False)

    def to_json(self) -> dict[str, Any]:
        return {
            "dia": self.dia,
            "tipo": self.tipo,
            "cantidad": self.cantidad,
            "unidad": self.unidad,
            "ritmo": self.ritmo.to_json() if self.ritmo else None,
            "zonas": list(self.zonas),
            "estructura": self.estructura.to_json() if self.estructura else None,
            "objetivo_km": self.objetivo_km(),
            "objetivo_min": self.objetivo_min(),
            "objetivo_km_estimado": self.objetivo_km_estimado(),
            "crudo": self.crudo,
        }


@dataclass(frozen=True)
class PlanSemanal:
    semana: int
    sesiones: dict[str, Sesion]
    crudo: str = ""
    # Estado de cumplimiento de fuerza por dia; lo mutan la ingesta y /fuerza.
    fuerza_completada: dict[str, bool] = field(default_factory=dict)

    def dias_fuerza(self) -> tuple[str, ...]:
        return tuple(d for d in ORDEN_DIAS if self.sesiones[d].es_fuerza)

    def volumen_planificado_km(
        self, dias: Sequence[str] | None = None, est: Estimacion = ESTIMACION
    ) -> float:
        """Derivado siempre de la suma de sesiones; nunca se declara aparte.

        Incluye los km estimados de las sesiones prescritas en minutos, para que
        el porcentaje semanal compare la misma base que el volumen real.
        `dias` limita el calculo a los dias transcurridos (semana en curso).
        """
        total = 0.0
        for d in dias if dias is not None else ORDEN_DIAS:
            km, _ = self.sesiones[d].km_para_volumen(est)
            if km:
                total += km
        return round(total, 2)

    def volumen_estimado_km(
        self, dias: Sequence[str] | None = None, est: Estimacion = ESTIMACION
    ) -> float:
        """Parte del volumen planificado que sale de una estimacion, no del plan."""
        total = 0.0
        for d in dias if dias is not None else ORDEN_DIAS:
            km, estimado = self.sesiones[d].km_para_volumen(est)
            if km and estimado:
                total += km
        return round(total, 2)

    def dias_sin_estimar(
        self, dias: Sequence[str] | None = None, est: Estimacion = ESTIMACION
    ) -> tuple[str, ...]:
        """Dias prescritos en minutos que no se pudieron convertir a km."""
        return tuple(
            d
            for d in (dias if dias is not None else ORDEN_DIAS)
            if self.sesiones[d].objetivo_min() is not None
            and self.sesiones[d].objetivo_km_estimado(est) is None
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "semana": self.semana,
            "sesiones": {d: self.sesiones[d].to_json() for d in ORDEN_DIAS},
            "fuerza_completada": {
                d: self.fuerza_completada.get(d, False) for d in self.dias_fuerza()
            },
            "volumen_planificado_km": self.volumen_planificado_km(),
            "volumen_estimado_km": self.volumen_estimado_km(),
            "crudo": self.crudo,
        }


# --------------------------------------------------------------------------
# Deserializacion (contraparte de to_json)
# --------------------------------------------------------------------------


def ritmo_desde_json(d: dict[str, Any] | None) -> Ritmo | None:
    return None if d is None else Ritmo(int(d["min_s_km"]), int(d["max_s_km"]))


def estructura_desde_json(d: dict[str, Any] | None) -> Estructura | None:
    if d is None:
        return None
    bloques = tuple(
        Bloque(
            reps=int(b["reps"]),
            distancia_km=float(b["distancia_km"]),
            ritmo=ritmo_desde_json(b.get("ritmo")),
            crudo=b.get("crudo", ""),
        )
        for b in d["bloques"]
    )
    return Estructura(bloques=bloques, crudo=d.get("crudo", ""))


def sesion_desde_json(d: dict[str, Any]) -> Sesion:
    return Sesion(
        dia=d["dia"],
        tipo=d["tipo"],
        cantidad=d.get("cantidad"),
        unidad=d.get("unidad"),
        ritmo=ritmo_desde_json(d.get("ritmo")),
        zonas=tuple(d.get("zonas", ())),
        estructura=estructura_desde_json(d.get("estructura")),
        crudo=d.get("crudo", ""),
    )


def plan_desde_json(d: dict[str, Any]) -> PlanSemanal:
    """Contraparte de `to_json`. Lanza `PlanCorrupto` si el archivo no sirve.

    La gramatica es estricta al entrar (rechaza el plan entero si falta un
    dia); esto es la misma exigencia al salir. Sin la validacion, un archivo
    editado a mano producia un `KeyError` crudo dentro del cron, en vez de un
    motivo legible.
    """
    from .errors import PlanCorrupto

    try:
        sesiones = {k: sesion_desde_json(v) for k, v in d["sesiones"].items()}
        semana = int(d["semana"])
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise PlanCorrupto(f"plan ilegible: {exc}") from exc

    faltantes = [x for x in ORDEN_DIAS if x not in sesiones]
    if faltantes:
        raise PlanCorrupto(
            f"al plan guardado le faltan los dias {', '.join(faltantes)}; "
            "los 7 son obligatorios"
        )
    return PlanSemanal(
        semana=semana,
        sesiones=sesiones,
        crudo=d.get("crudo", ""),
        fuerza_completada=dict(d.get("fuerza_completada", {})),
    )
