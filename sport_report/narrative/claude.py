"""Capa narrativa: una llamada semanal a la API de Claude.

El LLM narra, no calcula. Recibe unicamente el JSON del motor de calculo (fase 5)
y tiene prohibido introducir cifras que no esten ahi. Ademas, la narrativa se
verifica despues contra el JSON: los numeros que aparecen en el texto tienen que
existir en los datos.

Si la API falla por lo que sea, esta capa devuelve `texto=None` y el reporte se
envia igual sin la parte narrativa (spec 10): nunca hace caer la corrida.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .. import config

log = logging.getLogger(__name__)

MAX_TOKENS = 1024
TIMEOUT_S = 60.0

SISTEMA = """\
Eres el redactor de un reporte semanal de entrenamiento de running. Recibes un
JSON con metricas YA CALCULADAS y escribes el resumen para el atleta.

REGLA ABSOLUTA: no puedes introducir ninguna cifra que no este literalmente en el
JSON. No calcules, no promedies, no estimes, no conviertas unidades, no infieras
porcentajes ni totales. Si un numero que quieres decir no esta en el JSON, no lo
digas.

Como leer el JSON:
- Un bloque con "confiable": false NO es un hecho. Si lo mencionas, di
  explicitamente que no es confiable y por que (esta en "motivo").
- "avisos_datos" describe huecos de datos. Si hay alguno relevante, mencionalo.
- Una adherencia con "pct": null es un DATO FALTANTE, no un incumplimiento. Nunca
  la presentes como sesion no hecha.
- Los valores de "umbrales" son de literatura general, no estan calibrados a este
  atleta. No los presentes como verdad medica.

Formato de salida:
- 3 a 5 lineas de resumen ejecutivo, en espanol, tono directo y concreto.
- Despues, si "alertas" no esta vacio, una linea por alerta empezando con "Alerta:".
- Texto plano. Sin markdown, sin encabezados, sin vinetas, sin emojis.
- No des consejo medico ni prescribas entrenamientos.\
"""

INSTRUCCION = (
    "Redacta el resumen de esta semana a partir del siguiente JSON. "
    "Recuerda: ninguna cifra que no este en el JSON.\n\n"
)


@dataclass
class Narrativa:
    texto: str | None
    modelo: str = ""
    error: str = ""
    tokens_entrada: int = 0
    tokens_salida: int = 0
    numeros_no_verificados: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.texto is not None

    def to_json(self) -> dict[str, Any]:
        return {
            "texto": self.texto,
            "modelo": self.modelo,
            "error": self.error,
            "tokens_entrada": self.tokens_entrada,
            "tokens_salida": self.tokens_salida,
            "numeros_no_verificados": self.numeros_no_verificados,
        }


# --------------------------------------------------------------------------
# Verificacion de cifras
# --------------------------------------------------------------------------

_RE_NUMERO = re.compile(r"-?\d+(?:[.,]\d+)?")


def _numeros_del_json(datos: Any, acc: set[str] | None = None) -> set[str]:
    """Todos los numeros presentes en el JSON, incluidos los embebidos en texto."""
    acc = acc if acc is not None else set()
    if isinstance(datos, bool) or datos is None:
        return acc
    if isinstance(datos, (int, float)):
        acc.add(_norm(datos))
        return acc
    if isinstance(datos, str):
        for m in _RE_NUMERO.findall(datos):
            acc.add(_norm(m))
        return acc
    if isinstance(datos, dict):
        for k, v in datos.items():
            _numeros_del_json(k, acc)
            _numeros_del_json(v, acc)
        return acc
    if isinstance(datos, (list, tuple)):
        for v in datos:
            _numeros_del_json(v, acc)
    return acc


def _norm(valor: Any) -> str:
    """Normaliza un numero a texto comparable (sin ceros finales, coma -> punto)."""
    try:
        f = float(str(valor).replace(",", "."))
    except (TypeError, ValueError):
        return str(valor)
    if f == int(f):
        return str(int(f))
    return f"{f:.10g}"


def verificar_cifras(texto: str, datos: dict[str, Any]) -> list[str]:
    """Numeros del texto que no aparecen en el JSON.

    Tolera el redondeo a un decimal: si el modelo dice 1.7 y el JSON trae 1.72, se
    acepta. Lo que se busca es la cifra directamente inventada.
    """
    presentes = _numeros_del_json(datos)
    redondeados = set()
    for p in presentes:
        try:
            redondeados.add(_norm(round(float(p), 1)))
            redondeados.add(_norm(round(float(p))))
        except ValueError:
            continue
    conocidos = presentes | redondeados

    sospechosos: list[str] = []
    for bruto in _RE_NUMERO.findall(texto):
        n = _norm(bruto)
        if n in conocidos:
            continue
        try:
            f = float(n)
        except ValueError:
            continue
        if _norm(round(f, 1)) in conocidos or _norm(round(f)) in conocidos:
            continue
        sospechosos.append(bruto)
    return sospechosos


# --------------------------------------------------------------------------
# Llamada
# --------------------------------------------------------------------------


def _cliente(api_key: str):
    # Import perezoso: el resto del sistema no debe depender del SDK ni de que
    # haya API key configurada.
    import anthropic

    return anthropic.Anthropic(api_key=api_key, timeout=TIMEOUT_S)


def _texto_de(respuesta: Any) -> str:
    partes = [b.text for b in respuesta.content if getattr(b, "type", "") == "text"]
    return "\n".join(p.strip() for p in partes if p).strip()


def redactar(
    datos: dict[str, Any],
    cliente: Any = None,
    modelo: str | None = None,
    api_key: str | None = None,
) -> Narrativa:
    """Pide el resumen a Claude. Nunca lanza: ante cualquier fallo devuelve texto=None."""
    modelo = modelo or config.ANTHROPIC_MODEL
    clave = api_key if api_key is not None else config.ANTHROPIC_API_KEY

    if cliente is None:
        if not clave:
            log.warning("sin ANTHROPIC_API_KEY: el reporte se envia sin narrativa")
            return Narrativa(texto=None, modelo=modelo, error="ANTHROPIC_API_KEY sin configurar")
        try:
            cliente = _cliente(clave)
        except ImportError as exc:
            return Narrativa(texto=None, modelo=modelo, error=f"SDK anthropic no instalado: {exc}")

    cuerpo = INSTRUCCION + json.dumps(datos, ensure_ascii=False, indent=1)

    try:
        respuesta = cliente.messages.create(
            model=modelo,
            max_tokens=MAX_TOKENS,
            system=SISTEMA,
            messages=[{"role": "user", "content": cuerpo}],
        )
    except Exception as exc:  # incluye errores del SDK, red y timeouts
        log.error("fallo la llamada a la API de Claude: %s: %s", type(exc).__name__, exc)
        return Narrativa(texto=None, modelo=modelo, error=f"{type(exc).__name__}: {exc}")

    if getattr(respuesta, "stop_reason", "") == "refusal":
        detalle = getattr(respuesta, "stop_details", None)
        motivo = getattr(detalle, "category", "") if detalle else ""
        log.warning("el modelo declino redactar (%s)", motivo)
        return Narrativa(texto=None, modelo=modelo, error=f"refusal: {motivo}")

    texto = _texto_de(respuesta)
    if not texto:
        return Narrativa(texto=None, modelo=modelo, error="respuesta vacia")

    uso = getattr(respuesta, "usage", None)
    sospechosos = verificar_cifras(texto, datos)
    if sospechosos:
        # No se descarta la narrativa (un falso positivo dejaria el reporte mudo),
        # pero queda en el log y en el JSON para poder auditarlo.
        log.warning("la narrativa trae cifras que no estan en el JSON: %s", sospechosos)

    return Narrativa(
        texto=texto,
        modelo=getattr(respuesta, "model", modelo),
        tokens_entrada=getattr(uso, "input_tokens", 0) or 0,
        tokens_salida=getattr(uso, "output_tokens", 0) or 0,
        numeros_no_verificados=sospechosos,
    )
