"""La suite no puede escribir en los datos ni en los logs reales.

Lo garantiza `tests/conftest.py`. Si alguien lo borra o agrega una ruta nueva a
`config` sin aislarla, esto cae.
"""
from __future__ import annotations

from pathlib import Path

from sport_report import config, logging_setup


def _dentro_del_repo(ruta: Path) -> bool:
    return ruta.resolve().is_relative_to(config.ROOT.resolve())


def test_ninguna_ruta_de_config_apunta_al_repo():
    rutas = {
        nombre: valor
        for nombre, valor in vars(config).items()
        if isinstance(valor, Path) and nombre != "ROOT"
    }
    assert rutas, "config ya no expone rutas: revisar este test"
    en_el_repo = [n for n, r in rutas.items() if _dentro_del_repo(r)]
    assert en_el_repo == []


def test_los_logs_van_fuera_del_repo():
    assert not _dentro_del_repo(logging_setup.LOG_DIR)
