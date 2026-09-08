"""Ingesta: Strava -> sesiones normalizadas -> SQLite.

Strava es la fuente de **respaldo** (spec 4bis): solo se usa cuando la ingesta
de intervals.icu falla. Su API no expone GCT, oscilacion vertical, ratio
vertical ni bienestar, asi que esos campos quedan en `None` — nunca en cero.

La orquestacion (idempotencia, cache de zonas, banderas de streams y vueltas)
vive en `sport_report/fuentes/comun.py`; aca solo queda lo que habla con la API
de Strava.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .. import config
from ..db.models import SesionReal
from ..fechas import letra_dia
# ResumenIngesta se re-exporta a proposito: `run_weekly` y los tests lo
# importan desde aca desde antes de que existiera `fuentes/`.
from ..fuentes.comun import IngestaBase, ResumenIngesta, _a_local  # noqa: F401
from . import metricas
from .client import StravaClient
from .errors import StravaError

log = logging.getLogger(__name__)


class Ingesta(IngestaBase):
    NOMBRE = "strava"
    ERRORES = (StravaError,)

    # -- ganchos de la fuente --------------------------------------------

    def _cliente_nuevo(self) -> StravaClient:
        return StravaClient()

    def _listar(self, ini: datetime, fin: datetime) -> list[dict[str, Any]]:
        return self.cliente.actividades(ini, fin)

    def _streams(self, actividad_id: int) -> dict[str, list[Any]]:
        return self.cliente.streams(actividad_id)

    def _vueltas_crudas(self, actividad_id: int) -> list[dict[str, Any]]:
        return self.cliente.vueltas(actividad_id)

    def _normalizar_vueltas(
        self, actividad_id: int, crudas: list[dict[str, Any]]
    ) -> list[Any]:
        return metricas.normalizar_vueltas(actividad_id, crudas)

    def _zonas_remotas(self) -> list[dict[str, int]] | None:
        return self.cliente.zonas_hr()

    def _id(self, act: dict[str, Any]) -> int:
        return int(act["id"])

    def _inicio_local(self, act: dict[str, Any]) -> datetime:
        return _a_local(act["start_date"])

    def _tipo(self, act: dict[str, Any]) -> str:
        return act.get("type") or act.get("sport_type") or "Desconocido"

    # -- normalizacion ---------------------------------------------------

    def normalizar(
        self,
        act: dict[str, Any],
        streams: dict[str, list[Any]] | None,
        zonas: list[dict[str, int]] | None,
        streams_procesados: bool = False,
        vueltas_procesadas: bool = False,
    ) -> SesionReal:
        local = self._inicio_local(act)
        tipo = self._tipo(act)
        es_fuerza = tipo in config.TIPOS_FUERZA
        es_run = tipo in config.TIPOS_RUN

        carga = metricas.CargaSesion(None, False)
        deriva = None
        if es_run and streams:
            carga = metricas.carga_trimp(
                streams, zonas, self.carga.pesos_zona, self.carga.peso_fallback
            )
            deriva = metricas.deriva_cardiaca(streams, self.carga.decoupling_min_puntos)

        return SesionReal(
            strava_id=int(act["id"]),
            fecha_utc=local.astimezone(timezone.utc).isoformat(timespec="seconds"),
            fecha_local=local.date().isoformat(),
            dia_semana=letra_dia(local.date()),
            tipo_strava=tipo,
            es_fuerza=es_fuerza,
            nombre=act.get("name"),
            distancia_km=metricas.distancia_km(act) if not es_fuerza else None,
            duracion_mov_s=int(act["moving_time"]) if act.get("moving_time") else None,
            duracion_tot_s=int(act["elapsed_time"]) if act.get("elapsed_time") else None,
            hr_promedio=act.get("average_heartrate"),
            hr_maximo=act.get("max_heartrate"),
            cadencia_spm=metricas.cadencia_spm(act) if es_run else None,
            potencia_w=metricas.potencia_w(act) if es_run else None,
            decoupling_pct=deriva,
            carga=carga.carga,
            carga_impreciso=carga.impreciso,
            streams_procesados=streams_procesados,
            vueltas_procesadas=vueltas_procesadas,
        )
