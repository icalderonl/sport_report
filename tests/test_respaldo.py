"""Tests del respaldo de data/.

Lo que se comprueba no es que existan archivos sino que la copia SIRVA: que la
base se pueda abrir y tenga las mismas filas, incluso si habia otro proceso
escribiendo mientras se copiaba.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

import pytest

from sport_report import respaldo
from sport_report.db.models import SesionReal
from sport_report.db.repo import Repo
from sport_report.storage import escribir_json

LUNES = date(2026, 8, 31)


def _sesion(offset: int, **kw) -> SesionReal:
    f = LUNES + timedelta(days=offset)
    base = dict(
        strava_id=100 + offset,
        fecha_utc=f"{f}T12:00:00+00:00",
        fecha_local=f.isoformat(),
        dia_semana="LMWJVSD"[offset],
        tipo_strava="Run",
        es_fuerza=False,
        distancia_km=10.0,
        carga=250.0,
    )
    base.update(kw)
    return SesionReal(**base)


@pytest.fixture
def data(tmp_path):
    """Un `data/` poblado como el de la Pi: base, tokens, plan y reportes."""
    d = tmp_path / "data"
    d.mkdir()
    with Repo(d / "sport_report.db") as repo:
        for i in range(3):
            repo.guardar_sesion(_sesion(i))
    escribir_json(d / "tokens.json", {"refresh_token": "r1", "access_token": "a1"})
    escribir_json(d / "plan_actual.json", {"version": 1})
    escribir_json(d / "planes" / "2026-08-24.json", {"version": 1})
    escribir_json(d / "reportes" / "2026-08-24.json", {"volumen": {"real_km": 40}})
    return d


def test_la_base_respaldada_se_abre_y_tiene_las_mismas_filas(data, tmp_path):
    carpeta, piezas = respaldo.respaldar(destino=tmp_path / "resp", origen=data)

    copia = carpeta / "sport_report.db"
    con = sqlite3.connect(copia)
    try:
        assert con.execute("SELECT COUNT(*) FROM sesiones").fetchone()[0] == 3
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()
    assert piezas["sport_report.db"] > 0


def test_respalda_tokens_planes_y_reportes(data, tmp_path):
    carpeta, piezas = respaldo.respaldar(destino=tmp_path / "resp", origen=data)

    assert (carpeta / "tokens.json").is_file()
    assert (carpeta / "plan_actual.json").is_file()
    assert (carpeta / "planes" / "2026-08-24.json").is_file()
    assert (carpeta / "reportes" / "2026-08-24.json").is_file()
    assert set(piezas) == {
        "sport_report.db",
        "tokens.json",
        "plan_actual.json",
        "planes/",
        "reportes/",
    }


def test_la_copia_es_coherente_con_otro_proceso_escribiendo(data, tmp_path):
    """El bot corre 24/7: copiar el archivo suelto con WAL da una base rota.

    Se deja una transaccion abierta y sin confirmar mientras se respalda: la
    copia tiene que traer el estado confirmado, ni a medias ni con lo que
    todavia no se guardo.
    """
    with Repo(data / "sport_report.db") as repo:
        repo.con.execute("BEGIN")
        repo.con.execute(
            "INSERT INTO sesiones (strava_id, fecha_utc, fecha_local, dia_semana, "
            "tipo_strava, es_fuerza, ingerido_en) VALUES (999,'x','2026-09-01','M','Run',0,'x')"
        )
        carpeta, _ = respaldo.respaldar(destino=tmp_path / "resp", origen=data)
        repo.con.rollback()

    con = sqlite3.connect(carpeta / "sport_report.db")
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        # Las 3 confirmadas si; la que quedo sin confirmar no.
        assert con.execute("SELECT COUNT(*) FROM sesiones").fetchone()[0] == 3
        assert con.execute("SELECT COUNT(*) FROM sesiones WHERE strava_id=999").fetchone()[0] == 0
    finally:
        con.close()


def test_lo_que_no_existe_se_omite_sin_fallar(tmp_path):
    """Una instalacion nueva no tiene tokens ni planes archivados todavia."""
    vacio = tmp_path / "data"
    vacio.mkdir()

    carpeta, piezas = respaldo.respaldar(destino=tmp_path / "resp", origen=vacio)

    assert piezas == {}
    assert carpeta.is_dir()


def test_la_rotacion_conserva_los_mas_recientes(data, tmp_path):
    destino = tmp_path / "resp"
    base = datetime(2026, 9, 1, 7, 0)
    for i in range(6):
        respaldo.respaldar(
            destino=destino, origen=data, conservar=3, momento=base + timedelta(days=7 * i)
        )

    quedan = sorted(p.name for p in destino.glob("*") if p.is_dir())
    assert len(quedan) == 3
    assert quedan[-1].startswith("2026-10-06")  # el ultimo de los seis


def test_conservar_cero_no_borra_nada(data, tmp_path):
    destino = tmp_path / "resp"
    respaldo.respaldar(destino=destino, origen=data, conservar=0, momento=datetime(2026, 9, 1))

    assert len(list(destino.glob("*"))) == 1


def test_ultimo_devuelve_el_mas_reciente(data, tmp_path):
    destino = tmp_path / "resp"
    assert respaldo.ultimo(destino) is None

    base = datetime(2026, 9, 1, 7, 0)
    for i in range(3):
        respaldo.respaldar(
            destino=destino, origen=data, conservar=9, momento=base + timedelta(days=7 * i)
        )

    assert respaldo.ultimo(destino).name.startswith("2026-09-15")


def test_dos_respaldos_seguidos_no_chocan(data, tmp_path):
    """Mismo segundo, misma carpeta: tiene que sobrescribir sin reventar."""
    destino = tmp_path / "resp"
    momento = datetime(2026, 9, 1, 7, 0)
    respaldo.respaldar(destino=destino, origen=data, conservar=9, momento=momento)
    carpeta, piezas = respaldo.respaldar(destino=destino, origen=data, conservar=9, momento=momento)

    assert len(list(destino.glob("*"))) == 1
    assert piezas["sport_report.db"] > 0


def test_la_corrida_semanal_no_falla_si_el_respaldo_falla(monkeypatch, caplog):
    """El respaldo es un extra: nunca puede costar el reporte del lunes."""
    from sport_report import run_weekly

    def explota():
        raise OSError("medio de respaldo desmontado")

    monkeypatch.setattr(respaldo, "respaldar", explota)
    run_weekly._respaldar(run_weekly.log)  # no debe lanzar

    assert "no se pudo respaldar" in caplog.text
