#!/bin/sh
# Caddy (página + proxy) e enrich.py em segundo plano; o motor em primeiro plano, então se ele cair o container reinicia.
python3 /app/enrich.py --addr 127.0.0.1 --port 8082 &
caddy start --config /etc/caddy/Caddyfile --adapter caddyfile
exec google-maps-scraper -web -data-folder /gmapsdata -addr 127.0.0.1:8081
