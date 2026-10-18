#!/bin/bash
# Actualiza el codigo de una instalacion existente sin tocar data/, logs/ ni .env.
#
#   sudo bash actualizar.sh /tmp/sr.tar
#
# El tar sale de `git archive --format=tar -o sr.tar HEAD` en el PC: lleva solo
# los archivos versionados, asi que no puede pisar la base, los tokens ni el
# .env de la Pi (un `scp -r .` del checkout si lo haria).
#
# Para el bot, respalda data/, extrae, devuelve la propiedad a sportreport,
# reinicia y corre el diagnostico. No dispara el reporte semanal.
# No reinstala dependencias: si cambiaron, corre despues instalar.sh.
set -euo pipefail

DESTINO=/opt/sport_report
USUARIO=sportreport
TAR="${1:-/tmp/sr.tar}"

[ "$(id -u)" -eq 0 ] || { echo "Ejecutalo con sudo"; exit 1; }
[ -f "$TAR" ] || { echo "No existe $TAR"; exit 1; }
# La lista va a una variable: con pipefail, `tar | grep -q` puede fallar por
# SIGPIPE justo cuando grep encuentra algo.
CONTENIDO="$(tar -tf "$TAR")"
if grep -qE '^(data|logs|respaldos)/|^\.env$' <<<"$CONTENIDO"; then
  echo "$TAR trae data/, logs/, respaldos/ o .env: no lo extraigo"
  exit 1
fi
cd "$DESTINO"

echo "== Parando el bot"
systemctl stop sport-report-bot

echo "== Respaldo de data/"
if ! sudo -u "$USUARIO" .venv/bin/python -m sport_report.respaldo; then
  echo "Fallo el respaldo del modulo; copia simple de data/"
  cp -a data "/opt/sport_report_data_backup_$(date +%F_%H%M)"
fi

echo "== Extrayendo el codigo nuevo"
tar -xf "$TAR" --no-same-owner -C "$DESTINO"
chown -R "$USUARIO:$USUARIO" "$DESTINO"

echo "== Arrancando el bot"
systemctl restart sport-report-bot
sleep 5
systemctl is-active sport-report-bot
journalctl -u sport-report-bot -n 30 --no-pager

echo "== Diagnostico"
sudo -u "$USUARIO" .venv/bin/python -m sport_report.diagnostico || echo "diagnostico salio con $?"
rm -f "$TAR"
echo "== Listo"
