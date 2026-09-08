"""Regresiones de los bugs encontrados en la auditoria.

Un test por bug arreglado, con el nombre del sintoma y no el de la funcion:
lo que hay que evitar es que el sintoma vuelva, no que la implementacion
cambie. Ninguno toca la red.
"""
from __future__ import annotations

import itertools
import os
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from sport_report import fechas, storage
from sport_report.db.models import SesionReal
from sport_report.db.repo import Repo
from sport_report.engine import adherencia, foster, report
from sport_report.fechas import semana_de, semana_siguiente
from sport_report.plan.grammar import parse_plan
from sport_report.plan.store import PlanStore
from sport_report.run_weekly import ERROR, PARCIAL, ejecutar
from sport_report.storage import LockOcupado, escribir_json, lock, proceso_vivo
from sport_report.telegram import comandos, sender
from sport_report.telegram.formato import LIMITE_TELEGRAM, trozos
from tests.test_grammar import PLAN_SPEC

LUNES = date(2026, 8, 31)
SEMANA = semana_de(LUNES)

SOLO_LUNES = """semana: 1
L: easy 8km Z2
M: rest
W: rest
J: rest
V: rest
S: rest
D: rest"""


def _sesion(offset: int, **kw) -> SesionReal:
    f = LUNES + timedelta(days=offset)
    base = dict(
        fuente="strava",
        id_externo=str(9000 + offset),
        fecha_utc=f"{f}T12:00:00+00:00",
        fecha_local=f.isoformat(),
        dia_semana="LMWJVSD"[offset],
        tipo="Run",
        es_fuerza=False,
        distancia_km=10.0,
        duracion_mov_s=3000,
    )
    base.update(kw)
    return SesionReal(**base)


@pytest.fixture
def repo(tmp_path) -> Repo:
    r = Repo(tmp_path / "t.db")
    yield r
    r.cerrar()


@pytest.fixture
def store(tmp_path) -> PlanStore:
    return PlanStore(actual=tmp_path / "plan.json", archivo=tmp_path / "planes")


# --------------------------------------------------------------------------
# B1 · Las actividades que no son carrera no cuentan como kilometros corridos
# --------------------------------------------------------------------------


def test_una_salida_en_bici_no_entra_al_volumen_de_running():
    corrida = _sesion(0, distancia_km=8.0)
    bici = _sesion(1, fuente="strava", id_externo="999", tipo="Ride", distancia_km=60.0)

    r = adherencia.calcular(parse_plan(SOLO_LUNES), SEMANA, [corrida, bici])

    assert r.volumen_real_km == 8.0
    assert r.volumen_pct == 100.0


def test_una_salida_en_bici_sigue_rompiendo_un_dia_de_descanso():
    """El filtro es para los kilometros, no para detectar el dia libre."""
    bici = _sesion(1, tipo="Ride", distancia_km=60.0)

    r = adherencia.calcular(parse_plan(SOLO_LUNES), SEMANA, [bici])

    martes = next(d for d in r.dias if d.dia == "M")
    assert martes.estado == adherencia.DESCANSO_ROTO
    assert "Ride" in martes.nota


def test_una_salida_en_bici_no_cuenta_como_la_sesion_del_dia():
    """El lunes hay 8km planificados y solo se pedaleo: es incumplimiento."""
    bici = _sesion(0, tipo="Ride", distancia_km=60.0)

    r = adherencia.calcular(parse_plan(SOLO_LUNES), SEMANA, [bici])

    lunes = next(d for d in r.dias if d.dia == "L")
    assert lunes.estado == adherencia.SIN_SESION
    assert lunes.real is None


def test_el_grafico_de_volumen_solo_suma_carreras(repo):
    repo.guardar_sesion(_sesion(0, distancia_km=8.0))
    repo.guardar_sesion(_sesion(1, fuente="strava", id_externo="999", tipo="Ride", distancia_km=60.0))

    serie = dict(repo.volumen_semanal(SEMANA.fin, 1))

    assert serie[LUNES] == 8.0


def test_otro_deporte_sin_hr_no_se_reporta_como_hueco_de_datos(repo):
    repo.guardar_sesion(_sesion(0, distancia_km=8.0, carga=200.0))
    repo.guardar_sesion(_sesion(1, fuente="strava", id_externo="999", tipo="Ride", carga=None))

    datos = report.construir(SEMANA, repo)

    assert datos["carga"]["sesiones_sin_carga"] == 0
    assert datos["deriva_cardiaca"]["sin_dato"] == 1  # solo la corrida


# --------------------------------------------------------------------------
# B2 · `trozos` nunca devuelve un mensaje vacio (Telegram lo rechaza con 400)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "texto",
    [
        "x" * LIMITE_TELEGRAM + "\n" + "y" * 10,
        "x" * LIMITE_TELEGRAM,
        "x" * (LIMITE_TELEGRAM - 1) + "\n" + "y" * LIMITE_TELEGRAM,
    ],
)
def test_trozos_nunca_devuelve_un_trozo_vacio(texto):
    partes = trozos(texto)

    assert partes, "siempre tiene que salir al menos un trozo"
    assert all(p for p in partes), f"hay un trozo vacio en {[len(p) for p in partes]}"
    assert all(len(p) <= LIMITE_TELEGRAM for p in partes)
    assert "".join(partes).replace("\n", "") == texto.replace("\n", "")


# --------------------------------------------------------------------------
# B4 · El domingo la semana todavia esta en curso, tambien en /volumen
# --------------------------------------------------------------------------


@pytest.fixture
def historico_del_grafico(monkeypatch):
    """Captura el `historico` que /volumen le pasa al dibujante.

    Hay que mirar eso y no el texto de vuelta: el flag `ultima_en_curso` es lo
    unico que decide si la ultima barra se pinta como semana a medias, y no
    aparece en la respuesta.
    """
    capturado: dict = {}

    def falso(historico, destino=None):
        capturado.update(historico)
        return destino or Path("volumen.png")

    monkeypatch.setattr("sport_report.grafico.volumen_png", falso)
    return capturado


@pytest.mark.parametrize("offset", range(7))
def test_volumen_marca_la_semana_en_curso_todos_los_dias(repo, historico_del_grafico, offset):
    """El domingo la semana sigue en curso: con `<` la barra salia como cerrada."""
    repo.guardar_sesion(_sesion(0, distancia_km=8.0))
    hoy = LUNES + timedelta(days=offset)

    comandos.cmd_volumen(repo=repo, hoy=hoy, semanas=2)

    dia = "LMWJVSD"[offset]
    assert historico_del_grafico["ultima_en_curso"] is True, f"{dia} {hoy} no marcado"


def test_volumen_no_marca_en_curso_una_semana_ya_cerrada(repo, historico_del_grafico):
    repo.guardar_sesion(_sesion(0, distancia_km=8.0))

    # Consultado el lunes siguiente, la ultima barra es la semana nueva (vacia)
    # y la que se cerro queda atras: ninguna barra vieja se marca a medias.
    comandos.cmd_volumen(repo=repo, hoy=SEMANA.fin + timedelta(days=1), semanas=2)

    semanas = historico_del_grafico["semanas"]
    assert semanas[-1]["lunes"] == (SEMANA.inicio + timedelta(days=7)).isoformat()
    assert semanas[0]["km"] == 8.0


# --------------------------------------------------------------------------
# B5 · /fuerza marca la semana en curso, no el plan vigente
# --------------------------------------------------------------------------


def test_fuerza_marca_la_semana_en_curso_aunque_el_vigente_sea_la_proxima(store):
    """El domingo se carga `/setplan proxima`; `/fuerza` sigue siendo de hoy."""
    domingo = SEMANA.fin
    store.guardar(parse_plan(PLAN_SPEC), SEMANA)
    store.guardar(parse_plan(PLAN_SPEC), semana_siguiente(SEMANA))

    respuesta = comandos.cmd_fuerza(store, "W", hoy=domingo)

    assert "marcada como cumplida" in respuesta
    assert store.para_semana(SEMANA).plan.fuerza_completada["W"] is True
    assert store.para_semana(semana_siguiente(SEMANA)).plan.fuerza_completada["W"] is False


def test_fuerza_acepta_un_sufijo_de_semana(store):
    store.guardar(parse_plan(PLAN_SPEC), SEMANA)
    store.guardar(parse_plan(PLAN_SPEC), semana_siguiente(SEMANA))

    comandos.cmd_fuerza(store, "W proxima", hoy=SEMANA.fin)

    assert store.para_semana(semana_siguiente(SEMANA)).plan.fuerza_completada["W"] is True
    assert store.para_semana(SEMANA).plan.fuerza_completada["W"] is False


def test_fuerza_sin_plan_para_esa_semana_lo_dice(store):
    store.guardar(parse_plan(PLAN_SPEC), SEMANA)

    respuesta = comandos.cmd_fuerza(store, "W proxima", hoy=SEMANA.fin)

    assert "No hay plan guardado" in respuesta


def test_fuerza_sigue_funcionando_sin_sufijo_ni_planes_cruzados(store, monkeypatch):
    monkeypatch.setattr(fechas, "hoy_local", lambda: LUNES + timedelta(days=2))
    store.guardar(parse_plan(PLAN_SPEC), SEMANA)

    assert "marcada como cumplida" in comandos.cmd_fuerza(store, "miercoles")


# --------------------------------------------------------------------------
# B7 · Una semana que aun no empieza no revienta el motor
# --------------------------------------------------------------------------


def test_monotony_sobre_una_semana_que_no_empezo_no_lanza():
    r = foster.calcular({}, inicio=LUNES, dias=0)

    assert r.monotony is None
    assert r.confiable is False
    assert "no empieza" in r.motivo


def test_reporte_con_hasta_anterior_al_lunes_no_lanza(repo):
    datos = report.construir(SEMANA, repo, hasta=LUNES - timedelta(days=1))

    assert datos["semana"]["dias_transcurridos"] == 0
    assert datos["monotony"]["confiable"] is False
    assert all(d["estado"] == "pendiente" for d in datos["adherencia"]["dias"] if d["dia"])


# --------------------------------------------------------------------------
# B8 · Los streams de una corrida sin HR no se re-descargan cada vez
# --------------------------------------------------------------------------


def test_la_migracion_agrega_streams_procesados_a_una_base_vieja(tmp_path):
    vieja = tmp_path / "vieja.db"
    con = sqlite3.connect(vieja)
    con.executescript(
        """
        CREATE TABLE sesiones (
            strava_id INTEGER PRIMARY KEY, fecha_utc TEXT NOT NULL,
            fecha_local TEXT NOT NULL, dia_semana TEXT NOT NULL,
            tipo_strava TEXT NOT NULL, es_fuerza INTEGER NOT NULL DEFAULT 0,
            nombre TEXT, distancia_km REAL, duracion_mov_s INTEGER,
            duracion_tot_s INTEGER, hr_promedio REAL, hr_maximo REAL,
            cadencia_spm REAL, potencia_w REAL, decoupling_pct REAL, carga REAL,
            carga_impreciso INTEGER NOT NULL DEFAULT 0, ingerido_en TEXT NOT NULL);
        INSERT INTO sesiones (strava_id, fecha_utc, fecha_local, dia_semana,
            tipo_strava, carga, ingerido_en)
            VALUES (1,'2026-08-31T12:00:00+00:00','2026-08-31','L','Run',420.0,'x');
        INSERT INTO sesiones (strava_id, fecha_utc, fecha_local, dia_semana,
            tipo_strava, carga, ingerido_en)
            VALUES (2,'2026-09-01T12:00:00+00:00','2026-09-01','M','Run',NULL,'x');
        """
    )
    con.commit()
    con.close()

    with Repo(vieja) as r:
        # La que ya tenia carga no hay que volver a bajarla; la otra si, una vez.
        assert r.sesion("strava", "1").streams_procesados is True
        assert r.sesion("strava", "2").streams_procesados is False
    with Repo(vieja) as r:  # reabrir no vuelve a migrar ni rompe
        assert r.sesion("strava", "1").streams_procesados is True


# --------------------------------------------------------------------------
# B11 · El candado no se le quita a un proceso vivo
# --------------------------------------------------------------------------


def test_proceso_vivo_distingue_un_pid_real_de_uno_inexistente():
    assert proceso_vivo(os.getpid()) is True
    assert proceso_vivo(999_999) is False
    assert proceso_vivo(0) is False


def test_un_candado_viejo_de_un_proceso_vivo_se_respeta(tmp_path):
    destino = tmp_path / "x.json"
    candado = destino.with_suffix(".json.lock")
    candado.write_text(str(os.getpid()), encoding="utf-8")
    viejo = time.time() - 9999
    os.utime(candado, (viejo, viejo))

    with pytest.raises(LockOcupado):
        with lock(destino, timeout=0.2):
            pass


def test_un_candado_viejo_de_un_proceso_muerto_se_recicla(tmp_path):
    destino = tmp_path / "x.json"
    candado = destino.with_suffix(".json.lock")
    candado.write_text("999999", encoding="utf-8")
    viejo = time.time() - 9999
    os.utime(candado, (viejo, viejo))

    with lock(destino, timeout=0.2):
        pass
    assert not candado.exists()


def test_una_escritura_fallida_no_deja_temporales(tmp_path):
    destino = tmp_path / "x.json"

    with pytest.raises(TypeError):
        escribir_json(destino, {"no serializable": object()})

    assert list(tmp_path.glob("*.tmp*")) == []


# --------------------------------------------------------------------------
# B13 · Una corrida que quedo abierta no se cuenta como viva para siempre
# --------------------------------------------------------------------------


def test_una_corrida_vieja_sin_cerrar_se_marca_interrumpida(repo):
    hace_ocho_horas = (datetime.now(timezone.utc) - timedelta(hours=8)).isoformat(
        timespec="seconds"
    )
    repo.con.execute(
        "INSERT INTO corridas (inicio_utc, estado) VALUES (?, 'en_curso')",
        (hace_ocho_horas,),
    )
    repo.con.commit()

    repo.abrir_corrida()

    estados = [c["estado"] for c in repo.ultimas_corridas(5)]
    assert "interrumpida" in estados


def test_una_corrida_reciente_no_se_toca(repo):
    """Un backfill a mano puede solaparse con el cron: no matarlo."""
    hace_un_minuto = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(
        timespec="seconds"
    )
    repo.con.execute(
        "INSERT INTO corridas (inicio_utc, estado) VALUES (?, 'en_curso')",
        (hace_un_minuto,),
    )
    repo.con.commit()

    repo.sanear_corridas()

    assert [c["estado"] for c in repo.ultimas_corridas(5)] == ["en_curso"]


# --------------------------------------------------------------------------
# B14 / B15 · Un plan ilegible se ignora, no tumba la corrida
# --------------------------------------------------------------------------


def test_un_plan_guardado_al_que_le_faltan_dias_se_ignora(store):
    store.guardar(parse_plan(PLAN_SPEC), SEMANA)
    d = storage.leer_json(store.actual)
    d["plan"]["sesiones"].pop("J")
    escribir_json(store.actual, d)

    assert store.cargar() is None  # no un KeyError


def test_un_plan_de_un_formato_futuro_se_rechaza(store):
    store.guardar(parse_plan(PLAN_SPEC), SEMANA)
    d = storage.leer_json(store.actual)
    d["version"] = 99
    escribir_json(store.actual, d)

    assert store.cargar() is None


def test_el_reporte_sale_igual_con_el_plan_corrupto(repo, store):
    store.guardar(parse_plan(PLAN_SPEC), SEMANA)
    d = storage.leer_json(store.actual)
    d["plan"]["sesiones"].pop("J")
    escribir_json(store.actual, d)
    repo.guardar_sesion(_sesion(0, distancia_km=8.0))

    datos = report.construir(SEMANA, repo, store)

    assert datos["semana"]["plan_cargado"] is False
    assert datos["volumen"]["real_km"] == 8.0


# --------------------------------------------------------------------------
# B6 / B17 · El envio reintenta, y un envio a medias es `parcial`, no `error`
# --------------------------------------------------------------------------


@pytest.fixture
def telegram_falso(monkeypatch):
    """Sustituye el transporte de httpx y no deja dormir de verdad."""
    real = httpx.Client
    esperas: list[float] = []

    def montar(handler):
        monkeypatch.setattr(
            sender.httpx,
            "Client",
            lambda **kw: real(transport=httpx.MockTransport(handler)),
        )
        return esperas

    yield montar, esperas


def test_el_envio_reintenta_un_corte_de_red(telegram_falso):
    montar, esperas = telegram_falso
    intentos = itertools.count()

    def flaky(request):
        if next(intentos) < 2:
            raise httpx.ConnectError("sin red")
        return httpx.Response(200, json={"ok": True})

    montar(flaky)
    r = sender.enviar("hola", chat_id="1", token="t", dormir=esperas.append)

    assert bool(r) is True
    assert esperas == list(sender.ESPERAS_S[:2])


def test_el_envio_respeta_el_retry_after_de_telegram(telegram_falso):
    montar, esperas = telegram_falso
    intentos = itertools.count()

    def flood(request):
        if next(intentos) == 0:
            return httpx.Response(429, json={"parameters": {"retry_after": 7}})
        return httpx.Response(200, json={"ok": True})

    montar(flood)
    r = sender.enviar("hola", chat_id="1", token="t", dormir=esperas.append)

    assert bool(r) is True
    assert esperas == [7.0]


def test_un_400_no_se_reintenta(telegram_falso):
    montar, esperas = telegram_falso
    llamadas = itertools.count()

    def malo(request):
        next(llamadas)
        return httpx.Response(400, text="Bad Request")

    montar(malo)
    r = sender.enviar("hola", chat_id="1", token="t", dormir=esperas.append)

    assert bool(r) is False
    assert next(llamadas) == 1, "una peticion mal formada no mejora repitiendola"
    assert esperas == []


def test_un_mensaje_vacio_no_se_manda(telegram_falso):
    montar, _ = telegram_falso
    montar(lambda request: pytest.fail("no deberia salir ninguna peticion"))

    assert bool(sender.enviar("   ", chat_id="1", token="t")) is False


def test_un_envio_a_medias_deja_la_corrida_en_parcial(repo, store):
    store.guardar(parse_plan(PLAN_SPEC), SEMANA)

    r = ejecutar(
        SEMANA,
        repo=repo,
        plan_store=store,
        narrador=None,
        guardar=False,
        enviador=lambda m: sender.ResultadoEnvio(enviadas=1, totales=2),
    )

    assert r.estado == PARCIAL
    assert "incompleto" in r.problemas[0]


def test_un_envio_que_no_sale_sigue_siendo_error(repo, store):
    store.guardar(parse_plan(PLAN_SPEC), SEMANA)

    r = ejecutar(
        SEMANA,
        repo=repo,
        plan_store=store,
        narrador=None,
        guardar=False,
        enviador=lambda m: sender.ResultadoEnvio(enviadas=0, totales=2),
    )

    assert r.estado == ERROR


# --------------------------------------------------------------------------
# B3 · El grafico se escribe en un temporal propio de cada proceso
# --------------------------------------------------------------------------


def test_el_temporal_del_grafico_lleva_el_pid(tmp_path):
    pytest.importorskip("matplotlib")
    from sport_report import grafico

    destino = tmp_path / "volumen.png"
    historico = {
        "semanas": [{"lunes": LUNES.isoformat(), "km": 40.0}],
        "primera_fecha_con_datos": LUNES.isoformat(),
        "ultima_en_curso": False,
    }

    vistos: list[str] = []
    real = os.replace

    def espiar(origen, destino_):
        vistos.append(Path(origen).name)
        return real(origen, destino_)

    grafico.os.replace = espiar
    try:
        assert grafico.volumen_png(historico, destino) == destino
    finally:
        grafico.os.replace = real

    assert vistos and str(os.getpid()) in vistos[0]
    assert list(tmp_path.glob("*.tmp*")) == []


# --------------------------------------------------------------------------
# B16 · Los CLI validan sus argumentos en vez de lanzar un traceback
# --------------------------------------------------------------------------


@pytest.fixture
def sin_tocar_disco(monkeypatch):
    """Hace explotar `Repo` en los CLI.

    Estos tests llaman a `main()`, que sin argumentos validos abre
    `config.DATA_DIR` —la base REAL del usuario, no un tmp_path—. La validacion
    tiene que cortar antes de cualquier E/S, y asi el test lo demuestra en vez
    de suponerlo.
    """

    def prohibido(*a, **kw):
        raise AssertionError("el CLI toco la base antes de validar sus argumentos")

    from sport_report import run_weekly as rw
    from sport_report import backfill as bf

    monkeypatch.setattr(bf, "Repo", prohibido)
    monkeypatch.setattr(rw, "Repo", prohibido)


def test_backfill_rechaza_un_argumento_que_no_es_numero(capsys, sin_tocar_disco):
    from sport_report import backfill

    assert backfill.main(["muchos"]) == 2
    assert "no es un numero de dias" in capsys.readouterr().err


def test_backfill_rechaza_dias_negativos(capsys, sin_tocar_disco):
    from sport_report import backfill

    assert backfill.main(["-5"]) == 2
    assert "1 o mas" in capsys.readouterr().err


def test_run_weekly_rechaza_una_semana_mal_escrita(capsys, sin_tocar_disco):
    from sport_report import run_weekly

    assert run_weekly.main(["--semana", "ayer", "--dry-run"]) == 2
    assert "YYYY-MM-DD" in capsys.readouterr().err


# --------------------------------------------------------------------------
# B10 · El callback de OAuth exige el `state` de esta sesion
# --------------------------------------------------------------------------


def test_la_url_de_autorizacion_lleva_el_state():
    from sport_report.strava.auth import StravaAuth

    url = StravaAuth("123", "secreto").url_autorizacion("http://localhost:8721/x", state="abc")

    assert "state=abc" in url


def test_sin_state_no_se_pide_state_a_strava():
    """Compatibilidad: quien no lo use no manda el parametro vacio."""
    from sport_report.strava.auth import StravaAuth

    assert "state=" not in StravaAuth("123", "s").url_autorizacion("http://localhost:8721/x")


# --------------------------------------------------------------------------
# La banda de adherencia castigaba las series por kilometros que el plan
# nunca declara (visto en el reporte real del 2026-08-31)
# --------------------------------------------------------------------------


def test_una_sesion_de_series_no_se_cuenta_incumplida():
    """El caso que bajo la adherencia real a 75% sin que nadie fallara un dia.

    Una sesion con `estructura=` se compara contra la distancia DURA, que no
    incluye la recuperacion trotada entre repeticiones; lo real que llega de
    Strava si la incluye. Los dos lados no miden lo mismo, asi que toda sesion
    de series lee por encima de 100% por construccion —116.3% aqui, por 1.4km
    de recuperacion— y con el techo en 110% se contaba como incumplimiento.
    """
    r = adherencia.calcular(parse_plan(PLAN_SPEC), SEMANA, [_sesion(3, distancia_km=10.0)])

    jueves = next(d for d in r.dias if d.dia == "J")
    assert (jueves.objetivo, jueves.real) == (8.6, 10.0)
    assert jueves.pct == pytest.approx(116.3, abs=0.1)
    assert jueves.estado == adherencia.CUMPLIDA


def test_la_semana_real_que_salia_75_por_ciento_sale_100():
    """Los cuatro dias de carrera se corrieron; solo el jueves caia fuera."""
    reales = [
        _sesion(1, distancia_km=8.0),   # M easy 8km    -> 100%
        _sesion(3, distancia_km=10.0),  # J series 8.6km -> 116.3%
        _sesion(4, distancia_km=10.0),  # V prog 10km    -> 100%
        _sesion(6, distancia_km=16.1),  # D long 16km    -> 100.6%
    ]

    r = adherencia.calcular(parse_plan(PLAN_SPEC), SEMANA, reales)

    assert (r.sesiones_evaluables, r.sesiones_cumplidas) == (4, 4)
    assert r.pct_global == 100.0


def test_la_banda_ancha_sigue_viendo_una_desviacion_de_verdad():
    """80-120% no es lo mismo que no comprobar nada."""
    corto = adherencia.calcular(parse_plan(SOLO_LUNES), SEMANA, [_sesion(0, distancia_km=5.5)])
    largo = adherencia.calcular(parse_plan(SOLO_LUNES), SEMANA, [_sesion(0, distancia_km=11.0)])

    assert next(d for d in corto.dias if d.dia == "L").estado == adherencia.BAJO_PLAN
    assert next(d for d in largo.dias if d.dia == "L").estado == adherencia.SOBRE_PLAN
