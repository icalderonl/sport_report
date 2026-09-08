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

Que cubrir en el resumen:
- volumen real contra planificado, con contexto y no solo el numero;
- que implican el ACWR y el Monotony/Strain de esta semana, no solo repetirlos;
- deriva cardiaca si hubo sesiones largas relevantes;
- tendencia de la dinamica de carrera (cadencia, GCT, oscilacion y ratio
  vertical) contra la semana anterior;
- fatiga y descanso si hay datos de bienestar;
- las alertas, si "alertas" trae alguna.

Como leer el JSON:
- Un bloque con "confiable": false NO es un hecho. Si lo mencionas, di
  explicitamente que no es confiable y por que (esta en "motivo").
- "avisos_datos" describe huecos de datos. Si hay alguno relevante, mencionalo.
- Una adherencia con "pct": null es un DATO FALTANTE, no un incumplimiento. Nunca
  la presentes como sesion no hecha.
- Los valores de "umbrales" son de literatura general, no estan calibrados a este
  atleta. No los presentes como verdad medica.
- Un bloque con "disponible": false NO tiene dato. Su "motivo" dice por que. No
  lo presentes como un cero ni te lo saltes en silencio si venia al caso.
- Si "fuente"."fallback" es true, la semana se ingirio desde la fuente de
  respaldo y esta INCOMPLETA: dilo explicitamente y nombra lo que falta, que
  esta en "fuente"."no_disponibles". Nunca la presentes como una semana normal.
- NUNCA combines el bienestar (HRV, HR de reposo, sueno, sleep score, Body
  Battery, readiness) y la carga (ACWR, Monotony, Strain) en un solo indicador,
  puntaje o juicio unico. Reportalos por separado; si dices algo de la relacion
  entre ambos, di los dos numeros. El JSON no trae ningun indice combinado
  porque no existe uno con respaldo metodologico.

Formato de salida:
- 5 a 8 lineas de resumen ejecutivo, en espanol, tono directo y concreto. Mas
  detallado que un resumen de tres lineas, sin volverse un listado de cifras.
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

# Cuantos caracteres se toleran entre el nombre de la metrica y su numero.
_VENTANA = 24
# El hueco entre ambos no puede tener digitos (asi el numero que cuenta es el
# pegado al nombre y no uno mas lejano) ni saltos de linea, ni `.` o `;`, que
# cierran la frase. Sin esto ultimo, "...de adherencia. La carga semanal fue
# 558.2" hacia que 558.2 —correctamente citada como carga— se leyera como la
# adherencia. Se dejan pasar `,` y `:`, que van dentro de la frase.
_RE_HUECO_MALO = re.compile(r"[\d\n.;]")


# Mirando hacia adelante (el numero primero, el nombre despues) se prohiben
# ademas el parentesis de cierre y la coma. Motivo, con dos frases reales del
# modelo en la primera prueba contra la API:
#
#   "el ground contact time mejoro con una reduccion de 8.0 ms (232.0 ms),
#    pero la oscilacion vertical crecio 0.3 cm (8.4 cm) y el ratio vertical..."
#   "el HRV bajo 8.0 ms hasta 62.0 ms, la frecuencia cardiaca de reposo
#    subio 2.0 lpm (50.0 lpm)"
#
# El patron es el mismo en las dos: el numero pertenece a una metrica cuyo
# nombre queda descartado por el digito que hay en medio (el delta), y el
# siguiente nombre esta a pocos caracteres. Sin esta regla el numero se le
# colgaba a la metrica SIGUIENTE y se reportaba una confusion que no existia; y
# como el aviso sube al reporte del atleta, el sistema le decia todas las
# semanas que no se fiara de cifras correctas.
#
# Un parentesis que cierra, y una coma, pertenecen a lo que vino ANTES. Hacia
# atras la coma si se deja pasar ("la carga semanal, que fue 558.2"): ahi el
# nombre ya esta dicho y la coma no lo separa de su cifra.
_RE_HUECO_MALO_ADELANTE = re.compile(r"[\d\n.;),]")


def _hueco_valido(hueco: str, hacia_adelante: bool = False) -> bool:
    if len(hueco) > _VENTANA:
        return False
    malo = _RE_HUECO_MALO_ADELANTE if hacia_adelante else _RE_HUECO_MALO
    return not malo.search(hueco)


# (nombre legible, como aparece en el texto, rutas del JSON que puede citar)
METRICAS: tuple[tuple[str, re.Pattern[str], tuple[tuple[str, ...], ...]], ...] = (
    (
        "ACWR",
        re.compile(r"ACWR|ratio agudo[- :/]*cronico", re.I),
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
        # Con acento: el modelo escribe "monotonia" en espanol correcto y
        # `monoton[iy]a?` no lo alcanzaba, asi que la metrica quedaba sin cubrir.
        re.compile(r"monoton[iíy]a?", re.I),
        (("monotony", "monotony"), ("umbrales", "monotony_alta")),
    ),
    ("Strain", re.compile(r"strain", re.I), (("monotony", "strain"),)),
    (
        "deriva cardiaca",
        re.compile(r"deriva card[ií]aca|decoupling", re.I),
        (
            ("deriva_cardiaca", "promedio_pct"),
            ("deriva_cardiaca", "maximo_pct"),
            ("deriva_cardiaca", "n_sesiones"),
            ("umbrales", "decoupling_alto_pct"),
        ),
    ),
    (
        "cadencia",
        re.compile(r"cadencia", re.I),
        (
            ("cadencia", "valor"),
            ("cadencia", "semana_anterior"),
            ("cadencia", "delta"),
            ("cadencia", "n_sesiones"),
        ),
    ),
    (
        "GCT",
        # "ground contact time" incluido: es como lo escribio el modelo en la
        # primera prueba real contra la API.
        re.compile(r"\bGCT\b|tiempo de contacto|ground contact( time)?", re.I),
        (
            ("gct", "valor"),
            ("gct", "semana_anterior"),
            ("gct", "delta"),
            ("gct", "n_sesiones"),
        ),
    ),
    (
        "oscilacion vertical",
        re.compile(r"oscilaci[oó]n vertical", re.I),
        (
            ("oscilacion_vertical", "valor"),
            ("oscilacion_vertical", "semana_anterior"),
            ("oscilacion_vertical", "delta"),
            ("oscilacion_vertical", "n_sesiones"),
        ),
    ),
    (
        "ratio vertical",
        re.compile(r"ratio vertical", re.I),
        (
            ("ratio_vertical", "valor"),
            ("ratio_vertical", "semana_anterior"),
            ("ratio_vertical", "delta"),
            ("ratio_vertical", "n_sesiones"),
        ),
    ),
    (
        "HRV",
        re.compile(r"\bHRV\b|variabilidad card[ií]aca", re.I),
        (
            ("fatiga_descanso", "hrv", "valor"),
            ("fatiga_descanso", "hrv", "semana_anterior"),
            ("fatiga_descanso", "hrv", "delta"),
            ("fatiga_descanso", "hrv", "n_dias"),
            ("umbrales", "hrv_caida_ms"),
        ),
    ),
    (
        "HR de reposo",
        re.compile(r"(HR|pulso|frecuencia card[ií]aca) (en |de )?reposo", re.I),
        (
            ("fatiga_descanso", "hr_reposo", "valor"),
            ("fatiga_descanso", "hr_reposo", "semana_anterior"),
            ("fatiga_descanso", "hr_reposo", "delta"),
            ("fatiga_descanso", "hr_reposo", "n_dias"),
        ),
    ),
    (
        "sueno",
        re.compile(r"sue[nñ]o|\bdorm", re.I),
        (
            ("fatiga_descanso", "sueno_h", "valor"),
            ("fatiga_descanso", "sueno_h", "semana_anterior"),
            ("fatiga_descanso", "sueno_h", "delta"),
            ("fatiga_descanso", "sueno_h", "n_dias"),
            ("fatiga_descanso", "sueno_score", "valor"),
            ("fatiga_descanso", "sueno_score", "semana_anterior"),
            ("fatiga_descanso", "sueno_score", "delta"),
        ),
    ),
    (
        "readiness",
        re.compile(r"readiness|disponibilidad", re.I),
        (
            ("fatiga_descanso", "readiness", "valor"),
            ("fatiga_descanso", "readiness", "semana_anterior"),
            ("fatiga_descanso", "readiness", "delta"),
            ("fatiga_descanso", "readiness", "n_dias"),
            ("umbrales", "readiness_bajo"),
        ),
    ),
    (
        "Body Battery",
        re.compile(r"body battery", re.I),
        (
            ("fatiga_descanso", "body_battery", "valor"),
            ("fatiga_descanso", "body_battery", "semana_anterior"),
            ("fatiga_descanso", "body_battery", "delta"),
            ("fatiga_descanso", "body_battery", "n_dias"),
        ),
    ),
    (
        "adherencia",
        re.compile(r"adherencia", re.I),
        (
            ("adherencia", "pct_global"),
            ("adherencia", "sesiones_cumplidas"),
            ("adherencia", "sesiones_evaluables"),
        ),
    ),
    (
        "carga semanal",
        re.compile(r"carga semanal|carga de la semana", re.I),
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


def verificar_atribucion(
    texto: str,
    datos: dict[str, Any],
    metricas: tuple[tuple[str, "re.Pattern[str]", tuple[tuple[str, ...], ...]], ...] | None = None,
) -> list[str]:
    """Cifras que el texto atribuye a una metrica y no son de esa metrica.

    Es una senal mucho mas fuerte que la de `verificar_cifras`: aca hay una
    afirmacion concreta ("el ACWR es X") y un valor de referencia concreto con
    el que contrastarla.

    Tambien atrapa el caso de citar una metrica que el motor dejo en `null`: si
    el ACWR no se pudo calcular, cualquier numero pegado a la palabra ACWR esta
    mal, venga de donde venga.

    `metricas` permite pasar otra tabla de rutas para el mismo mecanismo: el
    reporte mensual tiene los mismos nombres de metrica en otras rutas del JSON
    (ver narrative/mensual.py). Por defecto, la tabla del reporte semanal.
    """
    metricas = metricas if metricas is not None else METRICAS
    # Posicion de cada nombre de metrica que aparezca en el texto.
    apariciones: list[tuple[int, int, int]] = []  # (inicio, fin, indice de metrica)
    for i, (_, patron, _) in enumerate(metricas):
        apariciones.extend((m.start(), m.end(), i) for m in patron.finditer(texto))

    problemas: list[str] = []
    vistos: set[tuple[int, str]] = set()
    for num in _RE_NUMERO.finditer(texto):
        duenno = _metrica_mas_cercana(texto, num, apariciones)
        if duenno is None:
            continue

        nombre, _, rutas = metricas[duenno]
        legitimos = _valores_legitimos(datos, rutas)
        n = _norm(num.group())
        if n in legitimos or _norm(round(float(n), 1)) in legitimos:
            continue
        if (duenno, n) in vistos:  # la misma confusion repetida no se reporta dos veces
            continue
        vistos.add((duenno, n))
        esperado = _valor_en(datos, rutas[0])
        problemas.append(
            f"{nombre}: el texto dice {num.group()} y el JSON trae "
            f"{'null' if esperado is None else esperado}"
        )
    return problemas


def _metrica_mas_cercana(
    texto: str, num: re.Match[str], apariciones: list[tuple[int, int, int]]
) -> int | None:
    """Indice de la metrica a la que pertenece un numero, o None si no hay ninguna.

    Se elige la MAS CERCANA, mire hacia donde mire. Sin esto, en "Monotony 0.97
    y Strain 768.2" el 0.97 se le colgaba tambien a Strain, que lo tiene a tres
    caracteres por la izquierda, y se reportaba una confusion que no existe: el
    numero es del nombre que tiene pegado, no de cualquiera que ande cerca.
    """
    mejor: tuple[int, int] | None = None  # (distancia, indice)
    for inicio, fin, indice in apariciones:
        adelante = False
        if fin <= num.start():  # el nombre va delante del numero
            hueco = texto[fin : num.start()]
        elif num.end() <= inicio:  # el numero va delante del nombre
            hueco = texto[num.end() : inicio]
            adelante = True
        else:  # se solapan (un numero dentro del nombre): no aplica
            continue
        if not _hueco_valido(hueco, hacia_adelante=adelante):
            continue
        if mejor is None or len(hueco) < mejor[0]:
            mejor = (len(hueco), indice)
    return None if mejor is None else mejor[1]


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
