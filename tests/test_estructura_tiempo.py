"""Tramos por tiempo en `estructura=`, convertidos a km al cargar el plan.

El caso que lo motiva es el jueves que carga la entrenadora en TrainingPeaks:
15' suave + 4 x (5' a 5:50-5:30 + 3' caminando) + 15' suave. Las repeticiones se
convierten al punto medio del ritmo escrito; el calentamiento y la vuelta a la
calma, al ritmo easy de la configuracion. Desde ahi el motor trabaja en km como
con cualquier `estructura=`: alineacion con vueltas incluida.
"""
from __future__ import annotations

import pytest

from sport_report.config import ESTIMACION
from sport_report.engine import adherencia
from sport_report.plan.errors import PlanInvalido
from sport_report.plan.grammar import parse_dias, parse_plan
from sport_report.plan.models import plan_desde_json
from sport_report.plan.render import avisos, describir_sesion
from sport_report.trainingpeaks.convertir import a_setplan, ritmo_de_texto
from tests.test_adherencia_multi import SEMANA, _dia
from tests.test_engine import sesion
from tests.test_trainingpeaks import SEMANA as SEMANA_TP
from tests.test_trainingpeaks import _bloque, _estructura, _paso, _w

EASY = ESTIMACION.ritmo_easy_s_km  # 7:00/km

PLAN = """L: rest
M: easy 50min
W: fuerza
J: series 62min estructura=15min+4x5min@5:50/5:30+15min
V: easy 50min
S: rest
D: long 14km @7:15/6:45
"""

CALENTAMIENTO_KM = round(900 / EASY, 3)  # 2.143
REP_KM = round(300 / 340, 3)  # 5' al punto medio de 5:30 y 5:50 (5:40)


def _jueves(texto: str = PLAN):
    return parse_plan(texto).dia("J")[0]


# --------------------------------------------------------------------------
# Conversion
# --------------------------------------------------------------------------


def test_repeticiones_al_ritmo_medio_y_el_resto_a_ritmo_easy():
    j = _jueves()
    assert j.estructura.segmentos_km() == (
        CALENTAMIENTO_KM, REP_KM, REP_KM, REP_KM, REP_KM, CALENTAMIENTO_KM
    )
    # Se compara en km aunque el total se haya escrito en minutos.
    assert j.objetivo_km() == pytest.approx(2 * CALENTAMIENTO_KM + 4 * REP_KM, abs=0.002)
    assert j.objetivo_min() is None


def test_se_muestra_que_los_km_son_estimados():
    assert describir_sesion(_jueves()) == (
        "series 62min [15min+4x5min@5:50/5:30+15min] -> 7.81km duros estimados"
    )


def test_el_volumen_planificado_marca_la_sesion_como_estimada():
    km, estimado = _jueves().km_para_volumen()
    assert estimado and km == pytest.approx(7.81, abs=0.01)
    # Y no aparece el aviso de "sin ritmo con que estimar".
    assert avisos(parse_plan(PLAN)) == []


def test_repeticion_sin_ritmo_toma_el_de_la_sesion():
    j = parse_dias("J: series 40min @5:40 estructura=10min+4x5min+10min")["J"][0]
    assert j.estructura.segmentos_km()[1] == REP_KM


def test_repeticion_sin_ningun_ritmo_se_rechaza():
    with pytest.raises(PlanInvalido) as exc:
        parse_dias("J: series 40min estructura=10min+4x5min+10min")
    assert "4x5min@5:30/5:50" in exc.value.render()


def test_tramo_suelto_con_ritmo_propio():
    j = parse_dias("J: tempo 40min")["J"][0]  # control: sin estructura
    assert j.estructura is None
    s = parse_dias("J: series 45min estructura=10min+20min@5:00+2x3min@4:40+10min")["J"][0]
    assert s.estructura.segmentos_km()[1] == 4.0


def test_se_puede_mezclar_tiempo_y_distancia():
    j = parse_dias("J: series 10km estructura=15min+6x1000m+10min")["J"][0]
    assert j.estructura.segmentos_km()[1:7] == (1.0,) * 6
    assert j.estructura.estimada


def test_segundos():
    j = parse_dias("J: fartlek 30min estructura=10x30s@4:00")["J"][0]
    assert j.estructura.segmentos_km() == (0.125,) * 10


def test_prog_por_tiempo_exige_ritmo_en_cada_bloque():
    v = parse_dias("V: prog 40min estructura=20min@6:00+20min@5:00")["V"][0]
    assert v.estructura.segmentos_km() == (round(1200 / 360, 3), 4.0)
    with pytest.raises(PlanInvalido):
        parse_dias("V: prog 40min estructura=20min+20min@5:00")


def test_duracion_cero_se_rechaza():
    with pytest.raises(PlanInvalido):
        parse_dias("J: series 30min estructura=0min+4x5min@5:30")


def test_ida_y_vuelta_por_json_conserva_los_km_estimados():
    """Si despues cambia el ritmo easy de la config, un plan cargado no se mueve."""
    plan = parse_plan(PLAN)
    otra = plan_desde_json(plan.to_json())
    assert otra.dia("J")[0].estructura == plan.dia("J")[0].estructura
    assert otra.dia("J")[0].estructura.estimada


def test_un_plan_por_distancia_se_guarda_igual_que_antes():
    plan = parse_plan(PLAN.replace(
        "J: series 62min estructura=15min+4x5min@5:50/5:30+15min",
        "J: series 8.6km estructura=2km+3x1000m+4x400m+2km",
    ))
    for b in plan.to_json()["sesiones"]["J"][0]["estructura"]["bloques"]:
        assert "duracion_s" not in b


# --------------------------------------------------------------------------
# Adherencia: se alinea por km contra las vueltas, como siempre
# --------------------------------------------------------------------------


def test_alinea_las_vueltas_contra_los_km_estimados():
    from sport_report.db.models import Vuelta

    pares = [
        (2.25, 900),                        # calentamiento a 6:40, dentro de tolerancia
        (0.88, 300), (0.25, 180),
        (0.90, 300), (0.24, 180),
        (0.87, 300), (0.26, 180),
        (0.89, 300), (0.25, 180),
        (2.10, 900),
    ]
    laps = [Vuelta("strava", "1003", i + 1, km, s) for i, (km, s) in enumerate(pares)]
    plan = parse_plan(PLAN)
    real = sesion(3, distancia_km=round(sum(k for k, _ in pares), 2), duracion_mov_s=3720)
    d = _dia(adherencia.calcular(plan, SEMANA, [real], vueltas={("strava", "1003"): laps}), "J")

    assert d.unidad == "km"
    assert d.real == pytest.approx(2.25 + 0.88 + 0.90 + 0.87 + 0.89 + 2.10, abs=0.01)
    assert d.recuperacion_km == pytest.approx(1.0, abs=0.01)
    assert d.estado == adherencia.CUMPLIDA


# --------------------------------------------------------------------------
# /tp
# --------------------------------------------------------------------------

DESCRIPCION = (
    "15\"easy +4x5x3\" + 15\"easy Calentamiento: 15min de trote suave + movilidad "
    "Desarrollo: Correr 5min a ritmo de 5:50 a 5:30/km seguido de 3min suaves o "
    "caminados Soltura: 15min de trote suave + estiramientos"
)

JUEVES_TP = _estructura(
    _bloque(_paso(900, "second", "warmUp")),
    _bloque(_paso(300, "second"), _paso(180, "second", "rest"), reps=4),
    _bloque(_paso(900, "second", "coolDown")),
)


@pytest.mark.parametrize(
    "texto, ritmo",
    [
        (DESCRIPCION, "@5:50/5:30"),
        ("series a 5:30-5:50", "@5:30/5:50"),
        ("reps a 4:45/km", "@4:45"),
        ("sin ritmo", None),
        ("primero 5:50 a 5:30 y luego 5:00 a 4:50", None),  # ambiguo
    ],
)
def test_ritmo_de_la_descripcion(texto, ritmo):
    assert ritmo_de_texto(texto) == ritmo


def test_tp_importa_el_jueves_de_la_entrenadora():
    w = _w("2026-10-29", titulo="Interval 4(5x3)", horas=62 / 60,
           estructura=JUEVES_TP, description=DESCRIPCION)
    b = a_setplan([w], SEMANA_TP)
    j = parse_plan(b.texto).dia("J")[0]
    assert j.tipo == "series"
    assert (j.cantidad, j.unidad) == (62, "min")
    assert j.estructura.crudo == "15min+4x5min@5:50/5:30+15min"
    assert j.objetivo_km() == pytest.approx(7.81, abs=0.01)
    assert not any("estructura" in a for a in b.avisos)


def test_tp_sin_ritmo_en_la_descripcion_no_importa_la_estructura():
    w = _w("2026-10-29", titulo="Interval 4(5x3)", horas=62 / 60, estructura=JUEVES_TP)
    b = a_setplan([w], SEMANA_TP)
    j = parse_plan(b.texto).dia("J")[0]
    assert j.estructura is None and j.tipo == "easy"
    assert any("no trae un ritmo claro" in a for a in b.avisos)


def test_tp_sin_duracion_planificada_suma_los_tramos():
    w = _w("2026-10-29", titulo="Interval", estructura=JUEVES_TP, description=DESCRIPCION)
    j = parse_plan(a_setplan([w], SEMANA_TP).texto).dia("J")[0]
    assert (j.cantidad, j.unidad) == (62, "min")


def test_tp_calentamiento_por_tiempo_con_series_por_distancia():
    est = _estructura(
        _bloque(_paso(900, "second", "warmUp")),
        _bloque(_paso(1000), _paso(200, clase="rest"), reps=6),
        _bloque(_paso(600, "second", "coolDown")),
    )
    w = _w("2026-10-29", titulo="Series 6x1000", km=10, estructura=est)
    j = parse_plan(a_setplan([w], SEMANA_TP).texto).dia("J")[0]
    assert j.estructura.crudo == "15min+6x1000m+10min"
