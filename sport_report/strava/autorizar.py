"""Flujo OAuth inicial de Strava. Se corre UNA vez, a mano, con navegador.

    python -m sport_report.strava.autorizar

Levanta un servidor local en http://localhost:8721, abre el navegador para que
autorices la app, recibe el `code` y lo canjea por los tokens, que quedan
guardados en data/tokens.json. Desde ahi el cron se refresca solo.

Se puede correr desde el PC aunque el sistema viva en la Pi: basta copiar
data/tokens.json a la Pi despues.
"""
from __future__ import annotations

import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from .. import config
from .auth import SCOPES, StravaAuth
from .errors import StravaAuthError

PUERTO = 8721
REDIRECT = f"http://localhost:{PUERTO}/exchange_token"

_resultado: dict[str, str] = {}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (nombre impuesto por la clase base)
        q = parse_qs(urlparse(self.path).query)
        _resultado["code"] = (q.get("code") or [""])[0]
        _resultado["scope"] = (q.get("scope") or [""])[0]
        _resultado["error"] = (q.get("error") or [""])[0]
        cuerpo = (
            "Listo, puedes cerrar esta pestana."
            if _resultado["code"]
            else f"Fallo la autorizacion: {_resultado['error']}"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(cuerpo.encode("utf-8"))
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def log_message(self, *args) -> None:  # silencia el log del servidor
        pass


def main() -> int:
    if not config.STRAVA_CLIENT_ID or not config.STRAVA_CLIENT_SECRET:
        print("Falta STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET en .env", file=sys.stderr)
        return 1

    auth = StravaAuth()
    url = auth.url_autorizacion(REDIRECT)

    print("En la config de tu app en Strava, 'Authorization Callback Domain'")
    print("debe ser exactamente: localhost\n")
    print("Abriendo el navegador. Si no se abre, pega esta URL:\n")
    print(url, "\n")
    webbrowser.open(url)

    servidor = HTTPServer(("localhost", PUERTO), _Handler)
    print(f"Esperando la respuesta en {REDIRECT} ...")
    servidor.serve_forever()
    servidor.server_close()

    if not _resultado.get("code"):
        print(f"No llego el code: {_resultado.get('error') or 'sin detalle'}", file=sys.stderr)
        return 1

    otorgado = set(filter(None, _resultado.get("scope", "").split(",")))
    faltantes = set(SCOPES.split(",")) - otorgado
    if faltantes:
        print(f"\nADVERTENCIA: faltan scopes {sorted(faltantes)}.")
        print("Sin activity:read_all no se ven las actividades privadas;")
        print("sin profile:read_all no se leen las zonas de HR.\n")

    try:
        tokens = auth.intercambiar_codigo(_resultado["code"])
    except StravaAuthError as exc:
        print(f"Error canjeando el code: {exc}", file=sys.stderr)
        return 1

    print(f"Tokens guardados en {auth.store.path}")
    print(f"El access_token vence en epoch {tokens.expires_at}; el refresh es automatico.")
    print("Copia ese archivo a la Pi (misma ruta) si autorizaste desde el PC.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
