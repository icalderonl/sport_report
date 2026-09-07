"""Modelo de una sesion real ya normalizada, tal como se guarda en SQLite."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from ..config import TIPOS_RUN


@dataclass(frozen=True)
class SesionReal:
    strava_id: int
    fecha_utc: str
    fecha_local: str  # YYYY-MM-DD en TZ_LOCAL
    dia_semana: str  # L M W J V S D
    tipo_strava: str
    es_fuerza: bool
    nombre: str | None = None
    # None (no 0.0) cuando la actividad no reporta distancia: una cinta sin
    # sensor no es "corri 0 km", es un dato que falta (spec 7).
    distancia_km: float | None = None
    duracion_mov_s: int | None = None
    duracion_tot_s: int | None = None
    hr_promedio: float | None = None
    hr_maximo: float | None = None
    cadencia_spm: float | None = None
    potencia_w: float | None = None
    decoupling_pct: float | None = None
    carga: float | None = None
    carga_impreciso: bool = False
    # True cuando ya se pidieron los streams a Strava y respondio, aunque la
    # actividad no tuviera HR. Sin esto una corrida sin pulsometro (carga NULL)
    # se vuelve a bajar en cada sincronizacion y gasta cuota para siempre.
    streams_procesados: bool = False
    # Idem para las vueltas. Una actividad puede no traer ninguna (registro
    # manual) y eso es una respuesta valida, no un motivo para reintentar.
    vueltas_procesadas: bool = False

    @property
    def fecha(self) -> date:
        return date.fromisoformat(self.fecha_local)

    @property
    def es_run(self) -> bool:
        """Si la sesion cuenta como carrera.

        Derivado de `tipo_strava` y no guardado como columna: el criterio vive
        en un solo lugar (`config.TIPOS_RUN`) y agregar un tipo nuevo
        reclasifica el historico sin migrar la base.

        Todo lo que agregue kilometros —volumen semanal, adherencia diaria,
        grafico— tiene que filtrar por esto. Una salida en bici tambien trae
        `distancia_km` y sin el filtro entra al volumen de running.
        """
        return self.tipo_strava in TIPOS_RUN

    @property
    def duracion_min(self) -> float | None:
        return self.duracion_mov_s / 60 if self.duracion_mov_s else None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Vuelta:
    """Una vuelta (`lap`) de una actividad, tal como la marco el reloj.

    Es el unico dato que permite separar el trabajo declarado en el plan de la
    recuperacion trotada entre repeticiones, que el plan nunca escribe. Sin
    vueltas solo se conoce el total de la actividad, y una sesion de series
    siempre lee por encima de lo prescrito.
    """

    strava_id: int
    indice: int  # lap_index de Strava, 1-based
    distancia_km: float
    duracion_mov_s: int

    @property
    def ritmo_s_km(self) -> float | None:
        if not self.distancia_km or not self.duracion_mov_s:
            return None
        return self.duracion_mov_s / self.distancia_km

    def to_json(self) -> dict[str, Any]:
        return asdict(self)
