#!/usr/bin/env bash
# Se ejecuta EN TU MAC. Sube el bot al servidor y lo instala.
# Uso:  ./desplegar/subir.sh IP_DEL_SERVIDOR
set -euo pipefail

IP="${1:-}"
[ -n "$IP" ] || { echo "Uso: ./desplegar/subir.sh IP_DEL_SERVIDOR"; exit 1; }
cd "$(dirname "$0")/.."

[ -f .env ] || { echo "Falta .env: ejecuta primero python3 configurar.py"; exit 1; }
python3 -m unittest -q 2>&1 | tail -2

echo "Subiendo archivos a $IP ..."
rsync -a --delete \
  --exclude '*.log' --exclude '.candado' --exclude '__pycache__' --exclude '.prueba_escritura.json' \
  -e "ssh -o StrictHostKeyChecking=accept-new" \
  ./ "root@$IP:/root/chollos-bot/"

echo "Instalando en el servidor ..."
ssh "root@$IP" "bash /root/chollos-bot/desplegar/instalar.sh"
