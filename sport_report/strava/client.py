"""Cliente HTTP de la API v3 de Strava.

Solo trae datos crudos; la normalizacion y los calculos van en ingest.py (fase 4).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Callable, Iterable

import httpx

from .auth import StravaAuth
from .errors import SinPermiso, StravaError, StravaRateLimit

log = logging.getLogger(__name__)

BASE = "https://www.strava.com/api/v3"
POR_PAGINA = 100

# Espera maxima total por rate limit dentro de una corrida. La cuota de 15
# minutos se renueva en el minuto 0/15/30/45, asi que 16 min cubre una ventana.
ESPERA_MAX_S = 16 * 60
REINTENTOS_5XX = 3

# `moving` permite descontar las pausas al recorrer el stream.
CLAVES_STREAM = (
    "time",
    "distance",
    "heartrate",
    "cadence",
    "watts",
    "velocity_smooth",
    "moving",
)


class StravaClient:
    def __init__(
        self,
        auth: StravaAuth | None = None,
        http: httpx.Client | None = None,
        dormir: Callable[[float], None] = time.sleep,
        pausa_entre_requests: float = 0.0,
    ):
        self.auth = auth or StravaAuth()
        self._http = http
        self._dormir = dormir
        # Usado en el backfill inicial para no vaciar la cuota de golpe.
        self.pausa_entre_requests = pausa_entre_requests
        self.ultimo_uso: str | None = None

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=60)
        return self._http

    # -- transporte ------------------------------------------------------

    def _get(self, ruta: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{BASE}{ruta}"
        espera_acumulada = 0.0
        intentos_5xx = 0
        reintento_auth = False

        while True:
            if self.pausa_entre_requests:
                self._dormir(self.pausa_entre_requests)

            token = self.auth.access_token()
            try:
                r = self.http.get(
                    url, params=params, headers={"Authorization": f"Bearer {token}"}
                )
            except httpx.HTTPError as exc:
                raise StravaError(f"fallo de red pidiendo {ruta}: {exc}") from exc

            self.ultimo_uso = r.headers.get("x-ratelimit-usage")

            if r.status_code == 401 and not reintento_auth:
                # El access_token puede haber sido revocado antes de expirar.
                log.info("401 en %s; forzando refresh del token", ruta)
                reintento_auth = True
                self.auth.access_token(forzar=True)
                continue

            if r.status_code == 429:
                espera = self._espera_rate_limit(r)
                espera_acumulada += espera
                if espera_acumulada > ESPERA_MAX_S:
                    raise StravaRateLimit(
                        f"cuota de Strava agotada en {ruta}; uso={self.ultimo_uso}"
                    )
                log.warning("429 en %s; esperando %.0fs (uso=%s)", ruta, espera, self.ultimo_uso)
                self._dormir(espera)
                continue

            if r.status_code in (403, 404):
                raise SinPermiso(
                    f"{r.status_code} en {ruta}: recurso inexistente o el token no tiene "
                    f"el scope necesario. {r.text[:200]}"
                )

            if r.status_code >= 500:
                intentos_5xx += 1
                if intentos_5xx > REINTENTOS_5XX:
                    raise StravaError(f"{r.status_code} persistente en {ruta}")
                espera = 2.0**intentos_5xx
                log.warning("%s en %s; reintento %d en %.0fs", r.status_code, ruta, intentos_5xx, espera)
                self._dormir(espera)
                continue

            if r.status_code >= 400:
                raise StravaError(f"{r.status_code} en {ruta}: {r.text[:300]}")

            return r.json()

    @staticmethod
    def _espera_rate_limit(r: httpx.Response) -> float:
        cabecera = r.headers.get("retry-after")
        if cabecera:
            try:
                return max(1.0, float(cabecera))
            except ValueError:
                pass
        # Sin Retry-After: esperar al proximo cuarto de hora.
        segundos_en_ventana = (time.time() % 900)
        return max(30.0, 900 - segundos_en_ventana)

    # -- endpoints -------------------------------------------------------

    def atleta(self) -> dict[str, Any]:
        return self._get("/athlete")

    def actividades(self, desde: datetime, hasta: datetime) -> list[dict[str, Any]]:
        """Actividades en [desde, hasta). Ambos datetime deben ser aware."""
        if desde.tzinfo is None or hasta.tzinfo is None:
            raise ValueError("desde/hasta deben tener zona horaria")

        salida: list[dict[str, Any]] = []
        pagina = 1
        while True:
            lote = self._get(
                "/athlete/activities",
                {
                    "after": int(desde.timestamp()),
                    "before": int(hasta.timestamp()),
                    "page": pagina,
                    "per_page": POR_PAGINA,
                },
            )
            if not lote:
                break
            salida.extend(lote)
            if len(lote) < POR_PAGINA:
                break
            pagina += 1
        log.info("Strava devolvio %d actividades entre %s y %s", len(salida), desde, hasta)
        return salida

    def actividad(self, actividad_id: int) -> dict[str, Any]:
        return self._get(f"/activities/{actividad_id}", {"include_all_efforts": "false"})

    def streams(
        self, actividad_id: int, claves: Iterable[str] = CLAVES_STREAM
    ) -> dict[str, list[Any]]:
        """Streams como dict {clave: lista}. Devuelve {} si la actividad no tiene.

        Una actividad sin streams (registro manual, subida sin dispositivo) es un
        caso normal, no un error: quien llama decide que hacer con la falta.
        """
        try:
            crudo = self._get(
                f"/activities/{actividad_id}/streams",
                {"keys": ",".join(claves), "key_by_type": "true"},
            )
        except SinPermiso:
            log.info("actividad %s sin streams disponibles", actividad_id)
            return {}
        if not isinstance(crudo, dict):
            return {}
        return {k: v.get("data", []) for k, v in crudo.items() if isinstance(v, dict)}

    def vueltas(self, actividad_id: int) -> list[dict[str, Any]]:
        """Vueltas (`laps`) de una actividad. [] si no tiene.

        Como con los streams, no tener vueltas es un caso normal —una subida
        manual no las trae— y no un error: quien llama decide que hacer.
        """
        try:
            crudo = self._get(f"/activities/{actividad_id}/laps")
        except SinPermiso:
            log.info("actividad %s sin vueltas disponibles", actividad_id)
            return []
        return crudo if isinstance(crudo, list) else []

    def zonas_hr(self) -> list[dict[str, int]] | None:
        """Zonas de HR del atleta, o None si no las tiene configuradas.

        Devolver None (en vez de inventar zonas) es lo que permite marcar la
        carga como imprecisa mas arriba, tal como exige la spec.
        """
        try:
            crudo = self._get("/athlete/zones")
        except SinPermiso as exc:
            log.warning("no se pudieron leer las zonas de HR: %s", exc)
            return None
        zonas = (crudo or {}).get("heart_rate", {}).get("zones") or []
        # Strava devuelve 5 zonas con min/max; la ultima trae max = -1 (sin tope).
        limpias = [
            {"min": int(z.get("min", 0)), "max": int(z.get("max", -1))}
            for z in zonas
            if isinstance(z, dict)
        ]
        if len(limpias) < 5 or all(z["max"] <= 0 and z["min"] <= 0 for z in limpias):
            log.warning("el atleta no tiene zonas de HR utiles configuradas en Strava")
            return None
        return limpias

    def cerrar(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None
        # El cliente de auth es otro: sin esto quedaba abierto para siempre.
        self.auth.cerrar()
