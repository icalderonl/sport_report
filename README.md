<img src="assets/logo-256.png" alt="" width="96" align="right">

# Sistema de reporte semanal de entrenamiento

[![tests](https://github.com/icalderonl/sport_report/actions/workflows/tests.yml/badge.svg)](https://github.com/icalderonl/sport_report/actions/workflows/tests.yml)

Corre en una Raspberry Pi, se dispara solo los **lunes a las 07:00** y manda por
Telegram el reporte de la semana de running que acaba de cerrar: adherencia al
plan, carga (ACWR, Monotony/Strain), deriva cardíaca, dinámica de carrera (GCT,
oscilación y ratio vertical), fatiga y descanso, un gráfico del volumen de las
últimas 16 semanas y un resumen narrativo generado por IA **sobre esas cifras ya
calculadas**. Una vez al mes añade un reporte mensual, **solo de carrera**,
narrado por un modelo más capaz.

Fuente de datos: **intervals.icu**, con **Strava como respaldo** si esa falla.
El plan se carga a mano por Telegram.

## Cómo funciona

```
Telegram (bot)  <-- /setplan /corregir /plan /carrera /fuerza /progreso /volumen /estado
      |
      v
data/plan_actual.json  (anclado a un lunes-domingo concreto)
data/carrera.json      (la carrera objetivo, que dura meses)
      |
      v
[timer lunes 07:00] -> intervals.icu ---> SQLite -> Motor de cálculo
                        (respaldo: Strava)              |
                                          +-------------+-------------+
                                          v                           v
                              Claude API (narrativa)        grafico.py (PNG)
                                          |                           |
                                          +----------> Telegram <-----+

  una vez al mes, además:  agregados del mes (solo carrera)
                             -> Claude (modelo mayor) -> un mensaje narrado
```

Dos procesos en la Pi: el **bot** corre siempre (systemd, long polling — la Pi
está detrás de NAT doméstico y un webhook exigiría puerto público y TLS) y la
**corrida semanal** la dispara un timer.

## Instalación

Cinco runbooks, en este orden:

1. [deploy/runbook-intervals.md](deploy/runbook-intervals.md) — API key y verificación de campos
2. [deploy/runbook-strava.md](deploy/runbook-strava.md) — app OAuth del respaldo (opcional)
3. [deploy/runbook-telegram.md](deploy/runbook-telegram.md) — bot y chat_id
4. [deploy/runbook-anthropic.md](deploy/runbook-anthropic.md) — API key (opcional)
5. [deploy/runbook-pi.md](deploy/runbook-pi.md) — instalación y operación

```bash
sudo bash deploy/instalar.sh
```

Requiere **Python 3.11+** (el SDK de Anthropic pide 3.10+ y el instalador aborta
por debajo de 3.11). Las dependencias son `httpx`, `python-telegram-bot`,
`python-dotenv`, `anthropic` y `matplotlib` — esta última solo para el gráfico,
y su ausencia no impide que el reporte llegue.

## Comandos

### Por Telegram

| Comando | Para qué |
|---|---|
| `/setplan` | Carga el plan de la semana |
| `/corregir <dia>: ...` | Reemplaza las sesiones de un día sin reenviar la semana |
| `/carrera <fecha> [nombre]` | Declara la carrera objetivo (`/carrera borrar` la quita) |
| `/plan` | Plan vigente y estado de fuerza |
| `/fuerza <dia>` | Marca una sesión de fuerza como cumplida (`anterior`/`proxima` para otra semana) |
| `/progreso` | Resumen corto: km de los programados y qué entrenamientos quedan |
| `/volumen` | Gráfico (PNG) de km por semana de las últimas 16 |
| `/estado` | Qué semana reportaría el cron ahora |

### Por consola

| Comando | Para qué |
|---|---|
| `python -m sport_report.diagnostico` | **Empieza por acá cuando algo falle.** Chequea todo sin tocar la red |
| `python -m sport_report.run_weekly --dry-run` | Imprime el reporte sin enviarlo y deja el gráfico en disco |
| `python -m sport_report.intervals.verificar` | Confirma la clave de intervals.icu (imprime su longitud, nunca la clave) |
| `python -m sport_report.intervals.verificar --volcar-claves` | Paso del despliegue: qué nombres de campo trae la API |
| `python -m sport_report.plan.migrar` | Reescribe los planes guardados al formato v2 |
| `python -m sport_report.strava.autorizar` | Flujo OAuth del respaldo (una sola vez) |
| `python -m sport_report.strava.verificar` | Confirma la conexión con Strava |
| `python -m sport_report.backfill 120` | Historial inicial: 28 días para ACWR, 112 para el gráfico |
| `python -m sport_report.narrative.probar` | Prueba la capa narrativa |
| `python -m sport_report.telegram.bot` | Levanta el bot en primer plano |
| `python -m sport_report.respaldo [destino]` | Copia `data/` (base, tokens, planes) |

`run_weekly` acepta además `--semana YYYY-MM-DD` para reprocesar una semana
concreta, `--fuente intervals|strava` para forzar la fuente, `--sin-ingesta`
para no consultar ninguna, `--sin-narrativa` para no llamar a Claude,
`--mensual` / `--sin-mensual` para forzar o saltar el reporte mensual, y
`--sin-respaldo` para no copiar `data/` al terminar.

## Formato del plan

```
/setplan
semana: 5
L: rest
M: easy 8km Z2
W: fuerza
J: series 8.6km @Z4 estructura=2km+3x1000m+4x400m+2km
V: prog 10km estructura=3km@6:00+3km@5:30+4km@5:00
S: rest
D: long 16km @6:15/5:45 Z2
```

Los 7 días son obligatorios, con **al menos una línea cada uno**. Un día puede
tener varias: `J: series 8.6km ...` y `J: fuerza` el mismo jueves es normal. Lo
que no se admite es `rest` conviviendo con otra sesión (es una contradicción, no
dos sesiones) ni dos `fuerza` el mismo día. Un plan incompleto o mal escrito se
**rechaza entero**, señalando línea y día. `/setplan proxima` lo ancla a la
semana siguiente (útil si lo cargas el domingo por la noche), y `/corregir` toca
un día suelto sin reenviar la semana.

## Decisiones que conviene conocer antes de tocar el código

**Una sesión prescrita en minutos aporta km estimados al volumen planificado.**
Si no, el porcentaje semanal compara dos bases distintas: el numerador suma los
km reales de todas las sesiones y el denominador solo las prescritas en km, así
que una sesión por tiempo lo infla. El ritmo escrito en el plan manda; si no hay,
se usa 7:00/km **solo** para sesiones `easy` (ajustable en `config.Estimacion`).
Una sesión por tiempo de otro tipo y sin ritmo no se estima: queda fuera del
volumen planificado y `/setplan` lo avisa al cargarlo. La adherencia **día a día**
no cambia — cada día se sigue comparando en su unidad nativa. Como el total puede
incluir km que no están escritos en ninguna línea del plan, el resumen dice
cuántos son estimados.

**Un día que todavía no llega no es un incumplimiento.** `/progreso` corre el
mismo motor con `hasta=hoy`: los días futuros quedan en estado `pendiente`, fuera
del porcentaje de sesiones y fuera del volumen planificado, y las ventanas
móviles de ACWR terminan hoy en vez de el domingo. **Hoy también cuenta como
pendiente mientras no haya nada registrado** — marcar la sesión del día como
incumplida a las diez de la mañana sería falso. La respuesta de `/progreso` es
deliberadamente corta: dos preguntas, cuánto llevo y qué me queda. Las métricas
de carga se quedan en el reporte del lunes.

**El gráfico de volumen es una imagen, no caracteres.** `sport_report/grafico.py`
lo dibuja con matplotlib (backend `Agg`, la Pi no tiene entorno gráfico) y se
manda como foto de Telegram, aparte del texto: el pie de foto son 1024
caracteres y el reporte no cabe. La serie **no entra al prompt de Claude**:
cualquier cosa que dijera sobre la tendencia sería una conclusión derivada, y la
verificación posterior solo sabe comprobar números. Si matplotlib falta o falla,
el reporte llega igual sin imagen y la corrida queda en `parcial`.

**intervals.icu es la fuente; Strava es el respaldo.** El selector
(`sport_report/fuentes/`) intenta intervals.icu y solo usa Strava si esa falla.
No hay respaldo del respaldo. Una semana que sale por el respaldo **no se
disfraza de semana normal**: queda registrada en `fuentes_semana`, el JSON trae
un bloque `fuente` con la lista de lo que falta, el aviso va al frente del
mensaje y la corrida queda en `parcial`.

**Lo que solo da intervals.icu queda en `null` cuando su API falla.** GCT,
oscilación vertical, ratio vertical, HRV, HR de reposo, sueño, sleep score,
Body Battery y Training Readiness se obtienen exclusivamente de ahí. Si no
llegan, se reportan como `disponible: false` con el motivo — y el motivo
distingue los tres casos que se arreglan distinto: la semana vino del respaldo,
el reloj no lo midió, o el campo no existe en la API. **Nunca un cero**: un GCT
de 0 ms diría que el pie no tocó el suelo, y un sueño de 0 h que no se durmió.

**Los nombres de campo que no se pudieron confirmar viven en una tabla, no
repartidos por el código.** `intervals/campos.py` declara candidatos por campo y
toma el primero que exista; si ninguno aparece, el dato es `null`.
`intervals.verificar --volcar-claves` imprime las claves reales de la API y dice
cuál acertó, y con esa salida se cierra la tabla en el despliegue. Es la
alternativa a adivinar en silencio.

**El bienestar y la carga se reportan por separado, siempre.** Un HRV a la baja
junto a un ACWR alto es una señal más fuerte que cualquiera de los dos solos,
pero no existe un índice que los combine con respaldo metodológico. El bloque
`fatiga_descanso` enuncia los dos hechos lado a lado con plantillas de Python, y
un test comprueba que no aparezca ningún campo que los fusione: si alguien lo
agrega, ese test cae y hay que discutirlo, no ajustarlo.

**El reporte mensual mide solo carrera.** Calle, pista, cinta, trail. La fuerza
y cualquier otro deporte quedan fuera del volumen, la carga, la adherencia y la
dinámica del mes, y el JSON lo declara para que el texto tenga que decirlo en la
primera línea. Se recalcula desde la base semana a semana y **no** lee los JSON
semanales archivados: puede faltar el de una corrida que falló, y entonces el
mensual no sería reproducible. Al contrario del semanal, la serie semanal sí
entra al prompt —es el contenido del mes—, y por eso todo lo derivable
(progresión, semanas al alza, deltas, promedios, máximos) va precalculado: el
modelo cita, no deriva. Se dispara en la primera corrida del mes nuevo con dos
guardas, la de calendario y la de que el archivo del mes no exista, que es la
que hace la decisión idempotente.

**Un día del plan puede tener varias sesiones.** Con una sola, la adherencia se
comporta exactamente como antes. Con varias se suman en la unidad común; si el
día mezcla km con minutos y alguna no se puede estimar, queda como **dato
faltante**, nunca como incumplimiento. La fuerza de un día doble va en su propio
bloque para no mezclar un check binario con un porcentaje de kilómetros, y el
porcentaje global cuenta corridas planificadas en vez de días.

**La carrera objetivo vive fuera del plan.** `data/carrera.json`, no dentro de
`plan_actual.json`: su vida útil son meses y `/setplan` reemplaza el plan cada
semana. Se usa como contexto en la narrativa y sobre todo en el mensual (semanas
restantes, fase del ciclo).

**Solo cuentan las actividades de carrera.** El volumen semanal, la adherencia
diaria y el gráfico filtran por `config.TIPOS_RUN`: una salida en bici también
trae distancia y sin el filtro entraba a los kilómetros de running. La
excepción es el día de descanso, que sí se marca como roto por cualquier
actividad: ahí lo que se evalúa es si hubo descanso, no cuánto se corrió.

**La carga no se mide en kilómetros.** Se usa un TRIMP por zona (minutos en zona
× peso de zona) recorriendo el stream de HR, porque el plan mezcla sesiones
prescritas por distancia con otras por tiempo. Una sesión sin pulsómetro **no
tiene carga** (`null`, no 0) y por eso no cuenta para ACWR ni Monotony.

**Las sesiones con `estructura=` se comparan contra la distancia dura derivada**,
no contra la cantidad tecleada: la cifra sale de los bloques y no depende de que
el total escrito a mano esté bien. El volumen semanal sí cuenta el 100% de lo
recorrido.

**Las vueltas del reloj son las que hacen comparable esa cifra.** La distancia
dura no incluye la recuperación trotada entre repeticiones —el plan nunca la
declara— pero la distancia de la actividad sí, así que los dos lados de la
división no medían lo mismo: un día de `2km+3x1000m+4x400m+2km` con 8.6 km
declarados y 10 km reales salía en 116.3% sin que el atleta se hubiera desviado
del plan. `engine/vueltas.py` empareja cada segmento declarado con la vuelta que
le corresponde y deja fuera el resto, así que se compara declarado contra
declarado.

El emparejamiento va en orden y por distancia, con la tolerancia que exige el
GPS. La distancia sola no basta: en un `4x400m` con 400 m de trote entre
repeticiones las ocho vueltas miden lo mismo. Lo que las separa es el ritmo, así
que entre todas las alineaciones válidas se elige la de **menor tiempo total** —
de las vueltas que podrían ser el trabajo declarado, el trabajo declarado es la
que se corrió rápido. Se resuelve con programación dinámica, no con la primera
coincidencia, porque a veces conviene descartar un emparejamiento temprano para
habilitar uno mejor más adelante.

**Si no se puede emparejar, no se inventa el número.** Sin vueltas marcadas, con
menos vueltas que segmentos, o si alguna no encuentra pareja, se vuelve a
comparar contra el total y la nota del día dice por qué el porcentaje sale alto.
La línea del reporte muestra los kilómetros que quedaron fuera (`+1.4km rec`)
para que el día a día no parezca contradecir el volumen semanal, que sí los
cuenta.

La banda de "cumplida" es **80–120%**, ancha a propósito: es el colchón para los
días en que el emparejamiento no se puede hacer.

Las vueltas se piden a Strava una sola vez por actividad y se guardan en la tabla
`vueltas`; una actividad sin ellas queda marcada igual para no volver a
preguntar. Es una llamada más por corrida ingerida, y la primera sincronización
después de actualizar recorre el histórico una vez.

**Hay tres estados distintos, no dos.** Sesión sin registrar = 0%. Sesión
registrada pero sin el dato que pide el plan (indoor sin GPS cuando el plan
pedía km) = `null`, dato faltante. No son lo mismo y el reporte los distingue.

**Ninguna cifra sale sin su bandera de confiabilidad.** ACWR con menos de 28 días
de histórico, Monotony con desviación 0, carga sin zonas de HR configuradas: todo
eso va marcado con el motivo, nunca presentado como dato fino.

**El LLM narra, no calcula.** Recibe solo el JSON del motor de cálculo, con
prohibición explícita de introducir cifras nuevas, y después el texto pasa por
**dos** verificaciones en código, que atrapan errores distintos: una comprueba
que cada número del texto *exista* en el JSON (la cifra inventada), y otra que un
número pegado al nombre de una métrica sea *de esa métrica* (la cifra
intercambiada). La segunda existe porque la primera no basta: con veinte métricas
en el JSON, escribir «ACWR 1.32» cuando 1.32 es el Monotony pasa la primera sin
problemas. Ninguna de las dos descarta el texto —un falso positivo dejaría el
reporte mudo—, pero una cifra mal atribuida sube a la sección «sobre los datos»
del mensaje para que el atleta sepa que no se fíe de ese número. Las cifras del
cuerpo del reporte salen del motor, no del modelo, y siguen siendo válidas.

**El reporte siempre llega.** Si Strava falla se reporta con lo que hay en la
base, avisando; si Claude falla se reporta sin narrativa. Solo un fallo del envío
por Telegram hace fracasar la corrida.

**`data/` se respalda en cada corrida semanal.** Ahí viven el `refresh_token`,
todo el histórico y los planes archivados, en una sola SD. La base se copia con
la API de backup de SQLite y no con `cp`: el bot puede estar escribiendo y, con
WAL activo, copiar el archivo suelto da una base inconsistente. Va **al final**
de la corrida porque la ingesta puede haber rotado el `refresh_token`, y un
respaldo con el token anterior no sirve para restaurar. Por defecto queda en
`./respaldos`, que protege contra un borrado accidental pero **no** contra la
muerte de la tarjeta: apunta `BACKUP_DIR` a otro medio. Que falle nunca hace
fracasar el reporte.

**El `refresh_token` de Strava rota.** Se persiste en `data/tokens.json` de forma
atómica en cada intercambio, antes de usarse. Sin esto el cron se rompe solo,
en silencio, semanas después de instalarlo. La clave de intervals.icu, en
cambio, no rota ni expira: no hay nada que gestionar. Como el respaldo casi
nunca se ejercita, el diagnóstico vigila su credencial aparte — puede podrirse
sin que nadie lo note hasta el día que hace falta.

**La base identifica cada sesión por `(fuente, id_externo)`.** El `strava_id
INTEGER PRIMARY KEY` de antes era un alias de rowid y no acepta el id textual de
intervals.icu, así que `sesiones` y `vueltas` se reconstruyen la primera vez que
se abre la base. Todo el histórico queda como `fuente = 'strava'`. Con dos
fuentes conviviendo, **por día local manda una sola** (intervals.icu gana, porque
su registro es el completo): sin esa regla, la misma corrida ingerida por las dos
se contaría dos veces.

## Fuera de alcance

- **TrainingPeaks y Garmin Connect**: sin API personal; la vía no oficial viola
  sus ToS y, en el caso de Garmin, está técnicamente rota desde marzo de 2026.
  Tampoco por la vía oficial se puede leer el calendario planificado de Garmin,
  que es la razón de que el plan se cargue por Telegram.
- **El calendario de intervals.icu como fuente del plan**: es técnicamente
  viable, se descartó por simplicidad — un solo canal de interacción.
- **RPE**: evaluado y descartado.

GCT, oscilación vertical y ratio vertical **ya no están fuera de alcance**:
intervals.icu los expone. Con el respaldo de Strava activo quedan en `null` esa
semana, dicho explícitamente.

Ver la sección 4 y 11 de la spec original antes de reabrir cualquiera de estas.

## Desarrollo

```bash
pip install -e ".[dev]"
python -m pytest -q
```

**Desarrolla en 3.11 o superior**, no solo porque lo diga `pyproject.toml`.
En un intérprete más viejo `pip` descarta en silencio las versiones que piden
3.10+ y resuelve a otras: con Python 3.9 instala `anthropic` 0.x mientras que
la Pi usa 1.x. Los tests pasan igual y estarías probando contra un major que
en producción no existe. La CI corre en 3.11 y 3.12 justamente por esto.

660 tests, ninguno toca la red: intervals.icu, Strava, Telegram y Claude se
prueban con dobles (`httpx.MockTransport` para los clientes HTTP).
`tests/test_regresiones.py` fija los bugs ya corregidos: cada test de ahí falla
si se revierte su arreglo.

```
sport_report/
  config.py         umbrales, pesos y ritmo de estimación, todo en un solo lugar
  fechas.py         semanas lunes-domingo en zona local
  storage.py        JSON atómico con lock (bot y cron comparten archivos)
  logging_setup.py  logs rotados en logs/
  plan/             gramática de /setplan, distancia dura, persistencia, carrera
  fuentes/          selector con respaldo + orquestación común de la ingesta
  intervals/        cliente, tabla de campos, ingesta y verificación (principal)
  strava/           OAuth con rotación, cliente, métricas, ingesta (respaldo)
  db/               esquema y repositorio SQLite
  engine/           ACWR, Foster, adherencia, vueltas, fatiga, mensual -> JSON único
  narrative/        llamada a Claude (semanal y mensual) + verificación de cifras
  telegram/         bot, comandos, formateo, envío de texto e imagen
  grafico.py        gráfico de volumen en PNG (matplotlib)
  backfill.py       carga histórica inicial, desde la fuente que se elija
  respaldo.py       copia de data/ con rotación
  run_weekly.py     orquestador (lo dispara el timer)
  diagnostico.py    chequeo de salud
deploy/             instalar.sh, unidades systemd y los cinco runbooks
.github/workflows/  CI: la suite en Python 3.11 y 3.12
assets/             logo del servicio y el script que lo regenera
```

## Licencia

MIT. Ver [LICENSE](LICENSE).
