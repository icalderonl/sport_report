#!/usr/bin/env bash
# Instalacion en la Raspberry Pi. Idempotente: se puede volver a correr.
#
#   sudo bash deploy/instalar.sh
#
# Asume que el codigo ya esta en DESTINO (copiado con scp o clonado).
set -euo pipefail

DESTINO="${DESTINO:-/opt/sport_report}"
# Interprete a usar. En sistemas cuyo python3 de apt es viejo (Raspbian buster
# trae 3.7) se pasa uno compilado aparte:
#   sudo PYTHON=/usr/local/bin/python3.12 bash deploy/instalar.sh
PYTHON="${PYTHON:-python3}"
USUARIO="${USUARIO:-sportreport}"
ZONA="${ZONA:-America/Santiago}"

if [[ $EUID -ne 0 ]]; then
  echo "Corre esto con sudo." >&2
  exit 1
fi

echo "==> Zona horaria del sistema"
# systemd interpreta OnCalendar en hora local: si la Pi esta en UTC, el reporte
# llegaria a las 03:00 o a las 04:00 segun el horario de verano.
actual="$(timedatectl show -p Timezone --value)"
if [[ "$actual" != "$ZONA" ]]; then
  echo "    cambiando de $actual a $ZONA"
  timedatectl set-timezone "$ZONA"
else
  echo "    ya es $ZONA"
fi

echo "==> Usuario de servicio"
if ! id -u "$USUARIO" >/dev/null 2>&1; then
  useradd --system --home-dir "$DESTINO" --shell /usr/sbin/nologin "$USUARIO"
  echo "    creado $USUARIO"
else
  echo "    $USUARIO ya existe"
fi

echo "==> Interprete"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "    no existe '$PYTHON'." >&2
  exit 1
fi

# Solo se instalan los paquetes de apt cuando se usa el python del sistema; con
# un interprete compilado aparte, apt no tiene nada que aportar (y en un
# sistema con repos archivados, 'apt-get update' falla).
if [[ "$PYTHON" == "python3" ]]; then
  echo "==> Dependencias del sistema"
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-dev
fi

py_ver="$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
echo "    $PYTHON -> $("$PYTHON" -V)"
if [[ "$(printf '%s\n3.11\n' "$py_ver" | sort -V | head -1)" != "3.11" ]]; then
  echo "    ERROR: se necesita Python 3.11+ (el SDK anthropic exige 3.10+)." >&2
  echo "    Pasa otro interprete con PYTHON=/ruta/al/python3.12" >&2
  exit 1
fi

# Sin sqlite3 el sistema no puede guardar nada, y sin ssl no habla con ninguna
# API. En un Python compilado a mano faltan si al configurar no estaban las
# cabeceras de desarrollo correspondientes: mejor detectarlo aca que en la
# primera corrida del cron.
"$PYTHON" -c 'import sqlite3, ssl, zoneinfo' || {
  echo "    ERROR: al interprete le faltan modulos (sqlite3 / ssl / zoneinfo)." >&2
  exit 1
}

echo "==> Entorno virtual e instalacion"
cd "$DESTINO"
[[ -d .venv ]] || "$PYTHON" -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -e .

echo "==> Directorios de datos"
mkdir -p data logs
chown -R "$USUARIO:$USUARIO" "$DESTINO"
chmod 750 "$DESTINO/data" "$DESTINO/logs"

if [[ -f .env ]]; then
  chown "$USUARIO:$USUARIO" .env
  chmod 600 .env
  echo "    .env con permisos 600"
else
  cp .env.example .env
  chown "$USUARIO:$USUARIO" .env
  chmod 600 .env
  echo "    .env creado desde el ejemplo: FALTA COMPLETARLO"
fi

echo "==> Unidades de systemd"
for u in sport-report-bot.service sport-report-weekly.service sport-report-weekly.timer; do
  sed -e "s#/opt/sport_report#$DESTINO#g" \
      -e "s#User=sportreport#User=$USUARIO#" \
      -e "s#Group=sportreport#Group=$USUARIO#" \
      "$DESTINO/deploy/$u" > "/etc/systemd/system/$u"
done
systemctl daemon-reload
systemctl enable --now sport-report-bot.service
systemctl enable --now sport-report-weekly.timer

echo
echo "Listo. Siguientes pasos:"
echo "  1. Completa $DESTINO/.env  (ver deploy/runbook-*.md)"
echo "  2. sudo -u $USUARIO $DESTINO/.venv/bin/python -m sport_report.strava.autorizar"
echo "  3. sudo -u $USUARIO $DESTINO/.venv/bin/python -m sport_report.backfill 120"
echo "  4. sudo -u $USUARIO $DESTINO/.venv/bin/python -m sport_report.diagnostico"
echo "  5. sudo systemctl restart sport-report-bot"
echo
systemctl list-timers sport-report-weekly.timer --no-pager || true
