"""Informe post-carrera. Lo dispara un timer diario; casi siempre no hace nada.

    python -m sport_report.run_post_carrera [opciones]

Cada dia a las 13:00 mira las carreras declaradas con /carrera. Si alguna es
de hoy o de los ultimos `POST_CARRERA.dias_ventana` dias y su informe no
existe, sincroniza, calcula, narra y envia. El primer intento es el mismo dia
de la carrera; si la actividad todavia no se subio, se reintenta al dia
siguiente, y los dias extra de la ventana cubren una Pi apagada.

Guarda idempotente: el JSON en `data/reportes/carreras/<fecha>.json`. Solo se
escribe cuando el informe llego, asi que un envio fallido se reintenta al dia
siguiente. Si la actividad de la carrera todavia no esta, no se envia nada
hasta el ultimo dia de la ventana; ese dia sale igual, diciendo que falta.

Un dia sin carreras pendientes no toca la red: ni fuentes ni Claude.
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Sequence

from . import fuentes
from .config import POST_CARRERA, PostCarrera
from .db.repo import Repo
from .engine import post_carrera as m_post
from .fechas import hoy_local, semana_de
from .logging_setup import setup
from .narrative.post_carrera import redactar_post_carrera
from .plan.carrera import Carrera, CarreraStore
from .plan.store import PlanStore
from .storage import escribir_json
from .telegram.sender import enviar

log = logging.getLogger(__name__)

OK = "ok"
PARCIAL = "parcial"
ERROR = "error"
#: La actividad no aparecio todavia y quedan dias de ventana: no se envia.
ESPERANDO = "esperando"


@dataclass
class Resultado:
    estado: str
    carrera: Carrera
    mensaje: str = ""
    datos: dict[str, Any] = field(default_factory=dict)
    enviado: bool = False
    problemas: list[str] = field(default_factory=list)


def pendientes(
    carreras: Sequence[Carrera],
    hoy: date | None = None,
    hecho: Callable[[date], bool] | None = None,
    cfg: PostCarrera = POST_CARRERA,
) -> list[Carrera]:
    """Carreras de hoy y de los ultimos dias sin informe, de la mas vieja a la
    mas nueva. Una carrera futura nunca entra.
    """
    hoy = hoy or hoy_local()
    ya_hecho = hecho or (lambda f: m_post.ruta_informe(f).exists())
    desde = hoy - timedelta(days=cfg.dias_ventana)
    return sorted(
        (c for c in carreras if desde <= c.fecha <= hoy and not ya_hecho(c.fecha)),
        key=lambda c: c.fecha,
    )


def ultimo_intento(carrera: Carrera, hoy: date, cfg: PostCarrera = POST_CARRERA) -> bool:
    return (hoy - carrera.fecha).days >= cfg.dias_ventana


# --------------------------------------------------------------------------
# Mensaje
# --------------------------------------------------------------------------


def _n(v: Any, dec: int = 1) -> str:
    if v is None:
        return "s/d"
    return f"{round(v, dec):g}"


def _signo(v: float) -> str:
    return f"+{v:g}" if v > 0 else f"{v:g}"


def formatear(datos: dict[str, Any], narrativa: str | None) -> str:
    """Cifras del motor arriba (siempre llegan) y la narrativa abajo."""
    c = datos["carrera"]
    titulo = c["nombre"] or "Carrera"
    lineas = [f"POST-CARRERA - {titulo} ({c['fecha']})", ""]

    a = datos["actividad"]
    if a["disponible"]:
        lineas.append(
            f"{_n(a['distancia_km'], 2)} km en {a['tiempo_mov'] or 's/d'} "
            f"({a['ritmo'] or 's/d'})"
        )
        if a["hr_promedio"] is not None:
            lineas.append(f"FC {_n(a['hr_promedio'], 0)} prom / {_n(a['hr_maximo'], 0)} max")
        if a["decoupling_pct"] is not None:
            lineas.append(f"Deriva cardiaca {_n(a['decoupling_pct'])}%")
    else:
        lineas.append(f"Sin actividad de carrera: {a['motivo']}")

    p = datos["parciales"]
    if p["disponible"]:
        lineas.append(
            f"Mitades {p['primera_mitad']} / {p['segunda_mitad']} "
            f"(split {_signo(p['split_pct'])}%, {p['tipo_split']})"
        )
        lineas.append("Cuartos " + " / ".join(q["ritmo"] for q in p["cuartos"]))

    f = datos["fatiga"]
    if f["senales"]:
        lineas += ["", "Senales de fatiga:"] + [f"- {s}" for s in f["senales"]]
    elif f["evaluadas"]:
        lineas.append("Sin senales de fatiga en " + ", ".join(f["evaluadas"]))

    ciclo = datos.get("ciclo")
    if ciclo:
        lineas += ["", f"Ciclo: {ciclo['semanas']} semanas, {_n(ciclo['total_km'])} km"]
        if ciclo["pico"]:
            lineas.append(f"Pico {_n(ciclo['pico']['km'])} km (semana {ciclo['pico']['lunes']})")
        af = ciclo["afinamiento"]
        if af["disponible"]:
            lineas.append(
                f"Afinamiento {_signo(af['reduccion_vs_pico_pct'])}% vs pico "
                f"({_n(af['promedio_km'])} km/sem)"
            )
        adh = ciclo["adherencia"]
        if adh["confiable"]:
            lineas.append(
                f"Adherencia {_n(adh['promedio_pct'])}% "
                f"({adh['semanas_con_plan']} semanas con plan)"
            )
        comp = datos["comparacion"]["ciclo_anterior"]
        if comp["disponible"] and comp["delta_total_km"] is not None:
            lineas.append(
                f"Vs ciclo anterior ({comp['carrera']}): {_signo(comp['delta_total_km'])} km"
            )

    if datos["avisos_datos"]:
        lineas += [""] + [f"Aviso: {x}" for x in datos["avisos_datos"]]

    if narrativa:
        lineas += ["", narrativa]
    return "\n".join(lineas)


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------


def ejecutar(
    carrera: Carrera,
    es_principal: bool,
    repo: Repo,
    plan_store: PlanStore | None = None,
    narrador: Callable[[dict], Any] | None = redactar_post_carrera,
    enviador: Callable[[str], Any] | None = enviar,
    guardar: bool = True,
    hoy: date | None = None,
    forzar: bool = False,
    cfg: PostCarrera = POST_CARRERA,
) -> Resultado:
    """Informe de una carrera. No lanza salvo un fallo del motor de calculo."""
    hoy = hoy or hoy_local()
    problemas: list[str] = []
    datos = m_post.construir(carrera, es_principal, repo, plan_store, cfg)

    if not datos["actividad"]["disponible"] and not forzar and not ultimo_intento(
        carrera, hoy, cfg
    ):
        log.info(
            "la actividad de la carrera %s no esta todavia: se reintenta manana",
            carrera.fecha,
        )
        return Resultado(estado=ESPERANDO, carrera=carrera, datos=datos)

    texto = None
    if narrador is not None:
        n = narrador(datos)
        datos["narrativa"] = n.to_json()
        if n.ok:
            texto = n.texto
            if n.cifras_mal_atribuidas:
                datos["avisos_datos"].append(
                    "el resumen automatico atribuye mal una cifra ("
                    + "; ".join(n.cifras_mal_atribuidas)
                    + "). Fiate de los numeros de arriba, no de los del resumen"
                )
        else:
            problemas.append(f"resumen automatico no disponible ({n.error})")

    mensaje = formatear(datos, texto)
    enviado = True
    if enviador is not None:
        envio = enviador(mensaje)
        enviado = bool(envio)
        if not enviado and getattr(envio, "enviadas", 0):
            # Llego una parte: el atleta ya lo tiene, no se repite manana.
            problemas.append(
                f"el envio por Telegram quedo incompleto ({envio.enviadas} de "
                f"{envio.totales} mensajes)"
            )
            enviado = True
        elif not enviado:
            problemas.append("fallo el envio por Telegram")

    # La guarda se escribe solo si llego: un envio fallido se reintenta manana.
    if guardar and enviado:
        escribir_json(m_post.ruta_informe(carrera.fecha), datos)

    estado = ERROR if not enviado else PARCIAL if problemas else OK
    return Resultado(
        estado=estado,
        carrera=carrera,
        mensaje=mensaje,
        datos=datos,
        enviado=enviado,
        problemas=problemas,
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _args(argv: list[str] | None):
    p = argparse.ArgumentParser(description="Informe post-carrera")
    p.add_argument(
        "--fecha",
        metavar="YYYY-MM-DD",
        help=(
            "genera el informe de la carrera de esa fecha aunque este fuera de la "
            "ventana o ya se haya enviado"
        ),
    )
    p.add_argument("--dry-run", action="store_true", help="imprime el informe en vez de enviarlo")
    p.add_argument("--sin-ingesta", action="store_true", help="no consulta ninguna fuente")
    p.add_argument("--sin-narrativa", action="store_true", help="no llama a la API de Claude")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    setup("run_post_carrera")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    a = _args(argv)
    hoy = hoy_local()
    store = CarreraStore()
    carreras = store.listar()
    principal = store.principal()

    if a.fecha:
        try:
            fecha = date.fromisoformat(a.fecha)
        except ValueError:
            print(f"--fecha espera YYYY-MM-DD; se recibio '{a.fecha}'", file=sys.stderr)
            return 2
        elegidas = [c for c in carreras if c.fecha == fecha]
        if not elegidas:
            print(f"no hay ninguna carrera declarada el {fecha}", file=sys.stderr)
            return 2
    else:
        elegidas = pendientes(carreras, hoy)
    if not elegidas:
        log.info("sin carreras pendientes de informe")
        return 0

    repo = Repo()
    seleccion: fuentes.ResultadoFuente | None = None
    codigo = 0
    try:
        if not a.sin_ingesta:
            # Desde el lunes de la carrera mas vieja: trae la actividad, sus
            # vueltas y el bienestar de la manana siguiente. Sin `semana`: este
            # rango no es una semana del reporte y no debe registrar su fuente.
            desde = semana_de(min(c.fecha for c in elegidas)).inicio
            seleccion = fuentes.sincronizar(desde, hoy, repo)
            if not seleccion.ok:
                log.warning("sin sincronizar (%s): se usa lo ya guardado", seleccion.motivo)
        for c in elegidas:
            r = ejecutar(
                c,
                es_principal=principal is not None and principal.fecha == c.fecha,
                repo=repo,
                narrador=None if a.sin_narrativa else redactar_post_carrera,
                enviador=None if a.dry_run else enviar,
                guardar=not a.dry_run,
                hoy=hoy,
                forzar=bool(a.fecha),
            )
            log.info("informe post-carrera %s: %s %s", c.fecha, r.estado, r.problemas)
            if a.dry_run and r.mensaje:
                print(r.mensaje)
            if r.problemas:
                print(f"[{r.estado}] " + "; ".join(r.problemas), file=sys.stderr)
            if r.estado == ERROR:
                codigo = 1
    except Exception:
        log.exception("el informe post-carrera fallo")
        return 1
    finally:
        if seleccion is not None:
            seleccion.cerrar()
        repo.cerrar()
    return codigo


if __name__ == "__main__":
    raise SystemExit(main())
