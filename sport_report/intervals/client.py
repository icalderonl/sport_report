"""Cliente HTTP de la API v1 de intervals.icu.

Solo trae datos crudos; la normalizacion vive en `ingest.py` y los nombres de
campo no confirmados, en `campos.py`.

Diferencias de fondo con el cliente de Strava, que explican por que este es mas
corto: la autenticacion es Basic con una clave que no expira (no hay refresh ni
reintento por 401) y el rango de actividades se pide por fecha, no por
timestamp, sin paginacion.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Callable, Iterable

import httpx

from .. import config
from .auth import credenciales
from .campos import adaptar_streams, zonas_hr
from .errors import IntervalsAuthError, IntervalsError, SinPermiso

log = logging.getLogger(__name__)

REINTENTOS_5XX = 3
ESPERA_MAX_S = 5 * 60

#: Streams que se piden. `time`, `distance` y `heartrate` son los que usan la
#: carga TRIMP y el decoupling; el resto es contexto.
CLAVES_STREAM = (
    "time",
    "distance",
    "heartrate",
    "cadence",
    "watts",
    "velocity_smooth",
    "moving",
)


class IntervalsClient:
    def __init__(
        self,
        clave: str | None = None,
        atleta_id: str | None = None,
        http: httpx.Client | None = None,
        dormir: Callable[[float], None] = time.sleep,
        base: str | None = None,
    ):
        self.base = (base or config.INTERVALS_BASE).rstrip("/")
        self.atleta_id = str(atleta_id or config.INTERVALS_ATHLETE_ID or "0")
        self._clave = clave
        self._http = http
        self._dormir = dormir

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=60, auth=credenciales(self._clave))
        return self._http

    # -- transporte ------------------------------------------------------

    def _get(self, ruta: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base}{ruta}"
        intentos_5xx = 0
        espera_acumulada = 0.0

        while True:
            try:
                r = self.http.get(url, params=params)
            except httpx.HTTPError as exc:
                raise IntervalsError(f"fallo de red pidiendo {ruta}: {exc}") from exc

            if r.status_code == 401:
                # Sin reintento a proposito: una clave rechazada no se arregla
                # insistiendo, y cada intento retrasa el respaldo.
                raise IntervalsAuthError(
                    f"401 en {ruta}: intervals.icu rechazo la clave. Revisa "
                    f"INTERVALS_API_KEY en el .env (se copia por scp, no se teclea)"
                )

            if r.status_code in (403, 404):
                raise SinPermiso(
                    f"{r.status_code} en {ruta}: el recurso no existe o la cuenta "
                    f"no puede verlo. {r.text[:200]}"
                )

            if r.status_code == 429:
                espera = self._espera_rate_limit(r)
                espera_acumulada += espera
                if espera_acumulada > ESPERA_MAX_S:
                    raise IntervalsError(f"429 persistente en {ruta}")
                log.warning("429 en %s; esperando %.0fs", ruta, espera)
                self._dormir(espera)
                continue

            if r.status_code >= 500:
                intentos_5xx += 1
                if intentos_5xx > REINTENTOS_5XX:
                    raise IntervalsError(f"{r.status_code} persistente en {ruta}")
                espera = 2.0**intentos_5xx
                log.warning(
                    "%s en %s; reintento %d en %.0fs", r.status_code, ruta, intentos_5xx, espera
                )
                self._dormir(espera)
                continue

            if r.status_code >= 400:
                raise IntervalsError(f"{r.status_code} en {ruta}: {r.text[:300]}")

            try:
                return r.json()
            except ValueError as exc:
                raise IntervalsError(f"respuesta no-JSON en {ruta}: {r.text[:200]}") from exc

    @staticmethod
    def _espera_rate_limit(r: httpx.Response) -> float:
        cabecera = r.headers.get("retry-after")
        if cabecera:
            try:
                return max(1.0, float(cabecera))
            except ValueError:
                pass
        return 30.0

    # -- endpoints -------------------------------------------------------

    def atleta(self) -> dict[str, Any]:
        """Perfil del atleta. Tambien sirve para verificar la clave."""
        crudo = self._get(f"/athlete/{self.atleta_id}")
        return crudo if isinstance(crudo, dict) else {}

    def actividades(self, desde: datetime, hasta: datetime) -> list[dict[str, Any]]:
        """Actividades cuyo dia local cae en [desde, hasta].

        El filtro de la API es por fecha (`oldest`/`newest`), no por instante,
        asi que se pide el rango de dias completo y quien llama ya recorta: es
        la unica forma de no perder una actividad de las 23:xx del domingo.
        """
        if desde.tzinfo is None or hasta.tzinfo is None:
            raise ValueError("desde/hasta deben tener zona horaria")

        crudo = self._get(
            f"/athlete/{self.atleta_id}/activities",
            {"oldest": desde.date().isoformat(), "newest": hasta.date().isoformat()},
        )
        lista = crudo if isinstance(crudo, list) else []
        log.info(
            "intervals.icu devolvio %d actividades entre %s y %s",
            len(lista),
            desde.date(),
            hasta.date(),
        )
        return [a for a in lista if isinstance(a, dict)]

    def actividad(self, actividad_id: Any) -> dict[str, Any]:
        crudo = self._get(f"/activity/{actividad_id}")
        return crudo if isinstance(crudo, dict) else {}

    def streams(
        self, actividad_id: Any, claves: Iterable[str] = CLAVES_STREAM
    ) -> dict[str, list[Any]]:
        """Streams como dict {clave: lista}. {} si la actividad no tiene.

        Una actividad sin streams (registro manual, subida sin dispositivo) es
        un caso normal, no un error: quien llama decide que hacer con la falta.
        """
        try:
            crudo = self._get(
                f"/activity/{actividad_id}/streams", {"types": ",".join(claves)}
            )
        except SinPermiso:
            log.info("actividad %s sin streams disponibles", actividad_id)
            return {}
        return adaptar_streams(crudo)

    def intervalos(self, actividad_id: Any) -> list[dict[str, Any]]:
        """Vueltas de una actividad. [] si no tiene.

        intervals.icu llama a esto `intervals`, y son los tramos detectados o
        editados, no necesariamente los laps que marco el reloj. Se usan igual
        (`engine/vueltas.alinear` solo mira distancia y tiempo), pero conviene
        compararlos una vez contra los de Strava para la misma sesion de series
        antes de fiarse de la alineacion — esta en el runbook.
        """
        try:
            crudo = self._get(f"/activity/{actividad_id}/intervals")
        except SinPermiso:
            log.info("actividad %s sin intervalos disponibles", actividad_id)
            return []
        if isinstance(crudo, dict):
            # Algunas respuestas envuelven la lista en el objeto de la actividad.
            for clave in ("icu_intervals", "intervals"):
                v = crudo.get(clave)
                if isinstance(v, list):
                    crudo = v
                    break
            else:
                return []
        return [v for v in crudo if isinstance(v, dict)] if isinstance(crudo, list) else []

    def sport_settings(self) -> Any:
        return self._get(f"/athlete/{self.atleta_id}/sport-settings")

    def zonas_hr(self) -> list[dict[str, int]] | None:
        """Zonas de HR del atleta, o None si no se pueden usar.

        Devolver None es lo que permite marcar la carga como imprecisa mas
        arriba en vez de inventar zonas.
        """
        try:
            crudo = self.sport_settings()
        except SinPermiso as exc:
            log.warning("no se pudieron leer las zonas de HR: %s", exc)
            return None
        return zonas_hr(crudo)

    def bienestar(self, desde: datetime, hasta: datetime) -> list[dict[str, Any]]:
        """Registros diarios de bienestar en [desde, hasta].

        [] cuando no hay: un dia sin registrar no es un dia en cero, y quien
        llama tiene que poder distinguirlo.
        """
        try:
            crudo = self._get(
                f"/athlete/{self.atleta_id}/wellness",
                {"oldest": desde.date().isoformat(), "newest": hasta.date().isoformat()},
            )
        except SinPermiso as exc:
            log.warning("no se pudo leer el bienestar: %s", exc)
            return []
        if isinstance(crudo, dict):
            # Puede venir indexado por fecha en vez de como lista.
            crudo = [
                {**v, "id": v.get("id", k)}
                for k, v in crudo.items()
                if isinstance(v, dict)
            ]
        return [d for d in crudo if isinstance(d, dict)] if isinstance(crudo, list) else []

    def cerrar(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None
