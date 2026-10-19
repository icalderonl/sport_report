"""Informe post-carrera: analisis de la carrera, cierre del ciclo y disparo.

Todo con dobles: ni fuentes, ni Claude, ni Telegram.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from sport_report import run_post_carrera as rpc
from sport_report.db.models import BienestarDia, SesionReal, Vuelta
from sport_report.db.repo import Repo
from sport_report.engine import post_carrera as m_post
from sport_report.narrative.claude import Narrativa, verificar_atribucion, verificar_cifras
from sport_report.narrative.post_carrera import (
    METRICAS_POST_CARRERA,
    redactar_post_carrera,
)
from sport_report.plan.carrera import Carrera
from sport_report.plan.store import PlanStore
from sport_report.storage import escribir_json
from tests.test_narrative import ClienteFalso, Respuesta

# Domingo. La semana de la carrera empieza el lunes 2026-11-09.
FECHA = date(2026, 11, 15)
CARRERA = Carrera(fecha=FECHA, nombre="Fedachi")


@pytest.fixture()
def entorno(tmp_path):
    repo = Repo(tmp_path / "t.db")
    store = PlanStore(actual=tmp_path / "plan.json", archivo=tmp_path / "planes")
    yield repo, store
    repo.cerrar()


def corrida(repo: Repo, fecha: date, km: float, seg_km: float = 300, **kw) -> SesionReal:
    base = dict(
        fuente="intervals",
        id_externo=f"i{fecha.isoformat()}-{km}",
        fecha_utc=f"{fecha}T12:00:00+00:00",
        fecha_local=fecha.isoformat(),
        dia_semana="LMWJVSD"[fecha.weekday()],
        tipo="Run",
        es_fuerza=False,
        distancia_km=km,
        duracion_mov_s=int(km * seg_km),
        duracion_tot_s=int(km * seg_km) + 30,
        carga=km * 40,
    )
    base.update(kw)
    s = SesionReal(**base)
    repo.guardar_sesion(s)
    return s


def vueltas(repo: Repo, s: SesionReal, ritmos: list[float], resto_km: float = 0.0) -> None:
    """Vueltas de 1 km con los ritmos dados (s/km), y un resto final opcional."""
    vs = [
        Vuelta(s.fuente, s.id_externo, i + 1, 1.0, int(r)) for i, r in enumerate(ritmos)
    ]
    if resto_km:
        vs.append(
            Vuelta(s.fuente, s.id_externo, len(vs) + 1, resto_km, int(resto_km * ritmos[-1]))
        )
    repo.guardar_vueltas(s.fuente, s.id_externo, vs)


def carrera_10k(repo: Repo, ritmos: list[float] | None = None, **kw) -> SesionReal:
    ritmos = ritmos or [300.0] * 10
    s = corrida(
        repo,
        FECHA,
        10.0,
        seg_km=sum(ritmos) / len(ritmos),
        hr_promedio=168.0,
        hr_maximo=184.0,
        cadencia_spm=182.0,
        decoupling_pct=kw.pop("decoupling_pct", 2.1),
        **kw,
    )
    vueltas(repo, s, ritmos)
    return s


# --------------------------------------------------------------------------
# La carrera
# --------------------------------------------------------------------------


def test_la_carrera_es_la_corrida_mas_larga_del_dia(entorno):
    repo, store = entorno
    corrida(repo, FECHA, 2.5, id_externo="calentamiento")
    carrera_10k(repo)
    j = m_post.construir(CARRERA, False, repo, store)
    a = j["actividad"]
    assert a["disponible"] and a["distancia_km"] == 10.0
    assert a["tiempo_mov"] == "50:00" and a["ritmo"] == "5:00/km"
    assert a["hr_promedio"] == 168.0


def test_sin_actividad_se_dice_y_no_hay_ceros(entorno):
    repo, store = entorno
    j = m_post.construir(CARRERA, False, repo, store)
    assert j["actividad"]["disponible"] is False
    assert j["parciales"]["disponible"] is False
    assert j["avisos_datos"]


def test_formatos_de_tiempo_y_ritmo():
    assert m_post.fmt_tiempo(6332) == "1:45:32"
    assert m_post.fmt_tiempo(1500) == "25:00"
    assert m_post.fmt_ritmo(301.6) == "5:02/km"
    assert m_post.fmt_ritmo(None) is None


# --------------------------------------------------------------------------
# Parciales y fatiga
# --------------------------------------------------------------------------


def test_split_parejo_sin_senales(entorno):
    repo, store = entorno
    carrera_10k(repo)
    j = m_post.construir(CARRERA, False, repo, store)
    p = j["parciales"]
    assert p["tipo_split"] == "parejo" and p["split_pct"] == 0
    assert j["fatiga"]["hay_senales"] is False
    assert "split" in j["fatiga"]["evaluadas"]


def test_segunda_mitad_lenta_es_senal_de_fatiga(entorno):
    repo, store = entorno
    carrera_10k(repo, [290.0] * 5 + [310.0] * 5)
    j = m_post.construir(CARRERA, False, repo, store)
    p = j["parciales"]
    assert p["primera_mitad"] == "4:50/km" and p["segunda_mitad"] == "5:10/km"
    assert p["split_pct"] == 6.9 and p["tipo_split"] == "positivo"
    assert any("segunda mitad" in s for s in j["fatiga"]["senales"])


def test_split_negativo(entorno):
    repo, store = entorno
    carrera_10k(repo, [305.0] * 5 + [295.0] * 5)
    p = m_post.construir(CARRERA, False, repo, store)["parciales"]
    assert p["tipo_split"] == "negativo" and p["split_pct"] < 0


def test_caida_en_el_ultimo_cuarto(entorno):
    repo, store = entorno
    # Parejo hasta el km 8 y los dos ultimos mucho mas lentos.
    carrera_10k(repo, [300.0] * 8 + [345.0] * 2)
    j = m_post.construir(CARRERA, False, repo, store)
    assert j["parciales"]["caida_ultimo_cuarto_pct"] >= 3
    assert any("ultimo cuarto" in s for s in j["fatiga"]["senales"])


def test_deriva_cardiaca_alta_es_senal(entorno):
    repo, store = entorno
    carrera_10k(repo, decoupling_pct=7.5)
    f = m_post.construir(CARRERA, False, repo, store)["fatiga"]
    assert any("deriva cardiaca de 7.5%" in s for s in f["senales"])


def test_sin_decoupling_no_se_evalua(entorno):
    repo, store = entorno
    carrera_10k(repo, decoupling_pct=None)
    f = m_post.construir(CARRERA, False, repo, store)["fatiga"]
    assert "deriva cardiaca" not in f["evaluadas"]
    assert any("deriva cardiaca" in x for x in f["no_evaluables"])


def test_la_vuelta_final_corta_no_cuenta_como_la_mas_rapida(entorno):
    repo, store = entorno
    s = corrida(repo, FECHA, 10.1, seg_km=300)
    vs = [Vuelta(s.fuente, s.id_externo, i + 1, 1.0, 300 + i) for i in range(10)]
    vs.append(Vuelta(s.fuente, s.id_externo, 11, 0.1, 20))  # 3:20/km: un sprint de 100 m
    repo.guardar_vueltas(s.fuente, s.id_externo, vs)
    p = m_post.construir(CARRERA, False, repo, store)["parciales"]
    assert p["vuelta_mas_rapida"]["vuelta"] == 1


def test_vueltas_que_no_cubren_la_carrera_no_dan_parciales(entorno):
    repo, store = entorno
    s = corrida(repo, FECHA, 21.1)
    vueltas(repo, s, [300.0] * 5)
    p = m_post.construir(CARRERA, False, repo, store)["parciales"]
    assert p["disponible"] is False and "cubren" in p["motivo"]


def test_una_vuelta_que_cruza_la_mitad_se_reparte():
    # 3 vueltas: 2 km a 5:00, 1 km a 6:00, 1 km a 6:00. Mitad en el km 2.
    vs = [
        Vuelta("i", "x", 1, 2.0, 600),
        Vuelta("i", "x", 2, 1.0, 360),
        Vuelta("i", "x", 3, 1.0, 360),
    ]
    assert m_post._ritmo_tramo(vs, 0, 2) == 300
    assert m_post._ritmo_tramo(vs, 1, 3) == 330


# --------------------------------------------------------------------------
# Bienestar
# --------------------------------------------------------------------------


def test_bienestar_del_dia_y_del_siguiente(entorno):
    repo, store = entorno
    carrera_10k(repo)
    dias = [
        BienestarDia(fecha_local=(FECHA - timedelta(days=i)).isoformat(), fuente="intervals", hrv=60.0)
        for i in range(1, 8)
    ]
    dias += [
        BienestarDia(fecha_local=FECHA.isoformat(), fuente="intervals", hrv=55.0),
        BienestarDia(
            fecha_local=(FECHA + timedelta(days=1)).isoformat(), fuente="intervals", hrv=48.0
        ),
    ]
    repo.guardar_bienestar(dias)
    b = m_post.construir(CARRERA, False, repo, store)["bienestar"]
    assert b["hrv"]["dia_carrera"] == 55.0 and b["hrv"]["dia_siguiente"] == 48.0
    assert b["hrv"]["promedio_28_dias_previos"] == 60.0
    assert b["disponible"]


# --------------------------------------------------------------------------
# Ciclo
# --------------------------------------------------------------------------


def _ciclo_con(repo: Repo, kms: list[float]) -> None:
    """Una corrida por semana, el martes, terminando la semana antes de la carrera."""
    lunes_carrera = FECHA - timedelta(days=FECHA.weekday())
    for i, km in enumerate(reversed(kms)):
        corrida(repo, lunes_carrera - timedelta(weeks=i + 1) + timedelta(days=1), km)


def test_solo_la_principal_trae_ciclo(entorno):
    repo, store = entorno
    carrera_10k(repo)
    assert m_post.construir(CARRERA, False, repo, store)["ciclo"] is None
    assert m_post.construir(CARRERA, True, repo, store)["ciclo"] is not None


def test_ciclo_pico_y_afinamiento(entorno):
    repo, store = entorno
    _ciclo_con(repo, [30, 40, 50, 60, 40, 30])
    carrera_10k(repo)
    c = m_post.construir(CARRERA, True, repo, store)["ciclo"]
    assert c["semanas"] == 16
    assert c["pico"]["km"] == 60
    # Las dos semanas previas a la de la carrera: 40 y 30 -> 35, un 41.7% menos.
    assert c["afinamiento"]["promedio_km"] == 35
    assert c["afinamiento"]["reduccion_vs_pico_pct"] == -41.7
    assert c["total_km"] == 260
    assert c["km_carrera"] == 10.0 and c["km_semana_carrera_sin_carrera"] == 0
    assert c["corrida_mas_larga"]["km"] == 60
    # Sin plan cargado: adherencia sin dato, no cero.
    assert c["adherencia"]["promedio_pct"] is None and not c["adherencia"]["confiable"]


def test_pico_en_el_afinamiento_no_se_presenta_como_reduccion(entorno):
    repo, store = entorno
    _ciclo_con(repo, [30, 40, 70])
    carrera_10k(repo)
    af = m_post.construir(CARRERA, True, repo, store)["ciclo"]["afinamiento"]
    assert af["disponible"] is False and "pico" in af["motivo"]


def _informe_previo(fecha: date, total_km: float, ritmo_s_km: float = 310.0) -> None:
    escribir_json(
        m_post.ruta_informe(fecha),
        {
            "carrera": {"fecha": fecha.isoformat(), "nombre": "Santiago", "principal": True},
            "actividad": {
                "disponible": True,
                "distancia_km": 10.0,
                "ritmo_s_km": ritmo_s_km,
                "ritmo": m_post.fmt_ritmo(ritmo_s_km),
                "tiempo_mov": "51:40",
                "hr_promedio": 170.0,
            },
            "ciclo": {
                "semanas": 12,
                "total_km": total_km,
                "promedio_semanal_km": 30.0,
                "pico": {"lunes": "2026-03-02", "km": 45.0},
                "adherencia": {"promedio_pct": 80.0},
            },
        },
    )


def test_el_ciclo_empieza_despues_de_la_carrera_anterior(entorno):
    repo, store = entorno
    carrera_10k(repo)
    _informe_previo(date(2026, 9, 20), 300.0)  # domingo, semana del 2026-09-14
    c = m_post.construir(CARRERA, True, repo, store)["ciclo"]
    assert c["inicio"] == "2026-09-21"
    assert "carrera anterior" in c["criterio_inicio"]


def test_comparacion_con_el_ciclo_y_carreras_similares(entorno):
    repo, store = entorno
    _ciclo_con(repo, [30, 40, 50, 60, 40, 30])
    carrera_10k(repo)
    _informe_previo(date(2026, 4, 12), 200.0)
    j = m_post.construir(CARRERA, True, repo, store)
    comp = j["comparacion"]["ciclo_anterior"]
    assert comp["disponible"] and comp["delta_total_km"] == 60
    assert comp["delta_pico_km"] == 15
    sim = j["comparacion"]["carreras_similares"]
    assert sim[0]["fecha"] == "2026-04-12" and sim[0]["delta_ritmo_s_km"] == -10


def test_sin_ciclo_anterior_se_dice(entorno):
    repo, store = entorno
    carrera_10k(repo)
    comp = m_post.construir(CARRERA, True, repo, store)["comparacion"]["ciclo_anterior"]
    assert comp["disponible"] is False and comp["motivo"]


# --------------------------------------------------------------------------
# Narrativa
# --------------------------------------------------------------------------


def test_la_narrativa_usa_su_tabla_y_verifica(entorno):
    repo, store = entorno
    carrera_10k(repo, [290.0] * 5 + [310.0] * 5)
    datos = m_post.construir(CARRERA, False, repo, store)
    texto = (
        "Corriste 10 km en 50:00 a 5:00/km con FC 168. "
        "La segunda mitad fue a 5:10/km, un split de 6.9%."
    )
    cliente = ClienteFalso(Respuesta(texto))
    n = redactar_post_carrera(datos, cliente=cliente, modelo="m")
    assert n.ok and n.numeros_no_verificados == [] and n.cifras_mal_atribuidas == []
    assert "POST-CARRERA" in cliente.llamadas[0]["system"]


def test_una_fc_inventada_se_detecta(entorno):
    repo, store = entorno
    carrera_10k(repo)
    datos = m_post.construir(CARRERA, False, repo, store)
    assert verificar_atribucion("FC 184 de promedio", datos, METRICAS_POST_CARRERA) == []
    assert verificar_atribucion("FC 175 de promedio", datos, METRICAS_POST_CARRERA)
    assert verificar_cifras("FC 175", datos) == ["175"]


# --------------------------------------------------------------------------
# Disparo
# --------------------------------------------------------------------------


def test_pendientes_es_el_mismo_dia_y_la_ventana():
    c = [CARRERA, Carrera(fecha=FECHA + timedelta(days=30), nombre="otra")]
    nunca = lambda f: False  # noqa: E731
    assert rpc.pendientes(c, hoy=FECHA - timedelta(days=1), hecho=nunca) == []
    assert rpc.pendientes(c, hoy=FECHA, hecho=nunca) == [CARRERA]  # el mismo dia
    assert rpc.pendientes(c, hoy=FECHA + timedelta(days=1), hecho=nunca) == [CARRERA]
    assert rpc.pendientes(c, hoy=FECHA + timedelta(days=3), hecho=nunca) == [CARRERA]
    assert rpc.pendientes(c, hoy=FECHA + timedelta(days=4), hecho=nunca) == []


def test_pendientes_respeta_la_guarda(entorno):
    _informe_previo(FECHA, 100.0)
    assert rpc.pendientes([CARRERA], hoy=FECHA + timedelta(days=1)) == []


def _narrador(datos):
    return Narrativa(texto="Buena carrera.", modelo="m")


def test_ejecutar_envia_y_escribe_la_guarda(entorno):
    repo, store = entorno
    carrera_10k(repo)
    enviados: list[str] = []
    r = rpc.ejecutar(
        CARRERA, True, repo, store,
        narrador=_narrador,
        enviador=lambda m: enviados.append(m) or True,
        hoy=FECHA + timedelta(days=1),
    )
    assert r.estado == rpc.OK and r.enviado
    assert enviados[0].startswith("POST-CARRERA - Fedachi (2026-11-15)")
    assert "10 km en 50:00 (5:00/km)" in enviados[0]
    assert "Buena carrera." in enviados[0]
    assert m_post.ruta_informe(FECHA).exists()


def test_sin_actividad_espera_hasta_el_ultimo_dia(entorno):
    repo, store = entorno
    enviados: list[str] = []
    enviar = lambda m: enviados.append(m) or True  # noqa: E731
    for dias in (0, 1):  # el mismo dia y el reintento del dia siguiente
        r = rpc.ejecutar(
            CARRERA, True, repo, store, narrador=None, enviador=enviar,
            hoy=FECHA + timedelta(days=dias),
        )
        assert r.estado == rpc.ESPERANDO and enviados == []
    assert not m_post.ruta_informe(FECHA).exists()

    r = rpc.ejecutar(
        CARRERA, True, repo, store, narrador=None, enviador=enviar,
        hoy=FECHA + timedelta(days=3),
    )
    assert r.enviado and "Sin actividad de carrera" in enviados[0]


def test_un_envio_fallido_no_escribe_la_guarda(entorno):
    repo, store = entorno
    carrera_10k(repo)
    r = rpc.ejecutar(
        CARRERA, True, repo, store, narrador=None, enviador=lambda m: False,
        hoy=FECHA + timedelta(days=1),
    )
    assert r.estado == rpc.ERROR
    assert not m_post.ruta_informe(FECHA).exists()


def test_sin_narrativa_el_informe_llega_igual(entorno):
    repo, store = entorno
    carrera_10k(repo)

    def falla(datos):
        return Narrativa(texto=None, modelo="m", error="sin clave")

    r = rpc.ejecutar(
        CARRERA, False, repo, store, narrador=falla, enviador=lambda m: True,
        hoy=FECHA + timedelta(days=1),
    )
    assert r.estado == rpc.PARCIAL and r.enviado
    assert "5:00/km" in r.mensaje


def test_main_sin_pendientes_no_abre_la_base(monkeypatch):
    abierta = []
    monkeypatch.setattr(rpc, "Repo", lambda *a, **k: abierta.append(1))
    assert rpc.main(["--dry-run"]) == 0
    assert abierta == []


def test_un_trozo_de_ritmo_no_se_atribuye_a_la_metrica_vecina(entorno):
    repo, store = entorno
    carrera_10k(repo)
    datos = m_post.construir(CARRERA, False, repo, store)
    assert verificar_atribucion("a 5:00/km con FC 168", datos, METRICAS_POST_CARRERA) == []
    # El ':' de una etiqueta no es un reloj: el numero se sigue verificando.
    assert verificar_atribucion("FC:175", datos, METRICAS_POST_CARRERA)
