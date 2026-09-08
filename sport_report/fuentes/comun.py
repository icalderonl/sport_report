"""Orquestacion comun de la ingesta, compartida por intervals.icu y Strava.

Aca vive todo lo que no depende de la fuente: el resumen de la corrida, el
rango de fechas, la cache de zonas de HR y el bucle idempotente que decide
cuando vale la pena volver a bajar streams y vueltas. Cada fuente concreta
subclasea `IngestaBase` y solo implementa los ganchos que hablan con su API.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from .. import config
from ..db.models import SesionReal
from ..db.repo import Repo
from ..fechas import letra_dia, semana_de
from ..plan.store import DiaSinFuerza, PlanStore

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
    # Que fuente sirvio estos datos, y si se llego a ella por respaldo. Lo lee
    # el reporte para marcar la semana degradada (spec 4bis).
    fuente: str = ""
    fallback: bool = False
    # Dias de bienestar persistidos. Solo intervals.icu los provee.
    bienestar_dias: int = 0
    # Corridas sin dinamica avanzada (GCT, oscilacion y ratio vertical).
    sin_dinamica: int = 0
    fuerza_marcada: list[str] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)

    @property
    def carga_imprecisa(self) -> bool:
        return self.zonas_origen not in config.ORIGENES_ZONAS_CONFIABLES

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
            "fuente": self.fuente,
            "fallback": self.fallback,
            "bienestar_dias": self.bienestar_dias,
            "sin_dinamica": self.sin_dinamica,
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


class IngestaBase:
    """Base comun de las fuentes de actividades reales.

    Es idempotente: reingerir el mismo rango no duplica filas ni vuelve a
    gastar cuota en streams que ya se procesaron.
    """

    #: Nombre de la fuente tal como se guarda en la base ('intervals', 'strava').
    NOMBRE: str = ""
    #: Errores propios de la fuente que esta clase sabe absorber.
    ERRORES: tuple[type[BaseException], ...] = ()

    def __init__(
        self,
        cliente: Any | None = None,
        repo: Repo | None = None,
        plan_store: PlanStore | None = None,
        carga: config.Carga = config.CARGA,
    ):
        self.cliente = cliente if cliente is not None else self._cliente_nuevo()
        self.repo = repo or Repo()
        self.plan_store = plan_store or PlanStore()
        self.carga = carga

    # -- ganchos de la fuente --------------------------------------------

    def _cliente_nuevo(self) -> Any:
        raise NotImplementedError

    def _listar(self, ini: datetime, fin: datetime) -> list[dict[str, Any]]:
        raise NotImplementedError

    def _streams(self, actividad_id: Any) -> dict[str, list[Any]]:
        raise NotImplementedError

    def _vueltas_crudas(self, actividad_id: Any) -> list[dict[str, Any]]:
        raise NotImplementedError

    def _normalizar_vueltas(
        self, actividad_id: Any, crudas: list[dict[str, Any]]
    ) -> list[Any]:
        raise NotImplementedError

    def _zonas_remotas(self) -> list[dict[str, int]] | None:
        raise NotImplementedError

    def _id(self, act: dict[str, Any]) -> Any:
        raise NotImplementedError

    def _inicio_local(self, act: dict[str, Any]) -> datetime:
        raise NotImplementedError

    def _tipo(self, act: dict[str, Any]) -> str:
        raise NotImplementedError

    def normalizar(
        self,
        act: dict[str, Any],
        streams: dict[str, list[Any]] | None,
        zonas: list[dict[str, int]] | None,
        streams_procesados: bool = False,
        vueltas_procesadas: bool = False,
    ) -> SesionReal:
        raise NotImplementedError

    def _extras(self, desde: date, hasta: date, resumen: ResumenIngesta) -> None:
        """Datos de la fuente que no son actividades (bienestar).

        No-op por defecto: Strava no expone nada de esto.
        """
        return None

    # -- zonas -----------------------------------------------------------

    def zonas(self, refrescar: bool = True) -> tuple[list[dict[str, int]] | None, str]:
        """Zonas de HR del atleta. Cachea en la base para no depender de la red.

        Devolver (None, 'fallback') es la senal de que la carga se pondera con
        un peso plano y el reporte debe marcarla como imprecisa.
        """
        if refrescar:
            try:
                frescas = self._zonas_remotas()
            except self.ERRORES as exc:
                log.warning("no se pudieron refrescar las zonas: %s", exc)
                frescas = None
            if frescas:
                self.repo.guardar_zonas(frescas, self.NOMBRE)
                return frescas, self.NOMBRE

        cacheadas, origen = self.repo.zonas()
        if cacheadas:
            # `repo.zonas()` solo devuelve zonas cuando el origen es confiable;
            # se propaga el que trae en vez de reafirmarlo con un literal.
            return cacheadas, origen or self.NOMBRE
        self.repo.guardar_zonas(None, "fallback")
        return None, "fallback"

    # -- sincronizacion --------------------------------------------------

    def sincronizar(
        self, desde: date, hasta: date, forzar: bool = False, refrescar_zonas: bool = True
    ) -> ResumenIngesta:
        """Trae y persiste las actividades de [desde, hasta] (fechas locales)."""
        resumen = ResumenIngesta(fuente=self.NOMBRE)
        zonas, origen = self.zonas(refrescar=refrescar_zonas)
        resumen.zonas_origen = origen
        if origen not in config.ORIGENES_ZONAS_CONFIABLES:
            resumen.avisos.append(
                "zonas de HR no disponibles: la carga se calculo con peso de zona "
                "fijo y es imprecisa"
            )

        ini, fin = _limites(desde, hasta)
        actividades = self._listar(ini, fin)
        resumen.actividades = len(actividades)

        for act in actividades:
            tipo = self._tipo(act)
            es_run = tipo in config.TIPOS_RUN
            es_fuerza = tipo in config.TIPOS_FUERZA
            if es_run:
                resumen.corridas += 1
            elif es_fuerza:
                resumen.fuerza += 1
            else:
                resumen.otras += 1

            streams: dict[str, list[Any]] = {}
            # Solo se marca cuando la fuente efectivamente respondio. Una
            # actividad sin streams es una respuesta valida (registro manual,
            # subida sin dispositivo); un fallo de red no lo es y hay que
            # reintentarlo.
            streams_ok = False
            vueltas_ok = False
            if es_run:
                aid = self._id(act)
                previa = self.repo.sesion(aid)
                nueva = previa is None or forzar
                if not nueva and previa.streams_procesados and previa.vueltas_procesadas:
                    # Ya procesada: no gastar cuota en volver a bajar el stream.
                    # Se mira `streams_procesados` y no `carga`: una corrida sin
                    # pulsometro nunca va a tener carga y se re-bajaba siempre.
                    resumen.reutilizadas += 1
                    self._marcar_si_fuerza(act, es_fuerza, resumen)
                    continue

                if nueva or not previa.vueltas_procesadas:
                    vueltas_ok = self._traer_vueltas(aid, resumen)

                if not nueva and previa.streams_procesados:
                    # A esta fila solo le faltaban las vueltas, que ya se
                    # guardaron. Re-normalizarla obligaria a bajar de nuevo el
                    # stream para no perder la carga, que es la llamada cara que
                    # se esta evitando; basta con mover la bandera.
                    self.repo.marcar_vueltas_procesadas(aid, vueltas_ok)
                    resumen.reutilizadas += 1
                    self._marcar_si_fuerza(act, es_fuerza, resumen)
                    continue

                try:
                    streams = self._streams(aid)
                    streams_ok = True
                except self.ERRORES as exc:
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

        self._extras(desde, hasta, resumen)

        log.info("ingesta %s %s..%s: %s", self.NOMBRE, desde, hasta, resumen.to_json())
        return resumen

    def _traer_vueltas(self, actividad_id: Any, resumen: ResumenIngesta) -> bool:
        """Baja y guarda las vueltas. Devuelve si la fuente respondio.

        Que no haya vueltas es una respuesta valida y se guarda como tal (lista
        vacia, bandera en 1) para no volver a pedirlas cada semana. Un fallo de
        red no lo es: deja la bandera en 0 y se reintenta en la proxima corrida.
        """
        try:
            crudas = self._vueltas_crudas(actividad_id)
        except self.ERRORES as exc:
            log.warning("vueltas de %s fallaron: %s", actividad_id, exc)
            return False
        vueltas = self._normalizar_vueltas(actividad_id, crudas)
        self.repo.guardar_vueltas(actividad_id, vueltas)
        if not vueltas:
            resumen.sin_vueltas += 1
        return True

    def _marcar_si_fuerza(
        self, act: dict[str, Any], es_fuerza: bool, resumen: ResumenIngesta
    ) -> None:
        """Una sesion de fuerza real marca automaticamente el dia del plan.

        Nunca desmarca: si el usuario ya la marco con /fuerza, esto es un no-op.
        """
        if not es_fuerza:
            return
        fecha = self._inicio_local(act).date()
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
