"""Estimacion de km por tiempo, semana en curso y grafico de volumen.

Los tres cambios comparten una misma idea: que el reporte no invente ni oculte.
Una sesion prescrita en minutos aporta km estimados y se dice que son estimados;
un dia que todavia no llega no es un incumplimiento; una semana sin historico se
dibuja en cero pero con la advertencia al lado.
"""
from __future__ import annotations

from datetime import date, timedelta

from sport_report.config import Estimacion
from sport_report.db.models import SesionReal
from sport_report.db.repo import Repo
from sport_report.engine import adherencia, foster, report
from sport_report.fechas import semana_de
from sport_report.plan.grammar import parse_plan
from sport_report.plan.models import Ritmo, Sesion
from sport_report.plan.store import PlanStore
from sport_report.telegram import comandos
from sport_report.telegram.formato import formatear_reporte, grafico_volumen

SEMANA = semana_de(date(2026, 9, 7))  # lunes 7 a domingo 13
LUNES, DOMINGO = SEMANA.inicio, SEMANA.fin

# Mismo plan de la spec pero con el viernes prescrito en minutos, que es el caso
# que dejaba el porcentaje de volumen inflado.
PLAN_CON_TIEMPO = """/setplan
semana: 9
L: rest
M: easy 8km Z2
W: fuerza
J: series 8.6km @Z4 estructura=2km+3x1000m+4x400m+2km
V: easy 40min Z2
S: rest
D: long 16km @6:15/5:45 Z2
"""


def sesion(dia_offset: int, **kw) -> SesionReal:
    f = LUNES + timedelta(days=dia_offset)
    base = dict(
        strava_id=2000 + dia_offset,
        fecha_utc=f"{f}T12:00:00+00:00",
        fecha_local=f.isoformat(),
        dia_semana="LMWJVSD"[dia_offset],
        tipo_strava="Run",
        es_fuerza=False,
        distancia_km=10.0,
        duracion_mov_s=3000,
    )
    base.update(kw)
    return SesionReal(**base)


# --------------------------------------------------------------------------
# Estimacion de km a partir de minutos
# --------------------------------------------------------------------------


def test_easy_por_tiempo_usa_el_ritmo_por_defecto():
    s = Sesion(dia="V", tipo="easy", cantidad=40, unidad="min")
    assert s.objetivo_km_estimado() == 5.71  # 40 min a 7:00/km
    assert s.km_para_volumen() == (5.71, True)


def test_el_ritmo_escrito_manda_sobre_el_por_defecto():
    """Si el atleta prescribio ritmo, es mas especifico que la configuracion."""
    s = Sesion(dia="V", tipo="easy", cantidad=40, unidad="min", ritmo=Ritmo(330, 330))
    assert s.objetivo_km_estimado() == 7.27  # 40 min a 5:30/km


def test_ventana_de_ritmo_usa_el_punto_medio():
    s = Sesion(dia="D", tipo="long", cantidad=90, unidad="min", ritmo=Ritmo(345, 375))
    assert s.objetivo_km_estimado() == 15.0  # 90 min a 6:00/km


def test_sesion_por_tiempo_sin_ritmo_y_que_no_es_easy_no_se_estima():
    """Inventar un ritmo para un tempo seria peor que dejarlo fuera."""
    s = Sesion(dia="J", tipo="tempo", cantidad=40, unidad="min")
    assert s.objetivo_km_estimado() is None
    assert s.km_para_volumen() == (None, False)


def test_sesion_en_km_no_se_estima():
    s = Sesion(dia="M", tipo="easy", cantidad=8, unidad="km")
    assert s.objetivo_km_estimado() is None
    assert s.km_para_volumen() == (8, False)


def test_ritmo_por_defecto_es_configurable():
    est = Estimacion(ritmo_easy_s_km=360)  # 6:00/km
    s = Sesion(dia="V", tipo="easy", cantidad=60, unidad="min")
    assert s.objetivo_km_estimado(est) == 10.0


def test_el_volumen_planificado_incluye_lo_estimado():
    p = parse_plan(PLAN_CON_TIEMPO)
    # 8 (M) + 8.6 (J) + 5.71 (V estimado) + 16 (D)
    assert p.volumen_planificado_km() == 38.31
    assert p.volumen_estimado_km() == 5.71
    assert p.dias_sin_estimar() == ()


def test_dia_por_tiempo_sin_ritmo_queda_avisado():
    plan = parse_plan(PLAN_CON_TIEMPO.replace("V: easy 40min Z2", "V: tempo 40min Z4"))
    assert plan.dias_sin_estimar() == ("V",)
    assert plan.volumen_planificado_km() == 32.6  # el viernes queda fuera

    r = adherencia.calcular(plan, SEMANA, [])
    assert any("sin ritmo con que estimar" in a for a in r.avisos)


# --------------------------------------------------------------------------
# El defecto que motivo el cambio
# --------------------------------------------------------------------------


def test_el_porcentaje_de_volumen_compara_la_misma_base():
    """Antes: 23.86 real / 32.6 plan = 73.2%, porque el viernes sumaba al
    numerador y no al denominador. Ahora ambos lados incluyen el viernes."""
    reales = [
        sesion(1, distancia_km=8.0),  # M
        sesion(3, distancia_km=10.0),  # J
        sesion(4, distancia_km=5.86),  # V, prescrito en minutos
    ]
    r = adherencia.calcular(parse_plan(PLAN_CON_TIEMPO), SEMANA, reales)
    assert r.volumen_real_km == 23.86
    assert r.volumen_planificado_km == 38.31
    assert r.volumen_pct == 62.3
    assert r.volumen_estimado_km == 5.71


def test_el_dia_por_tiempo_se_sigue_evaluando_en_minutos():
    """La estimacion es solo para el agregado: el dia se compara en su unidad."""
    r = adherencia.calcular(
        parse_plan(PLAN_CON_TIEMPO), SEMANA, [sesion(4, distancia_km=5.86, duracion_mov_s=2400)]
    )
    v = r.dias[4]
    assert (v.unidad, v.objetivo, v.real, v.pct) == ("min", 40, 40.0, 100.0)
    assert v.estado == adherencia.CUMPLIDA


# --------------------------------------------------------------------------
# Semana en curso
# --------------------------------------------------------------------------


def test_los_dias_futuros_quedan_pendientes_no_incumplidos():
    sabado = LUNES + timedelta(days=5)
    r = adherencia.calcular(
        parse_plan(PLAN_CON_TIEMPO), SEMANA, [sesion(1, distancia_km=8.0)], hasta=sabado
    )
    domingo = r.dias[6]
    assert domingo.estado == adherencia.PENDIENTE
    assert domingo.pct is None  # no es 0%: no ha llegado
    assert domingo.objetivo == 16  # pero se ve lo que viene
    assert r.dias_transcurridos == 6 and r.en_curso is True


def test_el_domingo_pendiente_no_entra_al_volumen_planificado():
    sabado = LUNES + timedelta(days=5)
    r = adherencia.calcular(parse_plan(PLAN_CON_TIEMPO), SEMANA, [], hasta=sabado)
    assert r.volumen_planificado_km == 22.31  # 38.31 - 16 del domingo


def test_semana_completa_no_tiene_dias_pendientes():
    r = adherencia.calcular(parse_plan(PLAN_CON_TIEMPO), SEMANA, [])
    assert r.dias_transcurridos == 7 and r.en_curso is False
    assert all(d.estado != adherencia.PENDIENTE for d in r.dias)


def test_una_fuerza_futura_no_cuenta_como_pendiente_de_cumplir():
    """El miercoles todavia no llega: no es una fuerza que se haya saltado."""
    martes = LUNES + timedelta(days=1)
    r = adherencia.calcular(parse_plan(PLAN_CON_TIEMPO), SEMANA, [], hasta=martes)
    assert r.fuerza_planificadas == 0


def test_monotony_de_semana_parcial_va_marcada_como_no_confiable():
    cargas = {LUNES + timedelta(days=i): 50.0 * (i + 1) for i in range(4)}
    r = foster.calcular(cargas, inicio=LUNES, dias=4)
    assert r.monotony is not None  # la cifra existe
    assert r.confiable is False  # pero no es comparable con una semana entera
    assert "4 de 7 dias" in r.motivo
    assert r.alerta == ""


# --------------------------------------------------------------------------
# Grafico de volumen
# --------------------------------------------------------------------------


def test_volumen_semanal_agrupa_por_lunes(tmp_path):
    repo = Repo(tmp_path / "t.db")
    try:
        repo.guardar_sesion(sesion(0, distancia_km=10.0))  # lunes
        repo.guardar_sesion(sesion(6, distancia_km=5.0))  # domingo, misma semana
        serie = repo.volumen_semanal(DOMINGO, 3)
        assert len(serie) == 3
        assert serie[-1] == (LUNES, 15.0)
        assert [km for _, km in serie[:2]] == [0.0, 0.0]  # sin datos, no huecos
    finally:
        repo.cerrar()


def test_volumen_semanal_ignora_sesiones_sin_distancia(tmp_path):
    repo = Repo(tmp_path / "t.db")
    try:
        repo.guardar_sesion(sesion(0, distancia_km=10.0))
        repo.guardar_sesion(sesion(1, distancia_km=None, tipo_strava="WeightTraining"))
        assert repo.volumen_semanal(DOMINGO, 1) == [(LUNES, 10.0)]
    finally:
        repo.cerrar()


def test_grafico_escala_a_la_semana_mayor():
    hist = {
        "semanas": [
            {"lunes": "2026-08-31", "km": 40.0},
            {"lunes": "2026-09-07", "km": 20.0},
        ],
        "primera_fecha_con_datos": "2026-08-31",
    }
    lineas = grafico_volumen(hist, ancho=10).splitlines()
    assert lineas[0] == "VOLUMEN ULTIMAS 2 SEMANAS (km)"
    assert lineas[1].count("█") == 10  # la mayor llena la barra
    assert lineas[2].count("█") == 5  # la mitad, media barra
    assert lineas[1].startswith("  31/08") and lineas[1].endswith("40")


def test_una_semana_con_pocos_km_no_se_dibuja_vacia():
    """Un 0.5 km no puede parecer lo mismo que una semana sin correr."""
    hist = {"semanas": [{"lunes": "2026-08-31", "km": 50.0}, {"lunes": "2026-09-07", "km": 0.5}]}
    lineas = grafico_volumen(hist, ancho=10).splitlines()
    assert lineas[2].count("█") == 1


def test_grafico_avisa_de_las_semanas_sin_historico():
    hist = {
        "semanas": [{"lunes": "2026-08-31", "km": 0.0}, {"lunes": "2026-09-07", "km": 30.0}],
        "primera_fecha_con_datos": "2026-09-07",
    }
    assert "sin historico antes del 07/09" in grafico_volumen(hist)


def test_no_avisa_si_el_historico_solo_empieza_a_media_semana():
    """Empezar el martes no deja esa semana sin datos: avisar seria mentir."""
    hist = {
        "semanas": [{"lunes": "2026-08-31", "km": 20.0}, {"lunes": "2026-09-07", "km": 30.0}],
        "primera_fecha_con_datos": "2026-09-01",  # martes de la primera semana
    }
    assert "sin historico" not in grafico_volumen(hist)


def test_la_semana_en_curso_va_marcada_en_el_grafico():
    """Una semana a medio correr parece un desplome si no se dice que va a medias."""
    hist = {
        "semanas": [{"lunes": "2026-08-31", "km": 40.0}, {"lunes": "2026-09-07", "km": 12.0}],
        "ultima_en_curso": True,
    }
    lineas = grafico_volumen(hist).splitlines()
    assert lineas[-1].endswith("(en curso)")
    assert not lineas[-2].endswith("(en curso)")


def test_grafico_vacio_cuando_no_hay_kilometros():
    assert grafico_volumen({"semanas": [{"lunes": "2026-09-07", "km": 0.0}]}) == ""
    assert grafico_volumen({"semanas": []}) == ""


# --------------------------------------------------------------------------
# Integracion: reporte y comandos, sin red
# --------------------------------------------------------------------------


def _repo_con_semana(tmp_path) -> Repo:
    repo = Repo(tmp_path / "t.db")
    repo.guardar_sesion(sesion(1, distancia_km=8.0, carga=60.0))
    repo.guardar_sesion(sesion(3, distancia_km=10.0, carga=90.0))
    repo.guardar_sesion(sesion(4, distancia_km=5.86, carga=40.0))
    return repo


def _store(tmp_path, texto: str = PLAN_CON_TIEMPO) -> PlanStore:
    store = PlanStore(tmp_path / "plan.json")
    store.guardar(parse_plan(texto), SEMANA)
    return store


def test_reporte_en_curso_marca_cabecera_y_no_penaliza_el_futuro(tmp_path):
    repo = _repo_con_semana(tmp_path)
    try:
        sabado = LUNES + timedelta(days=5)
        datos = report.construir(SEMANA, repo, _store(tmp_path), hasta=sabado)
        assert datos["semana"]["en_curso"] is True
        assert datos["semana"]["dias_transcurridos"] == 6
        assert datos["volumen"]["estimado_km"] == 5.71
        assert datos["monotony"]["confiable"] is False

        texto = formatear_reporte(datos)
        assert texto.startswith("AVANCE DE LA SEMANA")
        assert "6 de 7 dias" in texto
        assert "planificados hasta hoy" in texto
        assert "(pendiente)" in texto
    finally:
        repo.cerrar()


def test_reporte_de_semana_cerrada_no_cambia_de_titulo(tmp_path):
    repo = _repo_con_semana(tmp_path)
    try:
        datos = report.construir(SEMANA, repo, _store(tmp_path))
        assert datos["semana"]["en_curso"] is False
        assert formatear_reporte(datos).startswith("REPORTE SEMANAL")
    finally:
        repo.cerrar()


def test_el_reporte_trae_la_serie_de_16_semanas(tmp_path):
    repo = _repo_con_semana(tmp_path)
    try:
        datos = report.construir(SEMANA, repo, _store(tmp_path))
        assert len(datos["volumen_historico"]["semanas"]) == 16
        # la serie redondea a un decimal: alimenta el grafico, no el calculo
        assert datos["volumen_historico"]["semanas"][-1]["km"] == 23.9
        assert "VOLUMEN ULTIMAS 16 SEMANAS" in formatear_reporte(datos)
        assert "VOLUMEN ULTIMAS" not in formatear_reporte(datos, con_grafico=False)
    finally:
        repo.cerrar()


def test_progreso_sin_strava_responde_igual(tmp_path):
    """Si no hay red el comando no puede quedarse mudo: avisa y usa la base."""
    repo = _repo_con_semana(tmp_path)
    try:
        texto = comandos.cmd_progreso(
            _store(tmp_path), repo=repo, hoy=LUNES + timedelta(days=5), sincronizar=False
        )
        assert texto.startswith("AVANCE DE LA SEMANA")
    finally:
        repo.cerrar()


def test_progreso_avisa_si_la_ingesta_falla(tmp_path):
    class IngestaRota:
        def sincronizar(self, desde, hasta):
            raise RuntimeError("429 cuota agotada")

    repo = _repo_con_semana(tmp_path)
    try:
        texto = comandos.cmd_progreso(
            _store(tmp_path), repo=repo, ingesta=IngestaRota(), hoy=LUNES + timedelta(days=5)
        )
        assert "no se pudo sincronizar con Strava" in texto
        assert "429 cuota agotada" in texto
        assert "AVANCE DE LA SEMANA" in texto  # el reporte llega igual
    finally:
        repo.cerrar()


def test_comando_volumen(tmp_path):
    repo = _repo_con_semana(tmp_path)
    try:
        assert "VOLUMEN ULTIMAS 16 SEMANAS" in comandos.cmd_volumen(repo=repo, hoy=DOMINGO)
    finally:
        repo.cerrar()


def test_comando_volumen_sin_datos(tmp_path):
    repo = Repo(tmp_path / "vacia.db")
    try:
        assert "Todavia no hay kilometros" in comandos.cmd_volumen(repo=repo, hoy=DOMINGO)
    finally:
        repo.cerrar()
