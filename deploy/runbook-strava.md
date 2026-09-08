# Runbook — app de Strava y autorización inicial

La Pi corre sin nadie en el loop, así que necesita su propia app OAuth y su
propio `refresh_token`. Esto se hace **una sola vez**.

## 1. Registrar la app

1. Entra a <https://www.strava.com/settings/api> con tu cuenta.
2. Completa el formulario. El campo que importa:
   **Authorization Callback Domain** = `localhost` (exactamente eso, sin
   `http://`, sin puerto, sin ruta).
3. Anota **Client ID** y **Client Secret** → van a `.env`:

```
STRAVA_CLIENT_ID=12345
STRAVA_CLIENT_SECRET=abc...
```

## 2. Autorizar (una vez, con navegador)

```bash
python -m sport_report.strava.autorizar
```

Abre el navegador, te pide autorizar la app y guarda los tokens en
`data/tokens.json`. Puedes hacerlo desde el PC y **copiar ese archivo a la Pi**
(misma ruta) — no hace falta navegador en la Pi.

Los scopes que pide son `read`, `activity:read_all` y `profile:read_all`:

| Scope | Para qué |
|---|---|
| `activity:read_all` | ver también las actividades marcadas como privadas |
| `profile:read_all` | leer `GET /athlete/zones` (zonas de HR) |

Si autorizas sin `profile:read_all`, el sistema sigue funcionando pero la carga
se calcula con el peso de zona fallback y **queda marcada como imprecisa** en el
reporte, tal como exige la spec.

## 3. Verificar

```bash
python -m sport_report.strava.verificar
```

Debe imprimir el atleta, las zonas de HR (o avisar que no hay) y las
actividades de los últimos 7 días.

## Sobre la rotación del `refresh_token`

Strava puede devolver un `refresh_token` **nuevo en cualquier intercambio**. Si
no se persiste, el cron se rompe solo semanas después de instalarlo, en
silencio. Por eso en este sistema:

- Los tokens viven en `data/tokens.json`, **no en `.env`** (reescribir `.env`
  desde un proceso es frágil y se corrompe fácil).
- Se escriben de forma atómica (tmp + `os.replace`) con lock, en **cada**
  intercambio, aunque el `refresh_token` parezca no haber cambiado.
- Se persisten **antes** de devolver el `access_token` al llamador.

`STRAVA_REFRESH_TOKEN` en `.env` es opcional y sirve solo para sembrar la
primera corrida (por ejemplo si ya tenías un token de antes). Una vez que existe
`data/tokens.json`, ese valor deja de leerse.

### Si Strava rechaza el refresh

El sistema falla con un mensaje explícito pidiendo re-autorizar, no reintenta en
loop. Repite el paso 2. Causas típicas: revocaste el acceso desde
<https://www.strava.com/settings/apps>, o cambiaste el Client Secret.

## Límites de cuota

Strava permite 100 requests cada 15 minutos y 1000 al día (por app). Una corrida
semanal normal usa ~10 requests (1 de actividades + 1 stream por sesión + zonas).

El **backfill inicial** de 120 días sí puede acercarse al límite, así que el
cliente acepta `pausa_entre_requests` para espaciarlas. Ante un 429 espera y
reintenta; si la espera supera los 16 minutos aborta con `StravaRateLimit` en
vez de colgar la corrida.

## 4. Carga histórica inicial (backfill)

Dos cosas necesitan historial: **ACWR** pide 28 días para ser confiable y el
**gráfico de volumen** dibuja 16 semanas, o sea 112 días. Sin esto las primeras
cuatro semanas mostrarían ACWR y Monotony como no confiables, y el gráfico casi
vacío, aunque los datos ya estén en Strava.

```bash
python -m sport_report.backfill 120
```

Trae los días pedidos con 1.5 s de pausa entre requests, holgado dentro de las
100 por 15 minutos. Es idempotente: se puede repetir sin duplicar filas ni
volver a bajar streams ya procesados, así que el coste real es una sola vez.

Al final imprime cuántos días quedaron con carga registrada. Si ese número es
bajo, revisa el aviso de sesiones sin HR: una corrida sin banda ni pulsómetro
**no tiene carga** (queda `null`, no 0) y por eso no cuenta para ACWR.
