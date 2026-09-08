"""Ingesta: intervals.icu -> sesiones normalizadas + bienestar -> SQLite.

Es la fuente principal. Ademas de lo que da Strava, trae lo que motivo el
cambio: GCT, oscilacion vertical, ratio vertical y bienestar diario.

La regla que gobierna todo el archivo: **un dato que la fuente no trajo se
guarda como `None`**. Ni cero, ni estimado, ni derivado de otros campos. Es lo
que permite que el reporte diga "no disponible" con motivo en vez de mentir con
un cero, y lo que hace honesta la semana que sale por el respaldo de Strava.

La orquestacion (idempotencia, cache de zonas, banderas) vive en
`sport_report/fuentes/comun.py`.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Any

from .. import config
from ..db.models import BienestarDia, SesionReal, Vuelta
from ..fechas import letra_dia
from ..fuentes.comun import IngestaBase, ResumenIngesta, _limites
from ..strava import metricas
from . import campos
from .client import IntervalsClient
from .errors import IntervalsError

log = logging.getLogger(__name__)


def _a_local(act: dict[str, Any]) -> datetime:
    """Instante local de inicio.

    intervals.icu da `start_date_local` (sin zona, ya en la del atleta) y a
    veces `start_date` en UTC. Se prefiere el local porque es el que define el
    dia del plan; si solo hay UTC, se convierte.
    """
    crudo = act.get("start_date_local")
    if crudo:
        d = datetime.fromisoformat(str(crudo).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=config.TZ)

    crudo = act.get("start_date")
    if not crudo:
        raise IntervalsError(f"la actividad {act.get('id')} no trae fecha de inicio")
    return datetime.fromisoformat(str(crudo).replace("Z", "+00:00")).astimezone(config.TZ)


def normalizar_intervalos(
    fuente: str, id_externo: Any, crudos: list[dict[str, Any]]
) -> list[Vuelta]:
    """Intervalos de intervals.icu -> `Vuelta`, descartando los inservibles.

    Mismo criterio que en Strava: sin distancia o sin tiempo no aportan nada a
    la alineacion contra `estructura=` y solo pueden estorbar.
    """
    salida: list[Vuelta] = []
    for i, v in enumerate(crudos):
        if not isinstance(v, dict):
            continue
        metros = campos._numero(v.get("distance") or v.get("icu_distance"))
        segundos = campos._numero(
            v.get("moving_time")
            or v.get("elapsed_time")
            or v.get("icu_moving_time")
            or v.get("duration")
        )
        indice = campos._numero(v.get("id") if isinstance(v.get("id"), (int, float)) else None)
        if indice is None:
            indice = campos._numero(v.get("index")) or (i + 1)
        if not metros or not segundos or metros <= 0 or segundos <= 0:
            continue
        salida.append(
            Vuelta(
                fuente=fuente,
                id_externo=str(id_externo),
                indice=int(indice),
                distancia_km=round(metros / 1000.0, 3),
                duracion_mov_s=int(segundos),
            )
        )
    salida.sort(key=lambda v: v.indice)
    return salida


def normalizar_bienestar(fuente: str, crudo: dict[str, Any]) -> BienestarDia | None:
    """Un registro de wellness -> `BienestarDia`, o None si no tiene fecha.

    Se guarda `crudo` entero: varios nombres de campo no estan confirmados, y
    esto permite poblar una columna nueva mas adelante sin volver a pedir nada
    a la API.
    """
    fecha = crudo.get("id") or crudo.get("date") or crudo.get("day")
    if not fecha:
        return None
    try:
        fecha_local = date.fromisoformat(str(fecha)[:10]).isoformat()
    except ValueError:
        log.warning("registro de bienestar con fecha ilegible: %r", fecha)
        return None

    sueno_s = campos.numero(crudo, "sueno_s")
    return BienestarDia(
        fecha_local=fecha_local,
        fuente=fuente,
        hrv=campos.numero(crudo, "hrv"),
        hr_reposo=campos.numero(crudo, "hr_reposo"),
        # Las horas se derivan de los segundos que da la API; que no vengan es
        # None, no 0.0 horas de sueno.
        sueno_h=round(sueno_s / 3600.0, 2) if sueno_s else None,
        sueno_score=campos.numero(crudo, "sueno_score"),
        readiness=campos.numero(crudo, "readiness"),
        body_battery=campos.numero(crudo, "body_battery"),
        crudo=json.dumps(crudo, ensure_ascii=False)[:4000],
    )


class Ingesta(IngestaBase):
    NOMBRE = config.INTERVALS
    ERRORES = (IntervalsError,)

    # -- ganchos de la fuente --------------------------------------------

    def _cliente_nuevo(self) -> IntervalsClient:
        return IntervalsClient()

    def _listar(self, ini: datetime, fin: datetime) -> list[dict[str, Any]]:
        return self.cliente.actividades(ini, fin)

    def _streams(self, actividad_id: Any) -> dict[str, list[Any]]:
        return self.cliente.streams(actividad_id)

    def _vueltas_crudas(self, actividad_id: Any) -> list[dict[str, Any]]:
        return self.cliente.intervalos(actividad_id)

    def _normalizar_vueltas(
        self, actividad_id: Any, crudas: list[dict[str, Any]]
    ) -> list[Vuelta]:
        return normalizar_intervalos(self.NOMBRE, actividad_id, crudas)

    def _zonas_remotas(self) -> list[dict[str, int]] | None:
        return self.cliente.zonas_hr()

    def _id(self, act: dict[str, Any]) -> str:
        return str(act["id"])

    def _inicio_local(self, act: dict[str, Any]) -> datetime:
        return _a_local(act)

    def _tipo(self, act: dict[str, Any]) -> str:
        return str(act.get("type") or act.get("sport_type") or "Desconocido")

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

        metros = campos.numero(act, "distancia_m")
        # La potencia solo si el dispositivo la reporta: nunca se inventa.
        potencia = campos.numero(act, "potencia_w") if act.get("device_watts") else None

        return SesionReal(
            fuente=self.NOMBRE,
            id_externo=self._id(act),
            fecha_utc=local.astimezone(timezone.utc).isoformat(timespec="seconds"),
            fecha_local=local.date().isoformat(),
            dia_semana=letra_dia(local.date()),
            tipo=tipo,
            es_fuerza=es_fuerza,
            nombre=act.get("name"),
            distancia_km=(
                round(metros / 1000.0, 3) if metros and metros > 0 and not es_fuerza else None
            ),
            duracion_mov_s=campos.entero(act, "duracion_mov_s"),
            duracion_tot_s=campos.entero(act, "duracion_tot_s"),
            hr_promedio=campos.numero(act, "hr_promedio"),
            hr_maximo=campos.numero(act, "hr_maximo"),
            cadencia_spm=campos.cadencia_spm(campos.numero(act, "cadencia")) if es_run else None,
            potencia_w=potencia if es_run else None,
            # La dinamica avanzada es el motivo del cambio de fuente. Solo para
            # corridas, y `None` cuando el reloj no la midio.
            gct_ms=campos.gct_ms(campos.numero(act, "gct_ms")) if es_run else None,
            oscilacion_vertical_cm=(
                campos.oscilacion_cm(campos.numero(act, "oscilacion_vertical_cm"))
                if es_run
                else None
            ),
            ratio_vertical_pct=(
                campos.numero(act, "ratio_vertical_pct") if es_run else None
            ),
            decoupling_pct=deriva,
            carga=carga.carga,
            carga_impreciso=carga.impreciso,
            streams_procesados=streams_procesados,
            vueltas_procesadas=vueltas_procesadas,
        )

    # -- bienestar -------------------------------------------------------

    def _extras(self, desde: date, hasta: date, resumen: ResumenIngesta) -> None:
        """Trae y persiste el bienestar de la ventana.

        Un fallo aca NO tumba la ingesta: las actividades ya estan guardadas y
        perderlas por no poder leer el HRV seria peor. La semana queda sin
        bienestar y el reporte lo declara no disponible, que es la verdad.

        Se pide una semana mas atras del rango: el reporte compara la tendencia
        contra la semana anterior, y sin esos dias la comparacion no existe.
        """
        from datetime import timedelta

        ini, fin = _limites(desde - timedelta(days=7), hasta)
        try:
            crudos = self.cliente.bienestar(ini, fin)
        except self.ERRORES as exc:
            log.warning("el bienestar de %s..%s fallo: %s", desde, hasta, exc)
            resumen.avisos.append(
                "no se pudo leer el bienestar de intervals.icu: HRV, sueno, sleep "
                "score, Body Battery y readiness quedan sin dato esta semana"
            )
            return

        dias = [d for d in (normalizar_bienestar(self.NOMBRE, c) for c in crudos) if d]
        # Un dia que llego pero sin ninguna metrica no aporta nada y ensuciaria
        # el promedio con una fila vacia.
        utiles = [d for d in dias if not d.vacio]
        resumen.bienestar_dias = self.repo.guardar_bienestar(utiles)
        if not utiles:
            resumen.avisos.append(
                "intervals.icu no reporto ninguna metrica de bienestar en la ventana"
            )

    # -- resumen ---------------------------------------------------------

    def sincronizar(
        self, desde: date, hasta: date, forzar: bool = False, refrescar_zonas: bool = True
    ) -> ResumenIngesta:
        resumen = super().sincronizar(desde, hasta, forzar, refrescar_zonas)
        # Cuantas corridas de la semana quedaron sin dinamica avanzada. Lo usa
        # el reporte para decir "el reloj no la midio" en vez de callarse.
        sesiones = [s for s in self.repo.sesiones_entre(desde, hasta) if s.es_run]
        resumen.sin_dinamica = sum(1 for s in sesiones if s.gct_ms is None)
        if sesiones and resumen.sin_dinamica == len(sesiones):
            resumen.avisos.append(
                "ninguna corrida de la semana trae GCT ni oscilacion vertical: "
                "revisa que el reloj las mida y que los nombres de campo de "
                "intervals/campos.py sean los correctos"
            )
        return resumen
