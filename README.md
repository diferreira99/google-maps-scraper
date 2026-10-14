# Google Maps Scraper

Extrai listas de empresas do Google Maps (nome, telefone, e-mail, site, categoria, endereço, nota e
número de avaliações) para prospecção. Adaptado do
[google-maps-scraper-kit](https://github.com/Mahanaicoach/google-maps-scraper-kit) (MIT, ver `LICENSE`),
que usa o motor open-source [gosom/google-maps-scraper](https://github.com/gosom/google-maps-scraper).

Este guia reúne o que a skill do Claude Code do kit ensinava, para usar sem a skill.

## Página "Buscar leads"

O `Dockerfile` sobe o motor e uma página em português (`web/index.html`) no mesmo endereço. Nela você
digita o tipo de negócio e a cidade, acompanha as buscas e baixa a planilha já limpa (separada por `;`,
abre direto no Excel), com link de WhatsApp para celulares. O Caddy serve a página em `/` e repassa
`/api/*` para o motor. A interface original do motor deixa de ficar acessível.

## Como funciona

1. O **motor** roda num container Docker e expõe uma API em `http://localhost:8080`. Ele abre o
   Google Maps num Chromium e faz a raspagem.
2. O **`scrape.py`** (só Python 3, sem instalar nada) cria um job na API, espera terminar, baixa o CSV,
   mantém só os 8 campos úteis e procura e-mails nos sites das empresas.

## Rodar no seu computador

```bash
docker compose up -d --build                  # sobe o motor + página (1ª vez baixa a imagem)
curl http://localhost:8080/api/v1/jobs        # deve responder [] ou uma lista

python3 scrape.py "dentistas em Belo Horizonte MG" --city "Belo Horizonte, MG" --depth 5
```

No Windows use `py scrape.py ...`. Página de busca: http://localhost:8080.

### Opções do `scrape.py`

| Opção | O que faz |
|---|---|
| `--city "Cidade, UF"` | Descobre as coordenadas sozinho (OpenStreetMap). Ou passe `lat lon` depois da busca |
| `--depth 5` | Quanto rolar a lista. Mais alto = mais resultados, mais lento, mais chance de bloqueio |
| `--grid 3` | Divide a cidade em 3x3 (9 buscas, uma por vez) para passar do limite de ~120 resultados |
| `--keywords-file buscas.txt` | Várias buscas num job só (ver `buscas.exemplo.txt`) |
| `--socials` | Acrescenta Instagram, Facebook e LinkedIn achados no site de cada empresa |
| `--no-email` | Pula a busca de e-mails (mais rápido) |
| `--lang pt` | Idioma dos resultados, 2 letras (padrão `pt`; o motor recusa `pt-BR`) |
| `--out arquivo.csv` / `--json` | Nome e formato da saída. Padrão: `results-<id>.csv` |
| `--full` / `--fields a,b,c` | Todas as ~34 colunas, ou as colunas que você escolher |

Para usar outra porta, defina `SCRAPER_PORT` no `.env` (copie de `.env.example`). Para apontar para um
motor em outro lugar, como o Railway, use `SCRAPER_BASE_URL`.

## Usar a API direto (n8n, Prospecta, curl)

É um job assíncrono em 3 passos.

**1. Criar o job:** `POST /api/v1/jobs`

```bash
curl -s -X POST http://localhost:8080/api/v1/jobs -H "Content-Type: application/json" -d '{
  "name": "teste",
  "keywords": ["dentistas em Belo Horizonte MG"],
  "lat": "-19.9167", "lon": "-43.9345",
  "max_time": 600,
  "depth": 5, "lang": "pt", "zoom": 15, "radius": 10000,
  "email": true, "fast_mode": false
}'
# → {"id":"<uuid>"}
```

Campos **obrigatórios**. Sem eles a API devolve `422`:
- `keywords`: lista de buscas, **com a cidade dentro do texto** ("dentistas em BH MG");
- `lat` e `lon`: **como texto** (`"-19.9167"`), não número;
- `max_time`: limite do job em **segundos**.

Opcional: `"proxies": ["socks5://user:senha@host:porta", "http://host:porta"]`. O motor faz o rodízio
entre eles.

**2. Esperar:** `GET /api/v1/jobs/<id>` até o campo `Status` (com S maiúsculo) virar `ok` ou `failed`.
Consulte a cada 10 a 15 s.

**3. Baixar:** `GET /api/v1/jobs/<id>/download` devolve um CSV com 34 colunas. As úteis para
prospecção são `title, phone, emails, website, category, address, review_rating, review_count`.
Para tirar duplicadas, use `place_id`.

Outros endpoints: `GET /api/v1/jobs` lista os jobs, `DELETE /api/v1/jobs/<id>` apaga um job (os
resultados ocupam disco) e a documentação completa fica em `/api/docs`.

> Pela API direta você recebe só os e-mails que o próprio motor achou. A segunda passada nas páginas
> de contato e a busca de redes sociais são feitas pelo `scrape.py`.

## Rodar no Railway

O Railway não usa `docker-compose.yml`. Use o `Dockerfile` desta pasta:

1. Crie um serviço a partir deste repositório (GitHub Repo). O Railway encontra o `Dockerfile` na raiz.
2. Variável: `PORT=8080`.
3. Adicione um **volume** montado em `/gmapsdata`.
4. Plano com pelo menos 1 a 2 GB de RAM, porque o motor roda um Chromium.
5. **Não gere domínio público.** O motor não tem senha, então qualquer pessoa usaria o seu scraper.
   Coloque no mesmo projeto do serviço que vai chamá-lo e use a rede privada:
   `http://<nome-do-servico>.railway.internal:8080`.

O Google bloqueia IPs de datacenter, como os do Railway, mais rápido que IPs residenciais. Se os jobs
começarem a falhar ou voltar vazios, use `proxies`.

## Boas práticas e problemas comuns

- Comece com `depth 5` e **um job por vez**. Muitas buscas seguidas podem bloquear o IP por minutos
  ou horas. Isso não afeta a sua conta Google.
- Sinais de bloqueio: job `failed`, CSV vazio ou bem menor que o normal. Nesse caso, espere, baixe o
  depth ou use proxies.
- `422 missing max time`: falta `max_time`. `422 missing geo coordinates`: falta `lat`/`lon` em texto.
- Job preso em `working`: baixe o `depth`, aumente o `max_time` ou espere (pode ser bloqueio).
- CSV vazio: busca muito específica ou coordenadas erradas. Aumente o `radius`.
- **Não commite os CSVs**, porque têm dados pessoais (o `.gitignore` já ignora).

## Aspectos legais

Raspar o Google Maps viola os Termos de Serviço do Google. Telefones e e-mails são dados pessoais:
para prospecção, respeite a **LGPD** (base legal, opção de descadastro, não usar para spam). Trate os
dados como leads a verificar e não revenda os dados brutos.
