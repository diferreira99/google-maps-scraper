# Para o Railway (ou qualquer host que compila um Dockerfile em vez de usar docker-compose).
# Mesma imagem e argumentos do docker-compose.yml. O motor escuta na porta 8080.
# ATENÇÃO: o motor não tem autenticação. No Railway, NÃO gere domínio público;
# acesse pela rede privada: http://<nome-do-servico>.railway.internal:8080
FROM gosom/google-maps-scraper:v1.18.1
EXPOSE 8080
CMD ["-web", "-data-folder", "/gmapsdata"]
