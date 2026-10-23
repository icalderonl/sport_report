"""Cliente HTTP de la API Personal de Garmin Connect (solo lectura).

Solo trae datos crudos. Las rutas y parametros salen de la especificacion
OpenAPI publicada por Garmin (gc-developer-program/personal/swagger-docs).
Los rangos maximos por pedido tambien: sueno y Body Battery 31 dias, estres
28, resumenes diarios un ano.
"""
from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any, Callable

import httpx

from .. import config
from .auth import RUTA_TOKEN, clave
from .errors import GarminAuthError, GarminError, SinPermiso

log = logging.getLogger(__name__)

REINTENTOS_5XX = 3


class GarminClient:
    def __init__(
        self,
        clave_api: str | None = None,
        http: httpx.Client | None = None,
        dormir: Callable[[float], None] = time.sleep,
        base: str | None = None,
    ):
        self.base = (base or config.GARMIN_BASE).rstrip("/")
        self._clave = clave_api
        self._http = http
        self._dormir = dormir
        self._token: str | None = None

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=60)
        return self._http

    # -- autenticacion -----------------------------------------------------

    def token(self) -> str:
        """Access token vigente. Se pide uno por corrida: dura 24 h."""
        if self._token:
            return self._token
        try:
            r = self.http.post(
                f"{self.base}{RUTA_TOKEN}",
                headers={"Accept": "*/*", "Authorization": f"ApiKey {clave(self._clave)}"},
            )
        except httpx.HTTPError as exc:
            raise GarminError(f"fallo de red pidiendo el token: {exc}") from exc
        if r.status_code in (401, 403):
            raise GarminAuthError(
                f"{r.status_code} al pedir el token: Garmin rechazo la clave. "
                "Puede haber vencido o estar mal copiada en GARMIN_API_KEY. "
                f"Respuesta: {r.text[:200]!r}"
            )
        if r.status_code >= 400:
            raise GarminError(f"{r.status_code} al pedir el token: {r.text[:200]}")
        # La respuesta es el JWT como texto plano, no JSON.
        token = r.text.strip().strip('"')
        if not token:
            raise GarminError("Garmin devolvio un token vacio")
        self._token = token
        return token

    # -- transporte --------------------------------------------------------

    def _pedir(self, ruta: str, params: dict[str, Any] | None = None) -> httpx.Response:
        url = f"{self.base}{ruta}"
        intentos_5xx = 0
        while True:
            try:
                r = self.http.get(
                    url, params=params, headers={"Authorization": f"Bearer {self.token()}"}
                )
            except httpx.HTTPError as exc:
                raise GarminError(f"fallo de red pidiendo {ruta}: {exc}") from exc

            if r.status_code == 401:
                raise GarminAuthError(f"401 en {ruta}: token rechazado o clave vencida")
            if r.status_code in (403, 404):
                raise SinPermiso(f"{r.status_code} en {ruta}: {r.text[:200]}")
            if r.status_code == 429:
                # La especificacion no documenta cuotas; si aparece, se informa
                # en vez de esperar a ciegas.
                raise GarminError(
                    f"429 en {ruta} (retry-after={r.headers.get('retry-after')!r})"
                )
            if r.status_code >= 500:
                intentos_5xx += 1
                if intentos_5xx > REINTENTOS_5XX:
                    raise GarminError(f"{r.status_code} persistente en {ruta}")
                self._dormir(2.0**intentos_5xx)
                continue
            if r.status_code >= 400:
                raise GarminError(f"{r.status_code} en {ruta}: {r.text[:300]}")
            return r

    def _get(self, ruta: str, params: dict[str, Any] | None = None) -> Any:
        r = self._pedir(ruta, params)
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError as exc:
            raise GarminError(f"respuesta no-JSON en {ruta}: {r.text[:200]}") from exc

    # -- endpoints ---------------------------------------------------------

    def ajustes_usuario(self) -> dict[str, Any]:
        return self._get("/userprofile-service/userprofile/settings") or {}

    def actividades(self, desde: date, hasta: date, limite: int = 99) -> list[dict[str, Any]]:
        """Actividades con inicio local entre `desde` y `hasta`, ambos inclusive.

        Pagina con `start`: un backfill largo puede pasar de `limite`.
        """
        salida: list[dict[str, Any]] = []
        while True:
            pagina = (
                self._get(
                    "/activitylist-service/activities/search/activities",
                    {
                        "startDate": desde.isoformat(),
                        "endDate": hasta.isoformat(),
                        "start": len(salida),
                        "limit": limite,
                        "sortOrder": "asc",
                    },
                )
                or []
            )
            salida.extend(pagina)
            if len(pagina) < limite:
                return salida

    def actividad(self, actividad_id: Any) -> dict[str, Any]:
        return self._get(f"/activity-service/activity/{actividad_id}") or {}

    def archivo_actividad(self, actividad_id: Any) -> bytes:
        """Archivo original (FIT para un reloj) dentro de un ZIP."""
        return self._pedir(f"/download-service/files/activity/{actividad_id}").content

    def zonas_hr(self) -> list[dict[str, Any]]:
        return self._get("/biometric-service/heartRateZones/v2") or []

    def hrv(self, desde: date, hasta: date) -> dict[str, Any]:
        return self._get(f"/hrv-service/hrv/daily/{desde.isoformat()}/{hasta.isoformat()}") or {}

    def sueno(self, desde: date, hasta: date) -> list[dict[str, Any]]:
        """Maximo 31 dias por pedido."""
        return (
            self._get(
                "/sleep-service/sleep/dailySleepSummariesByDate",
                {"startDate": desde.isoformat(), "endDate": hasta.isoformat()},
            )
            or []
        )

    def resumenes_diarios(self, desde: date, hasta: date) -> list[dict[str, Any]]:
        """FC en reposo, estres y Body Battery por dia. Maximo un ano."""
        crudo = self._get(
            "/usersummary-service/v2/dailysummaries",
            {"startDate": desde.isoformat(), "endDate": hasta.isoformat()},
        )
        if isinstance(crudo, dict):
            return crudo.get("data") or []
        return crudo or []

    def cerrar(self) -> None:
        if self._http is not None:
            self._http.close()
