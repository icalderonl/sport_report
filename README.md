<img src="assets/logo-256.png" alt="" width="96" align="right">

# Sistema de reporte semanal de entrenamiento

[![tests](https://github.com/icalderonl/sport_report/actions/workflows/tests.yml/badge.svg)](https://github.com/icalderonl/sport_report/actions/workflows/tests.yml)

Corre en una Raspberry Pi, se dispara solo los **lunes a las 07:00** y manda por
Telegram el reporte de la semana de running que acaba de cerrar: adherencia al
plan, carga (ACWR, Monotony/Strain), deriva cardíaca, un gráfico del volumen de
las últimas 16 semanas y un resumen narrativo generado por IA **sobre esas cifras
ya calculadas**.

Fuente de datos: **solo Strava**. El plan se carga a mano por Telegram.

## Cómo funciona

```
Telegram (bot)  <-- /setplan /plan /fuerza /progreso /volumen /estado
      |
      v
data/plan_actual.json  (anclado a un lunes-domingo concreto)
      |
      v
[timer lunes 07:00] -> Ingesta Strava -> SQLite -> Motor de cálculo
                                                        |
                                          +-------------+-------------+
                                          v                           v
                              Claude API (narrativa)        grafico.py (PNG)
                                          |                           |
                                          +----------> Telegram <-----+
```

Dos procesos en la Pi: el **bot** corre siempre (systemd, long polling — la Pi
está detrás de NAT doméstico y un webhook exigiría puerto público y TLS) y la
**corrida semanal** la dispara un timer.

## Instalación

Cuatro runbooks, en este orden:

1. [deploy/runbook-strava.md](deploy/runbook-strava.md) — app OAuth y autorización
2. [deploy/runbook-telegram.md](deploy/runbook-telegram.md) — bot y chat_id
3. [deploy/runbook-anthropic.md](deploy/runbook-anthropic.md) — API key (opcional)
4. [deploy/runbook-pi.md](deploy/runbook-pi.md) — instalación y operación

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
| `python -m sport_report.strava.autorizar` | Flujo OAuth inicial (una sola vez) |
| `python -m sport_report.strava.verificar` | Confirma la conexión con Strava |
| `python -m sport_report.strava.backfill 120` | Historial inicial: 28 días para ACWR, 112 para el gráfico |
| `python -m sport_report.narrative.probar` | Prueba la capa narrativa |
| `python -m sport_report.telegram.bot` | Levanta el bot en primer plano |
| `python -m sport_report.respaldo [destino]` | Copia `data/` (base, tokens, planes) |

`run_weekly` acepta además `--semana YYYY-MM-DD` para reprocesar una semana
concreta, `--sin-ingesta` para no tocar Strava, `--sin-narrativa` para no llamar
a Claude y `--sin-respaldo` para no copiar `data/` al terminar.

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

Los 7 días son obligatorios. Un plan incompleto o mal escrito se **rechaza
entero**, señalando línea y día. `/setplan proxima` lo ancla a la semana
siguiente (útil si lo cargas el domingo por la noche).

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
no contra la cantidad tecleada. Una sesión de series siempre incluye la
recuperación trotada entre repeticiones, que el plan nunca declara: sin este
ajuste, toda sesión con series mostraría >100% de adherencia. El volumen semanal
sí cuenta el 100% de lo recorrido.

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
en silencio, semanas después de instalarlo.

## Fuera de alcance

- **GCT y oscilación vertical**: la API pública de Strava no los expone.
- **TrainingPeaks y Garmin Connect**: sin API personal; la vía no oficial viola
  sus ToS y, en el caso de Garmin, está técnicamente rota desde marzo de 2026.
- **RPE**: evaluado y descartado.

Ver la sección 6 de la spec original antes de reabrir cualquiera de estas.

## Desarrollo

```bash
pip install -e ".[dev]"
python -m pytest -q
```

315 tests, ninguno toca la red: Strava, Telegram y Claude se prueban con dobles.
`tests/test_regresiones.py` fija los bugs ya corregidos: cada test de ahí falla
si se revierte su arreglo.

```
sport_report/
  config.py         umbrales, pesos y ritmo de estimación, todo en un solo lugar
  fechas.py         semanas lunes-domingo en zona local
  storage.py        JSON atómico con lock (bot y cron comparten archivos)
  logging_setup.py  logs rotados en logs/
  plan/             gramática de /setplan, distancia dura, persistencia
  strava/           OAuth con rotación, cliente, métricas, ingesta, backfill
  db/               esquema y repositorio SQLite
  engine/           ACWR, Foster, adherencia -> JSON único
  narrative/        llamada a Claude + verificación de cifras
  telegram/         bot, comandos, formateo, envío de texto e imagen
  grafico.py        gráfico de volumen en PNG (matplotlib)
  respaldo.py       copia de data/ con rotación
  run_weekly.py     orquestador (lo dispara el timer)
  diagnostico.py    chequeo de salud
deploy/             instalar.sh, unidades systemd y los cuatro runbooks
.github/workflows/  CI: la suite en Python 3.11 y 3.12
assets/             logo del servicio y el script que lo regenera
```
