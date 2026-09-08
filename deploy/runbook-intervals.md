# Runbook: intervals.icu (fuente principal)

intervals.icu es el **origen de datos** del sistema. Strava queda como
**respaldo** y solo se usa si intervals.icu falla.

Se eligió porque tiene integración oficial con Garmin (no es scraping), da API
key personal gratuita a cualquier usuario sin aprobación de negocio, y expone lo
que Strava no expone: **GCT, oscilación vertical, ratio vertical** y **wellness**
(HRV, HR de reposo, sueño, sleep score, Training Readiness, Body Battery).

---

## 1. Conectar Garmin a intervals.icu

Si todavía no está conectado, en intervals.icu: **Settings → Connections →
Garmin Connect** y autorizar. A partir de ahí cada actividad del reloj llega sola.

Nada de esto lo hace el sistema: es una vez, a mano, en la web.

## 2. Obtener la API key

1. Entrar a <https://intervals.icu>.
2. **Settings → Developer Settings**.
3. Copiar la **API Key**.

La clave **no expira y no rota**. No hay OAuth, no hay `access_token` que
refrescar, no hay archivo de tokens que respaldar para esta fuente. Esa
simplicidad es una de las razones del cambio.

## 3. Escribir el `.env` — nunca teclear la clave en la Pi

> **Importante.** El teclado físico de la Pi pierde y sustituye caracteres. Una
> clave con un carácter perdido da un 401 que parece un problema de la API y no
> lo es.

El procedimiento es siempre el mismo:

1. Editar el `.env` **en el PC**, pegando la clave con copiar/pegar.
2. Copiarlo a la Pi:

```bash
scp .env sportreport@raspberrypi:/opt/sport_report/.env
```

3. Comprobar **contra la API**, no a ojo:

```bash
sudo -u sportreport /opt/sport_report/.venv/bin/python -m sport_report.intervals.verificar
```

`verificar` imprime el nombre del atleta, su id real y la **longitud** de la
clave — nunca la clave. Si da 401, el carácter que falta está en el archivo: se
vuelve a copiar por `scp`, **no se re-teclea**.

Claves del `.env`:

```ini
INTERVALS_API_KEY=<la clave>
# 0 = el atleta autenticado. Si verificar dice otro id, ponlo aquí.
INTERVALS_ATHLETE_ID=0
FUENTE_PRINCIPAL=intervals
```

## 4. Cerrar la tabla de campos (paso obligatorio del primer despliegue)

Varios nombres de campo de la API no se pudieron confirmar sin la cuenta real,
así que el código **no los adivina**: los declara como candidatos en
`sport_report/intervals/campos.py` y toma el primero que exista. Si ninguno
aparece, el dato queda en `null` y el reporte lo dice.

Para cerrar esa tabla con la respuesta real:

```bash
sudo -u sportreport /opt/sport_report/.venv/bin/python -m sport_report.intervals.verificar --volcar-claves
```

Imprime las claves reales de una actividad de carrera, de un registro de
wellness y de `sport-settings`, y por cada entrada de `CANDIDATOS` dice **qué
candidato acertó** y cuáles no existen.

Con esa salida hay que confirmar tres cosas:

1. **Los nombres** de `gct_ms`, `oscilacion_vertical_cm`, `ratio_vertical_pct` y
   `body_battery`. Si acertó un candidato, no hay nada que hacer. Si no acertó
   ninguno, agregar el nombre real al principio de la tupla correspondiente en
   `campos.CANDIDATOS`.
2. **Las unidades.** Garmin da GCT en milisegundos y oscilación vertical en
   milímetros; el reporte los quiere en ms y cm. La conversión se decide por
   magnitud en `campos.gct_ms` / `campos.oscilacion_cm`, en un solo sitio. Un
   GCT de carrera está entre 150 y 400 ms y una oscilación entre 0.4 y 15 cm: si
   los números del volcado están fuera de ese rango, ajustar ahí.
3. **El vocabulario de `type`.** `verificar` imprime los tipos distintos vistos
   y avisa de los que no están en `config.TIPOS_RUN` ni en `TIPOS_FUERZA`. Esto
   importa sobre todo para la **cinta**: si llega con un tipo propio que no está
   en `TIPOS_RUN`, sus kilómetros quedan fuera del volumen **en silencio**. El
   arreglo es una línea en `config.TIPOS_RUN`.

También conviene, una sola vez, comparar los intervalos de una sesión de series
contra las vueltas que Strava da para la misma actividad:

```bash
python -m sport_report.intervals.verificar --json <id_de_la_actividad>
```

intervals.icu llama `intervals` a lo que este sistema usa como vueltas, y son
tramos detectados o editados, no necesariamente los laps que marcó el reloj. Se
usan igual (la alineación solo mira distancia y tiempo), pero si difieren mucho
la comparación de una sesión con `estructura=` puede empeorar respecto a Strava.

## 5. Qué pasa cuando intervals.icu falla

El selector (`sport_report/fuentes/__init__.py`) intenta intervals.icu y, si
falla —caída, 401, timeout, 5xx persistente—, usa **Strava una vez**. Esa semana
queda **degradada**, y el sistema lo dice en todas partes:

- la semana se registra en la tabla `fuentes_semana` con `fallback = 1`;
- el JSON del reporte trae un bloque `fuente` con la lista de lo que falta;
- el aviso va **al frente** de "sobre los datos" en el mensaje de Telegram;
- `gct`, `oscilacion_vertical`, `ratio_vertical` y `fatiga_descanso` salen con
  `disponible: false` y su motivo, **en `null`, nunca en cero**;
- la corrida queda en `parcial`, para que aparezca en la bitácora.

**No hay respaldo del respaldo.** Con `FUENTE_PRINCIPAL=strava` forzado,
intervals.icu no respalda a Strava. Si la fuente que toca falla y no queda
respaldo, el reporte sale con lo que ya hay en la base, avisando.

Para probar el respaldo a propósito, una vez:

```bash
# Comentar INTERVALS_API_KEY en el .env, correr, y volver a ponerla.
python -m sport_report.run_weekly --dry-run --sin-narrativa --sin-mensual
```

El reporte tiene que salir por Strava y **decirlo**.

## 6. Mantener el respaldo vivo

Strava casi nunca se usa, así que su credencial puede podrirse sin que nadie lo
note hasta el día que hace falta. `python -m sport_report.diagnostico` la vigila
y la reporta como aviso (no como falla: sin respaldo el sistema sigue
funcionando, solo queda sin red de seguridad). Ver
[runbook-strava.md](runbook-strava.md) para reautorizar.

## 7. Orden del despliegue

Ver [runbook-pi.md](runbook-pi.md), sección de migración. En resumen:

```bash
# 1. Respaldo ANTES de copiar el codigo nuevo.
python -m sport_report.respaldo
# 2. scp del codigo y del .env escrito en el PC.
# 3. Verificar la clave contra la API.
python -m sport_report.intervals.verificar
# 4. Cerrar la tabla de campos.
python -m sport_report.intervals.verificar --volcar-claves
# 5. Migrar los planes al formato nuevo.
python -m sport_report.plan.migrar --dry-run && python -m sport_report.plan.migrar
# 6. Comprobar que la base migro sin perder nada.
python -m sport_report.diagnostico
# 7. Corrida en seco y despues real.
python -m sport_report.run_weekly --dry-run --sin-narrativa --sin-mensual
```

La base se migra sola la primera vez que se abre (la tabla `sesiones` se
reconstruye con clave `(fuente, id_externo)`, porque el id de intervals.icu es
texto y no cabe en un `INTEGER PRIMARY KEY`). Es la única operación destructiva
del esquema: **el respaldo del paso 1 no es opcional.**
