"""Ingesta completa desde intervals.icu: cliente simulado -> SQLite -> plan.

Calcado de tests/test_ingest.py (Strava) para que las dos fuentes se prueben
con el mismo criterio, mas lo que solo esta fuente trae: dinamica avanzada y
bienestar. El hilo conductor de los casos raros es siempre el mismo: lo que la
fuente no trajo tiene que quedar en `None`.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from sport_report.config import TZ, Carga
from sport_report.db.repo import Repo
from sport_report.fechas import semana_de
from sport_report.intervals.errors import IntervalsError
from sport_report.intervals.ingest import (
    Ingesta,
    normalizar_bienestar,
    normalizar_intervalos,
)
from sport_report.plan.grammar import parse_plan
from sport_report.plan.store import PlanStore
from tests.test_grammar import PLAN_SPEC
from tests.test_metricas import ZONAS, stream_plano

SEMANA = semana_de(date(2026, 9, 7))  # lunes 2026-09-07 a domingo 2026-09-13
CARGA = Carga(decoupling_min_puntos=600)


def actividad(
    id_: str,
    fecha: str,
    tipo: str = "Run",
    distancia: float = 10000,
    mov: int = 3000,
    **extra,
) -> dict:
    """Actividad como la devuelve intervals.icu, con los candidatos por defecto."""
    base = {
        "id": id_,
        "start_date_local": fecha,
        "type": tipo,
        "name": f"actividad {id_}",
        "distance": distancia,
        "moving_time": mov,
        "elapsed_time": mov + 120,
        "average_heartrate": 145.0,
        "max_heartrate": 172.0,
        "average_cadence": 87.0,
        "average_gct": 232.0,
        "average_vertical_oscillation": 8.4,
        "average_vertical_ratio": 7.9,
    }
    base.update(extra)
    return base


def bienestar(fecha: str, **extra) -> dict:
    base = {
        "id": fecha,
        "hrv": 68.0,
        "restingHR": 48.0,
        "sleepSecs": 27000,  # 7.5 h
        "sleepScore": 82.0,
        "readiness": 71.0,
    }
    base.update(extra)
    return base


class ClienteFalso:
    def __init__(
        self,
        actividades,
        streams=None,
        zonas=ZONAS,
        intervalos=None,
        bienestar=None,
        falla_bienestar=False,
    ):
        self._actividades = actividades
        self._streams = streams or {}
        self._zonas = zonas
        self._intervalos = intervalos or {}
        self._bienestar = bienestar if bienestar is not None else []
        self._falla_bienestar = falla_bienestar
        self.pedidos_streams: list = []
        self.pedidos_intervalos: list = []
        self.rangos_bienestar: list = []

    def actividades(self, desde, hasta):
        return list(self._actividades)

    def streams(self, actividad_id, claves=None):
        self.pedidos_streams.append(actividad_id)
        return self._streams.get(actividad_id, {})

    def intervalos(self, actividad_id):
        self.pedidos_intervalos.append(actividad_id)
        return list(self._intervalos.get(actividad_id, []))

    def zonas_hr(self):
        return self._zonas

    def bienestar(self, desde, hasta):
        self.rangos_bienestar.append((desde, hasta))
        if self._falla_bienestar:
            raise IntervalsError("wellness caido")
        return list(self._bienestar)

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


def test_normaliza_una_corrida_completa_con_dinamica(repo, plan_store):
    act = actividad("i1", "2026-09-08T08:00:00", average_watts=240, device_watts=True)
    s = _ingesta(ClienteFalso([]), repo, plan_store).normalizar(
        act, stream_plano(1200, hr=145), ZONAS
    )
    assert (s.fuente, s.id_externo) == ("intervals", "i1")
    assert (s.fecha_local, s.dia_semana) == ("2026-09-08", "M")
    assert s.distancia_km == 10.0
    assert s.cadencia_spm == 174.0
    assert s.potencia_w == 240.0
    # Lo que Strava no puede dar y por lo que se cambio de fuente.
    assert (s.gct_ms, s.oscilacion_vertical_cm, s.ratio_vertical_pct) == (232.0, 8.4, 7.9)
    assert s.carga is not None and s.carga_impreciso is False


def test_la_fecha_local_de_intervals_ya_viene_en_la_zona_del_atleta(repo, plan_store):
    """`start_date_local` no trae zona: interpretarlo como UTC corre el dia."""
    act = actividad("i1", "2026-09-07T23:30:00")
    s = _ingesta(ClienteFalso([]), repo, plan_store).normalizar(act, {}, ZONAS)
    assert (s.fecha_local, s.dia_semana) == ("2026-09-07", "L")


def test_si_solo_viene_la_fecha_utc_se_convierte(repo, plan_store):
    # 2026-09-08T02:30:00Z en America/Santiago (UTC-4) es el 7 de septiembre.
    act = actividad("i1", "2026-09-08T02:30:00", start_date_local=None)
    act["start_date"] = "2026-09-08T02:30:00Z"
    s = _ingesta(ClienteFalso([]), repo, plan_store).normalizar(act, {}, ZONAS)
    assert s.fecha_local == "2026-09-07"


def test_una_actividad_sin_fecha_no_se_inventa(repo, plan_store):
    act = actividad("i1", "2026-09-08T08:00:00")
    act["start_date_local"] = None
    with pytest.raises(IntervalsError):
        _ingesta(ClienteFalso([]), repo, plan_store).normalizar(act, {}, ZONAS)


def test_sin_dinamica_los_campos_quedan_en_none_no_en_cero(repo, plan_store):
    """El reloj puede no medirla. Un GCT de 0 ms diria que el pie no toco el suelo."""
    act = actividad(
        "i1",
        "2026-09-08T08:00:00",
        average_gct=None,
        average_vertical_oscillation=None,
        average_vertical_ratio=None,
    )
    s = _ingesta(ClienteFalso([]), repo, plan_store).normalizar(act, {}, ZONAS)
    assert s.gct_ms is None
    assert s.oscilacion_vertical_cm is None
    assert s.ratio_vertical_pct is None


def test_la_dinamica_llega_con_otro_nombre_de_campo_y_se_encuentra(repo, plan_store):
    """Es el seguro contra que la API use otro nombre del que se supuso."""
    act = actividad(
        "i1",
        "2026-09-08T08:00:00",
        average_gct=None,
        average_vertical_oscillation=None,
        ground_time=0.238,  # en segundos
        vertical_oscillation=91.0,  # en milimetros
    )
    s = _ingesta(ClienteFalso([]), repo, plan_store).normalizar(act, {}, ZONAS)
    assert s.gct_ms == 238.0
    assert s.oscilacion_vertical_cm == 9.1


def test_la_dinamica_no_se_le_pone_a_la_fuerza_ni_a_la_bici(repo, plan_store):
    for tipo in ("WeightTraining", "Ride"):
        act = actividad("i1", "2026-09-08T08:00:00", tipo=tipo)
        s = _ingesta(ClienteFalso([]), repo, plan_store).normalizar(act, {}, ZONAS)
        assert (s.gct_ms, s.oscilacion_vertical_cm, s.cadencia_spm) == (None, None, None)


def test_la_potencia_sin_device_watts_no_se_guarda(repo, plan_store):
    """Una potencia estimada por el reloj no es una potencia medida."""
    act = actividad("i1", "2026-09-08T08:00:00", average_watts=240)
    s = _ingesta(ClienteFalso([]), repo, plan_store).normalizar(act, {}, ZONAS)
    assert s.potencia_w is None


def test_sesion_indoor_sin_distancia_queda_en_none(repo, plan_store):
    """Dato faltante, no 0 km: la adherencia lo trata distinto (spec 7)."""
    act = actividad("i1", "2026-09-08T08:00:00", distancia=0)
    s = _ingesta(ClienteFalso([]), repo, plan_store).normalizar(act, {}, ZONAS)
    assert s.distancia_km is None


def test_una_corrida_en_cinta_cuenta_como_carrera(repo, plan_store):
    act = actividad("i1", "2026-09-08T08:00:00", tipo="VirtualRun")
    s = _ingesta(ClienteFalso([]), repo, plan_store).normalizar(act, {}, ZONAS)
    assert s.es_run is True and s.distancia_km == 10.0


# --------------------------------------------------------------------------
# Sincronizacion
# --------------------------------------------------------------------------


def test_sincronizar_guarda_las_sesiones_con_su_fuente(repo, plan_store):
    cli = ClienteFalso(
        [actividad("i1", "2026-09-07T08:00:00"), actividad("i2", "2026-09-09T08:00:00")],
        streams={"i1": stream_plano(1200, hr=145), "i2": stream_plano(1200, hr=150)},
    )
    r = _ingesta(cli, repo, plan_store).sincronizar(SEMANA.inicio, SEMANA.fin)

    assert (r.actividades, r.corridas, r.fuente) == (2, 2, "intervals")
    assert r.fallback is False
    sesiones = repo.sesiones_entre(SEMANA.inicio, SEMANA.fin)
    assert [s.fuente for s in sesiones] == ["intervals", "intervals"]
    assert [s.id_externo for s in sesiones] == ["i1", "i2"]


def test_reingerir_no_duplica_ni_vuelve_a_pedir_streams(repo, plan_store):
    cli = ClienteFalso(
        [actividad("i1", "2026-09-07T08:00:00")],
        streams={"i1": stream_plano(1200, hr=145)},
        intervalos={"i1": [{"distance": 2000.0, "moving_time": 800, "index": 1}]},
    )
    ing = _ingesta(cli, repo, plan_store)
    ing.sincronizar(SEMANA.inicio, SEMANA.fin)
    r2 = ing.sincronizar(SEMANA.inicio, SEMANA.fin)

    assert len(repo.sesiones_entre(SEMANA.inicio, SEMANA.fin)) == 1
    assert r2.reutilizadas == 1
    assert cli.pedidos_streams == ["i1"], "se volvio a gastar cuota en el stream"


def test_las_zonas_de_intervals_se_cachean_como_confiables(repo, plan_store):
    cli = ClienteFalso([actividad("i1", "2026-09-07T08:00:00")])
    r = _ingesta(cli, repo, plan_store).sincronizar(SEMANA.inicio, SEMANA.fin)
    assert r.zonas_origen == "intervals"
    assert r.carga_imprecisa is False
    assert repo.zonas() == (ZONAS, "intervals")


def test_sin_zonas_la_carga_queda_marcada_imprecisa(repo, plan_store):
    cli = ClienteFalso(
        [actividad("i1", "2026-09-07T08:00:00")],
        streams={"i1": stream_plano(1200, hr=145)},
        zonas=None,
    )
    r = _ingesta(cli, repo, plan_store).sincronizar(SEMANA.inicio, SEMANA.fin)
    assert (r.zonas_origen, r.carga_imprecisa) == ("fallback", True)
    assert any("imprecisa" in a for a in r.avisos)


def test_la_fuerza_de_intervals_marca_el_dia_del_plan(repo, plan_store):
    # El PLAN_SPEC tiene fuerza el miercoles (W) = 2026-09-09.
    cli = ClienteFalso([actividad("i9", "2026-09-09T19:00:00", tipo="WeightTraining")])
    r = _ingesta(cli, repo, plan_store).sincronizar(SEMANA.inicio, SEMANA.fin)

    assert r.fuerza == 1
    assert r.fuerza_marcada == ["2026-09-09 (W)"]
    assert plan_store.cargar().plan.fuerza_completada["W"] is True


def test_se_cuenta_cuantas_corridas_quedaron_sin_dinamica(repo, plan_store):
    cli = ClienteFalso(
        [
            actividad("i1", "2026-09-07T08:00:00"),
            actividad("i2", "2026-09-09T08:00:00", average_gct=None),
        ]
    )
    r = _ingesta(cli, repo, plan_store).sincronizar(SEMANA.inicio, SEMANA.fin)
    assert r.sin_dinamica == 1
    assert not any("ninguna corrida" in a for a in r.avisos)


def test_si_ninguna_corrida_trae_dinamica_se_avisa(repo, plan_store):
    """Sintoma tipico de que los nombres de campo de campos.py no son los reales."""
    cli = ClienteFalso([actividad("i1", "2026-09-07T08:00:00", average_gct=None)])
    r = _ingesta(cli, repo, plan_store).sincronizar(SEMANA.inicio, SEMANA.fin)
    assert r.sin_dinamica == 1
    assert any("ninguna corrida" in a and "campos.py" in a for a in r.avisos)


# --------------------------------------------------------------------------
# Intervalos como vueltas
# --------------------------------------------------------------------------


def test_los_intervalos_se_guardan_como_vueltas(repo, plan_store):
    cli = ClienteFalso(
        [actividad("i1", "2026-09-07T08:00:00")],
        intervalos={
            "i1": [
                {"index": 1, "distance": 2000.0, "moving_time": 800},
                {"index": 2, "distance": 1000.0, "moving_time": 240},
            ]
        },
    )
    _ingesta(cli, repo, plan_store).sincronizar(SEMANA.inicio, SEMANA.fin)

    vs = repo.vueltas("intervals", "i1")
    assert [(v.indice, v.distancia_km) for v in vs] == [(1, 2.0), (2, 1.0)]
    assert repo.sesion("intervals", "i1").vueltas_procesadas is True


def test_los_intervalos_inservibles_se_descartan():
    crudos = [
        {"index": 1, "distance": 2000.0, "moving_time": 800},
        {"index": 2, "distance": 0.0, "moving_time": 300},  # sin distancia
        {"index": 3, "distance": 400.0},  # sin tiempo
        "basura",
        {"index": 4, "distance": 400.0, "duration": 84},  # nombre alterno
    ]
    vs = normalizar_intervalos("intervals", "i1", crudos)
    assert [v.indice for v in vs] == [1, 4]
    assert all(v.clave == ("intervals", "i1") for v in vs)


def test_los_intervalos_salen_ordenados_por_indice():
    crudos = [
        {"index": 3, "distance": 400.0, "moving_time": 84},
        {"index": 1, "distance": 2000.0, "moving_time": 800},
    ]
    assert [v.indice for v in normalizar_intervalos("intervals", "i1", crudos)] == [1, 3]


def test_si_los_intervalos_fallan_se_reintentan_la_proxima_vez(repo, plan_store):
    class ClienteQueFalla(ClienteFalso):
        def intervalos(self, actividad_id):
            raise IntervalsError("500")

    cli = ClienteQueFalla(
        [actividad("i1", "2026-09-07T08:00:00")], streams={"i1": stream_plano(1200, hr=145)}
    )
    _ingesta(cli, repo, plan_store).sincronizar(SEMANA.inicio, SEMANA.fin)
    # Bandera en 0: un fallo de red no es "esta actividad no tiene vueltas".
    assert repo.sesion("intervals", "i1").vueltas_procesadas is False


# --------------------------------------------------------------------------
# Bienestar
# --------------------------------------------------------------------------


def test_el_bienestar_se_persiste_con_las_horas_derivadas(repo, plan_store):
    cli = ClienteFalso(
        [actividad("i1", "2026-09-07T08:00:00")],
        bienestar=[bienestar("2026-09-07"), bienestar("2026-09-08", hrv=61.0)],
    )
    r = _ingesta(cli, repo, plan_store).sincronizar(SEMANA.inicio, SEMANA.fin)

    assert r.bienestar_dias == 2
    dias = repo.bienestar_entre(SEMANA.inicio, SEMANA.fin)
    assert [d.fecha_local for d in dias] == ["2026-09-07", "2026-09-08"]
    assert (dias[0].hrv, dias[0].hr_reposo, dias[0].sueno_h) == (68.0, 48.0, 7.5)
    assert (dias[0].sueno_score, dias[0].readiness) == (82.0, 71.0)


def test_el_bienestar_se_pide_una_semana_mas_atras_para_la_tendencia(repo, plan_store):
    cli = ClienteFalso([actividad("i1", "2026-09-07T08:00:00")], bienestar=[])
    _ingesta(cli, repo, plan_store).sincronizar(SEMANA.inicio, SEMANA.fin)

    desde, hasta = cli.rangos_bienestar[0]
    assert desde.date() <= SEMANA.inicio - timedelta(days=7)
    assert hasta.date() >= SEMANA.fin


def test_body_battery_ausente_queda_en_none(repo, plan_store):
    """Puede no existir en la API. Cero seria "sin bateria", que es falso."""
    cli = ClienteFalso(
        [actividad("i1", "2026-09-07T08:00:00")], bienestar=[bienestar("2026-09-07")]
    )
    _ingesta(cli, repo, plan_store).sincronizar(SEMANA.inicio, SEMANA.fin)
    assert repo.bienestar_entre(SEMANA.inicio, SEMANA.fin)[0].body_battery is None


def test_body_battery_presente_se_guarda(repo, plan_store):
    cli = ClienteFalso(
        [actividad("i1", "2026-09-07T08:00:00")],
        bienestar=[bienestar("2026-09-07", bodyBattery=64.0)],
    )
    _ingesta(cli, repo, plan_store).sincronizar(SEMANA.inicio, SEMANA.fin)
    assert repo.bienestar_entre(SEMANA.inicio, SEMANA.fin)[0].body_battery == 64.0


def test_si_el_bienestar_falla_las_actividades_no_se_pierden(repo, plan_store):
    """Perder la semana de entrenamiento por no poder leer el HRV seria peor."""
    cli = ClienteFalso(
        [actividad("i1", "2026-09-07T08:00:00")],
        streams={"i1": stream_plano(1200, hr=145)},
        falla_bienestar=True,
    )
    r = _ingesta(cli, repo, plan_store).sincronizar(SEMANA.inicio, SEMANA.fin)

    assert len(repo.sesiones_entre(SEMANA.inicio, SEMANA.fin)) == 1
    assert r.bienestar_dias == 0
    assert any("no se pudo leer el bienestar" in a for a in r.avisos)
    # Y no hay filas en cero: la semana simplemente no tiene bienestar.
    assert repo.bienestar_entre(SEMANA.inicio, SEMANA.fin) == []


def test_un_dia_de_bienestar_sin_ninguna_metrica_no_se_guarda(repo, plan_store):
    """Una fila vacia ensuciaria el promedio de la semana con nada."""
    cli = ClienteFalso(
        [actividad("i1", "2026-09-07T08:00:00")],
        bienestar=[{"id": "2026-09-07", "ctl": 42.0, "atl": 38.0}],
    )
    r = _ingesta(cli, repo, plan_store).sincronizar(SEMANA.inicio, SEMANA.fin)
    assert r.bienestar_dias == 0
    assert any("ninguna metrica de bienestar" in a for a in r.avisos)


def test_el_registro_crudo_de_bienestar_se_conserva():
    """Permite poblar una columna nueva sin volver a pedirle nada a la API."""
    d = normalizar_bienestar("intervals", bienestar("2026-09-07", raro=1))
    assert d is not None and '"raro": 1' in d.crudo


def test_un_registro_de_bienestar_sin_fecha_legible_se_descarta():
    assert normalizar_bienestar("intervals", {"hrv": 60.0}) is None
    assert normalizar_bienestar("intervals", {"id": "ayer", "hrv": 60.0}) is None


def test_ctl_y_atl_de_intervals_no_se_usan():
    """La carga la calcula este sistema; mezclar dos definiciones la corrompe."""
    d = normalizar_bienestar("intervals", bienestar("2026-09-07", ctl=42.0, atl=38.0))
    assert not hasattr(d, "ctl") and not hasattr(d, "atl")
