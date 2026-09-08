# Runbook — instalación en la Raspberry Pi

## Antes de empezar

Necesitas las credenciales de los otros runbooks:

- [runbook-intervals.md](runbook-intervals.md) — API key de la **fuente
  principal** (obligatorio)
- [runbook-telegram.md](runbook-telegram.md) — bot y chat_id (obligatorio)
- [runbook-strava.md](runbook-strava.md) — app OAuth del **respaldo** (opcional:
  sin esto el sistema funciona, pero si intervals.icu cae esa semana no se
  reporta nada nuevo)
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

## 3. Completar credenciales

**No teclees las claves en la Pi.** Su teclado pierde y sustituye caracteres, y
una clave con un carácter perdido da un 401 que parece un problema de la API.
Escribe el `.env` en el PC y cópialo:

```bash
scp .env pi@raspberrypi.local:/tmp/env
sudo install -o sportreport -g sportreport -m 600 /tmp/env /opt/sport_report/.env
```

Después comprueba **contra la API**, no a ojo:

```bash
sudo -u sportreport /opt/sport_report/.venv/bin/python -m sport_report.intervals.verificar
```

Imprime el nombre del atleta y la **longitud** de la clave, nunca la clave. Si
da 401, el carácter que falta está en el archivo: se vuelve a copiar, no se
re-teclea.

La primera vez, además, hay que cerrar la tabla de nombres de campo (ver
[runbook-intervals.md](runbook-intervals.md), sección 4):

```bash
sudo -u sportreport /opt/sport_report/.venv/bin/python -m sport_report.intervals.verificar --volcar-claves
```

### Respaldo de Strava (opcional)

```bash
sudo -u sportreport /opt/sport_report/.venv/bin/python -m sport_report.strava.autorizar
```

Si la Pi no tiene navegador (lo normal), corre `autorizar` **en el PC** y copia
el `data/tokens.json` resultante:

```bash
scp data/tokens.json pi@raspberrypi.local:/tmp/
sudo install -o sportreport -g sportreport -m 600 /tmp/tokens.json /opt/sport_report/data/
```

## 3bis. Migrar una instalación anterior a fase 2

Solo aplica si la Pi ya venía corriendo la versión de solo-Strava. Dos cosas
cambian de formato, y **el respaldo del paso 1 no es opcional**: la
reconstrucción de la tabla `sesiones` es la única operación destructiva del
esquema.

```bash
cd /opt/sport_report

# 1. RESPALDO, antes de copiar el codigo nuevo.
sudo -u sportreport ./.venv/bin/python -m sport_report.respaldo
# Anota el recuento de sesiones y la primera fecha, para comparar despues:
sudo -u sportreport ./.venv/bin/python -m sport_report.diagnostico | grep historico

# 2. Copiar el codigo nuevo y reinstalar (pasos 1 y 2 de arriba).

# 3. La base se migra sola al primer `Repo()`. Comprobar que no perdio nada:
sudo -u sportreport ./.venv/bin/python -m sport_report.diagnostico | grep historico

# 4. Los planes guardados: v1 se lee igual, pero conviene reescribirlos ya.
sudo -u sportreport ./.venv/bin/python -m sport_report.plan.migrar --dry-run
sudo -u sportreport ./.venv/bin/python -m sport_report.plan.migrar
```

Qué cambia exactamente:

- **La base.** `sesiones` y `vueltas` se reconstruyen con clave `(fuente,
  id_externo)`: el `strava_id INTEGER PRIMARY KEY` era un alias de rowid y no
  acepta el id textual de intervals.icu. Todo el histórico queda como `fuente =
  'strava'`, con sus cargas, decouplings y banderas intactos. Se agregan las
  columnas de dinámica avanzada (en `NULL` para el histórico, porque Strava no
  las expone) y las tablas `bienestar` y `fuentes_semana`. Reabrir la base no
  vuelve a migrar.
- **Los planes.** Cada día pasa a guardar una *lista* de sesiones. Un
  `plan_actual.json` en v1 se sigue leyendo sin tocarlo y se reescribe en v2 la
  primera vez que algo lo modifica; `plan.migrar` lo hace de una para no dejarlo
  a medias en un momento que no eligió nadie. Un plan corrupto no se reescribe:
  se reporta y se deja para mirarlo a mano.

**El histórico no se re-ingiere desde intervals.icu.** Solo se ingiere hacia
adelante, así que las semanas viejas quedan sin dinámica avanzada ni bienestar, y
sus reportes lo dicen.

## 4. Historial inicial y verificación

```bash
cd /opt/sport_report
sudo -u sportreport ./.venv/bin/python -m sport_report.backfill 120
sudo -u sportreport ./.venv/bin/python -m sport_report.diagnostico
sudo systemctl restart sport-report-bot
```

`diagnostico` sale con código 0 (todo bien), 1 (advertencias) o 2 (fallas). Es
lo primero que hay que correr cuando algo no anda.

## 5. Probar la corrida completa sin enviar nada

```bash
sudo -u sportreport ./.venv/bin/python -m sport_report.run_weekly --dry-run
```

Imprime el mensaje exacto que mandaría. Con `--sin-ingesta` no consulta ninguna
fuente, con `--sin-narrativa` no llama a Claude, con `--fuente strava` fuerza el
respaldo y con `--sin-mensual` salta el reporte mensual.

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

### Respaldo de `data/`

La corrida semanal deja una copia en `BACKUP_DIR` (por defecto
`/opt/sport_report/respaldos`) y conserva las últimas 8. Se puede forzar a mano:

```bash
sudo -u sportreport /opt/sport_report/.venv/bin/python -m sport_report.respaldo
```

**El destino por defecto está en la misma SD que el original**, así que sirve
contra un borrado accidental pero no contra la muerte de la tarjeta, que es el
fallo típico de una Pi. Para que valga de verdad, monta otro medio y apúntalo:

```bash
# En /etc/fstab, por ejemplo un pendrive
sudo mkdir -p /mnt/respaldo
echo 'BACKUP_DIR=/mnt/respaldo/sport_report' | sudo tee -a /opt/sport_report/.env
sudo systemctl restart sport-report-bot
```

`diagnostico` avisa si el último respaldo tiene más de 8 días (la corrida
semanal debería dejar uno cada lunes) y si el destino sigue en la misma máquina.

#### Restaurar

Los respaldos son carpetas con fecha; dentro está lo mismo que en `data/`.

```bash
sudo systemctl stop sport-report-bot
sudo -u sportreport cp -r /mnt/respaldo/sport_report/2026-09-07T07-00/. /opt/sport_report/data/
sudo systemctl start sport-report-bot
sudo -u sportreport /opt/sport_report/.venv/bin/python -m sport_report.diagnostico
```

Detener el bot antes no es opcional: si escribe mientras se restaura, la base
queda con una mezcla de las dos versiones.

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
