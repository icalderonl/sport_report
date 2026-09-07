"""Ingesta: Strava -> sesiones normalizadas -> SQLite.

Es idempotente: reingerir el mismo rango no duplica filas ni vuelve a gastar
cuota en streams que ya se procesaron.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from .. import config
from ..db.models import SesionReal
from ..db.repo import Repo
from ..fechas import letra_dia, semana_de
from ..plan.store import DiaSinFuerza, PlanStore
from . import metricas
from .client import StravaClient
from .errors import StravaError

log = logging.getLogger(__name__)


@dataclass
class ResumenIngesta:
    actividades: int = 0
    corridas: int = 0
    fuerza: int = 0
    otras: int = 0
    sin_streams: int = 0
    sin_hr: int = 0
    sin_vueltas: int = 0
    reutilizadas: int = 0
    zonas_origen: str = "desconocido"
    fuerza_marcada: list[str] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)

    @property
    def carga_imprecisa(self) -> bool:
        return self.zonas_origen != "strava"

    def to_json(self) -> dict[str, Any]:
        return {
            "actividades": self.actividades,
            "corridas": self.corridas,
            "fuerza": self.fuerza,
            "otras": self.otras,
            "sin_streams": self.sin_streams,
            "sin_hr": self.sin_hr,
            "sin_vueltas": self.sin_vueltas,
            "reutilizadas": self.reutilizadas,
            "zonas_origen": self.zonas_origen,
            "carga_imprecisa": self.carga_imprecisa,
            "fuerza_marcada": self.fuerza_marcada,
            "avisos": self.avisos,
        }


def _a_local(iso_utc: str) -> datetime:
    """`start_date` de Strava viene en UTC con sufijo Z."""
    return datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone(config.TZ)


def _limites(desde: date, hasta: date) -> tuple[datetime, datetime]:
    """Rango [desde 00:00, hasta 23:59:59] local, con un minuto de holgura."""
    ini = datetime.combine(desde, time.min, tzinfo=config.TZ) - timedelta(minutes=1)
    fin = datetime.combine(hasta + timedelta(days=1), time.min, tzinfo=config.TZ)
    return ini, fin


class Ingesta:
    def __init__(
        self,
        cliente: StravaClient | None = None,
        repo: Repo | None = None,
        plan_store: PlanStore | None = None,
        carga: config.Carga = config.CARGA,
    ):
        self.cliente = cliente or StravaClient()
        self.repo = repo or Repo()
        self.plan_store = plan_store or PlanStore()
        self.carga = carga

    # -- zonas -----------------------------------------------------------

    def zonas(self, refrescar: bool = True) -> tuple[list[dict[str, int]] | None, str]:
        """Zonas de HR del atleta. Cachea en la base para no depender de la red.

        Devolver (None, 'fallback') es la senal de que la carga se pondera con
        un peso plano y el reporte debe marcarla como imprecisa.
        """
        if refrescar:
            try:
                frescas = self.cliente.zonas_hr()
            except StravaError as exc:
                log.warning("no se pudieron refrescar las zonas: %s", exc)
                frescas = None
            if frescas:
                self.repo.guardar_zonas(frescas, "strava")
                return frescas, "strava"

        cacheadas, origen = self.repo.zonas()
        if cacheadas:
            # `repo.zonas()` solo devuelve zonas cuando el origen fue Strava;
            # se propaga el que trae en vez de reafirmarlo con un literal.
            return cacheadas, origen or "strava"
        self.repo.guardar_zonas(None, "fallback")
        return None, "fallback"

    # -- normalizacion ---------------------------------------------------

    def normalizar(
        self,
        act: dict[str, Any],
        streams: dict[str, list[Any]] | None,
        zonas: list[dict[str, int]] | None,
        streams_procesados: bool = False,
        vueltas_procesadas: bool = False,
    ) -> SesionReal:
        local = _a_local(act["start_date"])
        tipo = act.get("type") or act.get("sport_type") or "Desconocido"
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

    # -- sincronizacion --------------------------------------------------

    def sincronizar(
        self, desde: date, hasta: date, forzar: bool = False, refrescar_zonas: bool = True
    ) -> ResumenIngesta:
        """Trae y persiste las actividades de [desde, hasta] (fechas locales)."""
        resumen = ResumenIngesta()
        zonas, origen = self.zonas(refrescar=refrescar_zonas)
        resumen.zonas_origen = origen
        if origen != "strava":
            resumen.avisos.append(
                "zonas de HR no disponibles: la carga se calculo con peso de zona "
                "fijo y es imprecisa"
            )

        ini, fin = _limites(desde, hasta)
        actividades = self.cliente.actividades(ini, fin)
        resumen.actividades = len(actividades)

        for act in actividades:
            tipo = act.get("type") or act.get("sport_type") or "Desconocido"
            es_run = tipo in config.TIPOS_RUN
            es_fuerza = tipo in config.TIPOS_FUERZA
            if es_run:
                resumen.corridas += 1
            elif es_fuerza:
                resumen.fuerza += 1
            else:
                resumen.otras += 1

            streams: dict[str, list[Any]] = {}
            # Solo se marca cuando Strava efectivamente respondio. Una actividad
            # sin streams es una respuesta valida (registro manual, subida sin
            # dispositivo); un fallo de red no lo es y hay que reintentarlo.
            streams_ok = False
            vueltas_ok = False
            if es_run:
                previa = self.repo.sesion(int(act["id"]))
                nueva = previa is None or forzar
                if not nueva and previa.streams_procesados and previa.vueltas_procesadas:
                    # Ya procesada: no gastar cuota en volver a bajar el stream.
                    # Se mira `streams_procesados` y no `carga`: una corrida sin
                    # pulsometro nunca va a tener carga y se re-bajaba siempre.
                    resumen.reutilizadas += 1
                    self._marcar_si_fuerza(act, es_fuerza, resumen)
                    continue

                if nueva or not previa.vueltas_procesadas:
                    vueltas_ok = self._traer_vueltas(int(act["id"]), resumen)

                if not nueva and previa.streams_procesados:
                    # A esta fila solo le faltaban las vueltas, que ya se
                    # guardaron. Re-normalizarla obligaria a bajar de nuevo el
                    # stream para no perder la carga, que es la llamada cara que
                    # se esta evitando; basta con mover la bandera.
                    self.repo.marcar_vueltas_procesadas(int(act["id"]), vueltas_ok)
                    resumen.reutilizadas += 1
                    self._marcar_si_fuerza(act, es_fuerza, resumen)
                    continue

                try:
                    streams = self.cliente.streams(int(act["id"]))
                    streams_ok = True
                except StravaError as exc:
                    log.warning("streams de %s fallaron: %s", act.get("id"), exc)
                if not streams:
                    resumen.sin_streams += 1
                elif "heartrate" not in streams:
                    resumen.sin_hr += 1

            sesion = self.normalizar(
                act, streams, zonas,
                streams_procesados=streams_ok,
                vueltas_procesadas=vueltas_ok,
            )
            self.repo.guardar_sesion(sesion)
            if es_run and sesion.carga is None:
                resumen.avisos.append(
                    f"{sesion.fecha_local} '{sesion.nombre}': sin HR, no cuenta para "
                    "la carga (ACWR/Monotony)"
                )
            self._marcar_si_fuerza(act, es_fuerza, resumen)

        log.info("ingesta %s..%s: %s", desde, hasta, resumen.to_json())
        return resumen

    def _traer_vueltas(self, actividad_id: int, resumen: ResumenIngesta) -> bool:
        """Baja y guarda las vueltas. Devuelve si Strava respondio.

        Que no haya vueltas es una respuesta valida y se guarda como tal (lista
        vacia, bandera en 1) para no volver a pedirlas cada semana. Un fallo de
        red no lo es: deja la bandera en 0 y se reintenta en la proxima corrida.
        """
        try:
            crudas = self.cliente.vueltas(actividad_id)
        except StravaError as exc:
            log.warning("vueltas de %s fallaron: %s", actividad_id, exc)
            return False
        vueltas = metricas.normalizar_vueltas(actividad_id, crudas)
        self.repo.guardar_vueltas(actividad_id, vueltas)
        if not vueltas:
            resumen.sin_vueltas += 1
        return True

    def _marcar_si_fuerza(
        self, act: dict[str, Any], es_fuerza: bool, resumen: ResumenIngesta
    ) -> None:
        """Una sesion de fuerza en Strava marca automaticamente el dia del plan.

        Nunca desmarca: si el usuario ya la marco con /fuerza, esto es un no-op.
        """
        if not es_fuerza:
            return
        fecha = _a_local(act["start_date"]).date()
        dia = letra_dia(fecha)
        try:
            self.plan_store.marcar_fuerza(dia, True, semana_de(fecha))
        except (FileNotFoundError, DiaSinFuerza) as exc:
            # Fuerza no planificada ese dia, o semana sin plan: no es un error,
            # el motor de adherencia lo reportara como actividad no planificada.
            log.info("fuerza el %s no se marco en el plan: %s", fecha, exc)
            return
        etiqueta = f"{fecha.isoformat()} ({dia})"
        if etiqueta not in resumen.fuerza_marcada:
            resumen.fuerza_marcada.append(etiqueta)
