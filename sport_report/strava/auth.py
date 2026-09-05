"""OAuth de Strava con persistencia del refresh_token rotado.

Strava puede devolver un `refresh_token` NUEVO en cualquier intercambio. Si no
se persiste, el siguiente cron intenta refrescar con un token ya invalidado y el
sistema se rompe solo, en silencio, semanas despues de instalarlo. Por eso:

  - los tokens viven en `data/tokens.json` (no en `.env`, que se reescribe mal),
  - se escriben de forma atomica en CADA intercambio, aunque el refresh_token
    parezca no haber cambiado,
  - se persiste ANTES de devolver el access_token al llamador, para que un fallo
    posterior no deje el token nuevo solo en memoria.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .. import config
from ..storage import escribir_json, leer_json
from .errors import StravaAuthError

log = logging.getLogger(__name__)

URL_TOKEN = "https://www.strava.com/oauth/token"
URL_AUTORIZAR = "https://www.strava.com/oauth/authorize"
SCOPES = "read,activity:read_all,profile:read_all"

# Margen para no llegar con un access_token recien vencido a mitad de corrida.
MARGEN_S = 300


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: int

    def vencido(self, margen: int = MARGEN_S, ahora: float | None = None) -> bool:
        return (ahora if ahora is not None else time.time()) + margen >= self.expires_at

    def to_json(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
        }

    @staticmethod
    def from_json(d: dict[str, Any]) -> "Tokens":
        return Tokens(
            access_token=d.get("access_token", ""),
            refresh_token=d["refresh_token"],
            expires_at=int(d.get("expires_at", 0)),
        )


class TokenStore:
    def __init__(self, path: Path | None = None, refresh_inicial: str | None = None):
        self.path = path or config.TOKENS_PATH
        # Bootstrap: la primera corrida puede tomar el refresh_token de .env.
        # Desde ahi manda tokens.json y .env deja de consultarse.
        self.refresh_inicial = (
            refresh_inicial if refresh_inicial is not None else config.STRAVA_REFRESH_TOKEN
        )

    def cargar(self) -> Tokens | None:
        d = leer_json(self.path)
        if d:
            return Tokens.from_json(d)
        if self.refresh_inicial:
            log.info("sin %s: arrancando con el refresh_token de .env", self.path.name)
            return Tokens(access_token="", refresh_token=self.refresh_inicial, expires_at=0)
        return None

    def guardar(self, tokens: Tokens) -> None:
        escribir_json(self.path, tokens.to_json())
        try:
            self.path.chmod(0o600)  # no-op efectivo en Windows
        except OSError:
            pass


class StravaAuth:
    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        store: TokenStore | None = None,
        http: httpx.Client | None = None,
    ):
        self.client_id = client_id if client_id is not None else config.STRAVA_CLIENT_ID
        self.client_secret = (
            client_secret if client_secret is not None else config.STRAVA_CLIENT_SECRET
        )
        self.store = store or TokenStore()
        self._http = http
        self._tokens: Tokens | None = None

    # -- HTTP ------------------------------------------------------------

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=30)
        return self._http

    def _post_token(self, extra: dict[str, str]) -> Tokens:
        if not self.client_id or not self.client_secret:
            raise StravaAuthError("faltan STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET en .env")
        datos = {"client_id": self.client_id, "client_secret": self.client_secret, **extra}
        try:
            r = self.http.post(URL_TOKEN, data=datos)
        except httpx.HTTPError as exc:
            raise StravaAuthError(f"no se pudo contactar a Strava: {exc}") from exc

        if r.status_code in (400, 401):
            raise StravaAuthError(
                "Strava rechazo el refresh_token. Hay que volver a autorizar la app "
                "(ver deploy/runbook-strava.md). Respuesta: " + r.text[:300]
            )
        if r.status_code >= 400:
            raise StravaAuthError(f"error {r.status_code} pidiendo el token: {r.text[:300]}")

        cuerpo = r.json()
        faltantes = [k for k in ("access_token", "refresh_token", "expires_at") if k not in cuerpo]
        if faltantes:
            raise StravaAuthError(f"respuesta de token incompleta, faltan: {faltantes}")
        return Tokens(
            access_token=cuerpo["access_token"],
            refresh_token=cuerpo["refresh_token"],
            expires_at=int(cuerpo["expires_at"]),
        )

    # -- API publica -----------------------------------------------------

    def intercambiar_codigo(self, code: str) -> Tokens:
        """Setup inicial (una sola vez): canjea el `code` del navegador."""
        tokens = self._post_token({"code": code, "grant_type": "authorization_code"})
        self.store.guardar(tokens)
        self._tokens = tokens
        return tokens

    def refrescar(self, tokens: Tokens | None = None) -> Tokens:
        previos = tokens or self._tokens or self.store.cargar()
        if previos is None:
            raise StravaAuthError(
                "no hay refresh_token. Corre el flujo inicial "
                "(python -m sport_report.strava.autorizar)"
            )
        nuevos = self._post_token(
            {"refresh_token": previos.refresh_token, "grant_type": "refresh_token"}
        )
        # Persistir SIEMPRE y ANTES de usarlo: si Strava roto el token y esto
        # falla despues, el proximo cron ya no tendria como entrar.
        self.store.guardar(nuevos)
        if nuevos.refresh_token != previos.refresh_token:
            log.info("Strava roto el refresh_token; el nuevo quedo persistido")
        self._tokens = nuevos
        return nuevos

    def access_token(self, forzar: bool = False) -> str:
        if self._tokens is None:
            self._tokens = self.store.cargar()
        if self._tokens is None:
            raise StravaAuthError(
                "no hay credenciales de Strava. Corre el flujo inicial "
                "(python -m sport_report.strava.autorizar)"
            )
        if forzar or not self._tokens.access_token or self._tokens.vencido():
            self._tokens = self.refrescar(self._tokens)
        return self._tokens.access_token

    def url_autorizacion(self, redirect_uri: str) -> str:
        from urllib.parse import urlencode

        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "approval_prompt": "force",
            "scope": SCOPES,
        }
        return f"{URL_AUTORIZAR}?{urlencode(params)}"
