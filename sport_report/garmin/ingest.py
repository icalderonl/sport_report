"""Ingesta: Garmin Connect Personal API -> sesiones normalizadas + bienestar.

Es el **respaldo** de intervals.icu: solo se usa cuando su ingesta falla. Lee
los mismos datos del reloj que intervals.icu, asi que las cifras coinciden
(comprobado con `garmin.comparar`), con dos diferencias conocidas:

- El tiempo en movimiento es el del reloj; intervals.icu lo recalcula y le da
  un 2-3 % mas. Una semana por este respaldo muestra el ritmo algo mas rapido.
- No hay readiness: la API no lo expone.

Los streams y las vueltas salen del archivo FIT (ver `fit.py`); el resto, de
los resumenes JSON. Igual que en intervals.icu, un dato que no llego es `None`.

La orquestacion (idempotencia, cache de zonas, banderas) vive en
`sport_report/fuentes/comun.py`.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .. import config
from ..db.models import BienestarDia, SesionReal
from ..fechas import letra_dia
from ..fuentes.comun import IngestaBase, ResumenIngesta
from ..intervals import campos
from ..strava import metricas
from . import fit
from .client import GarminClient
from .errors import GarminError

log = logging.getLogger(__name__)

#: `activityType.typeKey` de Garmin -> el vocabulario de intervals.icu, que es
#: el que usan `config.TIPOS_*` y el historico. Lo que no esta aca se guarda
#: con su nombre de Garmin y cuenta como "otra" actividad.
TIPOS: dict[str, str] = {
    "running": "Run",
    "street_running": "Run",
    "track_running": "Run",
    "ultra_run": "Run",
    "obstacle_run": "Run",
    "trail_running": "TrailRun",
    # La cinta, como en intervals.icu: el estimador la excluye por tipo.
    "treadmill_running": "VirtualRun",
    "indoor_running": "VirtualRun",
    "virtual_run": "VirtualRun",
    "strength_training": "WeightTraining",
    "cycling": "Ride",
    "road_biking": "Ride",
    "indoor_cycling": "VirtualRide",
    "virtual_ride": "VirtualRide",
    "gravel_cycling": "GravelRide",
    "mountain_biking": "MountainBikeRide",
    "e_bike_fitness": "EBikeRide",
    "e_bike_mountain": "EMountainBikeRide",
}

#: Maximo de dias por pedido de sueno (lo fija Garmin); se usa tambien para HRV.
VENTANA_BIENESTAR_DIAS = 31


def tipo_de(act: dict[str, Any]) -> str:
    clave = (act.get("activityType") or {}).get("typeKey") or ""
    return TIPOS.get(clave, clave or "Desconocido")


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def zonas_desde_garmin(crudo: list[dict[str, Any]] | None) -> list[dict[str, int]] | None:
    """Zonas de running de Garmin -> [{'min', 'max'}] como las de intervals.icu.

    Garmin da el piso de cada zona y la FC maxima; el reporte usa techos. El
    techo de una zona es el piso de la siguiente menos uno, y el de Z5 la FC
    maxima. None si no hay zonas de running ni por defecto, o no son crecientes.
    """
    por_deporte = {z.get("sport"): z for z in crudo or [] if isinstance(z, dict)}
    z = por_deporte.get("RUNNING") or por_deporte.get("DEFAULT")
    if not z:
        return None
    pisos = [_num(z.get(f"zone{i}Floor")) for i in range(1, 6)]
    maxima = _num(z.get("maxHeartRateUsed"))
    if any(p is None for p in pisos) or maxima is None:
        return None
    techos = [int(p) - 1 for p in pisos[1:]] + [int(maxima)]
    if any(b <= a for a, b in zip(techos, techos[1:])):
        log.warning("zonas de Garmin no crecientes: %r", z)
        return None
    zonas, anterior = [], 0
    for t in techos:
        zonas.append({"min": anterior, "max": t})
        anterior = t
    return zonas


def _por_fecha(registros: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    salida: dict[str, dict[str, Any]] = {}
    for r in registros or []:
        if not isinstance(r, dict):
            continue
        dto = r.get("dailySleepDTO", r)
        fecha = str(dto.get("calendarDate") or "")[:10]
        if fecha:
            salida[fecha] = dto
    return salida


def normalizar_bienestar(
    fecha: str,
    hrv: dict[str, Any] | None,
    sueno: dict[str, Any] | None,
    diario: dict[str, Any] | None,
) -> BienestarDia:
    """Un dia de Garmin -> `BienestarDia` con el mismo significado que intervals.icu.

    Las horas de sueno suman el sueno medido y el "sin medir", porque asi las
    cuenta intervals.icu (comprobado dia por dia con `comparar`). Body Battery
    y readiness quedan en None: intervals.icu no entrega Body Battery para esta
    cuenta, y una metrica que solo aparece en las semanas de respaldo no se
    puede comparar contra la semana anterior.
    """
    hrv, sueno, diario = hrv or {}, sueno or {}, diario or {}
    segundos = [
        _num(sueno.get("sleepTimeSeconds")),
        _num(sueno.get("unmeasurableSleepSeconds")),
    ]
    total = sum(s for s in segundos if s) if segundos[0] else None
    puntaje = ((sueno.get("sleepScores") or {}).get("overall") or {}).get("value")
    return BienestarDia(
        fecha_local=fecha,
        fuente=config.GARMIN,
        hrv=_num(hrv.get("lastNightAvg")),
        hr_reposo=_num((diario.get("heartRate") or {}).get("restingValue")),
        sueno_h=round(total / 3600.0, 2) if total else None,
        sueno_score=_num(puntaje),
        crudo=json.dumps(
            {"hrv": hrv, "sueno": sueno, "diario": diario}, ensure_ascii=False, default=str
        )[:4000],
    )


class Ingesta(IngestaBase):
    NOMBRE = config.GARMIN
    ERRORES = (GarminError,)

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        # El FIT trae streams y vueltas juntos: se baja una sola vez.
        self._fits: dict[str, fit.ActividadFit] = {}

    # -- ganchos de la fuente --------------------------------------------

    def _cliente_nuevo(self) -> GarminClient:
        return GarminClient()

    def _listar(self, ini: datetime, fin: datetime) -> list[dict[str, Any]]:
        # La busqueda es por fecha local; el corte fino lo da la hora de inicio.
        acts = self.cliente.actividades(ini.date(), fin.date())
        return [a for a in acts if ini <= self._inicio_local(a) < fin]

    def _fit(self, actividad_id: Any) -> fit.ActividadFit:
        clave = str(actividad_id)
        if clave not in self._fits:
            self._fits[clave] = fit.leer(self.cliente.archivo_actividad(clave))
        return self._fits[clave]

    def _streams(self, actividad_id: Any) -> dict[str, list[Any]]:
        return self._fit(actividad_id).streams

    def _vueltas_crudas(self, actividad_id: Any) -> list[dict[str, Any]]:
        return self._fit(actividad_id).vueltas

    def _normalizar_vueltas(self, actividad_id: Any, crudas: list[dict[str, Any]]) -> list[Any]:
        return metricas.normalizar_vueltas(self.NOMBRE, actividad_id, crudas)

    def _zonas_remotas(self) -> list[dict[str, int]] | None:
        return zonas_desde_garmin(self.cliente.zonas_hr())

    def _id(self, act: dict[str, Any]) -> str:
        return str(act["activityId"])

    def _inicio_local(self, act: dict[str, Any]) -> datetime:
        crudo = act.get("startTimeLocal")
        if crudo:
            return datetime.fromisoformat(str(crudo)[:19]).replace(tzinfo=config.TZ)
        crudo = act.get("startTimeGMT")
        if not crudo:
            raise GarminError(f"la actividad {act.get('activityId')} no trae fecha de inicio")
        return (
            datetime.fromisoformat(str(crudo)[:19])
            .replace(tzinfo=timezone.utc)
            .astimezone(config.TZ)
        )

    def _tipo(self, act: dict[str, Any]) -> str:
        return tipo_de(act)

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

        metros = _num(act.get("distance"))
        mov = _num(act.get("movingDuration"))
        tot = _num(act.get("elapsedDuration") or act.get("duration"))

        return SesionReal(
            fuente=self.NOMBRE,
            id_externo=self._id(act),
            fecha_utc=local.astimezone(timezone.utc).isoformat(timespec="seconds"),
            fecha_local=local.date().isoformat(),
            dia_semana=letra_dia(local.date()),
            tipo=tipo,
            es_fuerza=es_fuerza,
            nombre=act.get("activityName"),
            distancia_km=(
                round(metros / 1000.0, 3) if metros and metros > 0 and not es_fuerza else None
            ),
            duracion_mov_s=int(mov) if mov else None,
            duracion_tot_s=int(tot) if tot else None,
            hr_promedio=_num(act.get("averageHR")),
            hr_maximo=_num(act.get("maxHR")),
            cadencia_spm=(
                campos.cadencia_spm(_num(act.get("averageRunningCadenceInStepsPerMinute")))
                if es_run
                else None
            ),
            # Garmin solo da potencia cuando el reloj o un sensor la miden.
            potencia_w=_num(act.get("avgPower")) if es_run else None,
            gct_ms=campos.gct_ms(_num(act.get("avgGroundContactTime"))) if es_run else None,
            oscilacion_vertical_cm=(
                campos.oscilacion_cm(_num(act.get("avgVerticalOscillation"))) if es_run else None
            ),
            ratio_vertical_pct=(
                round(r, 2) if es_run and (r := _num(act.get("avgVerticalRatio"))) else None
            ),
            decoupling_pct=deriva,
            carga=carga.carga,
            carga_impreciso=carga.impreciso,
            streams_procesados=streams_procesados,
            vueltas_procesadas=vueltas_procesadas,
        )

    # -- bienestar -------------------------------------------------------

    def _extras(self, desde: date, hasta: date, resumen: ResumenIngesta) -> None:
        """Bienestar de la ventana, una semana mas atras como en intervals.icu.

        No pisa dias que ya trajo intervals.icu: esos son de la fuente
        principal y tienen readiness, que Garmin no da. Un fallo no tumba la
        ingesta: las actividades ya estan guardadas.
        """
        ini = desde - timedelta(days=7)
        hrv: dict[str, dict[str, Any]] = {}
        sueno: dict[str, dict[str, Any]] = {}
        try:
            tramo = ini
            while tramo <= hasta:
                fin_tramo = min(hasta, tramo + timedelta(days=VENTANA_BIENESTAR_DIAS - 1))
                hrv.update(
                    _por_fecha((self.cliente.hrv(tramo, fin_tramo) or {}).get("hrvSummaries"))
                )
                sueno.update(_por_fecha(self.cliente.sueno(tramo, fin_tramo)))
                tramo = fin_tramo + timedelta(days=1)
            diarios = _por_fecha(self.cliente.resumenes_diarios(ini, hasta))
        except self.ERRORES as exc:
            log.warning("el bienestar de Garmin %s..%s fallo: %s", desde, hasta, exc)
            resumen.avisos.append(
                "no se pudo leer el bienestar de Garmin: HRV, FC en reposo y sueno "
                "quedan sin dato esta semana"
            )
            return

        dias = [
            normalizar_bienestar(f, hrv.get(f), sueno.get(f), diarios.get(f))
            for f in sorted(set(hrv) | set(sueno) | set(diarios))
            if ini.isoformat() <= f <= hasta.isoformat()
        ]
        utiles = [d for d in dias if not d.vacio]
        resumen.bienestar_dias = self.repo.guardar_bienestar(
            utiles, sin_pisar=(config.INTERVALS,)
        )
        if not utiles:
            resumen.avisos.append("Garmin no reporto ninguna metrica de bienestar en la ventana")

    # -- resumen ---------------------------------------------------------

    def sincronizar(
        self, desde: date, hasta: date, forzar: bool = False, refrescar_zonas: bool = True
    ) -> ResumenIngesta:
        resumen = super().sincronizar(desde, hasta, forzar, refrescar_zonas)
        sesiones = [
            s for s in self.repo.sesiones_entre(desde, hasta)
            if s.es_run and s.fuente == self.NOMBRE
        ]
        resumen.sin_dinamica = sum(1 for s in sesiones if s.gct_ms is None)
        return resumen
