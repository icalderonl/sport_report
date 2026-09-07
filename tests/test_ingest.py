"""Tests de la ingesta completa: Strava simulado -> SQLite -> plan."""
from __future__ import annotations

from datetime import date, datetime

import pytest

from sport_report import fechas
from sport_report.config import TZ, Carga
from sport_report.db.repo import Repo
from sport_report.fechas import semana_de
from sport_report.plan.grammar import parse_plan
from sport_report.plan.store import PlanStore
from sport_report.strava.ingest import Ingesta
from tests.test_grammar import PLAN_SPEC
from tests.test_metricas import ZONAS, stream_plano

SEMANA = semana_de(date(2026, 9, 7))  # lunes 2026-09-07 a domingo 2026-09-13
CARGA = Carga(decoupling_min_puntos=600)


def actividad(
    id_: int,
    fecha: str,
    tipo: str = "Run",
    distancia: float = 10000,
    mov: int = 3000,
    **extra,
) -> dict:
    base = {
        "id": id_,
        "start_date": fecha,
        "type": tipo,
        "name": f"actividad {id_}",
        "distance": distancia,
        "moving_time": mov,
        "elapsed_time": mov + 120,
        "average_heartrate": 145.0,
        "max_heartrate": 172.0,
        "average_cadence": 87.0,
    }
    base.update(extra)
    return base


class ClienteFalso:
    def __init__(self, actividades, streams=None, zonas=ZONAS, vueltas=None):
        self._actividades = actividades
        self._streams = streams or {}
        self._zonas = zonas
        self._vueltas = vueltas or {}
        self.pedidos_streams: list[int] = []
        self.pedidos_vueltas: list[int] = []
        self.rangos: list[tuple] = []

    def actividades(self, desde, hasta):
        self.rangos.append((desde, hasta))
        return list(self._actividades)

    def streams(self, actividad_id, claves=None):
        self.pedidos_streams.append(actividad_id)
        return self._streams.get(actividad_id, {})

    def vueltas(self, actividad_id):
        self.pedidos_vueltas.append(actividad_id)
        return list(self._vueltas.get(actividad_id, []))

    def zonas_hr(self):
        return self._zonas

    def cerrar(self):
        pass


@pytest.fixture
def repo(tmp_path) -> Repo:
    with Repo(tmp_path / "test.db") as r:
        yield r


@pytest.fixture
def plan_store(tmp_path) -> PlanStore:
    s = PlanStore(actual=tmp_path / "plan.json", archivo=tmp_path / "planes")
    s.guardar(parse_plan(PLAN_SPEC), SEMANA)  # W = fuerza
    return s


def _ingesta(cliente, repo, plan_store) -> Ingesta:
    return Ingesta(cliente=cliente, repo=repo, plan_store=plan_store, carga=CARGA)


# --------------------------------------------------------------------------
# Normalizacion
# --------------------------------------------------------------------------


def test_fecha_local_define_el_dia(repo, plan_store):
    # 2026-09-08T02:30:00Z en America/Santiago (UTC-4) es el 7 de septiembre.
    act = actividad(1, "2026-09-08T02:30:00Z")
    s = _ingesta(ClienteFalso([]), repo, plan_store).normalizar(act, {}, ZONAS)
    assert s.fecha_local == "2026-09-07"
    assert s.dia_semana == "L"


def test_normaliza_una_corrida_completa(repo, plan_store):
    act = actividad(1, "2026-09-08T12:00:00Z", average_watts=240, device_watts=True)
    s = _ingesta(ClienteFalso([]), repo, plan_store).normalizar(
        act, stream_plano(1200, hr=145), ZONAS
    )
    assert s.distancia_km == 10.0
    assert s.cadencia_spm == 174.0
    assert s.potencia_w == 240.0
    assert s.carga is not None and s.carga_impreciso is False
    assert s.decoupling_pct == pytest.approx(0.0, abs=0.1)


def test_sesion_indoor_sin_distancia_queda_en_none(repo, plan_store):
    """Dato faltante, no 0 km: la adherencia lo debe tratar distinto (spec 7)."""
    act = actividad(1, "2026-09-08T12:00:00Z", distancia=0)
    s = _ingesta(ClienteFalso([]), repo, plan_store).normalizar(act, {}, ZONAS)
    assert s.distancia_km is None
    assert s.duracion_mov_s == 3000


def test_fuerza_no_lleva_distancia_ni_cadencia(repo, plan_store):
    act = actividad(1, "2026-09-09T12:00:00Z", tipo="WeightTraining", distancia=0)
    s = _ingesta(ClienteFalso([]), repo, plan_store).normalizar(act, {}, ZONAS)
    assert s.es_fuerza is True
    assert (s.distancia_km, s.cadencia_spm, s.carga) == (None, None, None)


def test_potencia_estimada_no_se_guarda(repo, plan_store):
    act = actividad(1, "2026-09-08T12:00:00Z", average_watts=240, device_watts=False)
    s = _ingesta(ClienteFalso([]), repo, plan_store).normalizar(act, {}, ZONAS)
    assert s.potencia_w is None


# --------------------------------------------------------------------------
# Sincronizacion
# --------------------------------------------------------------------------


def test_sincronizar_persiste_y_clasifica(repo, plan_store):
    acts = [
        actividad(1, "2026-09-08T12:00:00Z"),
        actividad(2, "2026-09-09T12:00:00Z", tipo="WeightTraining", distancia=0),
        actividad(3, "2026-09-10T12:00:00Z", tipo="Ride", distancia=30000),
    ]
    cli = ClienteFalso(acts, {1: stream_plano(1200, hr=145)})
    r = _ingesta(cli, repo, plan_store).sincronizar(SEMANA.inicio, SEMANA.fin)

    assert (r.actividades, r.corridas, r.fuerza, r.otras) == (3, 1, 1, 1)
    assert len(repo.sesiones_entre(SEMANA.inicio, SEMANA.fin)) == 3
    # Solo se piden streams de las corridas.
    assert cli.pedidos_streams == [1]


def test_la_fuerza_de_strava_marca_el_plan(repo, plan_store):
    act = actividad(9, "2026-09-09T18:00:00Z", tipo="WeightTraining", distancia=0)
    r = _ingesta(ClienteFalso([act]), repo, plan_store).sincronizar(SEMANA.inicio, SEMANA.fin)

    assert plan_store.para_semana(SEMANA).plan.fuerza_completada["W"] is True
    assert r.fuerza_marcada == ["2026-09-09 (W)"]


def test_fuerza_en_dia_no_planificado_no_es_error(repo, plan_store):
    act = actividad(9, "2026-09-07T18:00:00Z", tipo="Crossfit", distancia=0)
    r = _ingesta(ClienteFalso([act]), repo, plan_store).sincronizar(SEMANA.inicio, SEMANA.fin)
    assert r.fuerza_marcada == []
    assert repo.sesion(9).es_fuerza is True


def test_fuerza_marca_un_plan_ya_archivado(repo, plan_store):
    """El cron del lunes ingiere la semana cerrada, cuyo plan ya se archivo."""
    plan_store.guardar(parse_plan(PLAN_SPEC), fechas.semana_siguiente(SEMANA))
    act = actividad(9, "2026-09-09T18:00:00Z", tipo="WeightTraining", distancia=0)
    _ingesta(ClienteFalso([act]), repo, plan_store).sincronizar(SEMANA.inicio, SEMANA.fin)
    assert plan_store.para_semana(SEMANA).plan.fuerza_completada["W"] is True


def test_la_ingesta_no_desmarca_una_fuerza_manual(repo, plan_store):
    plan_store.marcar_fuerza("W", True, SEMANA)
    _ingesta(ClienteFalso([]), repo, plan_store).sincronizar(SEMANA.inicio, SEMANA.fin)
    assert plan_store.para_semana(SEMANA).plan.fuerza_completada["W"] is True


def test_reingerir_es_idempotente_y_no_regasta_cuota(repo, plan_store):
    acts = [actividad(1, "2026-09-08T12:00:00Z")]
    cli = ClienteFalso(acts, {1: stream_plano(1200, hr=145)})
    ing = _ingesta(cli, repo, plan_store)

    ing.sincronizar(SEMANA.inicio, SEMANA.fin)
    r2 = ing.sincronizar(SEMANA.inicio, SEMANA.fin)

    assert len(repo.sesiones_entre(SEMANA.inicio, SEMANA.fin)) == 1
    assert cli.pedidos_streams == [1]  # no se volvio a pedir
    assert r2.reutilizadas == 1


def test_forzar_vuelve_a_bajar_los_streams(repo, plan_store):
    cli = ClienteFalso([actividad(1, "2026-09-08T12:00:00Z")], {1: stream_plano(1200, hr=145)})
    ing = _ingesta(cli, repo, plan_store)
    ing.sincronizar(SEMANA.inicio, SEMANA.fin)
    ing.sincronizar(SEMANA.inicio, SEMANA.fin, forzar=True)
    assert cli.pedidos_streams == [1, 1]


def test_corrida_sin_hr_avisa_y_no_cuenta_para_la_carga(repo, plan_store):
    s = stream_plano(1200, hr=145)
    del s["heartrate"]
    cli = ClienteFalso([actividad(1, "2026-09-08T12:00:00Z")], {1: s})
    r = _ingesta(cli, repo, plan_store).sincronizar(SEMANA.inicio, SEMANA.fin)

    assert repo.sesion(1).carga is None
    assert r.sin_hr == 1
    assert any("sin HR" in a for a in r.avisos)


def test_sin_zonas_la_carga_queda_marcada_imprecisa(repo, plan_store):
    cli = ClienteFalso(
        [actividad(1, "2026-09-08T12:00:00Z")], {1: stream_plano(1200, hr=145)}, zonas=None
    )
    r = _ingesta(cli, repo, plan_store).sincronizar(SEMANA.inicio, SEMANA.fin)

    assert r.zonas_origen == "fallback" and r.carga_imprecisa is True
    assert any("imprecisa" in a for a in r.avisos)
    assert repo.sesion(1).carga_impreciso is True


def test_las_zonas_se_cachean_en_la_base(repo, plan_store):
    _ingesta(ClienteFalso([]), repo, plan_store).sincronizar(SEMANA.inicio, SEMANA.fin)
    zonas, origen = repo.zonas()
    assert origen == "strava"
    assert [z["max"] for z in zonas] == [120, 140, 155, 170, -1]


def test_si_strava_no_da_zonas_se_usa_la_cache(repo, plan_store):
    _ingesta(ClienteFalso([]), repo, plan_store).sincronizar(SEMANA.inicio, SEMANA.fin)
    r = _ingesta(ClienteFalso([], zonas=None), repo, plan_store).sincronizar(
        SEMANA.inicio, SEMANA.fin
    )
    assert r.zonas_origen == "strava"


def test_el_rango_consultado_cubre_toda_la_semana_local(repo, plan_store):
    cli = ClienteFalso([])
    _ingesta(cli, repo, plan_store).sincronizar(SEMANA.inicio, SEMANA.fin)
    desde, hasta = cli.rangos[0]
    assert desde < datetime(2026, 9, 7, 0, 0, tzinfo=TZ)
    assert hasta >= datetime(2026, 9, 14, 0, 0, tzinfo=TZ)


# --------------------------------------------------------------------------
# Repo
# --------------------------------------------------------------------------


def test_carga_diaria_agrupa_y_omite_dias_sin_carga(repo, plan_store):
    acts = [
        actividad(1, "2026-09-08T12:00:00Z"),
        actividad(2, "2026-09-08T20:00:00Z"),
        actividad(3, "2026-09-10T12:00:00Z"),  # sin streams -> sin carga
    ]
    streams = {1: stream_plano(1200, hr=145), 2: stream_plano(600, hr=130)}
    _ingesta(ClienteFalso(acts, streams), repo, plan_store).sincronizar(
        SEMANA.inicio, SEMANA.fin
    )

    diaria = repo.carga_diaria(SEMANA.inicio, SEMANA.fin)
    assert set(diaria) == {date(2026, 9, 8)}  # el dia sin carga no aparece
    assert diaria[date(2026, 9, 8)] > 0
    assert repo.dias_con_datos(SEMANA.inicio, SEMANA.fin) == 1


def test_bitacora_de_corridas(repo):
    cid = repo.abrir_corrida()
    repo.cerrar_corrida(cid, "ok", "todo bien")
    ultima = repo.ultimas_corridas(1)[0]
    assert ultima["estado"] == "ok" and ultima["fin_utc"]


def test_primera_fecha(repo, plan_store):
    _ingesta(ClienteFalso([actividad(1, "2026-09-08T12:00:00Z")]), repo, plan_store).sincronizar(
        SEMANA.inicio, SEMANA.fin
    )
    assert repo.primera_fecha() == date(2026, 9, 8)


# --------------------------------------------------------------------------
# Vueltas
# --------------------------------------------------------------------------

VUELTAS_CRUDAS = [
    {"lap_index": 1, "distance": 2000.0, "moving_time": 800},
    {"lap_index": 2, "distance": 1000.0, "moving_time": 240},
    {"lap_index": 3, "distance": 200.0, "moving_time": 90},
]


def test_las_vueltas_se_bajan_y_se_guardan(repo, plan_store):
    cli = ClienteFalso(
        [actividad(1, "2026-09-08T12:00:00Z")],
        {1: stream_plano(1200, hr=145)},
        vueltas={1: VUELTAS_CRUDAS},
    )

    _ingesta(cli, repo, plan_store).sincronizar(SEMANA.inicio, SEMANA.fin)

    assert cli.pedidos_vueltas == [1]
    assert [(v.indice, v.distancia_km) for v in repo.vueltas(1)] == [
        (1, 2.0), (2, 1.0), (3, 0.2)
    ]
    assert repo.sesion(1).vueltas_procesadas is True


def test_reingerir_no_vuelve_a_pedir_las_vueltas(repo, plan_store):
    cli = ClienteFalso(
        [actividad(1, "2026-09-08T12:00:00Z")],
        {1: stream_plano(1200, hr=145)},
        vueltas={1: VUELTAS_CRUDAS},
    )
    ing = _ingesta(cli, repo, plan_store)

    ing.sincronizar(SEMANA.inicio, SEMANA.fin)
    ing.sincronizar(SEMANA.inicio, SEMANA.fin)

    assert cli.pedidos_vueltas == [1]


def test_una_actividad_sin_vueltas_no_se_pregunta_para_siempre(repo, plan_store):
    """No tener vueltas es una respuesta valida: se marca y no se reintenta."""
    cli = ClienteFalso([actividad(1, "2026-09-08T12:00:00Z")], {1: stream_plano(1200, hr=145)})
    ing = _ingesta(cli, repo, plan_store)

    r1 = ing.sincronizar(SEMANA.inicio, SEMANA.fin)
    ing.sincronizar(SEMANA.inicio, SEMANA.fin)

    assert r1.sin_vueltas == 1
    assert cli.pedidos_vueltas == [1]
    assert repo.sesion(1).vueltas_procesadas is True


def test_si_las_vueltas_fallan_se_reintentan_la_proxima_vez(repo, plan_store):
    """Un fallo de red no es "esta actividad no tiene vueltas"."""
    from sport_report.strava.errors import StravaError

    class ClienteQueFalla(ClienteFalso):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.romper = True

        def vueltas(self, actividad_id):
            self.pedidos_vueltas.append(actividad_id)
            if self.romper:
                raise StravaError("timeout")
            return list(self._vueltas.get(actividad_id, []))

    cli = ClienteQueFalla(
        [actividad(1, "2026-09-08T12:00:00Z")],
        {1: stream_plano(1200, hr=145)},
        vueltas={1: VUELTAS_CRUDAS},
    )
    ing = _ingesta(cli, repo, plan_store)

    ing.sincronizar(SEMANA.inicio, SEMANA.fin)
    assert repo.sesion(1).vueltas_procesadas is False

    cli.romper = False
    ing.sincronizar(SEMANA.inicio, SEMANA.fin)

    assert cli.pedidos_vueltas == [1, 1]
    assert repo.sesion(1).vueltas_procesadas is True
    assert len(repo.vueltas(1)) == 3


def test_a_una_sesion_vieja_solo_se_le_piden_las_vueltas(repo, plan_store):
    """El caso del despliegue: la base ya tiene el historico con los streams
    procesados y solo faltan las vueltas. Re-bajar los streams seria gastar la
    cuota entera de Strava para no cambiar ni una cifra."""
    cli = ClienteFalso(
        [actividad(1, "2026-09-08T12:00:00Z")],
        {1: stream_plano(1200, hr=145)},
        vueltas={1: VUELTAS_CRUDAS},
    )
    ing = _ingesta(cli, repo, plan_store)
    ing.sincronizar(SEMANA.inicio, SEMANA.fin)
    carga_antes = repo.sesion(1).carga
    repo.marcar_vueltas_procesadas(1, False)
    repo.guardar_vueltas(1, [])
    cli.pedidos_streams.clear()
    cli.pedidos_vueltas.clear()

    r = ing.sincronizar(SEMANA.inicio, SEMANA.fin)

    assert cli.pedidos_vueltas == [1]
    assert cli.pedidos_streams == [], "no habia que volver a bajar el stream"
    assert r.reutilizadas == 1
    assert len(repo.vueltas(1)) == 3
    assert repo.sesion(1).carga == carga_antes, "la carga no se perdio"


def test_no_se_piden_vueltas_de_lo_que_no_es_carrera(repo, plan_store):
    cli = ClienteFalso([actividad(1, "2026-09-08T12:00:00Z", tipo="Ride", distancia=60000)])

    _ingesta(cli, repo, plan_store).sincronizar(SEMANA.inicio, SEMANA.fin)

    assert cli.pedidos_vueltas == []
