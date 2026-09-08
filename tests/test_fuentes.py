"""El selector de fuente: intervals.icu primero, Strava solo si esa falla.

Lo que se prueba aca no es HTTP (eso esta en test_intervals.py y test_strava.py)
sino la politica: quien va primero, cuando se cae al respaldo, que pasa si las
dos fallan, y que la semana degradada quede marcada como tal en vez de pasar
por completa.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from sport_report import config, fuentes
from sport_report.db.models import SesionReal
from sport_report.db.repo import Repo
from sport_report.engine import report
from sport_report.fechas import semana_de
from sport_report.fuentes import ResumenIngesta
from sport_report.intervals.errors import IntervalsAuthError, IntervalsError
from sport_report.plan.store import PlanStore
from sport_report.strava.errors import StravaError

SEMANA = semana_de(date(2026, 9, 7))


class FuenteFalsa:
    """Doble de una `IngestaBase`, con la misma interfaz que usa el selector."""

    def __init__(self, nombre: str, errores: tuple, excepcion=None):
        self.NOMBRE = nombre
        self.ERRORES = errores
        self.excepcion = excepcion
        self.llamadas: list[tuple] = []
        self.cliente = _Cliente()

    def sincronizar(self, desde, hasta, forzar=False):
        self.llamadas.append((desde, hasta, forzar))
        if self.excepcion:
            raise self.excepcion
        return ResumenIngesta(actividades=3, corridas=3, zonas_origen=self.NOMBRE)


class _Cliente:
    def __init__(self):
        self.cerrado = False

    def cerrar(self):
        self.cerrado = True


def intervals(excepcion=None) -> FuenteFalsa:
    return FuenteFalsa(config.INTERVALS, (IntervalsError,), excepcion)


def strava(excepcion=None) -> FuenteFalsa:
    return FuenteFalsa(config.STRAVA, (StravaError,), excepcion)


@pytest.fixture()
def repo(tmp_path):
    with Repo(tmp_path / "t.db") as r:
        yield r


def _sincronizar(repo, dobles, principal=None, semana=SEMANA.clave):
    return fuentes.sincronizar(
        SEMANA.inicio,
        SEMANA.fin,
        repo,
        plan_store=None,
        principal=principal,
        semana=semana,
        ingestas=dobles,
    )


# --------------------------------------------------------------------------
# Orden e intentos
# --------------------------------------------------------------------------


def test_intervals_va_primero_y_strava_no_se_toca(repo):
    i, s = intervals(), strava()
    r = _sincronizar(repo, {config.INTERVALS: i, config.STRAVA: s})

    assert (r.fuente, r.fallback, r.ok) == (config.INTERVALS, False, True)
    assert len(i.llamadas) == 1
    assert s.llamadas == [], "se consulto el respaldo sin necesidad"
    assert r.no_disponibles == ()


def test_si_intervals_falla_se_cae_a_strava(repo):
    i, s = intervals(IntervalsError("503 persistente")), strava()
    r = _sincronizar(repo, {config.INTERVALS: i, config.STRAVA: s})

    assert (r.fuente, r.fallback, r.ok) == (config.STRAVA, True, True)
    assert len(s.llamadas) == 1
    assert "503 persistente" in r.motivo


def test_una_clave_rechazada_tambien_dispara_el_respaldo(repo):
    """Un 401 es un IntervalsError: el reporte tiene que salir igual."""
    i, s = intervals(IntervalsAuthError("401: clave rechazada")), strava()
    r = _sincronizar(repo, {config.INTERVALS: i, config.STRAVA: s})
    assert (r.fuente, r.fallback) == (config.STRAVA, True)


def test_la_semana_de_respaldo_dice_que_le_falta(repo):
    i, s = intervals(IntervalsError("timeout")), strava()
    r = _sincronizar(repo, {config.INTERVALS: i, config.STRAVA: s})

    assert set(r.no_disponibles) == {
        "gct",
        "oscilacion_vertical",
        "ratio_vertical",
        "fatiga_descanso",
    }
    aviso = r.resumen.avisos[0]
    assert "respaldo" in aviso and "no esta completa" in aviso
    for falta in ("GCT", "oscilacion vertical", "ratio vertical", "bienestar"):
        assert falta in aviso
    assert (r.resumen.fuente, r.resumen.fallback) == (config.STRAVA, True)


def test_si_las_dos_fallan_no_se_lanza_y_se_reporta_lo_guardado(repo):
    i = intervals(IntervalsError("caido"))
    s = strava(StravaError("429 cuota agotada"))
    r = _sincronizar(repo, {config.INTERVALS: i, config.STRAVA: s})

    assert (r.ok, r.fuente, r.resumen) == (False, "", None)
    assert [f for f, _ in r.intentos] == [config.INTERVALS, config.STRAVA]
    assert "caido" in r.motivo and "429 cuota agotada" in r.motivo


def test_no_hay_respaldo_del_respaldo(repo):
    """Con Strava forzada, intervals.icu no la respalda."""
    i, s = intervals(), strava(StravaError("caido"))
    r = _sincronizar(
        repo, {config.INTERVALS: i, config.STRAVA: s}, principal=config.STRAVA
    )

    assert r.ok is False
    assert i.llamadas == [], "intervals.icu respaldo a Strava, y no debe"
    assert [f for f, _ in r.intentos] == [config.STRAVA]


def test_la_cadena_de_intento_es_explicita():
    assert fuentes.cadena(config.INTERVALS) == (config.INTERVALS, config.STRAVA)
    assert fuentes.cadena(config.STRAVA) == (config.STRAVA,)


def test_una_fuente_desconocida_es_un_error_de_programacion():
    with pytest.raises(ValueError) as exc:
        fuentes.abrir("garmin")
    assert "intervals" in str(exc.value) and "strava" in str(exc.value)


def test_una_fuente_sin_credencial_cuenta_como_intento_fallido(repo, monkeypatch):
    """Si falta INTERVALS_API_KEY, se respalda; no se cae la corrida."""
    monkeypatch.setattr(
        fuentes, "abrir", lambda n, *a, **k: (_ for _ in ()).throw(RuntimeError("falta la clave"))
    )
    r = fuentes.sincronizar(
        SEMANA.inicio, SEMANA.fin, repo, semana=SEMANA.clave, ingestas={config.STRAVA: strava()}
    )
    assert (r.fuente, r.fallback) == (config.STRAVA, True)
    assert "falta la clave" in r.motivo


def test_los_clientes_abiertos_se_cierran(repo):
    i, s = intervals(IntervalsError("caido")), strava()
    r = _sincronizar(repo, {config.INTERVALS: i, config.STRAVA: s})
    r.cerrar()
    assert (i.cliente.cerrado, s.cliente.cerrado) == (True, True)


# --------------------------------------------------------------------------
# Persistencia de la fuente de la semana
# --------------------------------------------------------------------------


def test_la_fuente_de_la_semana_queda_registrada(repo):
    _sincronizar(repo, {config.INTERVALS: intervals()})
    fila = repo.fuente_semana(SEMANA.clave)
    assert (fila["fuente"], fila["fallback"]) == (config.INTERVALS, False)


def test_el_respaldo_queda_registrado_con_su_motivo(repo):
    _sincronizar(
        repo,
        {config.INTERVALS: intervals(IntervalsError("503")), config.STRAVA: strava()},
    )
    fila = repo.fuente_semana(SEMANA.clave)
    assert (fila["fuente"], fila["fallback"]) == (config.STRAVA, True)
    assert "503" in fila["detalle"]


def test_si_ninguna_fuente_sirvio_no_se_registra_una_falsa(repo):
    """Mejor sin registro que con uno que diga que la semana se ingirio bien."""
    _sincronizar(
        repo,
        {
            config.INTERVALS: intervals(IntervalsError("x")),
            config.STRAVA: strava(StravaError("y")),
        },
    )
    assert repo.fuente_semana(SEMANA.clave) is None


def test_sin_clave_de_semana_no_se_registra_nada(repo):
    """El backfill cubre rangos que no son una semana."""
    _sincronizar(repo, {config.INTERVALS: intervals()}, semana=None)
    assert repo.fuente_semana(SEMANA.clave) is None


# --------------------------------------------------------------------------
# El bloque `fuente` del reporte
# --------------------------------------------------------------------------


def _plan_store(tmp_path):
    return PlanStore(actual=tmp_path / "plan.json", archivo=tmp_path / "planes")


def _corrida(repo, fuente: str, **kw) -> None:
    repo.guardar_sesion(
        SesionReal(
            fuente=fuente,
            id_externo="a1",
            fecha_utc=f"{SEMANA.inicio}T12:00:00+00:00",
            fecha_local=SEMANA.inicio.isoformat(),
            dia_semana="L",
            tipo="Run",
            es_fuerza=False,
            distancia_km=10.0,
            duracion_mov_s=3000,
            carga=400.0,
            **kw,
        )
    )


def test_el_reporte_marca_la_semana_de_respaldo(repo, tmp_path):
    _corrida(repo, config.STRAVA)
    repo.guardar_fuente_semana(SEMANA.clave, config.STRAVA, True, "intervals: 401")

    j = report.construir(SEMANA, repo, _plan_store(tmp_path))

    f = j["fuente"]
    assert (f["usada"], f["fallback"]) == (config.STRAVA, True)
    assert "401" in f["motivo"]
    assert "gct" in f["no_disponibles"] and "fatiga_descanso" in f["no_disponibles"]
    # Y el aviso va al frente, no perdido entre otros.
    assert "respaldo" in j["avisos_datos"][0]
    assert "no esta completa" in j["avisos_datos"][0]


def test_una_semana_normal_no_dice_nada_de_respaldo(repo, tmp_path):
    _corrida(repo, config.INTERVALS, gct_ms=232.0)
    repo.guardar_fuente_semana(SEMANA.clave, config.INTERVALS, False)

    j = report.construir(SEMANA, repo, _plan_store(tmp_path))

    assert (j["fuente"]["usada"], j["fuente"]["fallback"]) == (config.INTERVALS, False)
    assert j["fuente"]["no_disponibles"] == []
    assert not any("respaldo" in a for a in j["avisos_datos"])


def test_sin_registro_de_fuente_el_reporte_lo_dice_en_vez_de_suponer(repo, tmp_path):
    """Pasa con --sin-ingesta o al rehacer un reporte viejo."""
    _corrida(repo, config.STRAVA)
    j = report.construir(SEMANA, repo, _plan_store(tmp_path))

    assert j["fuente"]["usada"] is None
    assert "no hay registro" in j["fuente"]["motivo"]
    assert j["fuente"]["fallback"] is False
