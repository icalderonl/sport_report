"""Capa narrativa: una llamada semanal a la API de Claude.

El LLM narra, no calcula. Recibe unicamente el JSON del motor de calculo (fase 5)
y tiene prohibido introducir cifras que no esten ahi. Despues el texto pasa por
dos comprobaciones distintas, que atrapan errores distintos:

  - `verificar_cifras`: cada numero del texto tiene que EXISTIR en el JSON.
    Atrapa la cifra inventada de la nada.
  - `verificar_atribucion`: un numero pegado al nombre de una metrica tiene que
    ser de esa metrica. Atrapa la cifra intercambiada —"ACWR 1.32" cuando 1.32
    es el Monotony—, que la primera deja pasar porque 1.32 si esta en el JSON.

Ninguna de las dos descarta el texto: un falso positivo dejaria el reporte mudo.
La primera queda en el log y en el JSON; la segunda, ademas, sube a
`avisos_datos` para que el atleta sepa que no se fie de ese numero.

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

# El SDK ya reintenta solo (por defecto 2 veces, con backoff y respetando
# retry-after) ante fallo de red, 429 y 5xx. Se sube y se deja explicito porque
# el default esta pensado para trafico interactivo y esto es otra cosa: se
# ejecuta una vez por semana, un fallo cuesta la narrativa de esa semana entera,
# y la unidad de systemd tolera 30 minutos. Un `overloaded_error` de un minuto
# no deberia costar el resumen.
REINTENTOS = 5

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
    # Cifras pegadas al nombre de una metrica que no son de esa metrica.
    cifras_mal_atribuidas: list[str] = field(default_factory=list)

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
            "cifras_mal_atribuidas": self.cifras_mal_atribuidas,
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


# --------------------------------------------------------------------------
# Verificacion por metrica (atribucion)
# --------------------------------------------------------------------------
#
# `verificar_cifras` comprueba que un numero EXISTA en el JSON, no que este bien
# usado. Con veinte metricas ahi dentro, el error mas probable de un modelo no
# es inventar una cifra de la nada sino intercambiarlas: escribir "ACWR 1.32"
# cuando 1.32 es el Monotony pasa la comprobacion global sin problemas, porque
# 1.32 si esta en el JSON.
#
# Esto es lo contrario: se busca el nombre de la metrica en el texto y se exige
# que el numero pegado a el sea uno de LOS SUYOS. No se compara contra un unico
# valor porque una misma metrica se nombra junto a varias cifras legitimas —
# "ACWR 1.72 sobre el umbral 1.5" menciona el ratio y el umbral—, asi que cada
# metrica declara todas las rutas que puede citar.

# Cuantos caracteres se toleran entre el nombre de la metrica y su numero. Sin
# saltos de linea: si hay que cruzar una, ya no es la misma frase.
_VENTANA = 24
_NUM = r"-?\d+(?:[.,]\d+)?"


def _patron(alternativas: str) -> re.Pattern[str]:
    return re.compile(rf"(?:{alternativas})[^\d\n]{{0,{_VENTANA}}}({_NUM})", re.I)


# (nombre legible, patron, rutas del JSON que esa metrica puede citar)
METRICAS: tuple[tuple[str, re.Pattern[str], tuple[tuple[str, ...], ...]], ...] = (
    (
        "ACWR",
        _patron(r"ACWR|ratio agudo[- :/]*cronico"),
        (
            ("acwr", "ratio"),
            ("acwr", "aguda"),
            ("acwr", "cronica"),
            ("umbrales", "acwr_alto"),
            ("umbrales", "acwr_bajo"),
        ),
    ),
    (
        "Monotony",
        _patron(r"monoton[iy]a?"),
        (("monotony", "monotony"), ("umbrales", "monotony_alta")),
    ),
    ("Strain", _patron(r"strain"), (("monotony", "strain"),)),
    (
        "deriva cardiaca",
        _patron(r"deriva cardiaca|deriva card[ií]aca|decoupling"),
        (
            ("deriva_cardiaca", "promedio_pct"),
            ("deriva_cardiaca", "maximo_pct"),
            ("deriva_cardiaca", "n_sesiones"),
            ("umbrales", "decoupling_alto_pct"),
        ),
    ),
    (
        "cadencia",
        _patron(r"cadencia"),
        (
            ("cadencia", "valor"),
            ("cadencia", "semana_anterior"),
            ("cadencia", "delta"),
            ("cadencia", "n_sesiones"),
        ),
    ),
    (
        "adherencia",
        _patron(r"adherencia"),
        (
            ("adherencia", "pct_global"),
            ("adherencia", "sesiones_cumplidas"),
            ("adherencia", "sesiones_evaluables"),
        ),
    ),
    (
        "carga semanal",
        _patron(r"carga semanal|carga de la semana"),
        (("carga", "semanal"), ("monotony", "carga_semanal")),
    ),
)


def _valor_en(datos: Any, ruta: tuple[str, ...]) -> Any:
    for clave in ruta:
        if not isinstance(datos, dict) or clave not in datos:
            return None
        datos = datos[clave]
    return datos


def _valores_legitimos(datos: dict[str, Any], rutas: tuple[tuple[str, ...], ...]) -> set[str]:
    """Formas aceptables de las cifras de una metrica.

    Se tolera el redondeo a UN decimal y nada mas. `verificar_cifras` acepta
    ademas el redondeo a entero, pero aca eso rompe la comprobacion: `round(0.8)`
    y `round(0.97)` valen los dos 1, asi que el Monotony 0.97 colaba como si
    fuera el umbral `acwr_bajo` 0.8, que es exactamente la confusion que esto
    tiene que detectar. Lo mismo con 1.5 y 1.72, que redondean los dos a 2.
    """
    salida: set[str] = set()
    for ruta in rutas:
        valor = _valor_en(datos, ruta)
        if valor is None or isinstance(valor, bool) or not isinstance(valor, (int, float)):
            continue
        for forma in (float(valor), abs(float(valor))):
            salida.add(_norm(forma))
            salida.add(_norm(round(forma, 1)))
    return salida


def verificar_atribucion(texto: str, datos: dict[str, Any]) -> list[str]:
    """Cifras que el texto atribuye a una metrica y no son de esa metrica.

    Es una senal mucho mas fuerte que la de `verificar_cifras`: aca hay una
    afirmacion concreta ("el ACWR es X") y un valor de referencia concreto con
    el que contrastarla.

    Tambien atrapa el caso de citar una metrica que el motor dejo en `null`: si
    el ACWR no se pudo calcular, cualquier numero pegado a la palabra ACWR esta
    mal, venga de donde venga.
    """
    problemas: list[str] = []
    for nombre, patron, rutas in METRICAS:
        legitimos = _valores_legitimos(datos, rutas)
        for bruto in patron.findall(texto):
            n = _norm(bruto)
            if n in legitimos:
                continue
            try:
                f = float(n)
            except ValueError:
                continue
            if _norm(round(f, 1)) in legitimos:
                continue
            esperado = _valor_en(datos, rutas[0])
            problemas.append(
                f"{nombre}: el texto dice {bruto} y el JSON trae "
                f"{'null' if esperado is None else esperado}"
            )
    return problemas


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

    return anthropic.Anthropic(api_key=api_key, timeout=TIMEOUT_S, max_retries=REINTENTOS)


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

    # La serie de 16 semanas queda fuera del prompt a proposito: cualquier cosa
    # que el modelo dijera sobre ella ("tercera semana consecutiva subiendo")
    # seria una conclusion derivada, no una cifra del motor de calculo, y la
    # verificacion posterior solo sabe comprobar numeros. El grafico se dibuja
    # en codigo, que para eso no necesita narrador.
    datos = {k: v for k, v in datos.items() if k != "volumen_historico"}

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

    mal_atribuidas = verificar_atribucion(texto, datos)
    if mal_atribuidas:
        # Mas grave que lo anterior: no es una cifra que no se pueda ubicar, es
        # una cifra presentada como algo que no es. Tampoco se descarta el texto
        # —el reporte tiene que llegar— pero sube a `avisos_datos` para que el
        # atleta sepa que no se fie de ese numero.
        log.error("la narrativa atribuye mal una cifra: %s", mal_atribuidas)

    return Narrativa(
        texto=texto,
        modelo=getattr(respuesta, "model", modelo),
        tokens_entrada=getattr(uso, "input_tokens", 0) or 0,
        tokens_salida=getattr(uso, "output_tokens", 0) or 0,
        numeros_no_verificados=sospechosos,
        cifras_mal_atribuidas=mal_atribuidas,
    )
