"""Modelo de una sesion real ya normalizada, tal como se guarda en SQLite."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from ..config import TIPOS_RUN


@dataclass(frozen=True)
class SesionReal:
    # Identidad: (fuente, id_externo). Dos fuentes pueden traer la misma
    # actividad, y el id de intervals.icu es texto, no un entero.
    fuente: str  # 'intervals' | 'strava'
    id_externo: str
    fecha_utc: str
    fecha_local: str  # YYYY-MM-DD en TZ_LOCAL
    dia_semana: str  # L M W J V S D
    tipo: str  # vocabulario de la fuente ('Run', 'WeightTraining', ...)
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
    # Dinamica avanzada. Solo intervals.icu la provee; con el respaldo de
    # Strava se quedan en None y el reporte lo dice, nunca en 0.0.
    gct_ms: float | None = None
    oscilacion_vertical_cm: float | None = None
    ratio_vertical_pct: float | None = None
    decoupling_pct: float | None = None
    carga: float | None = None
    carga_impreciso: bool = False
    # True cuando ya se pidieron los streams a la fuente y respondio, aunque la
    # actividad no tuviera HR. Sin esto una corrida sin pulsometro (carga NULL)
    # se vuelve a bajar en cada sincronizacion y gasta cuota para siempre.
    streams_procesados: bool = False
    # Idem para las vueltas. Una actividad puede no traer ninguna (registro
    # manual) y eso es una respuesta valida, no un motivo para reintentar.
    vueltas_procesadas: bool = False

    @property
    def clave(self) -> tuple[str, str]:
        """Identidad de la sesion, tal como la indexan `vueltas_entre` y el repo."""
        return (self.fuente, self.id_externo)

    @property
    def fecha(self) -> date:
        return date.fromisoformat(self.fecha_local)

    @property
    def es_run(self) -> bool:
        """Si la sesion cuenta como carrera (calle, pista, cinta, trail).

        Derivado de `tipo` y no guardado como columna: el criterio vive en un
        solo lugar (`config.TIPOS_RUN`) y agregar un tipo nuevo reclasifica el
        historico sin migrar la base.

        Todo lo que agregue kilometros —volumen semanal, adherencia diaria,
        grafico, agregados mensuales— tiene que filtrar por esto. Una salida en
        bici tambien trae `distancia_km` y sin el filtro entra al volumen de
        running.
        """
        return self.tipo in TIPOS_RUN

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

    fuente: str
    id_externo: str
    indice: int  # 1-based, como lo numera la fuente
    distancia_km: float
    duracion_mov_s: int

    @property
    def clave(self) -> tuple[str, str]:
        return (self.fuente, self.id_externo)

    @property
    def ritmo_s_km(self) -> float | None:
        if not self.distancia_km or not self.duracion_mov_s:
            return None
        return self.duracion_mov_s / self.distancia_km

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BienestarDia:
    """Un dia de bienestar. Solo lo provee intervals.icu.

    Todos los campos son opcionales a proposito: el reloj puede no medir HRV,
    la API puede no exponer Body Battery, y una noche sin registrar no es un
    cero. `crudo` conserva el registro completo para poder poblar una columna
    nueva mas adelante sin volver a pedir nada.
    """

    fecha_local: str  # YYYY-MM-DD
    fuente: str
    hrv: float | None = None
    hr_reposo: float | None = None
    sueno_h: float | None = None
    sueno_score: float | None = None
    readiness: float | None = None
    body_battery: float | None = None
    crudo: str | None = None

    @property
    def fecha(self) -> date:
        return date.fromisoformat(self.fecha_local)

    @property
    def vacio(self) -> bool:
        """True si el dia no trae ni una metrica: no vale la pena reportarlo."""
        return all(
            getattr(self, c) is None
            for c in (
                "hrv",
                "hr_reposo",
                "sueno_h",
                "sueno_score",
                "readiness",
                "body_battery",
            )
        )

    def to_json(self) -> dict[str, Any]:
        return asdict(self)
