"""Grafico de volumen semanal como imagen PNG.

matplotlib es la unica dependencia pesada del proyecto y se importa perezoso: si
no esta instalada, o si falla al dibujar, el reporte tiene que llegar igual (sin
imagen). Backend Agg obligatorio — la Pi corre sin entorno grafico.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from . import config

log = logging.getLogger(__name__)

# Misma paleta que el logo del servicio.
AZUL = "#16233A"
AMBAR = "#FBBF24"
GRIS = "#94A3B8"

ANCHO_PULG = 9.0
ALTO_PULG = 4.2
DPI = 130


def _ruta_por_defecto() -> Path:
    return config.DATA_DIR / "graficos" / "volumen.png"


def volumen_png(historico: dict[str, Any], destino: Path | None = None) -> Path | None:
    """Dibuja los km por semana. Devuelve la ruta, o None si no hay nada que mostrar.

    Lanza si matplotlib falta o falla: el que llama decide si eso degrada la
    corrida (spec 10: el reporte siempre llega, con o sin imagen).
    """
    semanas = historico.get("semanas") or []
    if not semanas or max(s["km"] for s in semanas) <= 0:
        return None

    import matplotlib

    matplotlib.use("Agg")  # antes de importar pyplot
    import matplotlib.pyplot as plt

    destino = destino or _ruta_por_defecto()
    destino.parent.mkdir(parents=True, exist_ok=True)

    etiquetas = [_dia_mes(s["lunes"]) for s in semanas]
    valores = [s["km"] for s in semanas]
    ultima_en_curso = bool(historico.get("ultima_en_curso"))

    # La semana a medio correr se pinta distinto: si no, parece un desplome.
    colores = [AZUL] * len(valores)
    if ultima_en_curso:
        colores[-1] = AMBAR

    fig, ax = plt.subplots(figsize=(ANCHO_PULG, ALTO_PULG), dpi=DPI)
    barras = ax.bar(etiquetas, valores, color=colores, width=0.72)

    for barra, km in zip(barras, valores):
        if km <= 0:
            continue
        ax.annotate(
            f"{km:g}",
            (barra.get_x() + barra.get_width() / 2, km),
            textcoords="offset points",
            xytext=(0, 3),
            ha="center",
            fontsize=7.5,
            color=AZUL,
        )

    titulo = f"Volumen semanal - ultimas {len(semanas)} semanas"
    if ultima_en_curso:
        titulo += "  (la ultima, en curso)"
    ax.set_title(titulo, fontsize=11, color=AZUL, pad=12)
    ax.set_ylabel("km", fontsize=9, color=AZUL)
    ax.set_ylim(0, max(valores) * 1.15)

    ax.grid(axis="y", color=GRIS, alpha=0.3, linewidth=0.6)
    ax.set_axisbelow(True)
    for lado in ("top", "right", "left"):
        ax.spines[lado].set_visible(False)
    ax.spines["bottom"].set_color(GRIS)
    ax.tick_params(axis="x", labelrotation=45, labelsize=7.5, colors=AZUL, length=0)
    ax.tick_params(axis="y", labelsize=8, colors=AZUL, length=0)

    desde = historico.get("primera_fecha_con_datos")
    if desde and _lunes_iso(desde) > semanas[0]["lunes"]:
        fig.text(
            0.01,
            0.015,
            f"Sin historico antes del {_dia_mes(desde)}: esas semanas van en cero.",
            fontsize=7,
            color=GRIS,
        )

    fig.tight_layout()
    # Escritura atomica: el bot y el cron pueden dibujar a la vez.
    tmp = destino.with_suffix(".tmp.png")
    try:
        fig.savefig(tmp, format="png", facecolor="white")
    finally:
        plt.close(fig)
    os.replace(tmp, destino)
    log.info("grafico de volumen en %s", destino)
    return destino


def _dia_mes(iso: str) -> str:
    _, m, d = iso.split("-")
    return f"{d}/{m}"


def _lunes_iso(iso: str) -> str:
    from datetime import date, timedelta

    d = date.fromisoformat(iso)
    return (d - timedelta(days=d.weekday())).isoformat()
