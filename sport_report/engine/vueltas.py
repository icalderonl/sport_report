"""Alinear los segmentos declarados en el plan contra las vueltas reales.

El problema que resuelve: una sesion con `estructura=` se compara contra la
distancia dura derivada de los bloques, que NO incluye la recuperacion trotada
entre repeticiones porque el plan nunca la declara. Lo que llega de Strava como
`distance` de la actividad SI la incluye. Los dos lados no miden lo mismo y toda
sesion de series lee por encima de 100% por construccion.

Las vueltas son el unico dato que permite separarlas. Si el atleta apreto el
boton en cada repeticion, la actividad trae una vuelta por segmento declarado
mas las de recuperacion; alineando unas con otras se obtiene la distancia
declarada REAL, comparable contra la del plan.

Como se alinea
--------------
Los segmentos del plan y las vueltas van ambos en orden cronologico, asi que
esto es un emparejamiento de subsecuencia: hay que elegir, en orden y sin
repetir, una vuelta para cada segmento declarado. Las vueltas que sobran son la
recuperacion.

Una vuelta puede emparejarse con un segmento solo si su distancia cae dentro de
la tolerancia (el GPS no da la cifra exacta). Eso por si solo no basta: en un
`4x400m` con 400m de trote entre repeticiones, las ocho vueltas miden lo mismo y
la distancia no distingue cual es cual. Lo que si las distingue es el RITMO, asi
que entre todas las alineaciones validas se elige la de menor tiempo total. La
intuicion es directa: de las vueltas que podrian ser el trabajo declarado, el
trabajo declarado es la que se corrio rapido.

Se resuelve con programacion dinamica sobre (segmento, vuelta), que da el optimo
global y no la primera coincidencia que aparezca: sacrificar un emparejamiento
temprano a veces habilita uno mucho mejor mas adelante.

Cuando no se puede
------------------
Si el reloj no marco vueltas, si hay menos vueltas que segmentos declarados o si
alguna no encuentra pareja dentro de la tolerancia, NO se inventa un numero:
`Alineacion.ok` es False con el motivo escrito, y quien llama vuelve a comparar
contra el total de la actividad como se hacia antes. Un numero peor pero honesto
es preferible a uno bueno que puede estar inventado.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..db.models import Vuelta

# Cuanto puede desviarse una vuelta del segmento declarado para seguir siendo
# esa vuelta. El GPS no clava los 400m: sobre distancias cortas el error es de
# unas decenas de metros y sobre las largas es proporcional, asi que se toma el
# mayor de los dos criterios.
TOLERANCIA_REL = 0.12
TOLERANCIA_MIN_KM = 0.05

# Sin esto una actividad con una sola vuelta (el reloj no se toco) "alinearia"
# contra un plan de un solo bloque y daria la sensacion de haber medido algo.
MIN_VUELTAS = 2

_INF = float("inf")


@dataclass(frozen=True)
class Alineacion:
    """Resultado de emparejar los segmentos del plan con las vueltas reales."""

    ok: bool
    motivo: str = ""
    declarada_km: float | None = None
    recuperacion_km: float | None = None
    # indices (lap_index) de las vueltas que quedaron emparejadas.
    indices: tuple[int, ...] = ()

    def __bool__(self) -> bool:
        return self.ok


def tolerancia_km(esperado: float) -> float:
    return max(TOLERANCIA_MIN_KM, esperado * TOLERANCIA_REL)


def _compatible(esperado: float, v: Vuelta) -> bool:
    return abs(v.distancia_km - esperado) <= tolerancia_km(esperado)


def alinear(esperados: Sequence[float], vueltas: Sequence[Vuelta]) -> Alineacion:
    """Empareja cada segmento declarado con una vuelta, en orden.

    Devuelve la distancia declarada realmente recorrida y la que quedo fuera
    (recuperacion), o `ok=False` con el motivo si no se pudo emparejar todo.
    """
    n, m = len(esperados), len(vueltas)
    if not n:
        return Alineacion(False, "la sesion del plan no declara segmentos")
    if m < MIN_VUELTAS:
        return Alineacion(
            False,
            "la actividad no trae vueltas marcadas" if not m
            else "la actividad trae una sola vuelta: el reloj no se toco",
        )
    if m < n:
        return Alineacion(
            False, f"{m} vuelta(s) para {n} segmentos declarados: faltan vueltas"
        )

    # dp[i][j]: menor tiempo total emparejando los primeros i segmentos usando
    # solo las primeras j vueltas. dp[0][*] = 0 (nada que emparejar todavia).
    dp = [[_INF] * (m + 1) for _ in range(n + 1)]
    # de_donde[i][j] es True si el optimo en (i, j) uso la vuelta j-1.
    uso = [[False] * (m + 1) for _ in range(n + 1)]
    for j in range(m + 1):
        dp[0][j] = 0.0

    for i in range(1, n + 1):
        for j in range(i, m + 1):
            # Saltarse la vuelta j-1: es recuperacion.
            mejor = dp[i][j - 1]
            usada = False
            v = vueltas[j - 1]
            if _compatible(esperados[i - 1], v) and dp[i - 1][j - 1] < _INF:
                coste = dp[i - 1][j - 1] + float(v.duracion_mov_s or 0)
                if coste < mejor:
                    mejor, usada = coste, True
            dp[i][j] = mejor
            uso[i][j] = usada

    if dp[n][m] == _INF:
        return Alineacion(
            False,
            "las vueltas no calzan con los segmentos del plan: el reloj no marco "
            "cada repeticion, o la sesion no fue la planificada",
        )

    elegidas: list[Vuelta] = []
    i, j = n, m
    while i > 0:
        if uso[i][j]:
            elegidas.append(vueltas[j - 1])
            i -= 1
        j -= 1
    elegidas.reverse()

    declarada = round(sum(v.distancia_km for v in elegidas), 2)
    total = round(sum(v.distancia_km for v in vueltas), 2)
    return Alineacion(
        ok=True,
        declarada_km=declarada,
        recuperacion_km=round(total - declarada, 2),
        indices=tuple(v.indice for v in elegidas),
    )
