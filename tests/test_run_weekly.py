"""Tests del orquestador y del formateo del mensaje."""
from __future__ import annotations

from datetime import date

import pytest

from sport_report import run_weekly
from sport_report.db.repo import Repo
from sport_report.fechas import semana_de
from sport_report.narrative.claude import Narrativa, redactar
from sport_report.plan.grammar import parse_plan
from sport_report.plan.store import PlanStore
from sport_report.run_weekly import ERROR, OK, PARCIAL, ejecutar
from sport_report.strava.errors import StravaError
from sport_report.telegram.formato import formatear_reporte
from tests.test_engine import VUELTAS_SERIES, sesion
from tests.test_grammar import PLAN_SPEC
from tests.test_narrative import ClienteFalso, Respuesta

SEMANA = semana_de(date(2026, 9, 7))


@pytest.fixture
def entorno(tmp_path):
    repo = Repo(tmp_path / "t.db")
    store = PlanStore(actual=tmp_path / "plan.json", archivo=tmp_path / "planes")
    store.guardar(parse_plan(PLAN_SPEC), SEMANA)
    store.marcar_fuerza("W", True, SEMANA)
    for off, km, carga in ((1, 8.0, 92.0), (3, 10.0, 240.0), (4, 9.6, 150.0), (6, 16.1, 310.0)):
        repo.guardar_sesion(
            sesion(off, distancia_km=km, carga=carga, cadencia_spm=175.0, decoupling_pct=3.2)
        )
    yield repo, store
    repo.cerrar()


class Buzon:
    def __init__(self, ok=True):
        self.ok = ok
        self.mensajes: list[str] = []

    def __call__(self, texto: str) -> bool:
        self.mensajes.append(texto)
        return self.ok


class IngestaFalsa:
    def __init__(self, excepcion=None):
        self.excepcion = excepcion
        self.llamadas: list[tuple] = []

    def sincronizar(self, desde, hasta, **kw):
        self.llamadas.append((desde, hasta))
        if self.excepcion:
            raise self.excepcion
        from sport_report.strava.ingest import ResumenIngesta

        return ResumenIngesta(actividades=4, corridas=4, zonas_origen="strava")


def narrador_ok(datos):
    return Narrativa(texto="Semana solida.", modelo="claude-haiku-4-5")


def narrador_caido(datos):
    return Narrativa(texto=None, error="APIConnectionError: sin red")


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------


def test_corrida_completa(entorno):
    repo, store = entorno
    buzon = Buzon()
    ing = IngestaFalsa()
    r = ejecutar(SEMANA, repo, store, ingesta=ing, narrador=narrador_ok, enviador=buzon, guardar=False)

    assert r.estado == OK and r.enviado and r.problemas == []
    assert ing.llamadas == [(SEMANA.inicio, SEMANA.fin)]
    assert len(buzon.mensajes) == 1
    assert "REPORTE SEMANAL" in buzon.mensajes[0]
    assert "Semana solida." in buzon.mensajes[0]


def test_strava_caido_reporta_igual_con_lo_guardado(entorno):
    """Spec 10: sin Strava el sistema no se calla, avisa y usa la base."""
    repo, store = entorno
    buzon = Buzon()
    ing = IngestaFalsa(excepcion=StravaError("429 cuota agotada"))
    r = ejecutar(SEMANA, repo, store, ingesta=ing, narrador=narrador_ok, enviador=buzon, guardar=False)

    assert r.estado == PARCIAL and r.enviado
    assert any("no se pudo sincronizar" in p for p in r.problemas)
    assert "429 cuota agotada" in buzon.mensajes[0]
    # Las cifras siguen saliendo de lo que ya habia en la base.
    assert "43.7 km reales" in buzon.mensajes[0]


def test_claude_caido_reporta_igual_sin_narrativa(entorno):
    """Spec 10: el reporte se envia igual, sin la parte narrativa."""
    repo, store = entorno
    buzon = Buzon()
    r = ejecutar(SEMANA, repo, store, narrador=narrador_caido, enviador=buzon, guardar=False)

    assert r.estado == PARCIAL and r.enviado
    assert any("resumen automatico no disponible" in p for p in r.problemas)
    assert "REPORTE SEMANAL" in buzon.mensajes[0]
    assert "ADHERENCIA" in buzon.mensajes[0]


def test_fallo_de_envio_es_error(entorno):
    repo, store = entorno
    r = ejecutar(SEMANA, repo, store, narrador=narrador_ok, enviador=Buzon(ok=False), guardar=False)
    assert r.estado == ERROR and r.codigo_salida == 1


def test_los_dos_caidos_igual_manda_cifras(entorno):
    repo, store = entorno
    buzon = Buzon()
    r = ejecutar(
        SEMANA,
        repo,
        store,
        ingesta=IngestaFalsa(excepcion=StravaError("sin red")),
        narrador=narrador_caido,
        enviador=buzon,
        guardar=False,
    )
    assert r.estado == PARCIAL and r.enviado
    assert len(r.problemas) == 2
    assert "VOLUMEN" in buzon.mensajes[0]


def test_dry_run_no_envia(entorno):
    repo, store = entorno
    r = ejecutar(SEMANA, repo, store, narrador=None, enviador=None, guardar=False)
    assert r.estado == OK and r.enviado is True
    assert "REPORTE SEMANAL" in r.mensaje


def test_la_narrativa_queda_en_el_json(entorno):
    repo, store = entorno
    r = ejecutar(SEMANA, repo, store, narrador=narrador_ok, enviador=Buzon(), guardar=False)
    assert r.datos["narrativa"]["texto"] == "Semana solida."


def test_el_reporte_se_guarda_en_disco(entorno, tmp_path, monkeypatch):
    repo, store = entorno
    monkeypatch.setattr(run_weekly.config, "DATA_DIR", tmp_path / "salida")
    ejecutar(SEMANA, repo, store, narrador=None, enviador=Buzon(), guardar=True)
    assert (tmp_path / "salida" / "reportes" / "2026-09-07.json").exists()


def test_bitacora_de_corridas(entorno):
    repo, _ = entorno
    cid = repo.abrir_corrida()
    repo.cerrar_corrida(cid, PARCIAL, '["sin narrativa"]')
    assert repo.ultimas_corridas(1)[0]["estado"] == PARCIAL


# --------------------------------------------------------------------------
# Formateo
# --------------------------------------------------------------------------


def _mensaje(entorno, **kw) -> str:
    repo, store = entorno
    return ejecutar(SEMANA, repo, store, narrador=None, enviador=None, guardar=False, **kw).mensaje


def test_mensaje_trae_todas_las_secciones(entorno):
    m = _mensaje(entorno)
    for seccion in ("REPORTE SEMANAL", "VOLUMEN", "CARGA", "DERIVA CARDIACA", "CADENCIA", "ADHERENCIA"):
        assert seccion in m
    assert "Plan: semana 5" in m
    assert "7 de septiembre al 13 de septiembre" in m


def test_mensaje_marca_lo_no_confiable(entorno):
    m = _mensaje(entorno)
    # Con solo una semana en la base, el ACWR no puede ser confiable.
    assert "no confiable" in m
    assert "dias de historico real" in m


def test_mensaje_muestra_el_dia_a_dia(entorno):
    m = _mensaje(entorno)
    assert "[OK] mar  easy 8km -> 8km (100%)" in m
    # Sin vueltas guardadas el jueves se compara contra los 10km reales, que
    # incluyen la recuperacion trotada: lee 116.3% sin que el atleta se haya
    # desviado. Por eso la banda llega hasta 120%.
    assert "[OK] jue  series 8.6km -> 10km (116.3%)" in m
    assert "[OK] dom  long 16km -> 16.1km (100.6%)" in m
    assert "[OK] mie  fuerza cumplida" in m
    assert "[.] lun  descanso" in m


def test_con_vueltas_el_jueves_se_compara_declarado_contra_declarado(entorno):
    """El circulo completo: vueltas en la base -> reporte con la cifra limpia.

    La linea sigue diciendo los km trotados de recuperacion; sin eso el dia a
    dia parece contradecir el volumen de la semana, que si los cuenta.
    """
    repo, _ = entorno
    repo.guardar_vueltas("strava", "1003", VUELTAS_SERIES)

    m = _mensaje(entorno)

    assert "[OK] jue  series 8.6km -> 8.6km (100%) +1.4km rec" in m
    assert "43.7 km reales" in m  # el volumen sigue contando los 10km del jueves


def test_mensaje_cabe_en_un_solo_envio(entorno):
    from sport_report.telegram.formato import LIMITE_TELEGRAM, trozos

    m = _mensaje(entorno)
    assert len(trozos(m)) == 1 and len(m) < LIMITE_TELEGRAM


def test_formateo_de_dato_faltante():
    datos = {
        "semana": {"inicio": "2026-09-07", "fin": "2026-09-13", "numero_plan": 5},
        "volumen": {"planificado_km": 8.0, "real_km": 0.0, "pct": 0.0},
        "carga": {"semanal": 0.0, "impreciso": False},
        "acwr": {"ratio": None, "aguda": 0.0, "cronica": 0.0, "confiable": False, "motivo": "sin datos"},
        "monotony": {"monotony": None, "strain": None, "confiable": False, "motivo": "sin carga"},
        "deriva_cardiaca": {"promedio_pct": None, "maximo_pct": None, "n_sesiones": 0},
        "cadencia": {"valor": None, "delta": None},
        "adherencia": {
            "dias": [
                {
                    "dia": "M", "tipo_plan": "easy", "objetivo": 8.0, "unidad": "km",
                    "real": None, "pct": None, "estado": "dato_faltante", "nota": "",
                }
            ],
            "pct_global": None, "sesiones_cumplidas": 0, "sesiones_evaluables": 0,
            "fuerza": {"planificadas": 0, "cumplidas": 0},
        },
        "alertas": [],
        "avisos_datos": ["una sesion sin distancia registrada"],
    }
    m = formatear_reporte(datos, None)
    assert "[?] mar  easy 8km -> sin dato" in m
    assert "SOBRE LOS DATOS" in m
    assert "ACWR s/d" in m
    assert "no confiable: sin datos" in m


def test_formateo_de_alertas():
    datos = {
        "semana": {"inicio": "2026-09-07", "fin": "2026-09-13", "numero_plan": None},
        "volumen": {"planificado_km": 0.0, "real_km": 10.0, "pct": None},
        "carga": {"semanal": 100.0, "impreciso": True},
        "acwr": {"ratio": 1.8, "aguda": 90.0, "cronica": 50.0, "confiable": True, "motivo": ""},
        "monotony": {"monotony": 2.5, "strain": 250.0, "confiable": True, "motivo": ""},
        "deriva_cardiaca": {"promedio_pct": 6.0, "maximo_pct": 8.0, "n_sesiones": 2},
        "cadencia": {"valor": 170.0, "delta": -3.0},
        "adherencia": {"dias": [], "pct_global": None, "fuerza": {"planificadas": 0, "cumplidas": 0}},
        "alertas": ["ACWR 1.8 alto", "Monotony 2.5 alta"],
        "avisos_datos": [],
    }
    m = formatear_reporte(datos, "resumen del modelo")
    assert m.index("resumen del modelo") < m.index("VOLUMEN")
    assert "! ACWR 1.8 alto" in m
    assert "carga semanal 100 (imprecisa)" in m
    assert "(-3 vs semana anterior)" in m


def test_una_cifra_mal_atribuida_avisa_en_el_reporte(entorno):
    """El texto igual se manda —el reporte tiene que llegar— pero el atleta
    tiene que enterarse de que no se fie de ese numero."""
    repo, store = entorno
    buzon = Buzon()

    def narrador_confundido(datos):
        # Pasa por `redactar` de verdad: lo que se prueba es la cadena entera
        # (verificacion -> Narrativa -> ejecutar -> avisos -> mensaje), no que
        # `ejecutar` sepa leer un campo.
        real = datos["monotony"]["monotony"]
        cliente = ClienteFalso(Respuesta(f"Tu ACWR de la semana fue {real}."))
        return redactar(datos, cliente=cliente)

    r = ejecutar(SEMANA, repo, store, narrador=narrador_confundido, enviador=buzon, guardar=False)

    assert r.enviado
    assert "atribuye mal una cifra" in buzon.mensajes[0]
    assert "SOBRE LOS DATOS" in buzon.mensajes[0]
    # El texto del modelo sigue en el mensaje: no se descarta.
    assert "Tu ACWR de la semana fue" in buzon.mensajes[0]


def test_una_narrativa_correcta_no_agrega_ese_aviso(entorno):
    repo, store = entorno
    buzon = Buzon()
    ejecutar(SEMANA, repo, store, narrador=narrador_ok, enviador=buzon, guardar=False)

    assert "atribuye mal" not in buzon.mensajes[0]
