# Motor do scraper + página "Buscar leads" no mesmo endereço.
# O Caddy serve a página em / e repassa /api/* para o motor, que escuta só em 127.0.0.1:8081.
# ATENÇÃO: não há autenticação. Quem tiver a URL pode usar o scraper.
FROM caddy:2.8.4 AS caddy

FROM gosom/google-maps-scraper:v1.18.1
COPY --from=caddy /usr/bin/caddy /usr/bin/caddy
COPY Caddyfile /etc/caddy/Caddyfile
COPY web/ /srv/
COPY start.sh /start.sh
EXPOSE 8080
ENTRYPOINT ["/bin/sh", "/start.sh"]
