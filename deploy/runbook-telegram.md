# Runbook — crear el bot de Telegram

Se hace una sola vez, desde el teléfono o Telegram Desktop. No requiere la Pi.

## 1. Crear el bot

1. Abre un chat con **@BotFather**.
2. `/newbot` → te pide un nombre visible (ej. `Reporte de entrenamiento`) y un
   username que debe terminar en `bot` (ej. `mi_running_report_bot`).
3. BotFather responde con el **token**, con la forma `123456789:AAF...`.
   Ese token va a `TELEGRAM_BOT_TOKEN` en `.env`.

Opcional, para que Telegram te autocomplete los comandos: `/setcommands` en
BotFather, elige tu bot y pega:

```
setplan - Carga el plan de la semana
plan - Muestra el plan vigente y el estado de fuerza
fuerza - Marca una sesion de fuerza como cumplida
estado - Que semana reportaria el cron ahora
ayuda - Ayuda
```

## 2. Obtener el chat_id

El bot solo responde al chat configurado: el token no es un secreto fuerte y
cualquiera que lo tenga puede escribirle.

1. Abre un chat con **tu** bot y mándale cualquier mensaje (ej. `/start`).
2. En un navegador, con tu token:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Busca `"chat":{"id":123456789,...}`. Ese número va a `TELEGRAM_CHAT_ID`.

Si `getUpdates` devuelve una lista vacía, es porque el servicio del bot ya
consumió los updates: detén el servicio y vuelve a mandar el mensaje.

## 3. Configurar y probar

```bash
cp .env.example .env      # y completa TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID
pip install -e ".[dev]"
python -m sport_report.telegram.bot
```

Con el bot corriendo, en Telegram:

```
/setplan
semana: 1
L: rest
M: easy 8km Z2
W: fuerza
J: series 8.6km @Z4 estructura=2km+3x1000m+4x400m+2km
V: prog 10km estructura=3km@6:00+3km@5:30+4km@5:00
S: rest
D: long 16km @6:15/5:45 Z2
```

Debe responder con el resumen día por día. Luego `/plan` y `/fuerza W`.

Los logs quedan en `logs/bot.log`.

## Notas

- Se usa **long polling**, no webhook: la Pi está detrás de NAT doméstico y un
  webhook exigiría puerto público, dominio y TLS.
- El plan vive en `data/plan_actual.json`; los planes de semanas cerradas se
  archivan en `data/planes/<lunes>.json`. Ninguno de los dos va al repo.
- Si `/setplan` rechaza el plan, **no se toca el plan vigente**.
