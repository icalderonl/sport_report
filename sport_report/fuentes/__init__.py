"""Seleccion de fuente con respaldo: intervals.icu primero, Strava despues.

La regla completa, que es corta a proposito:

- La fuente principal sale de `config.FUENTE_PRINCIPAL` (por defecto
  intervals.icu) y es la que se intenta siempre.
- Si intervals.icu falla —caida, 401, timeout, 5xx persistente— se intenta
  Strava **una vez**. Es una semana degradada: sin GCT, sin oscilacion ni ratio
  vertical, sin bienestar. El reporte lo dice; no se presenta como completa.
- **No hay respaldo del respaldo.** Con `FUENTE_PRINCIPAL=strava` forzado,
  intervals.icu no respalda a Strava. Si la fuente que toca falla y no queda
  respaldo, no se lanza: el reporte sale con lo que ya hay en la base.
- Que fuente sirvio la semana se persiste en `fuentes_semana`, para que el
  reporte lo sepa incluso cuando la corrida va con --sin-ingesta.

La orquestacion comun de la ingesta vive en `comun.py`; cada fuente concreta es
una subclase de `IngestaBase` en su propio paquete.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from .. import config
from ..db.repo import Repo
from ..plan.store import PlanStore
from .comun import IngestaBase, ResumenIngesta

log = logging.getLogger(__name__)

__all__ = [
    "IngestaBase",
    "ResumenIngesta",
    "ResultadoFuente",
    "abrir",
    "cadena",
    "sincronizar",
]

#: Lo que cada fuente NO puede dar. Lo consume el reporte para listar en el
#: JSON exactamente lo que falta esa semana, sin que el texto y los datos
#: puedan discrepar.
NO_DISPONIBLE: dict[str, tuple[str, ...]] = {
    config.INTERVALS: (),
    config.STRAVA: (
        "gct",
        "oscilacion_vertical",
        "ratio_vertical",
        "fatiga_descanso",
    ),
}


@dataclass
class ResultadoFuente:
    """Que paso al intentar sincronizar. Nunca lanza por un fallo de fuente."""

    fuente: str = ""
    resumen: ResumenIngesta | None = None
    fallback: bool = False
    #: (fuente, motivo) de cada intento fallido, en orden.
    intentos: tuple[tuple[str, str], ...] = ()
    ingestas: list[IngestaBase] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.resumen is not None

    @property
    def motivo(self) -> str:
        """Por que se llego a esta fuente, o por que no hubo ninguna."""
        return "; ".join(f"{f}: {m}" for f, m in self.intentos)

    @property
    def no_disponibles(self) -> tuple[str, ...]:
        return NO_DISPONIBLE.get(self.fuente, ())

    def cerrar(self) -> None:
        for ing in self.ingestas:
            cliente = getattr(ing, "cliente", None)
            cerrar = getattr(cliente, "cerrar", None)
            if cerrar is not None:
                try:
                    cerrar()
                except Exception as exc:  # cerrar no puede tumbar la corrida
                    log.warning("no se pudo cerrar el cliente de %s: %s", ing.NOMBRE, exc)


def abrir(
    nombre: str, repo: Repo | None = None, plan_store: PlanStore | None = None
) -> IngestaBase:
    """Construye la ingesta de una fuente por nombre.

    El import va adentro para que el paquete no arrastre las dos fuentes (ni
    sus dependencias) cuando solo se usa una.
    """
    if nombre == config.INTERVALS:
        from ..intervals.ingest import Ingesta as IngestaIntervals

        return IngestaIntervals(repo=repo, plan_store=plan_store)
    if nombre == config.STRAVA:
        from ..strava.ingest import Ingesta as IngestaStrava

        return IngestaStrava(repo=repo, plan_store=plan_store)
    raise ValueError(
        f"fuente desconocida: {nombre!r}. Valores validos: "
        f"{config.INTERVALS!r}, {config.STRAVA!r}"
    )


def cadena(principal: str | None = None) -> tuple[str, ...]:
    """Orden de intento. Sin respaldo del respaldo."""
    principal = principal or config.FUENTE_PRINCIPAL
    if principal == config.INTERVALS:
        return (config.INTERVALS, config.STRAVA)
    # Strava forzada: intervals.icu no respalda a Strava.
    return (principal,)


def sincronizar(
    desde: date,
    hasta: date,
    repo: Repo,
    plan_store: PlanStore | None = None,
    principal: str | None = None,
    forzar: bool = False,
    semana: str | None = None,
    ingestas: dict[str, IngestaBase] | None = None,
) -> ResultadoFuente:
    """Sincroniza [desde, hasta] con la primera fuente que responda.

    `ingestas` permite inyectar dobles en los tests sin tocar `abrir`.
    `semana` es la clave (lunes ISO) con la que se registra la fuente usada;
    sin ella no se registra nada, que es lo que corresponde cuando el rango no
    es una semana (backfill).
    """
    orden = cadena(principal)
    resultado = ResultadoFuente()
    intentos: list[tuple[str, str]] = []

    for i, nombre in enumerate(orden):
        try:
            ing = (ingestas or {}).get(nombre) or abrir(nombre, repo, plan_store)
        except Exception as exc:
            # Falta la credencial de esa fuente: es un intento fallido, no una
            # excepcion que tumbe la corrida.
            log.warning("no se pudo preparar la fuente %s: %s", nombre, exc)
            intentos.append((nombre, str(exc)))
            continue

        resultado.ingestas.append(ing)
        try:
            resumen = ing.sincronizar(desde, hasta, forzar=forzar)
        except ing.ERRORES as exc:
            log.error("la ingesta de %s fallo: %s", nombre, exc)
            intentos.append((nombre, str(exc)))
            if i + 1 < len(orden):
                log.warning(
                    "se cae al respaldo de %s: la semana quedara sin %s",
                    orden[i + 1],
                    ", ".join(NO_DISPONIBLE.get(orden[i + 1], ())) or "nada",
                )
            continue

        resultado.fuente = nombre
        resultado.fallback = i > 0
        resumen.fuente = nombre
        resumen.fallback = resultado.fallback
        if resultado.fallback:
            resumen.avisos.insert(
                0,
                f"los datos de esta semana vienen de {nombre} (respaldo): no hay "
                "GCT, oscilacion vertical, ratio vertical ni bienestar (HRV, "
                "sueno, sleep score, Body Battery, readiness). La semana no "
                "esta completa",
            )
        resultado.resumen = resumen
        break

    resultado.intentos = tuple(intentos)

    log.info(
        "fuente de datos de %s..%s: %s (fallback=%s, intentos fallidos=%s)",
        desde,
        hasta,
        resultado.fuente or "ninguna",
        resultado.fallback,
        resultado.motivo or "ninguno",
    )

    if semana and resultado.ok:
        repo.guardar_fuente_semana(
            semana, resultado.fuente, resultado.fallback, resultado.motivo
        )

    return resultado
