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
    # proposito: una sesion con `estructura=` se compara contra la distancia
    # dura, que no incluye la recuperacion trotada entre repeticiones, asi que
    # lee por encima de 100% por construccion (ver adherencia.py). Con una
    # banda estrecha toda sesion de series se contaba como incumplida.
    adherencia_min_pct: float = 80.0
    adherencia_max_pct: float = 120.0


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


UMBRALES = Umbrales()
CARGA = Carga()
ESTIMACION = Estimacion()

# Semanas que muestra el grafico de volumen del reporte.
SEMANAS_GRAFICO = 16

STRAVA_CLIENT_ID = os.getenv("STRAVA_CLIENT_ID", "")
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET", "")
# Solo bootstrap de la primera corrida: despues manda data/tokens.json.
STRAVA_REFRESH_TOKEN = os.getenv("STRAVA_REFRESH_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5")

TIPOS_RUN = ("Run", "TrailRun", "VirtualRun")
TIPOS_FUERZA = ("WeightTraining", "Workout", "Crossfit")
