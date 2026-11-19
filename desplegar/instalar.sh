#!/usr/bin/env bash
# Se ejecuta EN EL SERVIDOR (como root), con los archivos ya subidos a /root/chollos-bot.
# Instala el bot, lo comprueba y solo lo activa si la comprobación sale bien.
set -euo pipefail

ORIGEN=/root/chollos-bot
DESTINO=/opt/chollos-bot

[ "$(id -u)" -eq 0 ] || { echo "Ejecútalo como root"; exit 1; }
[ -d "$ORIGEN" ] || { echo "No encuentro $ORIGEN: sube primero los archivos"; exit 1; }

echo "1/6 Instalando Python y utilidades..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 ca-certificates unattended-upgrades >/dev/null

echo "2/6 Creando usuario sin privilegios 'chollos'..."
id chollos >/dev/null 2>&1 || useradd --system --home "$DESTINO" --shell /usr/sbin/nologin chollos

echo "3/6 Copiando el bot a $DESTINO..."
mkdir -p "$DESTINO"
cp -a "$ORIGEN"/. "$DESTINO"/
rm -rf "$DESTINO/desplegar" "$DESTINO/__pycache__" "$DESTINO/.candado"
chown -R chollos:chollos "$DESTINO"
chmod 700 "$DESTINO"
chmod 600 "$DESTINO/.env" "$DESTINO"/estado.json "$DESTINO"/mercado.json 2>/dev/null || true

echo "4/6 Comprobando que todo funciona desde ESTE servidor..."
if ! sudo -u chollos -H python3 "$DESTINO/chollos.py" --comprobar; then
  echo
  echo "✘ La comprobación ha fallado. NO activo el servicio."
  echo "  Si falla Wallapop o Vinted, puede que bloqueen la IP de este servidor."
  exit 2
fi

echo "5/6 Instalando el servicio..."
install -m 644 "$ORIGEN/desplegar/chollos-bot.service" /etc/systemd/system/chollos-bot.service
systemctl daemon-reload

echo "6/6 Activando (arranca solo al encender el servidor)..."
systemctl enable --now chollos-bot
sleep 3
systemctl --no-pager --lines=5 status chollos-bot || true
echo
echo "✔ Listo. Ver registro en directo:  journalctl -u chollos-bot -f"
