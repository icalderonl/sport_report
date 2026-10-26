"""Tests de la importacion desde TrainingPeaks (cliente, conversion y /tp), sin red."""
from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from sport_report import config
from sport_report.fechas import RangoSemana
from sport_report.plan.grammar import parse_plan
from sport_report.plan.store import PlanStore
from sport_report.telegram import comandos
from sport_report.trainingpeaks import verificar
from sport_report.trainingpeaks.client import TPClient, cookie, resumen_cookie
from sport_report.trainingpeaks.convertir import a_setplan
from sport_report.trainingpeaks.errors import TPAuthError, TPError

SEMANA = RangoSemana(date(2026, 10, 26), date(2026, 11, 1))
COOKIE = "cookie-de-prueba-xyz"


# --------------------------------------------------------------------------
# Dobles
# --------------------------------------------------------------------------


def _paso(valor, unidad="meter", clase="active", nombre="paso"):
    return {
        "name": nombre,
        "length": {"value": valor, "unit": unidad},
        "targets": [{"minValue": 95, "maxValue": 100}],
        "intensityClass": clase,
    }


def _bloque(*pasos, reps=1):
    return {
        "type": "repetition" if reps > 1 else "step",
        "length": {"value": reps, "unit": "repetition"},
        "steps": list(pasos),
    }


def _estructura(*bloques, metrica="percentOfThresholdPace"):
    return {
        "structure": list(bloques),
        "primaryLengthMetric": "distance",
        "primaryIntensityMetric": metrica,
    }


def _w(dia: str, tipo=3, titulo="", km=None, horas=None, estructura=None, **extra):
    w = {
        "workoutId": hash((dia, titulo)) & 0xFFFF,
        "workoutDay": f"{dia}T00:00:00",
        "workoutTypeValueId": tipo,
        "title": titulo,
        "distancePlanned": km * 1000 if km else None,
        "totalTimePlanned": horas,
        "structure": estructura,
        "description": extra.pop("description", None),
    }
    w.update(extra)
    return w


SERIES = _estructura(
    _bloque(_paso(2000, clase="warmUp")),
    _bloque(_paso(1000), _paso(400, clase="rest"), reps=3),
    _bloque(_paso(2000, clase="coolDown")),
)


def _api(entrenamientos, estado_token=200):
    def handler(request: httpx.Request) -> httpx.Response:
        ruta = request.url.path
        if ruta == "/users/v3/token":
            assert request.headers["cookie"] == f"Production_tpAuth={COOKIE}"
            if estado_token != 200:
                return httpx.Response(estado_token, text="no")
            return httpx.Response(200, json={"success": True, "token": {"access_token": "tok"}})
        assert request.headers["authorization"] == "Bearer tok"
        assert request.method == "GET"
        if ruta == "/users/v3/user":
            return httpx.Response(200, json={"user": {"personId": 777, "userId": 1}})
        if ruta.startswith("/fitness/v6/athletes/777/workouts/"):
            return httpx.Response(200, json=entrenamientos)
        return httpx.Response(404)

    return handler


def _cliente(entrenamientos=(), estado_token=200) -> TPClient:
    return TPClient(
        cookie_tp=COOKIE,
        http=httpx.Client(transport=httpx.MockTransport(_api(list(entrenamientos), estado_token))),
        base="https://tp.test",
    )


# --------------------------------------------------------------------------
# Cliente
# --------------------------------------------------------------------------


def test_la_cookie_se_cambia_por_token_y_se_leen_los_entrenamientos():
    cli = _cliente([_w("2026-10-27", titulo="Rodaje", km=8)])
    ws = cli.entrenamientos(SEMANA.inicio, SEMANA.fin)
    assert [w["title"] for w in ws] == ["Rodaje"]
    assert cli.atleta_id() == 777


@pytest.mark.parametrize("estado", [401, 403])
def test_cookie_rechazada_es_error_de_autenticacion(estado):
    with pytest.raises(TPAuthError):
        _cliente(estado_token=estado).entrenamientos(SEMANA.inicio, SEMANA.fin)


def test_token_vacio_con_200_es_error_de_autenticacion():
    cli = TPClient(
        cookie_tp=COOKIE,
        http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))),
        base="https://tp.test",
    )
    with pytest.raises(TPAuthError):
        cli.token()


def test_la_cookie_acepta_el_par_nombre_valor(monkeypatch):
    assert cookie(f"Production_tpAuth={COOKIE};") == COOKIE
    monkeypatch.setattr(config, "TP_AUTH_COOKIE", "")
    with pytest.raises(TPAuthError):
        cookie()


def test_resumen_de_cookie_no_la_muestra():
    r = resumen_cookie(COOKIE)
    assert COOKIE not in r and str(len(COOKIE)) in r


# --------------------------------------------------------------------------
# Conversion
# --------------------------------------------------------------------------


def test_semana_completa_pasa_la_gramatica():
    ws = [
        _w("2026-10-27", titulo="Rodaje suave", km=8),
        _w("2026-10-28", tipo=9, titulo="Gimnasio"),
        _w("2026-10-29", titulo="Series 3x1000", km=9.2, estructura=SERIES),
        _w("2026-10-30", titulo="Tempo", horas=0.75),
        _w("2026-11-01", titulo="Fondo largo", km=18),
    ]
    b = a_setplan(ws, SEMANA)
    plan = parse_plan(b.texto)

    assert plan.dia("L")[0].tipo == "rest"
    assert plan.dia("M")[0].tipo == "easy" and plan.dia("M")[0].cantidad == 8
    assert plan.dia("W")[0].es_fuerza
    j = plan.dia("J")[0]
    assert j.tipo == "series" and j.estructura.crudo == "2km+3x1000m+2km"
    assert j.objetivo_km() == 7.0  # sin la recuperacion
    assert plan.dia("V")[0].unidad == "min" and plan.dia("V")[0].cantidad == 45
    assert plan.dia("D")[0].tipo == "long"
    assert b.importados == 5


def test_sin_distancia_planificada_se_suma_la_estructura():
    b = a_setplan([_w("2026-10-29", titulo="Series", estructura=SERIES)], SEMANA)
    j = parse_plan(b.texto).dia("J")[0]
    assert j.cantidad == 8.2  # 2 + 3x(1 + 0.4) + 2


def test_tramos_por_tiempo_no_entran_en_estructura():
    est = _estructura(_bloque(_paso(120, unidad="second"), _paso(60, "second", "rest"), reps=6))
    b = a_setplan([_w("2026-10-29", titulo="Fartlek", horas=0.5, estructura=est)], SEMANA)
    j = parse_plan(b.texto).dia("J")[0]
    assert j.estructura is None and j.tipo == "easy" and j.cantidad == 30
    assert any("tiempo" in a for a in b.avisos)


def test_dos_tramos_de_trabajo_alternados_se_despliegan():
    est = _estructura(_bloque(_paso(400), _paso(200), reps=2))
    b = a_setplan([_w("2026-10-29", titulo="Pista", km=5, estructura=est)], SEMANA)
    assert "estructura=400m+200m+400m+200m" in b.texto
    parse_plan(b.texto)


def test_estructura_sin_repeticiones_queda_como_sesion_continua():
    est = _estructura(
        _bloque(_paso(2000, clase="warmUp")),
        _bloque(_paso(6000)),
        _bloque(_paso(2000, clase="coolDown")),
    )
    b = a_setplan([_w("2026-10-29", titulo="Umbral", km=10, estructura=est)], SEMANA)
    j = parse_plan(b.texto).dia("J")[0]
    assert j.tipo == "tempo" and j.estructura is None and j.cantidad == 10


def test_otros_deportes_se_omiten_con_aviso():
    b = a_setplan([_w("2026-10-27", tipo=2, titulo="Bici", horas=1)], SEMANA)
    assert parse_plan(b.texto).dia("M")[0].tipo == "rest"
    assert any("bici" in a for a in b.avisos)
    assert b.importados == 0


def test_actividades_completadas_sin_plan_no_cuentan():
    suelta = _w("2026-10-27", titulo="Corrida de la tarde", totalTime=0.8, distance=8000)
    assert a_setplan([suelta], SEMANA).importados == 0


def test_dias_fuera_de_la_semana_se_ignoran():
    b = a_setplan([_w("2026-11-02", titulo="Rodaje", km=8)], SEMANA)
    assert b.importados == 0


def test_corrida_y_fuerza_el_mismo_dia():
    ws = [_w("2026-10-27", titulo="Rodaje", km=6), _w("2026-10-27", tipo=9)]
    m = parse_plan(a_setplan(ws, SEMANA).texto).dia("M")
    assert [s.tipo for s in m] == ["easy", "fuerza"]


def test_estructura_como_texto_json():
    b = a_setplan(
        [_w("2026-10-29", titulo="Series", km=9, estructura=json.dumps(SERIES))], SEMANA
    )
    assert "estructura=2km+3x1000m+2km" in b.texto


def test_titulo_y_descripcion_van_como_comentario():
    b = a_setplan(
        [_w("2026-10-27", titulo="Rodaje", km=8, description="Z2 conversable")], SEMANA
    )
    assert "# M: Rodaje — Z2 conversable" in b.texto
    assert parse_plan(b.texto).dia("M")[0].cantidad == 8


def test_se_avisa_que_ritmos_y_zonas_no_se_importan():
    b = a_setplan([_w("2026-10-29", titulo="Series", km=9, estructura=SERIES)], SEMANA)
    assert any("ritmos y zonas" in a for a in b.avisos)


# --------------------------------------------------------------------------
# /tp
# --------------------------------------------------------------------------


@pytest.fixture
def semana_fija(monkeypatch):
    monkeypatch.setattr(comandos, "resolver_rango", lambda sufijo: SEMANA)


def test_tp_guarda_el_plan_importado(semana_fija):
    store = PlanStore()
    ws = [
        _w("2026-10-27", titulo="Rodaje", km=8),
        _w("2026-10-29", titulo="Series", km=9.2, estructura=SERIES),
    ]
    r = comandos.cmd_tp(store, "", cliente=_cliente(ws))
    assert "Plan importado de TrainingPeaks" in r
    anclado = store.cargar()
    assert anclado is not None and anclado.rango == SEMANA
    assert anclado.plan.dia("J")[0].tipo == "series"


def test_tp_no_toca_el_plan_si_la_cookie_vencio(semana_fija):
    store = PlanStore()
    r = comandos.cmd_tp(store, "", cliente=_cliente(estado_token=401))
    assert "cookie" in r and store.cargar() is None


def test_tp_no_guarda_si_la_conversion_no_valida(semana_fija):
    store = PlanStore()
    sin_cantidad = _w("2026-10-27", titulo="Rodaje")
    sin_cantidad["structure"] = _estructura(_bloque(_paso(1800, unidad="second")))
    r = comandos.cmd_tp(store, "", cliente=_cliente([sin_cantidad]))
    assert "No cargue el plan" in r and "/setplan" in r
    assert store.cargar() is None


def test_tp_sin_nada_planificado_no_carga(semana_fija):
    store = PlanStore()
    r = comandos.cmd_tp(store, "", cliente=_cliente([]))
    assert "No cargue nada" in r and store.cargar() is None


def test_tp_rechaza_un_sufijo_desconocido():
    assert "no reconocido" in comandos.cmd_tp(PlanStore(), "ayer", cliente=_cliente())


def test_tp_error_de_red_no_toca_el_plan(semana_fija):
    class Roto:
        def entrenamientos(self, *a):
            raise TPError("se cayo")

    r = comandos.cmd_tp(PlanStore(), "", cliente=Roto())
    assert "No pude leer TrainingPeaks" in r


# --------------------------------------------------------------------------
# verificar
# --------------------------------------------------------------------------


def test_verificar_no_imprime_la_cookie(monkeypatch, capsys):
    monkeypatch.setattr(config, "TP_AUTH_COOKIE", COOKIE)
    monkeypatch.setattr(verificar, "TPClient", lambda: _cliente([_w("2026-10-27", km=8)]))
    assert verificar.main([]) == 0
    salida = capsys.readouterr().out
    assert COOKIE not in salida and "tok" not in salida.replace("token", "")
    assert "borrador" in salida


def test_verificar_cookie_vencida_sale_con_2(monkeypatch, capsys):
    monkeypatch.setattr(verificar, "TPClient", lambda: _cliente(estado_token=401))
    assert verificar.main([]) == 2
