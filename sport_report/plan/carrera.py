"""La carrera objetivo: una fecha que persiste entre semanas.

Vive en su propio archivo (`data/carrera.json`) y NO dentro del plan: su vida
util son meses, y `/setplan` reemplaza el plan cada semana — guardarla ahi la
borraria cada lunes.

Ojo con la palabra: aca "carrera" es la competencia objetivo. La actividad de
correr se dice "corrida" en todo el resto del codigo (`es_run`,
`PlanSemanal.corridas`), para que las dos acepciones no se mezclen.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .. import config
from ..config import CARRERA, Carrera as CfgCarrera
from ..fechas import ahora_local, hoy_local, semana_de
from ..storage import escribir_json, leer_json, lock

log = logging.getLogger(__name__)

VERSION_FORMATO = 1

# Fases del ciclo, ordenadas de lejos a cerca de la carrera.
BASE = "base"
CONSTRUCCION = "construccion"
AFINAMIENTO = "afinamiento"
SEMANA_DE_CARRERA = "semana_de_carrera"
PASADA = "pasada"


class CarreraInvalida(ValueError):
    pass


@dataclass(frozen=True)
class Carrera:
    fecha: date
    nombre: str = ""
    creado_en: str = ""

    def semanas_restantes(self, hoy: date | None = None) -> int:
        """Semanas completas entre la semana de `hoy` y la de la carrera.

        Se cuenta por semanas (lunes a lunes) y no por dias sueltos: el plan es
        semanal y "faltan 3 semanas" es lo que significa algo para entrenar.
        Negativo si la carrera ya paso.
        """
        hoy = hoy or hoy_local()
        lunes_hoy = semana_de(hoy).inicio
        lunes_carrera = semana_de(self.fecha).inicio
        return (lunes_carrera - lunes_hoy).days // 7

    def fase(self, hoy: date | None = None, cfg: CfgCarrera = CARRERA) -> str:
        restantes = self.semanas_restantes(hoy)
        if restantes < 0:
            return PASADA
        if restantes == 0:
            return SEMANA_DE_CARRERA
        if restantes <= cfg.semanas_afinamiento:
            return AFINAMIENTO
        if restantes <= cfg.semanas_construccion:
            return CONSTRUCCION
        return BASE

    def contexto(self, hoy: date | None = None, cfg: CfgCarrera = CARRERA) -> dict[str, Any]:
        """Lo que consume la narrativa: cifras ya calculadas y una nota."""
        restantes = self.semanas_restantes(hoy)
        fase = self.fase(hoy, cfg)
        notas = {
            PASADA: f"la carrera fue hace {abs(restantes)} semana(s)",
            SEMANA_DE_CARRERA: "es la semana de la carrera",
            AFINAMIENTO: (
                f"a {restantes} semana(s): fase de afinamiento, que empieza a "
                f"{cfg.semanas_afinamiento} semanas de la carrera"
            ),
            CONSTRUCCION: f"a {restantes} semana(s): fase de construccion",
            BASE: (
                f"a {restantes} semana(s): fase de base, la construccion empieza a "
                f"{cfg.semanas_construccion} semanas"
            ),
        }
        return {
            "fecha": self.fecha.isoformat(),
            "nombre": self.nombre,
            "semanas_restantes": restantes,
            "fase": fase,
            "nota": notas[fase],
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "version": VERSION_FORMATO,
            "fecha": self.fecha.isoformat(),
            "nombre": self.nombre,
            "creado_en": self.creado_en,
        }

    @staticmethod
    def from_json(d: dict[str, Any]) -> "Carrera":
        version = d.get("version", 1)
        if not isinstance(version, int) or version > VERSION_FORMATO:
            raise CarreraInvalida(
                f"carrera escrita con el formato v{version}; esta version entiende "
                f"hasta la v{VERSION_FORMATO}"
            )
        try:
            fecha = date.fromisoformat(d["fecha"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CarreraInvalida(f"la carrera guardada no trae una fecha valida: {exc}") from exc
        return Carrera(
            fecha=fecha,
            nombre=str(d.get("nombre", "")),
            creado_en=str(d.get("creado_en", "")),
        )


#: Tope de antelacion. Una fecha a mas de esto casi siempre es un error de
#: tecleo (2036 por 2026), y aceptarla dejaria el contexto de la narrativa
#: hablando de 500 semanas restantes.
ANOS_MAXIMOS = 2


def parse_fecha(texto: str, hoy: date | None = None) -> date:
    """Valida la fecha de la carrera con un mensaje accionable."""
    hoy = hoy or hoy_local()
    try:
        fecha = date.fromisoformat(texto.strip())
    except ValueError:
        raise CarreraInvalida(
            f"'{texto.strip()}' no es una fecha. Se escribe asi: "
            "`/carrera 2026-11-15 Maraton de Santiago`"
        ) from None
    if fecha < hoy:
        raise CarreraInvalida(
            f"{fecha} ya paso (hoy es {hoy}). Si quieres borrar la carrera "
            "guardada, usa `/carrera borrar`"
        )
    if (fecha - hoy).days > ANOS_MAXIMOS * 366:
        raise CarreraInvalida(
            f"{fecha} esta a mas de {ANOS_MAXIMOS} anos: revisa el ano por si "
            "se colo un tecleo"
        )
    return fecha


class CarreraStore:
    """Persistencia de la carrera objetivo. Mismo lock y escritura atomica
    que `PlanStore`: el bot y el cron comparten el archivo.
    """

    def __init__(self, ruta: Path | None = None):
        self.ruta = ruta or config.CARRERA_PATH

    def cargar(self) -> Carrera | None:
        """La carrera guardada, o None. Un archivo ilegible se trata como None.

        Igual que con el plan: el reporte del lunes tiene que llegar aunque el
        archivo este roto. El motivo queda en el log.
        """
        try:
            d = leer_json(self.ruta)
        except (OSError, ValueError) as exc:
            log.error("no se pudo leer %s: %s", self.ruta, exc)
            return None
        if not d:
            return None
        try:
            return Carrera.from_json(d)
        except CarreraInvalida as exc:
            log.error("%s esta corrupto y se ignora: %s", self.ruta, exc)
            return None

    def guardar(self, carrera: Carrera) -> Carrera:
        with lock(self.ruta):
            nueva = Carrera(
                fecha=carrera.fecha,
                nombre=carrera.nombre,
                creado_en=carrera.creado_en
                or ahora_local().isoformat(timespec="seconds"),
            )
            escribir_json(self.ruta, nueva.to_json(), con_lock=False)
        return nueva

    def borrar(self) -> bool:
        """True si habia algo que borrar."""
        with lock(self.ruta):
            if not self.ruta.exists():
                return False
            self.ruta.unlink()
        return True
