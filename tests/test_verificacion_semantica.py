"""Verificaciones que no miran cifras: direccion de un cambio y conteo de sesiones.

Nacen del reporte del 2026-11-02, que paso las dos comprobaciones de cifras con
dos errores de hecho: "el ratio vertical mejoro 0.4" con un delta de +0.4 (mas
alto es peor) y "en cuatro sesiones" con tres corridas.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from sport_report import config
from sport_report.db.models import BienestarDia
from sport_report.engine import mensual as m_mensual
from sport_report.engine import report
from sport_report.narrative.claude import (
    SISTEMA,
    Narrativa,
    redactar,
    verificar_conteo_sesiones,
    verificar_direccion,
)
from sport_report.narrative.mensual import (
    DIRECCIONES_MENSUALES,
    SISTEMA_MENSUAL_ACTIVIDAD,
    redactar_mensual_actividad,
)
from sport_report.plan import carrera as m_carrera
from sport_report.plan.carrera import Carrera
from sport_report.run_weekly import ejecutar
from tests.test_mensual import SEPTIEMBRE, LUNES_SEP, corrida
from tests.test_narrative import ClienteFalso, Respuesta
from tests.test_run_weekly import SEMANA, Buzon
from tests.test_run_weekly import entorno  # noqa: F401  (fixture)

# Texto real de la narrativa semanal del 2026-11-02 (tres primeros parrafos).
TEXTO_2026_11_02 = (
    "La semana cerró con 20.17 km de running en cuatro sesiones, lo que representa el "
    "72.4% del volumen planificado de 27.86 km. El viernes se registró una sesión "
    "faltante de running fácil, aunque ese día se completó trabajo de fuerza y ciclismo "
    "en sala. A pesar de esto, la adherencia global fue del 75% considerando las "
    "sesiones evaluables, y ambas sesiones de fuerza se cumplieron sin inconvenientes.\n\n"
    "En dinámica de carrera, la cadencia se mantuvo estable en 160.6 spm con una caída "
    "mínima de 0.3 spm respecto a la semana anterior. El tiempo de contacto con el suelo "
    "aumentó 4.7 ms a 291.2 ms, mientras que la oscilación vertical bajó 0.3 cm a 10.4 "
    "cm. El ratio vertical mejoró 0.4 puntos porcentuales a 12.7%, sugiriendo un patrón "
    "ligeramente más eficiente en la propulsión.\n\n"
    "El bienestar mostró un ligero deterioro: la variabilidad de frecuencia cardíaca "
    "(HRV) cayó 0.8 ms a 41.6 ms, la frecuencia cardíaca de reposo subió 0.7 lpm a 59.3 "
    "lpm, y el sueño disminuyó 0.3 horas a 6.7 horas. El sleep score fue más bajo, "
    "cayendo 5.7 puntos a 72.3. La deriva cardíaca en las tres sesiones largas fue "
    "negativa en promedio (-1.6%). Readiness y Body Battery no reportaron datos."
)


def _d(delta):
    return {"delta": delta}


DATOS_2026_11_02 = {
    "cadencia": _d(-0.3),
    "gct": _d(4.7),
    "oscilacion_vertical": _d(-0.3),
    "ratio_vertical": _d(0.4),
    "fatiga_descanso": {
        "hrv": _d(-0.8),
        "hr_reposo": _d(0.7),
        "sueno_h": _d(-0.3),
        "sueno_score": _d(-5.7),
        "readiness": _d(None),
        "body_battery": _d(None),
    },
    "volumen": {"sesiones_running": 3},
    "adherencia": {
        "sesiones_evaluables": 4,
        "sesiones_cumplidas": 3,
        "fuerza": {"planificadas": 2, "cumplidas": 2},
    },
    "deriva_cardiaca": {"n_sesiones": 3},
}


# --------------------------------------------------------------------------
# Direccion
# --------------------------------------------------------------------------


def test_el_reporte_real_solo_marca_el_ratio_vertical():
    """Los otros siete cambios del texto estan bien descritos: no se marcan."""
    problemas = verificar_direccion(TEXTO_2026_11_02, DATOS_2026_11_02)
    assert problemas == [
        "ratio vertical: el texto dice 'mejoró' pero el delta es +0.4 y menor es mejor"
    ]


@pytest.mark.parametrize(
    "texto",
    [
        "El GCT mejoró 4.7 ms.",  # subio y menor es mejor
        "La oscilación vertical subió 0.3 cm.",  # bajo
        "El ratio vertical bajó a 12.7%.",  # subio
        "La HR de reposo mejoró 0.7 lpm.",  # subio y menor es mejor
        "El HRV aumentó 0.8 ms.",  # bajo
    ],
)
def test_direccion_contraria_al_delta(texto):
    assert verificar_direccion(texto, DATOS_2026_11_02)


@pytest.mark.parametrize(
    "texto",
    [
        "El GCT empeoró 4.7 ms.",
        "El ratio vertical subió 0.4 a 12.7%, un empeoramiento leve.",
        "La oscilación vertical mejoró 0.3 cm.",
        "El HRV cayó 0.8 ms.",
        "El ratio vertical no bajó esta semana.",  # negacion: no afirma una bajada
    ],
)
def test_direccion_correcta_no_se_marca(texto):
    assert verificar_direccion(texto, DATOS_2026_11_02) == []


def test_la_cadencia_no_se_califica_como_mejora():
    """Subir o bajar la cadencia no es bueno ni malo sin contexto."""
    assert verificar_direccion("La cadencia bajó 0.3 spm.", DATOS_2026_11_02) == []
    problemas = verificar_direccion("La cadencia mejoró 0.3 spm.", DATOS_2026_11_02)
    assert len(problemas) == 1 and "sentido fijo" in problemas[0]


def test_una_tendencia_sin_semana_anterior_se_marca():
    datos = {"gct": _d(None)}
    assert verificar_direccion("El GCT subió a 291.2 ms.", datos)


def test_el_sleep_score_no_se_confunde_con_las_horas_de_sueno():
    datos = {"fatiga_descanso": {"sueno_h": _d(0.5), "sueno_score": _d(-5.7)}}
    assert verificar_direccion("El puntaje de sueño bajó 5.7 puntos.", datos) == []
    assert verificar_direccion("Las horas de sueño subieron 0.5 h.", datos) == []


def test_la_conjuncion_corta_la_clausula():
    """Lo que viene despues de "y" habla de otra cosa."""
    datos = {"gct": _d(0.0)}
    assert verificar_direccion("El GCT se mantuvo y el ritmo bajó.", datos) == []


def test_la_polaridad_sale_de_una_sola_tabla():
    """El JSON y el verificador leen `config.MEJOR_SI`; nada mas la redefine."""
    assert config.MEJOR_SI["ratio_vertical"] == "menor"
    assert config.MEJOR_SI["gct"] == "menor"
    assert config.MEJOR_SI["oscilacion_vertical"] == "menor"
    assert config.MEJOR_SI["cadencia"] is None


def test_la_direccion_mensual_lee_el_delta_mes_contra_mes():
    datos = {"dinamica": {"ratio_vertical": {"delta": 0.4}, "gct": {"delta": -3.0}}}
    texto = "En el mes el ratio vertical mejoró 0.4 y el GCT bajó 3.0 ms."
    problemas = verificar_direccion(texto, datos, DIRECCIONES_MENSUALES)
    assert len(problemas) == 1 and problemas[0].startswith("ratio vertical")


# --------------------------------------------------------------------------
# Conteo de sesiones
# --------------------------------------------------------------------------


def test_el_reporte_real_solo_marca_las_cuatro_sesiones():
    assert verificar_conteo_sesiones(TEXTO_2026_11_02, DATOS_2026_11_02) == [
        "sesiones de running: el texto dice 'cuatro sesiones' y el JSON trae 3"
    ]


@pytest.mark.parametrize(
    "texto",
    [
        "Corriste 20.17 km en tres sesiones.",
        "Se cumplieron 3 de las cuatro sesiones planificadas.",
        "Quedó una sesión faltante el viernes.",
        "Las dos sesiones de fuerza se cumplieron.",
        "Hubo tres corridas esta semana.",
        "Hiciste dos sesiones de bici en sala.",  # otro deporte: no se verifica
        "Fueron cinco sesiones.",  # sin tema claro: no se verifica
    ],
)
def test_conteos_correctos_o_sin_tema_no_se_marcan(texto):
    assert verificar_conteo_sesiones(texto, DATOS_2026_11_02) == []


@pytest.mark.parametrize(
    "texto",
    [
        "Hubo cuatro corridas esta semana.",
        "La deriva se midió en 5 sesiones largas.",
        "Se cumplieron tres sesiones de fuerza.",
    ],
)
def test_conteos_equivocados_se_marcan(texto):
    assert verificar_conteo_sesiones(texto, DATOS_2026_11_02)


# --------------------------------------------------------------------------
# Cadena completa: narrativa -> avisos del reporte semanal
# --------------------------------------------------------------------------


def test_el_json_semanal_trae_corridas_realizadas_y_polaridad(entorno):
    repo, store = entorno
    j = report.construir(SEMANA, repo, store)
    assert j["volumen"]["sesiones_running"] == 4
    assert j["adherencia"]["sesiones_evaluables"] >= 1
    assert j["cadencia"]["mejor"] is None
    for clave in ("gct", "oscilacion_vertical", "ratio_vertical"):
        assert j[clave]["mejor"] == "menor"
    if j["fatiga_descanso"].get("hrv"):
        assert j["fatiga_descanso"]["hrv"]["mejor"] == "mayor"


def test_un_conteo_erroneo_avisa_en_el_reporte(entorno):
    repo, store = entorno
    buzon = Buzon()

    def narrador(datos):
        cliente = ClienteFalso(Respuesta("Corriste en siete sesiones de running."))
        return redactar(datos, cliente=cliente)

    ejecutar(SEMANA, repo, store, narrador=narrador, enviador=buzon, guardar=False)
    assert "contradice los datos" in buzon.mensajes[0]
    assert "siete sesiones" in buzon.mensajes[0]


def test_una_narrativa_sin_contradicciones_no_avisa(entorno):
    repo, store = entorno
    buzon = Buzon()

    def narrador(datos):
        return Narrativa(texto="Semana solida.", modelo="x")

    ejecutar(SEMANA, repo, store, narrador=narrador, enviador=buzon, guardar=False)
    assert "contradice" not in buzon.mensajes[0]


def test_el_prompt_semanal_explica_direccion_conteo_estimado_y_causas():
    assert '"mejor"' in SISTEMA
    assert "sesiones_running" in SISTEMA
    assert "estimado_km" in SISTEMA
    assert "no expliques causas" in SISTEMA


# --------------------------------------------------------------------------
# Mensual
# --------------------------------------------------------------------------


@pytest.fixture()
def entorno_mensual(tmp_path):
    from sport_report.db.repo import Repo
    from sport_report.plan.store import PlanStore

    repo = Repo(tmp_path / "m.db")
    yield repo, PlanStore(actual=tmp_path / "plan.json", archivo=tmp_path / "planes")
    repo.cerrar()


def test_el_mensual_declara_el_rango_real_de_semanas(entorno_mensual):
    repo, store = entorno_mensual
    j = m_mensual.construir(SEPTIEMBRE, repo, store)
    # Septiembre 2026: lunes 7, 14, 21 y 28; la ultima termina el 4 de octubre.
    assert j["mes"]["rango_semanas"] == {"inicio": "2026-09-07", "fin": "2026-10-04"}
    assert "rango_semanas" in SISTEMA_MENSUAL_ACTIVIDAD


def test_semana_sin_nada_registrado_puede_ser_hueco_de_datos(entorno_mensual):
    repo, store = entorno_mensual
    for lunes in LUNES_SEP[1:]:
        corrida(repo, lunes + timedelta(days=1), 10.0)
    avisos = m_mensual.construir(SEPTIEMBRE, repo, store)["avisos_datos"]
    hueco = [a for a in avisos if "2026-09-07" in a]
    assert hueco and "hueco de datos" in hueco[0] and "no se puede distinguir" in hueco[0]


def test_semana_sin_corridas_con_bienestar_no_es_hueco(entorno_mensual):
    repo, store = entorno_mensual
    for lunes in LUNES_SEP[1:]:
        corrida(repo, lunes + timedelta(days=1), 10.0)
    repo.guardar_bienestar(
        [
            BienestarDia(
                fecha_local=(LUNES_SEP[0] + timedelta(days=i)).isoformat(),
                fuente="intervals",
                hrv=60.0,
            )
            for i in range(5)
        ]
    )
    avisos = m_mensual.construir(SEPTIEMBRE, repo, store)["avisos_datos"]
    aviso = next(a for a in avisos if "2026-09-07" in a)
    assert "no un hueco de datos" in aviso and "5 dia(s) de bienestar" in aviso


def test_las_semanas_con_corridas_no_generan_aviso_de_hueco(entorno_mensual):
    repo, store = entorno_mensual
    for lunes in LUNES_SEP:
        corrida(repo, lunes + timedelta(days=1), 10.0)
    avisos = m_mensual.construir(SEPTIEMBRE, repo, store)["avisos_datos"]
    assert not any("hueco" in a for a in avisos)


def test_la_dinamica_mensual_trae_su_polaridad(entorno_mensual):
    repo, store = entorno_mensual
    d = m_mensual.construir(SEPTIEMBRE, repo, store)["dinamica"]
    assert d["ratio_vertical"]["mejor"] == "menor"
    assert d["cadencia"]["mejor"] is None


def test_una_direccion_erronea_del_mensual_queda_registrada():
    datos = {"dinamica": {"ratio_vertical": {"delta": 0.4}}}
    cliente = ClienteFalso(Respuesta("Solo carrera. El ratio vertical mejoró 0.4."))
    n = redactar_mensual_actividad(datos, cliente=cliente)
    assert n.direcciones_erroneas and "ratio vertical" in n.direcciones_erroneas[0]
    assert n.to_json()["direcciones_erroneas"] == n.direcciones_erroneas


# --------------------------------------------------------------------------
# Fase de la carrera: "a las 8 semanas" era ambiguo
# --------------------------------------------------------------------------


def test_la_fase_siguiente_viene_calculada_y_sin_ambiguedad():
    # Valdivia 2027-02-21, mirado el lunes 2026-11-02: 15 semanas.
    c = Carrera(fecha=date(2027, 2, 21), nombre="Valdivia")
    d = c.contexto(date(2026, 11, 2))
    assert d["semanas_restantes"] == 15
    assert d["fase"] == m_carrera.BASE
    assert d["fase_siguiente"] == m_carrera.CONSTRUCCION
    assert d["semanas_para_fase_siguiente"] == 7
    assert "cuando falten 8 semanas para la carrera" in d["nota"]
    assert "dentro de 7 semana(s)" in d["nota"]


def test_en_construccion_la_siguiente_es_el_afinamiento():
    c = Carrera(fecha=date(2027, 2, 21))
    d = c.contexto(date(2027, 2, 21) - timedelta(weeks=5))
    assert d["fase"] == m_carrera.CONSTRUCCION
    assert d["fase_siguiente"] == m_carrera.AFINAMIENTO
    assert d["semanas_para_fase_siguiente"] == 3


def test_pasada_la_carrera_no_hay_fase_siguiente():
    c = Carrera(fecha=date(2026, 10, 1))
    d = c.contexto(date(2026, 11, 2))
    assert d["fase_siguiente"] is None and d["semanas_para_fase_siguiente"] is None
