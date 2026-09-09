"""Reporte mensual: agregados de 4-5 semanas, solo de carrera, DOS mensajes.

Cosas que se prueban con especial insistencia:

- **el alcance**: el mensual mide carrera y nada mas. Un mes con fuerza y bici
  no puede colar esos kilometros en el volumen mensual;
- **la idempotencia del disparo**: el mensual sale una vez al mes, y una
  corrida repetida, un `--semana` retroactivo o un reintento tras un fallo no
  pueden mandarlo dos veces;
- **la clasificacion de largos y calidad**: solo el plan dice que corrida fue
  un largo, un tempo, una serie o un fartlek, asi que sin plan no hay con que
  clasificar y el reporte lo dice en vez de adivinar;
- **el gatillo del segundo mensaje**: fatiga y dinamica solo se manda cuando el
  motor detecto una variacion relevante contra el mes anterior.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from sport_report import config, run_weekly
from sport_report.db.models import BienestarDia, SesionReal
from sport_report.db.repo import Repo
from sport_report.engine import mensual as m_mensual
from sport_report.fechas import mes_de, semana_de, semanas_del_mes
from sport_report.narrative.claude import Narrativa, verificar_atribucion
from sport_report.narrative.mensual import (
    METRICAS_MENSUALES,
    SISTEMA_MENSUAL_ACTIVIDAD,
    SISTEMA_MENSUAL_FISIOLOGIA,
    redactar_mensual_actividad,
    redactar_mensual_fisiologia,
)
from sport_report.plan.carrera import Carrera, CarreraStore
from sport_report.plan.grammar import parse_plan
from sport_report.plan.store import PlanStore
from sport_report.run_weekly import OK, PARCIAL, ejecutar, toca_mensual
from tests.test_grammar import PLAN_SPEC
from tests.test_narrative import ClienteFalso, Respuesta

SEPTIEMBRE = mes_de(date(2026, 9, 15))
# Los lunes de septiembre 2026: 7, 14, 21, 28.
LUNES_SEP = [s.inicio for s in semanas_del_mes(SEPTIEMBRE)]


@pytest.fixture()
def entorno(tmp_path):
    repo = Repo(tmp_path / "t.db")
    store = PlanStore(actual=tmp_path / "plan.json", archivo=tmp_path / "planes")
    yield repo, store
    repo.cerrar()


def corrida(repo: Repo, fecha: date, km: float = 10.0, **kw) -> None:
    base = dict(
        fuente="intervals",
        id_externo=f"i{fecha.isoformat()}-{km}-{kw.get('tipo', 'Run')}",
        fecha_utc=f"{fecha}T12:00:00+00:00",
        fecha_local=fecha.isoformat(),
        dia_semana="LMWJVSD"[fecha.weekday()],
        tipo="Run",
        es_fuerza=False,
        distancia_km=km,
        duracion_mov_s=int(km * 300),
        carga=km * 40,
        cadencia_spm=176.0,
        gct_ms=232.0,
        oscilacion_vertical_cm=8.4,
        ratio_vertical_pct=7.9,
    )
    base.update(kw)
    repo.guardar_sesion(SesionReal(**base))


def _semanas_con(repo: Repo, kms: list[float], **kw) -> None:
    """Una corrida por semana del mes, con los km dados."""
    for lunes, km in zip(LUNES_SEP, kms):
        corrida(repo, lunes + timedelta(days=1), km, **kw)


# --------------------------------------------------------------------------
# Alcance: solo carrera
# --------------------------------------------------------------------------


def test_el_mensual_declara_su_alcance(entorno):
    repo, store = entorno
    j = m_mensual.construir(SEPTIEMBRE, repo, store)
    assert j["mes"]["alcance"] == "solo carrera"
    assert set(j["mes"]["tipos_incluidos"]) == set(config.TIPOS_RUN)
    assert "solo carrera" in j["mes"]["nota_alcance"]
    assert "fuerza" in j["mes"]["nota_alcance"]


def test_la_fuerza_y_la_bici_no_entran_en_las_cifras(entorno):
    repo, store = entorno
    martes = LUNES_SEP[0] + timedelta(days=1)
    corrida(repo, martes, 10.0)
    corrida(repo, martes, 60.0, tipo="Ride", id_externo="bici")
    corrida(
        repo,
        martes,
        0.0,
        tipo="WeightTraining",
        es_fuerza=True,
        distancia_km=None,
        id_externo="fuerza",
    )

    j = m_mensual.construir(SEPTIEMBRE, repo, store)

    assert j["volumen"]["total_km"] == 10.0, "entraron km que no son de carrera"
    assert j["carga"]["corridas"] == 1
    assert j["volumen"]["por_semana"][0]["km"] == 10.0


def test_la_cinta_si_cuenta_como_carrera(entorno):
    repo, store = entorno
    corrida(repo, LUNES_SEP[0] + timedelta(days=1), 8.0, tipo="VirtualRun")
    j = m_mensual.construir(SEPTIEMBRE, repo, store)
    assert j["volumen"]["total_km"] == 8.0


def test_la_adherencia_mensual_no_reporta_la_fuerza(entorno):
    repo, store = entorno
    store.guardar(parse_plan(PLAN_SPEC), semana_de(LUNES_SEP[0]))
    j = m_mensual.construir(SEPTIEMBRE, repo, store)
    assert "fuerza" not in j["adherencia"]
    assert "la fuerza no entra" in j["adherencia"]["nota"]


# --------------------------------------------------------------------------
# Agregados
# --------------------------------------------------------------------------


def test_el_volumen_trae_la_serie_y_lo_derivable_ya_calculado(entorno):
    """La serie va al prompt: si el modelo tuviera que derivar, inventaria."""
    repo, store = entorno
    _semanas_con(repo, [30.0, 35.0, 40.0, 45.0])

    v = m_mensual.construir(SEPTIEMBRE, repo, store)["volumen"]

    assert v["total_km"] == 150.0
    assert [s["km"] for s in v["por_semana"]] == [30.0, 35.0, 40.0, 45.0]
    assert v["promedio_km"] == 37.5
    assert v["progresion_pct"] == 50.0  # de 30 a 45
    assert v["semanas_al_alza"] == 3
    assert v["confiable"] is True


def test_el_volumen_se_compara_contra_el_mes_anterior(entorno):
    repo, store = entorno
    _semanas_con(repo, [30.0, 30.0, 30.0, 30.0])
    corrida(repo, date(2026, 8, 11), 50.0)  # agosto

    v = m_mensual.construir(SEPTIEMBRE, repo, store)["volumen"]
    assert v["mes_anterior_km"] == 50.0
    assert v["delta_km"] == 70.0


def test_un_mes_con_pocas_semanas_no_reporta_progresion(entorno):
    """Dos semanas con datos no son una progresion mensual."""
    repo, store = entorno
    _semanas_con(repo, [30.0, 35.0])

    v = m_mensual.construir(SEPTIEMBRE, repo, store)["volumen"]
    assert v["progresion_pct"] is None
    assert v["confiable"] is False
    assert "no alcanza para hablar de progresion" in v["motivo"]


def test_el_acwr_del_mes_es_la_serie_no_el_dato_de_una_semana(entorno):
    repo, store = entorno
    _semanas_con(repo, [30.0, 35.0, 40.0, 45.0])

    a = m_mensual.construir(SEPTIEMBRE, repo, store)["acwr"]
    assert len(a["por_semana"]) == 4
    assert all("lunes" in x and "confiable" in x for x in a["por_semana"])


def test_sin_historico_el_acwr_mensual_no_es_confiable(entorno):
    repo, store = entorno
    _semanas_con(repo, [30.0, 35.0])

    a = m_mensual.construir(SEPTIEMBRE, repo, store)["acwr"]
    assert a["promedio"] is None
    assert a["confiable"] is False
    assert "confiable" in a["motivo"]


def test_una_semana_sin_plan_no_cuenta_como_cero_de_adherencia(entorno):
    repo, store = entorno
    store.guardar(parse_plan(PLAN_SPEC), semana_de(LUNES_SEP[0]))
    _semanas_con(repo, [30.0, 35.0, 40.0, 45.0])

    adh = m_mensual.construir(SEPTIEMBRE, repo, store)["adherencia"]
    assert adh["semanas_con_plan"] == 1
    assert adh["semanas_sin_plan"] == 3
    assert [x["pct"] for x in adh["por_semana"]][1:] == [None, None, None]


def test_sin_ninguna_semana_con_plan_la_adherencia_no_es_confiable(entorno):
    repo, store = entorno
    _semanas_con(repo, [30.0, 35.0])
    adh = m_mensual.construir(SEPTIEMBRE, repo, store)["adherencia"]
    assert adh["promedio_pct"] is None and adh["confiable"] is False


def test_la_dinamica_compara_mes_contra_mes_anterior(entorno):
    repo, store = entorno
    _semanas_con(repo, [30.0, 35.0])
    corrida(repo, date(2026, 8, 11), 30.0, gct_ms=240.0)

    d = m_mensual.construir(SEPTIEMBRE, repo, store)["dinamica"]
    assert d["gct"]["valor"] == 232.0
    assert d["gct"]["mes_anterior"] == 240.0
    assert d["gct"]["delta"] == -8.0
    assert d["gct"]["unidad"] == "ms"
    for clave in ("cadencia", "gct", "oscilacion_vertical", "ratio_vertical"):
        assert clave in d


def test_sin_dinamica_en_el_mes_se_dice_en_vez_de_poner_cero(entorno):
    repo, store = entorno
    _semanas_con(repo, [30.0, 35.0], gct_ms=None)

    d = m_mensual.construir(SEPTIEMBRE, repo, store)["dinamica"]
    assert d["gct"]["valor"] is None
    assert d["gct"]["disponible"] is False
    assert "ninguna de las" in d["gct"]["motivo"]


def test_las_semanas_por_respaldo_se_listan_y_se_avisan(entorno):
    repo, store = entorno
    _semanas_con(repo, [30.0, 35.0])
    repo.guardar_fuente_semana(LUNES_SEP[0].isoformat(), "intervals", False)
    repo.guardar_fuente_semana(LUNES_SEP[1].isoformat(), "strava", True, "401")

    j = m_mensual.construir(SEPTIEMBRE, repo, store)

    assert [f["fallback"] for f in j["fuentes_usadas"]] == [False, True]
    assert any("respaldo" in a for a in j["avisos_datos"])
    assert j["dinamica"]["semanas_con_dinamica"] == 3


def test_las_semanas_a_caballo_se_declaran(entorno):
    """Una semana partida entre dos meses no se puede repartir sin inventar."""
    repo, store = entorno
    j = m_mensual.construir(SEPTIEMBRE, repo, store)
    # La del 28 de septiembre termina el 4 de octubre.
    assert j["mes"]["semanas_incompletas"] == ["2026-09-28"]


def test_un_mes_sin_corridas_lo_dice(entorno):
    repo, store = entorno
    j = m_mensual.construir(SEPTIEMBRE, repo, store)
    assert j["volumen"]["total_km"] == 0.0
    assert any("ninguna corrida" in a for a in j["avisos_datos"])


def test_el_bienestar_va_como_contexto_etiquetado(entorno):
    repo, store = entorno
    _semanas_con(repo, [30.0, 35.0])
    for i in range(10):
        repo.guardar_bienestar(
            [
                BienestarDia(
                    fecha_local=(SEPTIEMBRE.inicio + timedelta(days=i)).isoformat(),
                    fuente="intervals",
                    hrv=64.0,
                )
            ]
        )
    f = m_mensual.construir(SEPTIEMBRE, repo, store)["fatiga_descanso"]
    assert f["hrv"]["valor"] == 64.0
    assert "cuerpo completo" in f["nota"]
    # Y sigue sin existir un indice que fusione bienestar y carga.
    assert set(f["cruce_carga"]) == {
        "acwr", "acwr_confiable", "monotony", "monotony_confiable", "lectura",
        "carga_texto", "nota",
    }


def test_el_contexto_de_la_carrera_entra_si_hay_una(entorno):
    repo, store = entorno
    c = Carrera(fecha=date(2026, 11, 15), nombre="Maraton")
    j = m_mensual.construir(SEPTIEMBRE, repo, store, carrera=c)
    assert j["carrera"]["nombre"] == "Maraton"
    assert "semanas_restantes" in j["carrera"]


def test_sin_carrera_declarada_el_bloque_es_null(entorno):
    repo, store = entorno
    assert m_mensual.construir(SEPTIEMBRE, repo, store)["carrera"] is None


def test_las_alertas_del_mes_miran_el_maximo(entorno):
    repo, store = entorno
    # Una semana con mucha carga y las demas vacias dispara Monotony alto.
    for i in range(40):
        corrida(repo, SEPTIEMBRE.inicio - timedelta(days=40) + timedelta(days=i), 8.0)
    corrida(repo, LUNES_SEP[1] + timedelta(days=1), 40.0)

    j = m_mensual.construir(SEPTIEMBRE, repo, store)
    assert isinstance(j["alertas"], list)
    assert j["version"] == 2


# --------------------------------------------------------------------------
# Largos y calidad: solo el plan dice que fue cada corrida
# --------------------------------------------------------------------------


def test_el_largo_de_la_semana_sale_del_plan(entorno):
    repo, store = entorno
    store.guardar(parse_plan(PLAN_SPEC), semana_de(LUNES_SEP[0]))
    domingo = LUNES_SEP[0] + timedelta(days=6)  # PLAN_SPEC: D -> long 16km
    corrida(repo, domingo, 16.0)

    largos = m_mensual.construir(SEPTIEMBRE, repo, store)["largos"]
    assert largos["por_semana"][0] == {"lunes": "2026-09-07", "km": 16.0}
    assert largos["por_semana"][1]["km"] is None, "sin plan esa semana no hay largo"
    assert largos["semanas_con_largo"] == 1
    assert largos["promedio_km"] == 16.0
    assert largos["maximo_km"] == 16.0
    assert largos["disponible"] is True


def test_sin_ningun_plan_el_largo_no_se_puede_clasificar(entorno):
    repo, store = entorno
    _semanas_con(repo, [30.0, 35.0])

    largos = m_mensual.construir(SEPTIEMBRE, repo, store)["largos"]
    assert largos["disponible"] is False
    assert all(s["km"] is None for s in largos["por_semana"])
    assert "no habia plan" in largos["motivo"] or "prescrito" in largos["motivo"]


def test_la_calidad_cuenta_las_sesiones_de_series_del_plan(entorno):
    repo, store = entorno
    store.guardar(parse_plan(PLAN_SPEC), semana_de(LUNES_SEP[0]))
    jueves = LUNES_SEP[0] + timedelta(days=3)  # PLAN_SPEC: J -> series 8.6km
    corrida(repo, jueves, 9.0)  # el real trae la recuperacion, el plan no

    calidad = m_mensual.construir(SEPTIEMBRE, repo, store)["calidad"]
    assert calidad["series_km"] == 9.0
    assert calidad["tempo_km"] == 0.0
    assert calidad["fartlek_km"] == 0.0
    assert calidad["n_sesiones"] == 1
    assert calidad["km_total"] == 9.0
    assert calidad["pct_volumen_total"] == 100.0  # unica corrida del mes
    assert calidad["disponible"] is True


def test_dias_sin_corrida_no_cuentan_como_sesion_de_calidad(entorno):
    """El plan prescribe series el jueves, pero esa semana no se corrio nada."""
    repo, store = entorno
    store.guardar(parse_plan(PLAN_SPEC), semana_de(LUNES_SEP[0]))
    calidad = m_mensual.construir(SEPTIEMBRE, repo, store)["calidad"]
    assert calidad["n_sesiones"] == 0
    assert calidad["km_total"] == 0.0


def test_sin_ningun_plan_la_calidad_no_es_confiable(entorno):
    repo, store = entorno
    _semanas_con(repo, [30.0, 35.0])
    calidad = m_mensual.construir(SEPTIEMBRE, repo, store)["calidad"]
    assert calidad["disponible"] is False
    assert "no se pudo" in calidad["motivo"]


# --------------------------------------------------------------------------
# El gatillo del segundo mensaje (fatiga y dinamica)
# --------------------------------------------------------------------------


def test_sin_variaciones_el_segundo_mensaje_no_es_relevante(entorno):
    repo, store = entorno
    _semanas_con(repo, [30.0, 35.0, 40.0, 45.0])
    j = m_mensual.construir(SEPTIEMBRE, repo, store)
    assert j["revisar_fatiga_dinamica"] == {"relevante": False, "motivos": []}


def test_un_cambio_de_dinamica_dispara_el_segundo_mensaje(entorno):
    repo, store = entorno
    _semanas_con(repo, [30.0, 35.0])
    # El mes anterior tenia GCT 240.0; este mes es 232.0 (default de `corrida`):
    # delta -8.0, justo en el umbral (config.UMBRALES.gct_cambio_ms == 8.0).
    corrida(repo, date(2026, 8, 11), 30.0, gct_ms=240.0)

    j = m_mensual.construir(SEPTIEMBRE, repo, store)
    assert j["revisar_fatiga_dinamica"]["relevante"] is True
    assert any("GCT" in m for m in j["revisar_fatiga_dinamica"]["motivos"])


def test_una_alerta_de_acwr_o_monotony_dispara_el_segundo_mensaje(entorno):
    repo, store = entorno
    for i in range(40):
        corrida(repo, SEPTIEMBRE.inicio - timedelta(days=40) + timedelta(days=i), 8.0)
    corrida(repo, LUNES_SEP[1] + timedelta(days=1), 40.0)

    j = m_mensual.construir(SEPTIEMBRE, repo, store)
    if j["alertas"]:
        assert j["revisar_fatiga_dinamica"]["relevante"] is True
        assert set(j["alertas"]) <= set(j["revisar_fatiga_dinamica"]["motivos"])


# --------------------------------------------------------------------------
# Cuando toca el mensual
# --------------------------------------------------------------------------


def test_toca_en_la_primera_corrida_del_mes_nuevo():
    """El 5 de octubre se reporta septiembre."""
    mes = toca_mensual(hoy=date(2026, 10, 5), hecho=lambda m: False)
    assert mes is not None and mes.clave == "2026-09"


def test_no_se_repite_si_el_archivo_del_mes_ya_existe():
    """La guarda que hace la decision idempotente."""
    assert toca_mensual(hoy=date(2026, 10, 5), hecho=lambda m: True) is None


def test_forzar_lo_manda_aunque_ya_se_haya_enviado():
    mes = toca_mensual(hoy=date(2026, 10, 5), forzar=True, hecho=lambda m: True)
    assert mes is not None and mes.clave == "2026-09"


def test_a_mitad_de_mes_apunta_al_mes_anterior_pero_ya_esta_hecho():
    """El 19 de octubre el mensual de septiembre ya se envio el dia 5."""
    assert toca_mensual(hoy=date(2026, 10, 19), hecho=lambda m: True) is None
    # Y si por lo que sea no se envio, se manda: mejor tarde que nunca.
    mes = toca_mensual(hoy=date(2026, 10, 19), hecho=lambda m: False)
    assert mes.clave == "2026-09"


def test_en_enero_el_mensual_es_de_diciembre():
    mes = toca_mensual(hoy=date(2027, 1, 4), hecho=lambda m: False)
    assert mes.clave == "2026-12"


def test_la_ruta_del_mensual_sale_de_data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    assert run_weekly.ruta_mensual(SEPTIEMBRE) == tmp_path / "reportes" / "mensual" / "2026-09.json"


# --------------------------------------------------------------------------
# Encadenado con la corrida semanal
# --------------------------------------------------------------------------


class Buzon:
    def __init__(self, ok=True):
        self.ok = ok
        self.mensajes: list[str] = []

    def __call__(self, texto: str) -> bool:
        self.mensajes.append(texto)
        return self.ok


def _entorno_semana(tmp_path):
    repo = Repo(tmp_path / "t.db")
    store = PlanStore(actual=tmp_path / "plan.json", archivo=tmp_path / "planes")
    semana = semana_de(LUNES_SEP[3])
    store.guardar(parse_plan(PLAN_SPEC), semana)
    for lunes, km in zip(LUNES_SEP, [30.0, 35.0, 40.0, 45.0]):
        corrida(repo, lunes + timedelta(days=1), km)
    return repo, store, semana


def _narrador_mensual(texto="Mes solido, solo carrera."):
    return lambda datos: Narrativa(texto=texto, modelo="claude-sonnet-5")


def test_el_mensual_va_en_un_mensaje_aparte_sin_grafico(tmp_path):
    """Sin variaciones relevantes, el mensual son SEMANAL + ACTIVIDAD: dos mensajes."""
    repo, store, semana = _entorno_semana(tmp_path)
    buzon = Buzon()
    fotos = []

    r = ejecutar(
        semana,
        repo,
        store,
        narrador=None,
        enviador=buzon,
        enviador_foto=lambda ruta, caption="": fotos.append(ruta) or True,
        guardar=False,
        mensual=SEPTIEMBRE,
        narrador_mensual_actividad=_narrador_mensual(),
        carrera_store=CarreraStore(ruta=tmp_path / "carrera.json"),
    )
    repo.cerrar()

    assert r.estado == OK
    assert not r.datos_mensuales["revisar_fatiga_dinamica"]["relevante"]
    assert len(buzon.mensajes) == 2, "el mensual tiene que ir en su propio mensaje"
    assert "REPORTE SEMANAL" in buzon.mensajes[0]
    assert "REPORTE MENSUAL" in buzon.mensajes[1]
    assert "solo carrera" in buzon.mensajes[1]
    # Y sin datos crudos: el mensual es solo narrado.
    assert "ADHERENCIA" not in buzon.mensajes[1]
    assert "ACWR" not in buzon.mensajes[1]
    # Un solo grafico, el del semanal.
    assert len(fotos) == 1
    assert r.mensual == "2026-09"


def test_con_variaciones_relevantes_va_un_tercer_mensaje(tmp_path):
    repo, store, semana = _entorno_semana(tmp_path)
    corrida(repo, date(2026, 8, 11), 30.0, gct_ms=240.0)  # dispara el gatillo de GCT
    buzon = Buzon()

    r = ejecutar(
        semana,
        repo,
        store,
        narrador=None,
        enviador=buzon,
        guardar=False,
        mensual=SEPTIEMBRE,
        narrador_mensual_actividad=_narrador_mensual("Actividad del mes."),
        narrador_mensual_fisiologia=_narrador_mensual("Fatiga y dinamica del mes."),
        carrera_store=CarreraStore(ruta=tmp_path / "carrera.json"),
    )
    repo.cerrar()

    assert r.datos_mensuales["revisar_fatiga_dinamica"]["relevante"] is True
    assert len(buzon.mensajes) == 3
    assert "REPORTE MENSUAL" in buzon.mensajes[1] and "Actividad del mes." in buzon.mensajes[1]
    assert "fatiga y dinamica" in buzon.mensajes[2]
    assert "Fatiga y dinamica del mes." in buzon.mensajes[2]


def test_sin_variaciones_relevantes_no_se_llama_al_narrador_de_fisiologia(tmp_path):
    """El motor ya decidio que no hace falta: no vale la pena gastar la llamada."""
    repo, store, semana = _entorno_semana(tmp_path)
    llamado = []

    r = ejecutar(
        semana,
        repo,
        store,
        narrador=None,
        enviador=Buzon(),
        guardar=False,
        mensual=SEPTIEMBRE,
        narrador_mensual_actividad=_narrador_mensual(),
        narrador_mensual_fisiologia=lambda d: llamado.append(1) or Narrativa(texto="x"),
        carrera_store=CarreraStore(ruta=tmp_path / "carrera.json"),
    )
    repo.cerrar()

    assert not r.datos_mensuales["revisar_fatiga_dinamica"]["relevante"]
    assert llamado == []


def test_sin_mensual_solo_va_el_semanal(tmp_path):
    repo, store, semana = _entorno_semana(tmp_path)
    buzon = Buzon()
    r = ejecutar(
        semana, repo, store, narrador=None, enviador=buzon, guardar=False, mensual=None
    )
    repo.cerrar()
    assert len(buzon.mensajes) == 1 and r.mensual == ""


def test_el_json_mensual_se_guarda_en_disco(tmp_path, monkeypatch):
    repo, store, semana = _entorno_semana(tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "salida")

    ejecutar(
        semana,
        repo,
        store,
        narrador=None,
        enviador=Buzon(),
        guardar=True,
        mensual=SEPTIEMBRE,
        narrador_mensual_actividad=_narrador_mensual(),
        carrera_store=CarreraStore(ruta=tmp_path / "carrera.json"),
    )
    repo.cerrar()

    ruta = tmp_path / "salida" / "reportes" / "mensual" / "2026-09.json"
    assert ruta.exists()
    d = json.loads(ruta.read_text(encoding="utf-8"))
    assert d["mes"]["clave"] == "2026-09"
    assert d["narrativa"]["texto"] == "Mes solido, solo carrera."


def test_si_la_narrativa_mensual_falla_el_semanal_ya_salio(tmp_path):
    repo, store, semana = _entorno_semana(tmp_path)
    buzon = Buzon()

    r = ejecutar(
        semana,
        repo,
        store,
        narrador=None,
        enviador=buzon,
        guardar=False,
        mensual=SEPTIEMBRE,
        narrador_mensual_actividad=lambda d: Narrativa(texto=None, error="APIConnectionError"),
        carrera_store=CarreraStore(ruta=tmp_path / "carrera.json"),
    )
    repo.cerrar()

    assert r.estado == PARCIAL
    assert len(buzon.mensajes) == 1, "el semanal tiene que haber llegado igual"
    assert any("mensual sin narrativa" in p for p in r.problemas)


def test_si_el_motor_mensual_revienta_el_semanal_no_cae(tmp_path, monkeypatch):
    repo, store, semana = _entorno_semana(tmp_path)
    monkeypatch.setattr(
        m_mensual, "construir", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    buzon = Buzon()

    r = ejecutar(
        semana, repo, store, narrador=None, enviador=buzon, guardar=False,
        mensual=SEPTIEMBRE, narrador_mensual_actividad=_narrador_mensual(),
    )
    repo.cerrar()

    assert r.estado == PARCIAL and r.enviado
    assert len(buzon.mensajes) == 1
    assert any("boom" in p for p in r.problemas)


def test_si_el_semanal_no_se_pudo_enviar_no_se_manda_el_mensual(tmp_path):
    """Con Telegram caido, insistir con un segundo mensaje no ayuda."""
    repo, store, semana = _entorno_semana(tmp_path)
    r = ejecutar(
        semana, repo, store, narrador=None, enviador=Buzon(ok=False), guardar=False,
        mensual=SEPTIEMBRE, narrador_mensual_actividad=_narrador_mensual(),
    )
    repo.cerrar()
    assert r.datos_mensuales == {}


# --------------------------------------------------------------------------
# La narrativa mensual
# --------------------------------------------------------------------------


MENSUAL_JSON = {
    "mes": {"clave": "2026-09", "semanas": 4, "alcance": "solo carrera"},
    "volumen": {"total_km": 150.0, "promedio_km": 37.5, "progresion_pct": 50.0,
                "semanas_al_alza": 3, "mes_anterior_km": 120.0, "delta_km": 30.0,
                "por_semana": [{"lunes": "2026-09-07", "km": 30.0}]},
    "acwr": {"promedio": 1.1, "maximo": 1.3, "confiable": True, "motivo": ""},
    "monotony": {"promedio": 1.5, "maximo": 1.8, "confiable": True, "motivo": ""},
    "adherencia": {"promedio_pct": 92.0, "semanas_con_plan": 4, "semanas_sin_plan": 0},
    "largos": {"promedio_km": 17.5, "maximo_km": 18.0, "semanas_con_largo": 4,
               "disponible": True, "motivo": "",
               "por_semana": [{"lunes": "2026-09-07", "km": 17.0}]},
    "calidad": {"tempo_km": 12.0, "series_km": 8.6, "fartlek_km": 0.0, "km_total": 20.6,
                "n_sesiones": 3, "pct_volumen_total": 13.7, "semanas_sin_plan": 0,
                "disponible": True, "motivo": ""},
    "dinamica": {
        "cadencia": {"valor": 176.0, "mes_anterior": 174.0, "delta": 2.0, "unidad": "spm"},
        "gct": {"valor": 232.0, "mes_anterior": 240.0, "delta": -8.0, "unidad": "ms"},
        "oscilacion_vertical": {"valor": 8.4, "mes_anterior": 8.1, "delta": 0.3, "unidad": "cm"},
        "ratio_vertical": {"valor": 7.9, "mes_anterior": 7.6, "delta": 0.3, "unidad": "%"},
    },
    "fatiga_descanso": {"hrv": {"valor": 64.0, "semana_anterior": 70.0, "delta": -6.0}},
    "carrera": {"fecha": "2026-11-15", "nombre": "Maraton", "semanas_restantes": 6,
                "fase": "construccion", "nota": "a 6 semanas"},
    "revisar_fatiga_dinamica": {"relevante": True, "motivos": ["GCT cambio -8 ms"]},
    "alertas": [],
    "avisos_datos": [],
    "umbrales": {"acwr_alto": 1.5, "acwr_bajo": 0.8, "monotony_alta": 2.0,
                 "cadencia_cambio_spm": 3.0, "gct_cambio_ms": 8.0,
                 "oscilacion_vertical_cambio_cm": 0.5, "ratio_vertical_cambio_pct": 0.5},
}


def test_el_prompt_de_actividad_declara_el_alcance_y_las_prohibiciones():
    assert "SOLO CARRERA" in SISTEMA_MENSUAL_ACTIVIDAD
    assert "en la primera" in SISTEMA_MENSUAL_ACTIVIDAD and "linea" in SISTEMA_MENSUAL_ACTIVIDAD
    assert "no calcules" in SISTEMA_MENSUAL_ACTIVIDAD.lower()
    assert "6 a 10 lineas" in SISTEMA_MENSUAL_ACTIVIDAD
    # El primer mensaje no habla de fatiga ni de dinamica: eso va en el otro.
    assert "no hables de fatiga" in SISTEMA_MENSUAL_ACTIVIDAD.lower()


def test_el_prompt_de_fisiologia_solo_se_manda_cuando_hay_variacion():
    assert "SOLO CARRERA" in SISTEMA_MENSUAL_FISIOLOGIA
    assert "NUNCA combines el bienestar" in SISTEMA_MENSUAL_FISIOLOGIA
    assert "revisar_fatiga_dinamica" in SISTEMA_MENSUAL_FISIOLOGIA
    assert "5 a 8 lineas" in SISTEMA_MENSUAL_FISIOLOGIA


def test_los_prompts_mensuales_dicen_que_la_serie_ya_viene_derivada():
    """Es lo que evita que el modelo calcule la progresion por su cuenta."""
    for sistema in (SISTEMA_MENSUAL_ACTIVIDAD, SISTEMA_MENSUAL_FISIOLOGIA):
        assert "ya esta calculado" in sistema
        assert "citar e interpretar" in sistema


def test_la_narrativa_de_actividad_usa_el_modelo_configurado():
    cliente = ClienteFalso(Respuesta("Mes de solo carrera: 150.0 km."))
    n = redactar_mensual_actividad(MENSUAL_JSON, cliente=cliente)
    assert n.ok
    assert n.modelo == config.ANTHROPIC_MODEL_MENSUAL == "claude-sonnet-5"
    assert cliente.llamadas[0]["model"] == "claude-sonnet-5"


def test_la_narrativa_de_actividad_manda_la_serie_semanal():
    """Al reves que el semanal: aca la serie ES el contenido."""
    cliente = ClienteFalso(Respuesta("ok"))
    redactar_mensual_actividad(MENSUAL_JSON, cliente=cliente)
    cuerpo = cliente.llamadas[0]["messages"][0]["content"]
    assert "por_semana" in cuerpo and "2026-09-07" in cuerpo


def test_una_cifra_inventada_en_la_actividad_se_detecta():
    cliente = ClienteFalso(Respuesta("Corriste 999.9 km en el mes."))
    n = redactar_mensual_actividad(MENSUAL_JSON, cliente=cliente)
    assert n.ok  # el texto no se descarta
    assert "999.9" in " ".join(n.numeros_no_verificados)


def test_una_cifra_mal_atribuida_en_la_actividad_se_detecta():
    """1.8 es el maximo de Monotony, no un valor de ACWR.

    (1.5 no serviria de ejemplo: es el umbral acwr_alto, y por lo tanto una
    cifra legitima al lado de la palabra ACWR.)
    """
    cliente = ClienteFalso(Respuesta("El ACWR promedio del mes fue 1.8."))
    n = redactar_mensual_actividad(MENSUAL_JSON, cliente=cliente)
    assert n.cifras_mal_atribuidas
    assert n.cifras_mal_atribuidas[0].startswith("ACWR")


def test_la_narrativa_de_fisiologia_usa_el_modelo_configurado():
    cliente = ClienteFalso(Respuesta("El GCT bajo 8.0 ms este mes."))
    n = redactar_mensual_fisiologia(MENSUAL_JSON, cliente=cliente)
    assert n.ok
    assert n.modelo == config.ANTHROPIC_MODEL_MENSUAL


def test_una_cifra_inventada_en_la_fisiologia_se_detecta():
    cliente = ClienteFalso(Respuesta("El HRV bajo hasta 111.1."))
    n = redactar_mensual_fisiologia(MENSUAL_JSON, cliente=cliente)
    assert "111.1" in " ".join(n.numeros_no_verificados)


def test_las_rutas_mensuales_son_las_del_json_mensual():
    """El mismo mecanismo del semanal, con otras rutas."""
    assert verificar_atribucion(
        "El ACWR promedio fue 1.1 y el maximo 1.3.", MENSUAL_JSON, METRICAS_MENSUALES
    ) == []
    assert verificar_atribucion(
        "El volumen del mes fue 150.0 km, 30.0 mas que el mes anterior.",
        MENSUAL_JSON,
        METRICAS_MENSUALES,
    ) == []
    assert verificar_atribucion(
        "El GCT bajo a 232.0 ms.", MENSUAL_JSON, METRICAS_MENSUALES
    ) == []


def test_citar_el_largo_y_la_calidad_es_legitimo():
    assert verificar_atribucion(
        "El largo promedio fue 17.5 km, con un maximo de 18.0.",
        MENSUAL_JSON,
        METRICAS_MENSUALES,
    ) == []
    assert verificar_atribucion(
        "Hiciste 3 sesiones de calidad por 20.6 km, un 13.7% del volumen.",
        MENSUAL_JSON,
        METRICAS_MENSUALES,
    ) == []


def test_citar_las_semanas_restantes_de_la_carrera_es_legitimo():
    assert verificar_atribucion(
        "Faltan 6 semanas para la carrera.", MENSUAL_JSON, METRICAS_MENSUALES
    ) == []
    assert verificar_atribucion(
        "Faltan 3 semanas para la carrera.", MENSUAL_JSON, METRICAS_MENSUALES
    )


def test_la_narrativa_mensual_nunca_lanza():
    class ClienteRoto:
        class messages:
            @staticmethod
            def create(**kw):
                raise RuntimeError("500")

    n = redactar_mensual_actividad(MENSUAL_JSON, cliente=ClienteRoto())
    assert n.ok is False and "500" in n.error
    n2 = redactar_mensual_fisiologia(MENSUAL_JSON, cliente=ClienteRoto())
    assert n2.ok is False and "500" in n2.error


def test_un_rechazo_del_modelo_no_es_un_texto_vacio():
    cliente = ClienteFalso(Respuesta("", stop_reason="refusal"))
    n = redactar_mensual_actividad(MENSUAL_JSON, cliente=cliente)
    assert n.ok is False and "rechazo" in n.error


def test_el_modelo_puede_citar_el_rango_de_una_serie_semanal():
    """"El ACWR se movio entre 0.91 y 1.29 a lo largo del mes, con promedio 1.1"

    Frase real del 2026-09-09. La serie semanal entera va en el prompt mensual
    —es el contenido del reporte—, asi que citar el minimo de las cinco semanas
    es legitimo. Antes se reportaba como cifra mal atribuida, y ese aviso sube
    al reporte del atleta.
    """
    datos = {
        "acwr": {
            "promedio": 1.1,
            "maximo": 1.29,
            "por_semana": [
                {"lunes": "2026-08-03", "valor": 0.91, "confiable": True},
                {"lunes": "2026-08-10", "valor": 1.29, "confiable": True},
                {"lunes": "2026-08-17", "valor": None, "confiable": False},
            ],
        },
        "umbrales": {"acwr_alto": 1.5},
    }
    texto = "El ACWR se movio entre 0.91 y 1.29 a lo largo del mes, con promedio de 1.1."
    assert verificar_atribucion(texto, datos, METRICAS_MENSUALES) == []

    # Y una cifra que no esta ni en la serie ni en los agregados sigue saliendo.
    assert verificar_atribucion("El ACWR promedio fue 2.4.", datos, METRICAS_MENSUALES)


def test_una_serie_con_huecos_no_rompe_la_atribucion():
    """`valor: None` en una semana sin dato confiable es lo normal en el mensual."""
    datos = {"monotony": {"promedio": None, "maximo": None,
                          "por_semana": [{"lunes": "2026-08-03", "valor": None}]}}
    assert verificar_atribucion("La monotonia fue 0.9.", datos, METRICAS_MENSUALES)
