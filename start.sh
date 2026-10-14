#!/bin/sh
# Caddy em segundo plano (página + proxy); o motor em primeiro plano, então se ele cair o container reinicia.
caddy start --config /etc/caddy/Caddyfile --adapter caddyfile
exec google-maps-scraper -web -data-folder /gmapsdata -addr 127.0.0.1:8081
