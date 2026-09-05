# Runbook — instalación en la Raspberry Pi

## Antes de empezar

Necesitas las credenciales de los otros tres runbooks:

- [runbook-strava.md](runbook-strava.md) — app OAuth (obligatorio)
- [runbook-telegram.md](runbook-telegram.md) — bot y chat_id (obligatorio)
- [runbook-anthropic.md](runbook-anthropic.md) — API key (opcional: sin esto el
  reporte llega igual, sin el párrafo de resumen)

La Pi necesita **Python 3.11+**. Raspberry Pi OS bookworm trae 3.11; si estás en
bullseye (3.9), actualiza el sistema o el SDK de Anthropic no va a instalar.

## 1. Copiar el código

Desde el PC:

```bash
scp -r . pi@raspberrypi.local:/tmp/sport_report
```

En la Pi:

```bash
sudo mkdir -p /opt/sport_report
sudo cp -r /tmp/sport_report/. /opt/sport_report/
```

## 2. Instalar

```bash
sudo bash /opt/sport_report/deploy/instalar.sh
```

El script es idempotente — se puede volver a correr después de cada
actualización del código. Hace:

- Deja la **zona horaria** del sistema en `America/Santiago`. Esto no es
  cosmético: `systemd` interpreta `OnCalendar` en hora local, así que con la Pi
  en UTC el reporte llegaría a las 03:00 o 04:00 según el horario de verano.
- Crea el usuario de servicio `sportreport` (sin shell, sin login).
- Crea el venv e instala el paquete.
- Deja `.env` en modo `600` y `data/` `logs/` en `750`.
- Instala y habilita las unidades de systemd.

## 3. Completar credenciales y autorizar

```bash
sudo -u sportreport nano /opt/sport_report/.env

sudo -u sportreport /opt/sport_report/.venv/bin/python -m sport_report.strava.autorizar
```

Si la Pi no tiene navegador (lo normal), corre `autorizar` **en el PC** y copia
el `data/tokens.json` resultante:

```bash
scp data/tokens.json pi@raspberrypi.local:/tmp/
sudo install -o sportreport -g sportreport -m 600 /tmp/tokens.json /opt/sport_report/data/
```

## 4. Historial inicial y verificación

```bash
cd /opt/sport_report
sudo -u sportreport ./.venv/bin/python -m sport_report.strava.backfill 35
sudo -u sportreport ./.venv/bin/python -m sport_report.diagnostico
sudo systemctl restart sport-report-bot
```

`diagnostico` sale con código 0 (todo bien), 1 (advertencias) o 2 (fallas). Es
lo primero que hay que correr cuando algo no anda.

## 5. Probar la corrida completa sin enviar nada

```bash
sudo -u sportreport ./.venv/bin/python -m sport_report.run_weekly --dry-run
```

Imprime el mensaje exacto que mandaría. Con `--sin-ingesta` no toca Strava y con
`--sin-narrativa` no llama a Claude.

Para forzar el envío real de una semana concreta:

```bash
sudo -u sportreport ./.venv/bin/python -m sport_report.run_weekly --semana 2026-09-07
```

---

## Operación

```bash
# Estado
systemctl status sport-report-bot
systemctl list-timers sport-report-weekly.timer

# Logs de systemd
journalctl -u sport-report-bot -f
journalctl -u sport-report-weekly -n 100

# Logs propios (rotan solos, 2 MB x 5 archivos)
tail -f /opt/sport_report/logs/bot.log
tail -f /opt/sport_report/logs/run_weekly.log

# Disparar el reporte a mano, sin esperar al lunes
sudo systemctl start sport-report-weekly.service
```

### Actualizar el código

```bash
sudo cp -r /tmp/sport_report/. /opt/sport_report/
sudo bash /opt/sport_report/deploy/instalar.sh
sudo systemctl restart sport-report-bot
```

`instalar.sh` no pisa el `.env` existente ni `data/`. Se puede saltar cuando solo
cambio codigo Python (la instalacion es `pip install -e .`, el venv apunta al
fuente y basta el `restart`); hay que correrlo cuando cambian las dependencias.

**matplotlib** es la unica dependencia pesada: ~80 MB con numpy. En Raspberry Pi
OS de 64 bits hay wheels para aarch64, asi que `pip` no compila nada. Si aun asi
fallara, el sistema sigue funcionando: el reporte llega sin la imagen y la
corrida queda en `parcial`.

---

## Diagnóstico de fallas

| Síntoma | Causa probable | Qué hacer |
|---|---|---|
| El bot no responde | Servicio caído o `chat_id` equivocado | `systemctl status sport-report-bot`; si el log dice "mensaje ignorado de chat no autorizado", el `TELEGRAM_CHAT_ID` del `.env` no es el tuyo |
| El bot responde pero no llega el reporte del lunes | El timer no está habilitado, o la Pi estaba apagada | `systemctl list-timers`; con `Persistent=true` la corrida perdida se ejecuta al arrancar |
| El reporte llegó a las 3 de la mañana | La Pi está en UTC | `timedatectl set-timezone America/Santiago` y `systemctl restart sport-report-weekly.timer` |
| "no se pudo sincronizar con Strava" en el mensaje | Token revocado, cuota, o red | `diagnostico`; si dice "hay que re-autorizar", repite el paso 3 |
| El reporte llega sin resumen narrativo | Falta la API key de Anthropic, o la API falló | Es el comportamiento esperado, no una falla. El motivo está en `run_weekly.log` |
| ACWR y Monotony salen "no confiable" | Menos de 28 días de histórico | Corre el backfill; si ya lo corriste, mira cuántas sesiones tienen HR — sin pulsómetro no hay carga |
| Todas las sesiones sin carga | El reloj no manda HR, o falta el scope | `python -m sport_report.strava.verificar` muestra los streams de la última corrida |
| La carga sale "imprecisa" | Sin zonas de HR en Strava | Configúralas en Strava, o acéptalo: el sistema lo marca en cada reporte |
| Locks huérfanos en `data/` | Un proceso murió a mitad de escritura | Se limpian solos a los 30 s; si persisten, `rm data/*.lock` con los servicios detenidos |

### Reprocesar una semana

Los datos crudos quedan en SQLite, así que se puede recalcular sin volver a
consultar Strava:

```bash
sudo -u sportreport ./.venv/bin/python -m sport_report.run_weekly \
    --semana 2026-09-07 --sin-ingesta --dry-run
```

El JSON de cada corrida queda en `data/reportes/<lunes>.json` y la bitácora en
la tabla `corridas` de SQLite (los últimos 5 los muestra `diagnostico`).

---

## Respaldo

Lo único irreemplazable:

```bash
sudo tar czf ~/sport_report_backup.tgz \
    -C /opt/sport_report .env data/
```

- `.env` — las credenciales.
- `data/tokens.json` — el `refresh_token` vigente. **Si lo pierdes hay que
  volver a autorizar** (paso 3), no es recuperable.
- `data/sport_report.db` — el histórico. Se puede reconstruir con el backfill,
  pero solo hasta donde llegue el límite de Strava.
- `data/plan_actual.json` y `data/planes/` — los planes cargados a mano.
