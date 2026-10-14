#!/bin/sh
# Caddy (página + proxy) e api.py em segundo plano; o motor em primeiro plano, então se ele cair o container reinicia.
python3 /app/api.py --addr 127.0.0.1 --port 8082 &
# A página e o motor ficam atrás da senha de admin (ADMIN_USER / ADMIN_PASS nas variáveis do Railway).
if [ -z "$ADMIN_USER" ] || [ -z "$ADMIN_PASS" ]; then
	echo "ERRO: defina ADMIN_USER e ADMIN_PASS nas variáveis do serviço" >&2
	exit 1
fi
ADMIN_PASS_HASH="$(caddy hash-password --plaintext "$ADMIN_PASS")"
export ADMIN_PASS_HASH
caddy start --config /etc/caddy/Caddyfile --adapter caddyfile
exec google-maps-scraper -web -data-folder /gmapsdata -addr 127.0.0.1:8081
