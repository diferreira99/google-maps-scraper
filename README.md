# Google Maps Scraper

Extrai listas de empresas do Google Maps (nome, categoria, telefone, WhatsApp, avaliação, site, redes
sociais e endereço) para prospecção. Adaptado do
[google-maps-scraper-kit](https://github.com/Mahanaicoach/google-maps-scraper-kit) (MIT, ver `LICENSE`),
que usa o motor open-source [gosom/google-maps-scraper](https://github.com/gosom/google-maps-scraper).

Este guia reúne o que a skill do Claude Code do kit ensinava, para usar sem a skill.

## Página "Buscar leads"

O `Dockerfile` sobe o motor e uma página em português (`web/index.html`) no mesmo endereço. Nela você
digita o tipo de negócio e a cidade, acompanha as buscas e baixa a planilha já limpa (separada por `;`,
abre direto no Excel) com Empresa, Categoria, Telefone, WhatsApp, Avaliação, Site, Redes sociais e Endereço. As
buscas não coletam e-mail, o que as deixa bem mais rápidas (3 a 6 minutos). O botão **Buscar redes
sociais** visita a página inicial do site de cada empresa (pelo `api.py`, no servidor) e acrescenta
Instagram, Facebook, LinkedIn e o botão de WhatsApp do site. Só funciona para empresas com site que divulgam essas redes
(em geral de 40 a 70% delas). O Caddy serve a página em `/` e repassa
`/api/*` para o motor, e `/leads` e `/enrich` para o `api.py`, que só acessa endereços públicos. A interface original do motor deixa de ficar acessível.

## Acesso e segurança

- **Página do scraper e motor (`/api/*`):** pedem usuário e senha (`ADMIN_USER` e `ADMIN_PASS` nas
  variáveis do serviço no Railway).
- **API de leads (`/leads`, `/enrich`):** aceita o login de um usuário do Prospecta+ (token do Supabase em
  `Authorization: Bearer <token>`), um token de servidor (lista em `API_TOKENS`, para o n8n) ou a senha
  de admin. Precisa de `SUPABASE_URL` e `SUPABASE_ANON_KEY`.
- **`AUTH_MODO`:** `transicao` aceita chamadas sem login (para versões antigas do app), mas recusa login
  errado; `obrigatorio` exige login; `desligado` não confere nada.

## API de leads limpos (para o Prospecta, n8n etc.)

Endereço base: `https://google-maps-scraper-production-3f90.up.railway.app`. Exige login (veja acima).

**Criar busca:** `POST /leads/buscas`

```json
{ "busca": "contabilidade", "cidade": "São José dos Campos, SP" }
```

Opcionais: `"buscas": ["contabilidade", "assessoria contábil"]` no lugar de `busca`,
`"profundidade": 5`, `"tempo_limite_min": 20`, `"raio_m": 10000`, `"proxies": [...]`.
Resposta: `{"id": "...", "status": "na_fila", ...}`. O servidor roda uma busca por vez.

**Reaproveitamento (cache):** se já existe uma busca igual (mesmos termos e cidade, e mesma grade),
a API devolve essa em vez de ir ao Google de novo: na hora, se ela terminou com resultados nos últimos
7 dias (`"reaproveitada": true`, `"idade_dias": 1.5`), ou a própria busca, se ela ainda está na fila ou
rodando (`"em_andamento": true`), sem duplicar na fila. Vale para todo mundo que usa a API. Para mudar o
prazo, mande `"reusar_dias": 3` (0 desliga); para forçar uma busca nova, `"forcar_nova": true`.

**Cidade inteira (opcional):** `"grade": 3` divide a cidade em 3 x 3 = 9 regiões e busca cada uma
(o Google mostra no máximo uns 120 resultados por busca, então uma cidade grande precisa de várias).
Vira uma busca só na API: o `status` só fica `pronta` quando todas as regiões terminam, e
`regioes`/`regioes_prontas`/`regioes_com_erro` mostram o andamento. Os resultados juntam as regiões,
tiram as repetidas e descartam o que cai fora dos limites da cidade. Leva cerca de 9 vezes mais tempo
e faz 9 vezes mais acessos ao Google; `"grade"` aceita de 2 a 5.

**Acompanhar e pegar os leads:** `GET /leads/buscas/<id>`

`status` passa por `na_fila`, `buscando` e `pronta` (ou `falhou`). Os leads já aparecem durante a busca
(parciais). Filtros: `?celular=1` (só celulares), `?whatsapp=1` (só com WhatsApp), `?relevantes=0` (inclui
resultados fora do tema), `?enriquecer=1` (visita os sites e acrescenta Instagram, Facebook, LinkedIn e
o WhatsApp do site; leva de 1 a 2 minutos na primeira vez e fica guardado depois).

Cada lead:

```json
{
  "nome": "Ação Contábil", "categoria": "Escritório de contabilidade",
  "telefone": "+55 12 99114-2186", "telefone_e164": "+5512991142186", "celular": true,
  "whatsapp": "https://wa.me/5512991142186", "whatsapp_origem": "celular", "whatsapp_confirmado": true,
  "site": "https://acaocontabilsjc.com.br/", "instagram": "", "facebook": "", "linkedin": "",
  "endereco": "Av. Dr. Ademar de Barros, 566 - Sl 1409 - Jardim São Dimas, ...", "bairro": "Jardim São Dimas",
  "cep": "12245-050", "horario": "seg a qui 08:00–17:30 · sex 08:00–17:00 · sáb e dom fechado",
  "nota": 5.0, "avaliacoes": 37, "avaliacoes_negativas": 0,
  "ultimas_avaliacoes": [{"nota": 5, "data": "2026-05-14", "quando": "4 meses atrás", "texto": "Atendimento impecável..."}],
  "google_maps": "https://www.google.com/maps/place/...", "place_id": "ChIJ...", "relevante": true
}
```

`avaliacoes_negativas` conta as notas 1 e 2. `ultimas_avaliacoes` traz até 5 das avaliações que o Google
mostra, das mais recentes para as mais antigas, sem o nome de quem avaliou.

**WhatsApp:** `whatsapp` vem preenchido para celulares, para links de WhatsApp que o Google tinha no
lugar do site ou nos links de agendamento/pedido online e, com `?enriquecer=1`, para o botão de WhatsApp encontrado no site da empresa
(`whatsapp_origem` diz de onde veio). Com `?verificar_whatsapp=1`, a confirmação começa em segundo plano: cada número é consultado no
servidor `prospecta-whatsapp` (`/check-number`), um por vez, com 2 segundos entre consultas. A resposta
volta na hora, e o campo `verificacao` (`{"feitos": 12, "total": 57, "rodando": true, "erro": null}`)
mostra o andamento; consulte `GET /leads/buscas/<id>` de novo até `rodando` virar `false`. No fim,
`whatsapp_confirmado` vira `true` ou `false`, e telefones fixos com WhatsApp Business ganham o link.
O resultado fica guardado, então cada número só é consultado uma vez. `?whatsapp=1` filtra quem tem
WhatsApp e não foi descartado na confirmação. Precisa das variáveis `WHATSAPP_CHECK_URL` e
`WHATSAPP_CHECK_TOKEN` (o `API_TOKEN` do `prospecta-whatsapp`).

**Outras rotas:** `GET /leads/buscas` lista as buscas, `DELETE /leads/buscas/<id>` apaga uma (numa
grade, apaga todas as regiões) e `POST /leads/buscas/<id>/refazer` coloca a mesma busca na fila de novo
e apaga a antiga. O `status` `interrompida` marca uma busca cortada por um reinício do servidor.

**O que a limpeza faz:**
- tira duplicadas e junta dois cadastros da mesma empresa (mesmo telefone no mesmo endereço) num só
  lead, guardando o outro nome em `outros_nomes`;
- normaliza o telefone e marca se é celular; sem confirmação, o WhatsApp só é preenchido para celular;
- quando o "site" do Google é um link de WhatsApp ou de rede social, move para o campo certo;
- remove rastreamento (`utm_*`) do site e o nome da empresa repetido no início do endereço;
- marca como `relevante: false` o que não tem a ver com a busca (por exemplo, empresa de software
  numa busca de contabilidade).

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
