# Motor do scraper + página "Buscar leads" no mesmo endereço.
# O Caddy serve a página em / e repassa /api/* para o motor (127.0.0.1:8081) e /enrich para o
# serviço de redes sociais e e-mails (enrich.py, 127.0.0.1:8082).
# ATENÇÃO: não há autenticação. Quem tiver a URL pode usar o scraper.
FROM caddy:2.8.4 AS caddy

FROM gosom/google-maps-scraper:v1.18.1
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
COPY --from=caddy /usr/bin/caddy /usr/bin/caddy
COPY Caddyfile /etc/caddy/Caddyfile
COPY web/ /srv/
COPY scrape.py enrich.py /app/
COPY start.sh /start.sh
EXPOSE 8080
ENTRYPOINT ["/bin/sh", "/start.sh"]
