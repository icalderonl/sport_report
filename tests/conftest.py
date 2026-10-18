"""Aisla la suite de los datos y logs reales del proyecto.

Sin esto, los tests que corren `run_weekly`, el grafico o el bot sin parchear
rutas escribian en `data/` y `logs/` del repo. No es solo ruido: el mensual se
da por enviado si existe `data/reportes/mensual/<mes>.json`, asi que correr la
suite donde viven los datos de verdad bloqueaba el reporte mensual real.

Cada test arranca con todas las rutas apuntando a su `tmp_path`. Un test que
necesite otra ruta la sigue parcheando como antes; su `monkeypatch` gana.
"""
from __future__ import annotations

import logging

import pytest

from sport_report import config, logging_setup


@pytest.fixture(autouse=True)
def _rutas_aisladas(tmp_path_factory, monkeypatch):
    # Un directorio aparte y no `tmp_path`: hay tests que comparan el contenido
    # de su tmp_path y no esperan encontrar ahi data/ ni logs/.
    raiz = tmp_path_factory.mktemp("rutas")
    data = raiz / "data"

    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "DB_PATH", data / config.DB_PATH.name)
    monkeypatch.setattr(config, "TOKENS_PATH", data / config.TOKENS_PATH.name)
    monkeypatch.setattr(config, "PLAN_ACTUAL_PATH", data / config.PLAN_ACTUAL_PATH.name)
    monkeypatch.setattr(config, "PLANES_DIR", data / config.PLANES_DIR.name)
    monkeypatch.setattr(config, "CARRERA_PATH", data / config.CARRERA_PATH.name)
    monkeypatch.setattr(config, "BACKUP_DIR", raiz / "respaldos")
    monkeypatch.setattr(config, "LOG_DIR", raiz / "logs")
    # logging_setup importa LOG_DIR por nombre: parchear config no le llega.
    monkeypatch.setattr(logging_setup, "LOG_DIR", raiz / "logs")

    # `logging_setup.setup` reemplaza los handlers del logger raiz. Se
    # restauran al terminar para no dejar un archivo abierto en un directorio
    # temporal ni arrastrar el handler de un test al siguiente.
    root = logging.getLogger()
    previos, nivel = list(root.handlers), root.level
    yield
    for h in root.handlers:
        if h not in previos:
            h.close()
    root.handlers[:] = previos
    root.setLevel(nivel)
