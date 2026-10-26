"""Configuracion central. Todo valor ajustable vive aca, no disperso en el codigo."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv

    # Ruta explicita, no busqueda desde el cwd: systemd arranca el proceso con
    # un directorio de trabajo que no tiene por que ser el del proyecto.
    load_dotenv(ROOT / ".env")
except ImportError:  # el parser (fase 1) no necesita dotenv
    pass


def _path(env: str, default: str) -> Path:
    p = Path(os.getenv(env, default))
    return p if p.is_absolute() else ROOT / p


DATA_DIR = _path("DATA_DIR", "data")
LOG_DIR = _path("LOG_DIR", "logs")
# Fuera de data/ a proposito: no tiene sentido guardar el respaldo dentro de
# lo que se respalda. Apuntarlo a otro medio (pendrive, NAS) es lo unico que
# protege contra la muerte de la SD de la Pi.
BACKUP_DIR = _path("BACKUP_DIR", "respaldos")
# Con la corrida semanal enganchada, 8 son unos dos meses de historia.
RESPALDOS_CONSERVAR = int(os.getenv("BACKUP_KEEP", "8"))

DB_PATH = DATA_DIR / "sport_report.db"
TOKENS_PATH = DATA_DIR / "tokens.json"
PLAN_ACTUAL_PATH = DATA_DIR / "plan_actual.json"
PLANES_DIR = DATA_DIR / "planes"
# Aparte del plan a proposito: la carrera objetivo dura meses y /setplan
# reemplaza el plan cada semana.
CARRERA_PATH = DATA_DIR / "carrera.json"
# Los reportes NO tienen constante: se derivan de DATA_DIR al escribirlos
# (`run_weekly.ruta_reporte` / `ruta_mensual`). Una constante calculada en el
# import se queda con el DATA_DIR de ese momento y se desincroniza.

TZ = ZoneInfo(os.getenv("TZ_LOCAL", "America/Santiago"))


@dataclass(frozen=True)
class Umbrales:
    """Valores de literatura general, NO calibrados a este atleta (spec 10)."""

    acwr_alto: float = 1.5
    acwr_bajo: float = 0.8
    monotony_alta: float = 2.0
    decoupling_alto_pct: float = 5.0
    # Dias minimos de historico real para reportar ACWR como confiable.
    acwr_dias_minimos: int = 28
    # Sesiones minimas con carga registrada en la ventana cronica.
    acwr_sesiones_minimas: int = 8
    # Banda dentro de la cual una sesion se considera cumplida. Es ancha a
    # proposito: cuando no hay vueltas con que separar la recuperacion trotada,
    # una sesion con `estructura=` se compara contra el total y lee por encima
    # de 100% sin que el atleta se haya desviado (ver engine/vueltas.py). Con
    # una banda estrecha esos dias se contaban como incumplimiento.
    adherencia_min_pct: float = 80.0
    adherencia_max_pct: float = 120.0
    # Cambios mes contra mes en dinamica de carrera que ameritan el segundo
    # mensaje del reporte mensual (fatiga y dinamica). Literatura general, no
    # calibrados a este atleta, igual que el resto de estos umbrales.
    cadencia_cambio_spm: float = 3.0
    gct_cambio_ms: float = 8.0
    oscilacion_vertical_cambio_cm: float = 0.5
    ratio_vertical_cambio_pct: float = 0.5


@dataclass(frozen=True)
class Carga:
    """Pesos tipo TRIMP por zona de HR. Ajustables."""

    pesos_zona: tuple[float, ...] = (1.0, 2.0, 3.0, 4.0, 5.0)
    # Usado solo si el atleta no tiene zonas configuradas en Strava.
    # El output debe quedar marcado como impreciso (spec 5).
    peso_fallback: float = 2.5
    # Minimo de puntos de stream para calcular decoupling; bajo esto -> None.
    decoupling_min_puntos: int = 600


@dataclass(frozen=True)
class Estimacion:
    """Como convertir a km una sesion que el plan prescribe en minutos.

    Sin esto el % de volumen semanal mezcla dos bases: el numerador suma los km
    reales de TODAS las sesiones y el denominador solo las prescritas en km, asi
    que una sesion por tiempo infla el porcentaje.

    Prioridad: el ritmo escrito en el plan manda; si no hay, se usa el ritmo por
    defecto pero SOLO para sesiones `easy`. Una sesion por tiempo de otro tipo y
    sin ritmo no se estima: queda fuera del volumen planificado y con aviso.
    """

    ritmo_easy_s_km: int = 420  # 7:00 min/km
    tipos_con_ritmo_por_defecto: tuple[str, ...] = ("easy",)


@dataclass(frozen=True)
class Bienestar:
    """Bienestar diario (solo lo provee intervals.icu).

    Los umbrales son de literatura general, igual que los de `Umbrales`: no
    estan calibrados a este atleta y son valores iniciales ajustables.
    """

    # Dias con dato minimos para hablar de una tendencia semanal. Con menos, la
    # metrica se declara no disponible: un promedio de un dia no es tendencia.
    dias_minimos: int = 3
    # Caida de HRV (ms) respecto a la semana anterior que merece una alerta.
    hrv_caida_ms: float = 5.0
    readiness_bajo: float = 40.0


@dataclass(frozen=True)
class Carrera:
    """Fases del ciclo alrededor de la carrera objetivo, en semanas restantes.

    Ajustables: son convenciones de planificacion, no verdades. Solo se usan
    para etiquetar el contexto que lee la narrativa, nunca para calcular carga.
    """

    semanas_afinamiento: int = 2
    semanas_construccion: int = 8


@dataclass(frozen=True)
class PostCarrera:
    """Informe post-carrera (`run_post_carrera`): el mismo dia a las 21:00.

    Los umbrales de fatiga son convenciones de lectura de parciales, no
    verdades fisiologicas, igual que el resto de esta configuracion.
    """

    # Semanas que cubre el cierre de ciclo, contando la de la carrera. Si hubo
    # otra carrera principal reportada dentro de esa ventana, el ciclo empieza
    # la semana siguiente a ella.
    semanas_ciclo: int = 16
    # Dias despues de la carrera en que se sigue reintentando (uno por dia) si
    # la actividad todavia no se sincronizo. El ultimo se envia sin ella.
    dias_ventana: int = 3
    # Segunda mitad mas lenta que la primera en al menos este %: senal de fatiga.
    split_positivo_pct: float = 3.0
    # Diferencia bajo la cual el split se lee como parejo.
    split_parejo_pct: float = 1.0
    # Ultimo cuarto mas lento que el promedio de los tres primeros en este %.
    caida_final_pct: float = 3.0
    # Fraccion minima de la distancia que tienen que cubrir las vueltas para
    # leer parciales; con menos, las mitades no representan la carrera.
    cobertura_vueltas_min: float = 0.8
    # Carreras anteriores con distancia dentro de este % se comparan.
    distancia_similar_pct: float = 10.0


@dataclass(frozen=True)
class Estimador:
    """Estimador de tiempo de carrera (`engine/estimador.py`).

    Convenciones de la literatura, no calibradas a este atleta.
    """

    # Riegel: T2 = T1 * (D2/D1)^k. 1.06 es el valor clasico; el rango cubre a
    # un amateur con poco volumen (k alto) y a uno muy entrenado (k bajo).
    exponente: float = 1.06
    exponente_min: float = 1.04
    exponente_max: float = 1.10
    # No se extrapola mas alla de este factor de distancia (ni hacia arriba ni
    # hacia abajo): de un 5K a una maraton el error es mayor que el dato.
    factor_max: float = 4.0
    # Antiguedad maxima de una carrera de referencia, y la que da confianza alta.
    semanas_carrera: int = 16
    semanas_carrera_alta: int = 8
    # Ventana de entrenamientos para los mejores segmentos.
    semanas_entrenamiento: int = 12
    segmentos_km: tuple[float, ...] = (3.0, 5.0, 10.0, 15.0, 21.0975)
    # Tanda (2011, J Hum Sport Exerc 6(3)), solo maraton. Ventana de 8 semanas
    # que en el paper termina 7 dias antes de la carrera. Error estandar de la
    # estimacion: 5.77 s/km (~4 min en maraton); el rango usa 2 errores (~95%).
    tanda_semanas: int = 8
    tanda_see_s_km: float = 5.77
    tanda_rango_see: float = 2.0
    # Rango de la muestra con que se ajusto (22 corredores, 46 maratones). Fuera
    # de el la formula extrapola y la confianza baja.
    tanda_km_min: float = 40.4
    tanda_km_max: float = 110.7
    tanda_ritmo_min_s_km: float = 253.3
    tanda_ritmo_max_s_km: float = 330.6
    tanda_distancia_min_km: float = 40.0


UMBRALES = Umbrales()
CARGA = Carga()
ESTIMACION = Estimacion()
BIENESTAR = Bienestar()
CARRERA = Carrera()
POST_CARRERA = PostCarrera()
ESTIMADOR = Estimador()

# Semanas que muestra el grafico de volumen del reporte.
SEMANAS_GRAFICO = 16

# --- intervals.icu: la fuente principal (fase 2) ---
# Clave personal (Settings -> Developer Settings). No expira ni rota: se copia
# al .env por scp y no se teclea en la Pi.
INTERVALS_API_KEY = os.getenv("INTERVALS_API_KEY", "")
# '0' es la convencion de "el atleta autenticado". Si no funciona,
# `python -m sport_report.intervals.verificar` imprime el id real.
INTERVALS_ATHLETE_ID = os.getenv("INTERVALS_ATHLETE_ID", "0")
INTERVALS_BASE = os.getenv("INTERVALS_BASE", "https://intervals.icu/api/v1")

INTERVALS = "intervals"
GARMIN = "garmin"
STRAVA = "strava"
# Cual fuente se intenta primero. Con 'intervals', Garmin queda como respaldo y
# solo se usa si intervals.icu falla. Con 'garmin' o 'strava' forzados NO hay
# respaldo (no hay respaldo del respaldo). Strava ya no entra a la cadena: solo
# se usa si se fuerza.
FUENTE_PRINCIPAL = os.getenv("FUENTE_PRINCIPAL", INTERVALS)

# --- Garmin Connect Personal API: el respaldo de intervals.icu ---
# Clave personal: Garmin Connect -> Ajustes -> Personal API. A diferencia de
# intervals.icu, VENCE (30 dias a un ano) y se renueva a mano.
# `python -m sport_report.garmin.verificar` comprueba que sirve.
GARMIN_API_KEY = os.getenv("GARMIN_API_KEY", "")
GARMIN_BASE = os.getenv("GARMIN_BASE", "https://personal.connectapi.garmin.com")
# La API no informa cuando vence la clave: se anota a mano (AAAA-MM-DD) al
# crearla. Sin esto el respaldo puede morir en silencio justo cuando hace falta.
GARMIN_API_KEY_VENCE = os.getenv("GARMIN_API_KEY_VENCE", "")
# Con cuantos dias de anticipacion avisan `diagnostico` y el reporte semanal.
GARMIN_AVISO_DIAS_DIAGNOSTICO = 14
GARMIN_AVISO_DIAS_REPORTE = 7

# --- TrainingPeaks: de donde viene el plan, importado a pedido con /tp ---
# Cookie de sesion `Production_tpAuth` copiada del navegador. No hay API
# oficial para particulares: VENCE sin aviso y se renueva a mano.
# `python -m sport_report.trainingpeaks.verificar` comprueba que sirve.
TP_AUTH_COOKIE = os.getenv("TP_AUTH_COOKIE", "")
TP_BASE = os.getenv("TP_BASE", "https://tpapi.trainingpeaks.com")

# --- Strava: ya no es respaldo; solo se usa si se fuerza FUENTE_PRINCIPAL ---
STRAVA_CLIENT_ID = os.getenv("STRAVA_CLIENT_ID", "")
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET", "")
# Solo bootstrap de la primera corrida: despues manda data/tokens.json.
STRAVA_REFRESH_TOKEN = os.getenv("STRAVA_REFRESH_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5")
# El mensual usa un modelo mas capaz: es una llamada al mes, el costo a esa
# frecuencia es irrelevante, y la ventaja esta en sostener un texto sobre
# cuatro semanas de datos. No en calcular: eso lo sigue haciendo Python.
ANTHROPIC_MODEL_MENSUAL = os.getenv("ANTHROPIC_MODEL_MENSUAL", "claude-sonnet-5")

TIPOS_RUN = ("Run", "TrailRun", "VirtualRun")
TIPOS_FUERZA = ("WeightTraining", "Workout", "Crossfit")
# Distancia minima para que una corrida entre a los promedios de tecnica
# (cadencia, GCT, oscilacion, ratio). Un test de esfuerzo en cinta queda
# registrado como corrida de 39 m: su carga cuenta, su cadencia no es de carrera.
TECNICA_MIN_KM = 0.5
# Solo para el resumen por deporte de una semana sin plan: el ciclismo no entra
# a ningun volumen ni carga de running.
TIPOS_CICLISMO = (
    "Ride", "VirtualRide", "GravelRide", "MountainBikeRide", "EBikeRide",
    "EMountainBikeRide", "Velomobile",
)
# Variantes bajo techo, que el resumen detalla aparte. Una cinta o un rodillo
# que la fuente registre como `Run`/`Ride` normales no se distingue.
TIPOS_INDOOR = ("VirtualRun", "VirtualRide")

# Origenes de zonas de HR que se consideran datos del atleta. Cualquier otro
# (hoy solo 'fallback') implica carga ponderada con peso plano, que el reporte
# debe marcar como imprecisa.
ORIGENES_ZONAS_CONFIABLES = ("intervals", "garmin", "strava")
