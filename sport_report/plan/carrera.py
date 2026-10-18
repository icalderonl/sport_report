"""Las carreras objetivo: fechas que persisten entre semanas.

Viven en su propio archivo (`data/carrera.json`) y NO dentro del plan: su vida
util son meses, y `/setplan` reemplaza el plan cada semana — guardarlas ahi las
borraria cada lunes.

Puede haber varias, pero solo UNA es la principal: es la que define la fase del
ciclo y la cuenta regresiva del plan (C-N). Las demas quedan anotadas y no
alteran ninguna cifra. Si se borra la principal no se promueve otra sola: la
fase del ciclo cambiaria sin que el atleta lo haya decidido.

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

# v2: una lista de carreras y la fecha de la principal. Un archivo v1 (una sola
# carrera) se lee como una lista de una, que queda como principal.
VERSION_FORMATO = 2

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
    """Persistencia de las carreras objetivo. Mismo lock y escritura atomica
    que `PlanStore`: el bot y el cron comparten el archivo.

    Una carrera se identifica por su fecha: dos carreras el mismo dia no son
    un caso real, y declarar otra vez una fecha actualiza su nombre.
    """

    def __init__(self, ruta: Path | None = None):
        self.ruta = ruta or config.CARRERA_PATH

    # -- lectura ---------------------------------------------------------

    def _leer(self) -> tuple[list[Carrera], date | None]:
        """(carreras ordenadas por fecha, fecha de la principal o None).

        Un archivo ilegible se trata como vacio, igual que con el plan: el
        reporte del lunes tiene que llegar aunque el archivo este roto. El
        motivo queda en el log.
        """
        try:
            d = leer_json(self.ruta)
        except (OSError, ValueError) as exc:
            log.error("no se pudo leer %s: %s", self.ruta, exc)
            return [], None
        if not d:
            return [], None
        try:
            return _desde_json(d)
        except CarreraInvalida as exc:
            log.error("%s esta corrupto y se ignora: %s", self.ruta, exc)
            return [], None

    def listar(self) -> list[Carrera]:
        return self._leer()[0]

    def principal(self) -> Carrera | None:
        carreras, fecha = self._leer()
        return next((c for c in carreras if c.fecha == fecha), None)

    def cargar(self) -> Carrera | None:
        """La carrera principal, o None. Es la que consumen el reporte y el plan."""
        return self.principal()

    # -- escritura -------------------------------------------------------

    def _escribir(self, carreras: list[Carrera], principal: date | None) -> None:
        if not carreras:
            if self.ruta.exists():
                self.ruta.unlink()
            return
        escribir_json(self.ruta, _a_json(carreras, principal), con_lock=False)

    def agregar(self, carrera: Carrera) -> tuple[Carrera, bool]:
        """Agrega (o actualiza, si la fecha ya existe) una carrera.

        Devuelve (la carrera guardada, si es la principal). La primera que se
        declara queda como principal; las siguientes no le quitan el puesto.
        """
        with lock(self.ruta):
            carreras, principal = self._leer()
            previa = next((c for c in carreras if c.fecha == carrera.fecha), None)
            nueva = Carrera(
                fecha=carrera.fecha,
                nombre=carrera.nombre,
                creado_en=carrera.creado_en
                or (previa.creado_en if previa else "")
                or ahora_local().isoformat(timespec="seconds"),
            )
            carreras = sorted(
                [c for c in carreras if c.fecha != carrera.fecha] + [nueva],
                key=lambda c: c.fecha,
            )
            if principal is None and len(carreras) == 1:
                principal = nueva.fecha
            self._escribir(carreras, principal)
        return nueva, principal == nueva.fecha

    def guardar(self, carrera: Carrera) -> Carrera:
        """Compatibilidad: igual que `agregar`, sin decir si es la principal."""
        return self.agregar(carrera)[0]

    def marcar_principal(self, fecha: date) -> Carrera:
        with lock(self.ruta):
            carreras, _ = self._leer()
            elegida = next((c for c in carreras if c.fecha == fecha), None)
            if elegida is None:
                raise CarreraInvalida(f"no hay ninguna carrera el {fecha}")
            self._escribir(carreras, fecha)
        return elegida

    def borrar(self, fecha: date | None = None) -> tuple[Carrera | None, bool]:
        """Borra la carrera de esa fecha, o todas si `fecha` es None.

        Devuelve (la borrada o None, si era la principal). Con `fecha=None`
        la primera parte es la principal que habia, si habia. La principal
        borrada NO se reemplaza: la elige el atleta.
        """
        with lock(self.ruta):
            carreras, principal = self._leer()
            if fecha is None:
                if not carreras and not self.ruta.exists():
                    return None, False
                previa = next((c for c in carreras if c.fecha == principal), None)
                self._escribir([], None)
                return previa or (carreras[0] if carreras else None), previa is not None
            borrada = next((c for c in carreras if c.fecha == fecha), None)
            if borrada is None:
                return None, False
            restantes = [c for c in carreras if c.fecha != fecha]
            era_principal = principal == fecha
            self._escribir(restantes, None if era_principal else principal)
        return borrada, era_principal


def _a_json(carreras: list[Carrera], principal: date | None) -> dict[str, Any]:
    return {
        "version": VERSION_FORMATO,
        "carreras": [
            {k: v for k, v in c.to_json().items() if k != "version"} for c in carreras
        ],
        "principal": principal.isoformat() if principal else None,
    }


def _desde_json(d: dict[str, Any]) -> tuple[list[Carrera], date | None]:
    version = d.get("version", 1)
    if not isinstance(version, int) or version > VERSION_FORMATO:
        raise CarreraInvalida(
            f"carrera escrita con el formato v{version}; esta version entiende "
            f"hasta la v{VERSION_FORMATO}"
        )
    if version < 2:
        # v1: una sola carrera, que era por definicion la objetivo.
        unica = Carrera.from_json(d)
        return [unica], unica.fecha
    crudas = d.get("carreras")
    if not isinstance(crudas, list):
        raise CarreraInvalida("el archivo v2 no trae la lista `carreras`")
    carreras = sorted(
        (Carrera.from_json({**c, "version": 1}) for c in crudas), key=lambda c: c.fecha
    )
    principal = None
    if d.get("principal"):
        try:
            principal = date.fromisoformat(d["principal"])
        except (TypeError, ValueError) as exc:
            raise CarreraInvalida(f"fecha de la principal invalida: {exc}") from exc
        if all(c.fecha != principal for c in carreras):
            # Apunta a una carrera que no esta: se trata como sin principal en
            # vez de inventar una.
            log.error("la principal %s no esta en la lista de carreras", principal)
            principal = None
    return list(carreras), principal
