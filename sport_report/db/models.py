"""Modelo de una sesion real ya normalizada, tal como se guarda en SQLite."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any


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

    @property
    def fecha(self) -> date:
        return date.fromisoformat(self.fecha_local)

    @property
    def duracion_min(self) -> float | None:
        return self.duracion_mov_s / 60 if self.duracion_mov_s else None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)
