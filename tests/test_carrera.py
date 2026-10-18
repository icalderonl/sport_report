"""La carrera objetivo (/carrera) y la correccion de un dia (/corregir).

Las dos cosas comparten un tema: cambiar una parte del estado sin arrastrar el
resto. La carrera tiene que sobrevivir a los /setplan de cada semana, y
/corregir tiene que tocar un dia sin reescribir el plan entero ni abrir una
segunda ruta de validacion.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from sport_report.config import Carrera as CfgCarrera
from sport_report.fechas import semana_de
from sport_report.plan import carrera as m_carrera
from sport_report.plan.carrera import (
    Carrera,
    CarreraInvalida,
    CarreraStore,
    parse_fecha,
)
from sport_report.plan.grammar import parse_plan
from sport_report.plan.store import PlanStore
from sport_report.telegram import comandos
from tests.test_grammar import PLAN_SPEC

LUNES = date(2026, 9, 7)
SEMANA = semana_de(LUNES)
CARRERA_FECHA = date(2026, 11, 15)  # domingo, a 9 semanas del 7 de septiembre


@pytest.fixture()
def store(tmp_path) -> CarreraStore:
    return CarreraStore(ruta=tmp_path / "carrera.json")


@pytest.fixture()
def plan_store(tmp_path) -> PlanStore:
    s = PlanStore(actual=tmp_path / "plan.json", archivo=tmp_path / "planes")
    s.guardar(parse_plan(PLAN_SPEC), SEMANA)
    return s


# --------------------------------------------------------------------------
# El modelo
# --------------------------------------------------------------------------


def test_las_semanas_restantes_se_cuentan_de_lunes_a_lunes():
    """"Faltan 3 semanas" es lo que significa algo para entrenar."""
    c = Carrera(fecha=CARRERA_FECHA)
    assert c.semanas_restantes(LUNES) == 9
    # Cualquier dia de la misma semana da lo mismo.
    assert c.semanas_restantes(LUNES + timedelta(days=6)) == 9
    assert c.semanas_restantes(LUNES + timedelta(days=7)) == 8


def test_una_carrera_pasada_da_semanas_negativas():
    c = Carrera(fecha=LUNES - timedelta(days=14))
    assert c.semanas_restantes(LUNES) == -2
    assert c.fase(LUNES) == m_carrera.PASADA


def test_las_fases_salen_de_las_semanas_restantes():
    c = Carrera(fecha=CARRERA_FECHA)
    # Con los umbrales por defecto: afinamiento a 2 semanas, construccion a 8.
    assert c.fase(CARRERA_FECHA) == m_carrera.SEMANA_DE_CARRERA
    assert c.fase(CARRERA_FECHA - timedelta(weeks=1)) == m_carrera.AFINAMIENTO
    assert c.fase(CARRERA_FECHA - timedelta(weeks=2)) == m_carrera.AFINAMIENTO
    assert c.fase(CARRERA_FECHA - timedelta(weeks=3)) == m_carrera.CONSTRUCCION
    assert c.fase(CARRERA_FECHA - timedelta(weeks=8)) == m_carrera.CONSTRUCCION
    assert c.fase(CARRERA_FECHA - timedelta(weeks=9)) == m_carrera.BASE


def test_los_umbrales_de_fase_son_ajustables():
    c = Carrera(fecha=CARRERA_FECHA)
    cfg = CfgCarrera(semanas_afinamiento=3, semanas_construccion=12)
    assert c.fase(CARRERA_FECHA - timedelta(weeks=3), cfg) == m_carrera.AFINAMIENTO
    assert c.fase(CARRERA_FECHA - timedelta(weeks=9), cfg) == m_carrera.CONSTRUCCION


def test_el_contexto_trae_las_cifras_ya_calculadas():
    """La narrativa cita, no calcula: las semanas restantes vienen hechas."""
    c = Carrera(fecha=CARRERA_FECHA, nombre="Maraton de Santiago")
    d = c.contexto(LUNES)
    assert d["fecha"] == "2026-11-15"
    assert d["nombre"] == "Maraton de Santiago"
    assert d["semanas_restantes"] == 9
    assert d["fase"] == m_carrera.BASE
    assert "9 semana" in d["nota"]


# --------------------------------------------------------------------------
# Validacion de la fecha
# --------------------------------------------------------------------------


def test_una_fecha_mal_escrita_dice_como_se_escribe():
    with pytest.raises(CarreraInvalida) as exc:
        parse_fecha("15/11/2026", LUNES)
    assert "/carrera 2026-11-15" in str(exc.value)


def test_una_fecha_pasada_se_rechaza_y_ofrece_borrar():
    with pytest.raises(CarreraInvalida) as exc:
        parse_fecha("2026-09-01", LUNES)
    assert "ya paso" in str(exc.value) and "/carrera borrar" in str(exc.value)


def test_una_fecha_a_mas_de_dos_anos_huele_a_tecleo():
    """El teclado de la Pi pierde y sustituye caracteres: 2036 por 2026."""
    with pytest.raises(CarreraInvalida) as exc:
        parse_fecha("2036-11-15", LUNES)
    assert "revisa el ano" in str(exc.value)


def test_hoy_mismo_es_una_fecha_valida():
    assert parse_fecha(LUNES.isoformat(), LUNES) == LUNES


# --------------------------------------------------------------------------
# Persistencia
# --------------------------------------------------------------------------


def test_la_carrera_se_guarda_y_se_lee(store):
    store.guardar(Carrera(fecha=CARRERA_FECHA, nombre="Maraton"))
    c = store.cargar()
    assert (c.fecha, c.nombre) == (CARRERA_FECHA, "Maraton")
    assert c.creado_en, "no quedo cuando se declaro"


def test_sin_archivo_no_hay_carrera(store):
    assert store.cargar() is None


def test_declarar_otra_carrera_se_suma_y_no_quita_la_principal(store):
    store.guardar(Carrera(fecha=CARRERA_FECHA, nombre="Maraton"))
    store.guardar(Carrera(fecha=date(2026, 10, 18), nombre="10K"))
    assert [c.nombre for c in store.listar()] == ["10K", "Maraton"], "ordenadas por fecha"
    assert store.cargar().nombre == "Maraton", "la primera declarada sigue de principal"


def test_la_misma_fecha_actualiza_el_nombre_en_vez_de_duplicar(store):
    store.guardar(Carrera(fecha=CARRERA_FECHA, nombre="Maraton"))
    store.guardar(Carrera(fecha=CARRERA_FECHA, nombre="Maraton de Santiago"))
    assert [c.nombre for c in store.listar()] == ["Maraton de Santiago"]
    assert store.cargar().nombre == "Maraton de Santiago"


def test_marcar_otra_como_principal(store):
    store.guardar(Carrera(fecha=CARRERA_FECHA, nombre="Maraton"))
    store.guardar(Carrera(fecha=date(2026, 12, 6), nombre="Otra"))
    store.marcar_principal(date(2026, 12, 6))
    assert store.cargar().nombre == "Otra"
    with pytest.raises(CarreraInvalida):
        store.marcar_principal(date(2027, 1, 1))


def test_borrar_la_principal_no_promueve_otra(store):
    """Promover sola a otra cambiaria la fase del ciclo sin que nadie lo decida."""
    store.guardar(Carrera(fecha=CARRERA_FECHA, nombre="Maraton"))
    store.guardar(Carrera(fecha=date(2026, 12, 6), nombre="Otra"))
    borrada, era_principal = store.borrar(CARRERA_FECHA)
    assert borrada.nombre == "Maraton" and era_principal
    assert store.cargar() is None
    assert [c.nombre for c in store.listar()] == ["Otra"]


def test_borrar_quita_el_archivo(store):
    store.guardar(Carrera(fecha=CARRERA_FECHA))
    assert store.borrar()[0] is not None
    assert store.cargar() is None
    assert not store.ruta.exists()
    assert store.borrar() == (None, False), "borrar dos veces no puede fallar"


def test_un_archivo_v1_se_lee_como_principal(store):
    store.ruta.parent.mkdir(parents=True, exist_ok=True)
    store.ruta.write_text(
        json.dumps({"version": 1, "fecha": "2026-11-15", "nombre": "Maraton"}),
        encoding="utf-8",
    )
    assert store.cargar().nombre == "Maraton"
    store.guardar(Carrera(fecha=date(2026, 12, 6), nombre="Otra"))
    d = json.loads(store.ruta.read_text(encoding="utf-8"))
    assert d["version"] == 2 and d["principal"] == "2026-11-15"
    assert len(d["carreras"]) == 2


def test_un_archivo_ilegible_se_trata_como_sin_carrera(store):
    store.ruta.parent.mkdir(parents=True, exist_ok=True)
    store.ruta.write_text("{no es json", encoding="utf-8")
    assert store.cargar() is None, "una carrera corrupta no puede tumbar el reporte"


def test_un_formato_futuro_no_se_interpreta_a_la_fuerza(store):
    store.ruta.parent.mkdir(parents=True, exist_ok=True)
    store.ruta.write_text(json.dumps({"version": 99, "fecha": "2026-11-15"}), encoding="utf-8")
    assert store.cargar() is None


# --------------------------------------------------------------------------
# La semana del plan contada hacia la carrera principal (C-N)
# --------------------------------------------------------------------------

PLAN_SIN_SEMANA = PLAN_SPEC.replace("semana: 5\n", "")


def test_sin_semana_el_plan_se_numera_hacia_la_principal(store, tmp_path):
    from sport_report.fechas import hoy_local

    ps = PlanStore(actual=tmp_path / "p2.json", archivo=tmp_path / "planes2")
    # Relativa a hoy: /setplan ancla a la semana en curso real.
    fecha = hoy_local() + timedelta(days=60)
    store.guardar(Carrera(fecha=fecha, nombre="Maraton"))
    r = comandos.cmd_setplan(ps, "/setplan\n" + PLAN_SIN_SEMANA, store)
    restantes = Carrera(fecha=fecha).semanas_restantes(semana_de(hoy_local()).inicio)
    assert restantes > 0
    assert f"Semana Maraton - {restantes}" in r
    plan = ps.cargar().plan
    assert plan.semana is None and plan.semana_carrera == restantes
    assert plan.etiqueta_semana() == f"Maraton - {restantes}"


def test_una_carrera_sin_nombre_se_etiqueta_c_n(store, tmp_path):
    from sport_report.fechas import hoy_local

    ps = PlanStore(actual=tmp_path / "p2.json", archivo=tmp_path / "planes2")
    store.guardar(Carrera(fecha=hoy_local() + timedelta(days=60)))
    comandos.cmd_setplan(ps, "/setplan\n" + PLAN_SIN_SEMANA, store)
    assert ps.cargar().plan.etiqueta_semana().startswith("C-")


def test_la_semana_tecleada_manda_sobre_la_cuenta_regresiva(store, tmp_path):
    ps = PlanStore(actual=tmp_path / "p2.json", archivo=tmp_path / "planes2")
    store.guardar(Carrera(fecha=CARRERA_FECHA))
    comandos.cmd_setplan(ps, "/setplan\n" + PLAN_SPEC, store)
    plan = ps.cargar().plan
    assert plan.semana == 5 and plan.semana_carrera is None
    assert plan.etiqueta_semana() == "5"


def test_sin_principal_el_plan_se_acepta_sin_numero(store, tmp_path):
    ps = PlanStore(actual=tmp_path / "p2.json", archivo=tmp_path / "planes2")
    r = comandos.cmd_setplan(ps, "/setplan\n" + PLAN_SIN_SEMANA, store)
    assert "Plan cargado" in r and "no hay carrera principal" in r
    assert ps.cargar().plan.etiqueta_semana() is None


def test_la_cuenta_regresiva_se_mide_desde_la_semana_anclada():
    """`/setplan proxima` el domingo ancla una semana mas cerca de la carrera."""
    from sport_report.fechas import semana_siguiente

    c = Carrera(fecha=CARRERA_FECHA)
    assert c.semanas_restantes(semana_siguiente(SEMANA).inicio) == (
        c.semanas_restantes(SEMANA.inicio) - 1
    )


def test_la_cuenta_regresiva_sobrevive_al_guardar_y_leer():
    from sport_report.plan.models import plan_desde_json
    from dataclasses import replace

    p = replace(parse_plan(PLAN_SIN_SEMANA), semana_carrera=3, carrera_nombre="Fedachi")
    q = plan_desde_json(p.to_json())
    assert (q.semana, q.semana_carrera, q.etiqueta_semana()) == (None, 3, "Fedachi - 3")


# --------------------------------------------------------------------------
# El comando /carrera
# --------------------------------------------------------------------------


def test_declarar_una_carrera_responde_con_la_fase(store):
    r = comandos.cmd_carrera(store, "2026-11-15 Maraton de Santiago", hoy=LUNES)
    assert "agregada como principal" in r
    assert "2026-11-15" in r and "Maraton de Santiago" in r
    assert "9 semana" in r
    assert store.cargar().nombre == "Maraton de Santiago"


def test_declarar_sin_nombre_funciona(store):
    comandos.cmd_carrera(store, "2026-11-15", hoy=LUNES)
    assert store.cargar().nombre == ""


def test_consultar_sin_carrera_explica_como_declararla(store):
    r = comandos.cmd_carrera(store, "", hoy=LUNES)
    assert "No hay carrera declarada" in r and "/carrera 2026-11-15" in r


def test_consultar_muestra_la_guardada(store):
    comandos.cmd_carrera(store, "2026-11-15 Maraton", hoy=LUNES)
    r = comandos.cmd_carrera(store, "", hoy=LUNES)
    assert "Maraton" in r and "9 semana" in r


def test_borrar_por_comando(store):
    comandos.cmd_carrera(store, "2026-11-15", hoy=LUNES)
    assert "borrada" in comandos.cmd_carrera(store, "borrar", hoy=LUNES)
    assert store.cargar() is None
    assert "No habia" in comandos.cmd_carrera(store, "borrar", hoy=LUNES)


def test_varias_carreras_por_comando(store):
    comandos.cmd_carrera(store, "2026-11-15 Maraton", hoy=LUNES)
    r = comandos.cmd_carrera(store, "2026-10-18 10K", hoy=LUNES)
    assert "no es la principal" in r
    lista = comandos.cmd_carrera(store, "", hoy=LUNES)
    assert lista.index("1. 2026-10-18") < lista.index("2. 2026-11-15")
    assert "Maraton [principal]" in lista and "10K [principal]" not in lista

    r = comandos.cmd_carrera(store, "principal 1", hoy=LUNES)
    assert "10K [principal]" in r
    assert store.cargar().nombre == "10K"


def test_borrar_con_varias_pide_cual(store):
    comandos.cmd_carrera(store, "2026-11-15 Maraton", hoy=LUNES)
    comandos.cmd_carrera(store, "2026-10-18 10K", hoy=LUNES)
    r = comandos.cmd_carrera(store, "borrar", hoy=LUNES)
    assert "indica cual" in r
    assert len(store.listar()) == 2, "no se borra nada sin saber cual"


def test_borrar_la_principal_por_comando_pide_elegir_otra(store):
    comandos.cmd_carrera(store, "2026-11-15 Maraton", hoy=LUNES)
    comandos.cmd_carrera(store, "2026-10-18 10K", hoy=LUNES)
    r = comandos.cmd_carrera(store, "borrar 2026-11-15", hoy=LUNES)
    assert "Era la principal" in r and "/carrera principal" in r
    assert store.cargar() is None
    assert "Ninguna es la principal" in comandos.cmd_carrera(store, "", hoy=LUNES)


def test_principal_con_numero_fuera_de_rango(store):
    comandos.cmd_carrera(store, "2026-11-15 Maraton", hoy=LUNES)
    r = comandos.cmd_carrera(store, "principal 5", hoy=LUNES)
    assert "no hay carrera 5" in r.lower()
    assert store.cargar().nombre == "Maraton"


def test_una_fecha_invalida_no_borra_la_carrera_guardada(store):
    comandos.cmd_carrera(store, "2026-11-15 Maraton", hoy=LUNES)
    r = comandos.cmd_carrera(store, "15-11-2026", hoy=LUNES)
    assert "no es una fecha" in r.lower()
    assert store.cargar().nombre == "Maraton", "se perdio la carrera por un tecleo"


def test_setplan_no_borra_la_carrera(store, plan_store):
    """Es el motivo de que viva en su propio archivo."""
    comandos.cmd_carrera(store, "2026-11-15 Maraton", hoy=LUNES)
    comandos.cmd_setplan(plan_store, PLAN_SPEC)
    assert store.cargar().nombre == "Maraton"


# --------------------------------------------------------------------------
# El comando /corregir
# --------------------------------------------------------------------------


def test_corregir_reemplaza_el_dia_completo(plan_store):
    r = comandos.cmd_corregir(
        plan_store, "/corregir M: tempo 10km @4:50 Z4", hoy=LUNES + timedelta(days=1)
    )
    assert "Corregido: martes" in r
    sesiones = plan_store.cargar().plan.sesiones["M"]
    assert [(x.tipo, x.cantidad) for x in sesiones] == [("tempo", 10.0)]


def test_corregir_puede_dejar_dos_sesiones_ese_dia(plan_store):
    r = comandos.cmd_corregir(
        plan_store, "/corregir\nM: easy 8km Z2\nM: fuerza", hoy=LUNES
    )
    assert "Corregido: martes" in r
    assert [x.tipo for x in plan_store.cargar().plan.sesiones["M"]] == ["easy", "fuerza"]


def test_corregir_varios_dias_en_un_comando(plan_store):
    comandos.cmd_corregir(
        plan_store, "/corregir\nM: easy 6km Z2\nV: rest", hoy=LUNES
    )
    p = plan_store.cargar().plan
    assert p.sesiones["M"][0].cantidad == 6.0
    assert p.es_descanso("V") is True


def test_corregir_no_toca_los_otros_dias(plan_store):
    antes = plan_store.cargar().plan
    comandos.cmd_corregir(plan_store, "/corregir M: easy 6km Z2", hoy=LUNES)
    despues = plan_store.cargar().plan
    for d in ("L", "W", "J", "V", "S", "D"):
        assert [x.crudo for x in despues.sesiones[d]] == [x.crudo for x in antes.sesiones[d]]
    assert despues.semana == antes.semana


def test_corregir_quitando_la_fuerza_limpia_su_marca(plan_store):
    """Dejar la marca mostraria una fuerza cumplida que ya no esta planificada."""
    plan_store.marcar_fuerza("W", True, SEMANA)
    assert plan_store.cargar().plan.fuerza_completada.get("W") is True

    comandos.cmd_corregir(plan_store, "/corregir W: easy 8km Z2", hoy=LUNES)

    p = plan_store.cargar().plan
    assert p.fuerza_de("W") is None
    assert "W" not in p.fuerza_completada


def test_corregir_manteniendo_la_fuerza_conserva_su_marca(plan_store):
    plan_store.marcar_fuerza("W", True, SEMANA)
    comandos.cmd_corregir(plan_store, "/corregir\nW: easy 8km Z2\nW: fuerza", hoy=LUNES)
    p = plan_store.cargar().plan
    assert p.fuerza_completada.get("W") is True


def test_corregir_usa_los_mismos_mensajes_de_error_que_setplan(plan_store):
    """Un solo parser: el error de una linea es identico por construccion."""
    r_corregir = comandos.cmd_corregir(plan_store, "/corregir M: easy 8 km Z2", hoy=LUNES)
    r_setplan = comandos.cmd_setplan(
        plan_store, PLAN_SPEC.replace("M: easy 8km Z2", "M: easy 8 km Z2")
    )
    # El numero de linea cambia (1 en el comando suelto, 3 en el plan entero);
    # el dia y el motivo tienen que ser exactamente los mismos.
    def _motivo(texto: str) -> str:
        linea = [x for x in texto.splitlines() if "Linea" in x][-1]
        return linea.split(",", 1)[1].strip()  # se descarta "  - Linea N"

    assert _motivo(r_corregir) == _motivo(r_setplan)
    assert "unidad" in _motivo(r_corregir)


def test_corregir_un_dia_invalido_lo_dice(plan_store):
    r = comandos.cmd_corregir(plan_store, "/corregir X: easy 8km", hoy=LUNES)
    assert "no reconocido" in r.lower() or "formato" in r.lower()


def test_corregir_sin_plan_cargado_manda_a_setplan(tmp_path):
    vacio = PlanStore(actual=tmp_path / "nada.json", archivo=tmp_path / "planes")
    r = comandos.cmd_corregir(vacio, "/corregir M: easy 8km Z2", hoy=LUNES)
    assert "No hay plan cargado" in r


def test_corregir_falla_si_hoy_no_pertenece_a_la_semana_cargada(plan_store):
    """El dia corregido no pertenece al plan vigente: mejor decirlo."""
    r = comandos.cmd_corregir(
        plan_store, "/corregir M: easy 8km Z2", hoy=LUNES + timedelta(days=14)
    )
    assert "no es la semana" in r
    assert "2026-09-07" in r and "2026-09-21" in r
    # Y no se escribio nada.
    assert plan_store.cargar().plan.sesiones["M"][0].cantidad == 8.0


def test_corregir_vacio_lo_dice(plan_store):
    r = comandos.cmd_corregir(plan_store, "/corregir", hoy=LUNES)
    assert "ninguna linea que corregir" in r


def test_la_ayuda_documenta_los_comandos_nuevos():
    assert "/corregir" in comandos.AYUDA
    assert "/carrera" in comandos.AYUDA
    assert "mas de una linea" in comandos.AYUDA
