"""Estimacion de km por tiempo, semana en curso y grafico de volumen.

Los tres cambios comparten una misma idea: que el reporte no invente ni oculte.
Una sesion prescrita en minutos aporta km estimados y se dice que son estimados;
un dia que todavia no llega no es un incumplimiento; una semana sin historico se
dibuja en cero pero con la advertencia al lado.
"""
from __future__ import annotations

from datetime import date, timedelta

from sport_report import diagnostico
from sport_report.config import Estimacion
from sport_report.db.models import SesionReal
from sport_report.db.repo import Repo
from sport_report.engine import adherencia, foster, report
from sport_report.fechas import semana_de
from sport_report.plan.grammar import parse_plan
from sport_report.plan import render
from sport_report.plan.models import Ritmo, Sesion
from sport_report.plan.store import PlanStore
from sport_report.telegram import comandos
from sport_report.grafico import volumen_png
from sport_report.telegram.formato import formatear_progreso, formatear_reporte

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


HIST = {
    "semanas": [
        {"lunes": "2026-08-31", "km": 40.0},
        {"lunes": "2026-09-07", "km": 12.0},
    ],
    "primera_fecha_con_datos": "2026-08-31",
    "ultima_en_curso": True,
}


FIRMA_PNG = bytes.fromhex("89504e470d0a1a0a")


def test_genera_un_png_valido(tmp_path):
    destino = tmp_path / "v.png"
    assert volumen_png(HIST, destino) == destino
    datos = destino.read_bytes()
    assert datos.startswith(FIRMA_PNG)
    assert len(datos) > 5000  # no es una imagen vacia


def test_no_deja_el_temporal_a_medias(tmp_path):
    destino = tmp_path / "v.png"
    volumen_png(HIST, destino)
    assert list(tmp_path.glob("*.tmp.png")) == []


def test_redibujar_sobreescribe_la_misma_ruta(tmp_path):
    destino = tmp_path / "v.png"
    volumen_png(HIST, destino)
    primero = destino.stat().st_size
    volumen_png({**HIST, "semanas": [{"lunes": "2026-09-14", "km": 5.0}]}, destino)
    assert destino.read_bytes().startswith(FIRMA_PNG)
    assert destino.stat().st_size != primero  # se redibujo, no se apendizo


def test_sin_kilometros_no_dibuja_nada(tmp_path):
    destino = tmp_path / "v.png"
    assert volumen_png({"semanas": [{"lunes": "2026-09-07", "km": 0.0}]}, destino) is None
    assert volumen_png({"semanas": []}, destino) is None
    assert not destino.exists()


def test_crea_el_directorio_si_no_existe(tmp_path):
    destino = tmp_path / "nueva" / "carpeta" / "v.png"
    assert volumen_png(HIST, destino) == destino
    assert destino.is_file()


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
        # el mensaje de texto NO dibuja: la imagen se manda aparte
        assert "VOLUMEN ULTIMAS" not in formatear_reporte(datos)
    finally:
        repo.cerrar()


def test_progreso_es_un_resumen_corto_no_el_reporte(tmp_path):
    """Responde dos preguntas: cuanto llevo y que me queda. Nada mas."""
    repo = _repo_con_semana(tmp_path)
    try:
        texto = comandos.cmd_progreso(
            _store(tmp_path), repo=repo, hoy=LUNES + timedelta(days=5), sincronizar=False
        )
        # lo que si tiene
        assert "23.9 km de 38.3 km programados (62%)" in texto
        assert "QUEDA ESTA SEMANA" in texto
        assert "dom  long 16km" in texto
        assert "Quedan 16 km en el plan." in texto
        assert "sab  rest" not in texto  # un descanso no es algo por hacer
        # lo que no: las metricas de carga se quedan en el reporte del lunes
        for seccion in ("ACWR", "Monotony", "DERIVA CARDIACA", "CADENCIA", "VOLUMEN"):
            assert seccion not in texto
        assert len(texto.splitlines()) < 15
    finally:
        repo.cerrar()


def test_progreso_no_da_por_perdido_el_dia_de_hoy(tmp_path):
    """A media manana del viernes, la sesion del viernes aun puede hacerse."""
    repo = Repo(tmp_path / "t.db")
    try:
        repo.guardar_sesion(sesion(1, distancia_km=8.0))  # solo el martes
        viernes = LUNES + timedelta(days=4)
        texto = comandos.cmd_progreso(
            _store(tmp_path), repo=repo, hoy=viernes, sincronizar=False
        )
        assert "vie  easy 40min" in texto  # aparece en lo que queda
        assert "Sin registrar: vie" not in texto  # y no como incumplido
    finally:
        repo.cerrar()


def test_progreso_marca_los_dias_que_si_se_perdieron(tmp_path):
    repo = Repo(tmp_path / "t.db")
    try:
        viernes = LUNES + timedelta(days=4)
        texto = comandos.cmd_progreso(
            _store(tmp_path), repo=repo, hoy=viernes, sincronizar=False
        )
        # el miercoles de fuerza tambien cuenta como perdido
        assert "Sin registrar: mar, mie, jue" in texto
        assert "0 de 5 entrenamientos hechos" in texto
    finally:
        repo.cerrar()


def test_progreso_con_la_semana_terminada(tmp_path):
    repo = _repo_con_semana(tmp_path)
    try:
        texto = comandos.cmd_progreso(
            _store(tmp_path), repo=repo, hoy=DOMINGO, sincronizar=False
        )
        assert "no queda nada por hacer" in texto
        assert "QUEDA ESTA SEMANA" not in texto
    finally:
        repo.cerrar()


def test_progreso_sin_plan_no_revienta(tmp_path):
    repo = _repo_con_semana(tmp_path)
    try:
        vacio = PlanStore(tmp_path / "sin_plan.json")
        texto = comandos.cmd_progreso(
            vacio, repo=repo, hoy=LUNES + timedelta(days=5), sincronizar=False
        )
        assert "No hay plan cargado" in texto
        assert "23.9 km" in texto  # lo real se reporta igual
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
        assert "23.9 km de 38.3 km" in texto  # el resumen llega igual
    finally:
        repo.cerrar()


def test_comando_volumen_devuelve_imagen_y_pie(tmp_path):
    repo = _repo_con_semana(tmp_path)
    destino = tmp_path / "v.png"
    try:
        ruta, texto = comandos.cmd_volumen(repo=repo, hoy=DOMINGO, destino=destino)
        assert ruta == destino
        assert destino.read_bytes().startswith(FIRMA_PNG)
        assert "ultimas 16 semanas" in texto
    finally:
        repo.cerrar()


def test_comando_volumen_sin_datos_no_dibuja(tmp_path):
    repo = Repo(tmp_path / "vacia.db")
    destino = tmp_path / "v.png"
    try:
        ruta, texto = comandos.cmd_volumen(repo=repo, hoy=DOMINGO, destino=destino)
        assert ruta is None
        assert "Todavia no hay kilometros" in texto
        assert not destino.exists()
    finally:
        repo.cerrar()


# --------------------------------------------------------------------------
# Diagnostico: estado de los tokens de Strava
# --------------------------------------------------------------------------


def test_diagnostico_reconoce_la_semilla_del_env():
    """Sin tokens.json pero con STRAVA_REFRESH_TOKEN el sistema SI puede arrancar."""
    marca, detalle = diagnostico.estado_tokens(None, refresh_env="a" * 40)
    assert marca == diagnostico.AVISO  # no FALLA: no impide operar
    assert "STRAVA_REFRESH_TOKEN" in detalle


def test_diagnostico_falla_si_no_hay_ni_archivo_ni_semilla():
    marca, detalle = diagnostico.estado_tokens(None, refresh_env="")
    assert marca == diagnostico.FALLA
    assert "autorizar" in detalle


def test_diagnostico_falla_si_el_archivo_no_trae_refresh_token():
    marca, detalle = diagnostico.estado_tokens({"access_token": "x", "expires_at": 0})
    assert marca == diagnostico.FALLA
    assert "re-autorizar" in detalle


def test_diagnostico_con_access_token_vigente():
    marca, detalle = diagnostico.estado_tokens(
        {"refresh_token": "r", "expires_at": 6000}, ahora=0.0
    )
    assert marca == diagnostico.OK
    assert "100 min" in detalle


def test_diagnostico_con_access_token_vencido_no_es_falla():
    """Vencido es normal: la corrida lo refresca sola."""
    marca, detalle = diagnostico.estado_tokens(
        {"refresh_token": "r", "expires_at": 10}, ahora=1000.0
    )
    assert marca == diagnostico.OK
    assert "se refresca solo" in detalle


# --------------------------------------------------------------------------
# Render del plan
# --------------------------------------------------------------------------


def test_el_resumen_del_plan_no_repite_el_dia():
    texto = render.resumen(parse_plan(PLAN_CON_TIEMPO))
    assert "L  descanso" in texto
    assert "L lunes" not in texto and "lunes" not in texto


def test_el_resumen_declara_los_km_estimados():
    texto = render.resumen(parse_plan(PLAN_CON_TIEMPO))
    assert "Volumen planificado: 38.31 km  (5.71 estimados de sesiones por tiempo)" in texto


def test_el_resumen_avisa_del_dia_que_no_se_pudo_estimar():
    plan = parse_plan(PLAN_CON_TIEMPO.replace("V: easy 40min Z2", "V: tempo 40min Z4"))
    texto = render.resumen(plan)
    assert "viernes: prescrito en minutos y sin ritmo" in texto
    assert "Volumen planificado: 32.6 km" in texto
    assert "estimados de sesiones por tiempo" not in texto


def test_la_ultima_zona_sin_tope_no_se_imprime_como_menos_uno():
    """Strava manda max=-1 para 'sin tope'; en crudo parecia un dato corrupto."""
    zonas = [
        {"min": 0, "max": 118},
        {"min": 118, "max": 147},
        {"min": 147, "max": 161},
        {"min": 161, "max": 176},
        {"min": 176, "max": -1},
    ]
    texto = diagnostico.describir_zonas(zonas)
    assert texto == "Z1<=118 Z2<=147 Z3<=161 Z4<=176 Z5>176"
    assert "-1" not in texto
