"""Cliente HTTP de solo lectura contra la API interna de TrainingPeaks.

TrainingPeaks no da API oficial a usuarios particulares (la de partners exige
aprobacion), asi que se usa la misma que la web: la cookie de sesion
`Production_tpAuth` se cambia por un access token en `/users/v3/token`, y con
ese token se leen los entrenamientos del calendario. Es el mismo mecanismo que
usan tp2intervals y TrainingPeaks-MCP; las rutas salen de ahi, no de una
especificacion publicada, y pueden cambiar sin aviso.

Solo se hacen GET. Nunca se escribe nada en la cuenta.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

import httpx

from .. import config
from .errors import TPAuthError, TPError

log = logging.getLogger(__name__)

NOMBRE_COOKIE = "Production_tpAuth"
RUTA_TOKEN = "/users/v3/token"
RUTA_USUARIO = "/users/v3/user"


def cookie(valor: str | None = None) -> str:
    """El valor de la cookie, aceptando tambien `Production_tpAuth=<valor>`."""
    valor = (valor if valor is not None else config.TP_AUTH_COOKIE).strip().strip(";")
    prefijo = f"{NOMBRE_COOKIE}="
    if valor.startswith(prefijo):
        valor = valor[len(prefijo) :]
    if not valor:
        raise TPAuthError(
            "falta TP_AUTH_COOKIE en el .env. Se copia del navegador con la "
            "sesion de TrainingPeaks abierta (DevTools -> Application -> "
            f"Cookies -> {NOMBRE_COOKIE})"
        )
    return valor


def resumen_cookie(valor: str | None = None) -> str:
    """Longitud y ultimos dos caracteres, nunca la cookie."""
    try:
        v = cookie(valor)
    except TPAuthError:
        return "ausente"
    return f"{len(v)} caracteres, termina en ...{v[-2:]}"


class TPClient:
    def __init__(
        self,
        cookie_tp: str | None = None,
        http: httpx.Client | None = None,
        base: str | None = None,
    ):
        self.base = (base or config.TP_BASE).rstrip("/")
        self._cookie = cookie_tp
        self._http = http
        self._token: str | None = None
        self._atleta: int | None = None

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=30)
        return self._http

    # -- autenticacion -----------------------------------------------------

    def token(self) -> str:
        """Access token de la sesion. Dura una hora: se pide uno por comando."""
        if self._token:
            return self._token
        try:
            r = self.http.get(
                f"{self.base}{RUTA_TOKEN}",
                headers={
                    "Cookie": f"{NOMBRE_COOKIE}={cookie(self._cookie)}",
                    "Accept": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise TPError(f"fallo de red pidiendo el token: {exc}") from exc
        if r.status_code in (401, 403):
            raise TPAuthError(
                f"{r.status_code} al pedir el token: TrainingPeaks rechazo la "
                "cookie. Lo normal es que haya vencido; copia una nueva en "
                "TP_AUTH_COOKIE"
            )
        if r.status_code >= 400:
            raise TPError(f"{r.status_code} al pedir el token: {r.text[:200]}")
        try:
            token = ((r.json() or {}).get("token") or {}).get("access_token")
        except ValueError as exc:
            raise TPError(f"respuesta no-JSON al pedir el token: {r.text[:200]}") from exc
        if not token:
            # Con una cookie vencida TP a veces responde 200 sin token.
            raise TPAuthError("TrainingPeaks no devolvio token: la cookie no sirve")
        self._token = str(token)
        return self._token

    # -- transporte --------------------------------------------------------

    def _get(self, ruta: str) -> Any:
        try:
            r = self.http.get(
                f"{self.base}{ruta}",
                headers={"Authorization": f"Bearer {self.token()}", "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise TPError(f"fallo de red pidiendo {ruta}: {exc}") from exc
        if r.status_code in (401, 403):
            raise TPAuthError(f"{r.status_code} en {ruta}: token rechazado")
        if r.status_code >= 400:
            raise TPError(f"{r.status_code} en {ruta}: {r.text[:300]}")
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError as exc:
            raise TPError(f"respuesta no-JSON en {ruta}: {r.text[:200]}") from exc

    # -- endpoints ---------------------------------------------------------

    def usuario(self) -> dict[str, Any]:
        return (self._get(RUTA_USUARIO) or {}).get("user") or {}

    def atleta_id(self) -> int:
        """Id del atleta autenticado.

        `personId` y no `athletes[0]`: en una cuenta de entrenador esa lista es
        la de sus atletas. En una de atleta coinciden.
        """
        if self._atleta is None:
            u = self.usuario()
            valor = u.get("personId") or u.get("userId")
            if not valor:
                raise TPError(f"{RUTA_USUARIO} no trae personId ni userId")
            self._atleta = int(valor)
        return self._atleta

    def entrenamientos(self, desde: date, hasta: date) -> list[dict[str, Any]]:
        """Entrenamientos del calendario entre `desde` y `hasta`, ambos inclusive.

        Trae planificados y completados mezclados; distinguirlos es tarea de
        quien convierte.
        """
        ruta = (
            f"/fitness/v6/athletes/{self.atleta_id()}/workouts/"
            f"{desde.isoformat()}/{hasta.isoformat()}"
        )
        return self._get(ruta) or []

    def cerrar(self) -> None:
        if self._http is not None:
            self._http.close()
