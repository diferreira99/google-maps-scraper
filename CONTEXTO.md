# Contexto do projeto: Prospecta+ e API de leads

Documento de passagem: como tudo está montado, o que foi feito e por quê, e o que falta. Serve para
retomar o trabalho numa sessão nova (ou com outra pessoa) sem depender da conversa original.
Atualizado em 14/10/2026.

**Senhas e chaves não ficam aqui.** O documento diz só onde cada uma está guardada.

---

## 1. Visão geral

```
                 ┌───────────────────────────── Vercel ─────────────────────────────┐
 usuário ──────▶ │  Prospecta+ (SaaS, um index.html)   prospeera.vercel.app        │
                 └───────┬───────────────┬──────────────────────┬──────────────────┘
                         │ login         │ busca de leads       │ campanhas, status, filas
                         ▼               ▼                      ▼
                   Supabase        API de leads (Railway)     n8n (Railway)
                   (auth + dados)  google-maps-scraper        primary-production-c1c7c
                                   ├─ /leads  (api.py)          │
                                   ├─ /enrich (api.py)          ▼
                                   └─ motor gosom (Chromium)  prospecta-whatsapp (Railway, Baileys)
                                                                 └─ seu número de WhatsApp
```

- **Busca de leads:** o SaaS deixa escolher a fonte. **API oficial** (nossa, Google Maps direto) é o padrão;
  **Backup (Serper)** é o fluxo antigo do n8n.
- **Campanhas:** o SaaS manda os leads para o n8n, que gera a mensagem com IA e envia pelo
  `prospecta-whatsapp`, com 60–120 s entre mensagens e pausa de 30 min a cada 10.

## 2. Onde está cada coisa

| Peça | Onde | Detalhes |
|---|---|---|
| SaaS Prospecta+ | Vercel, projeto `prospeera` (`prj_k6BdXrHE2XN3z6W7LUNLYJa3vmNW`) | Só `prospeera.vercel.app`. Publicado à mão (sem Git). Ver seção 6 |
| API de leads + motor | Railway, projeto `beautiful-vibrancy`, serviço `google-maps-scraper` | Repositório `diferreira99/google-maps-scraper` (branch `main`, deploy automático). URL `https://google-maps-scraper-production-3f90.up.railway.app`. Volume `gmaps-data` em `/gmapsdata` |
| n8n | Railway, projeto `disciplined-rebirth` (Primary, Worker, Postgres, Redis) | `https://primary-production-c1c7c.up.railway.app` |
| Servidor WhatsApp | Railway, projeto `intelligent-celebration`, serviço `prospecta-whatsapp` | Repositório `diferreira99/prospecta-whatsapp`. **Sem volume** (ver pendências) |
| Banco e login | Supabase `wktvwbmjubiqtbrvnapj` | Tabelas usadas: `user_data`, `status_campanha`, `respostas_historico`, `lid_mapping` e outras |
| Cópia antiga do scraper | `diferreira99/diferreira99`, branch `claude/google-maps-scraper-setup-cgnn1u`, pasta `google-maps-scraper/` | Primeira versão, não é mais usada |

Há outros dois projetos no Railway com um `prospecta-whatsapp` (`diligent-commitment`) e `delightful-appreciation`
(Igor). O que está em uso é o do `intelligent-celebration`.

### Variáveis e segredos (onde ficam)

- **Serviço `google-maps-scraper` (Railway):** `PORT`, `ADMIN_USER`, `ADMIN_PASS` (senha da página do scraper),
  `AUTH_MODO`, `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `WHATSAPP_CHECK_URL`, `WHATSAPP_CHECK_TOKEN`
  (= `API_TOKEN` do prospecta-whatsapp), `WHATSAPP_CHECK_DELAY_S`. Opcional: `API_TOKENS` (tokens de servidor).
- **Serviço `prospecta-whatsapp` (Railway):** `API_TOKEN`, `N8N_WEBHOOK_RESPOSTA`, `PROSPECTA_USER_ID`, `SUPABASE_SERVICE_KEY`,
  `AUTH_DIR=/data/auth_info` (sessão do WhatsApp no volume `whatsapp-sessao`, montado em `/data`).
- **n8n:** a chave `service_role` do Supabase e a chave da Serper estão **escritas dentro do código dos nós**
  (pendência: mover para credenciais).
- **SaaS:** só a chave pública (`anon`) do Supabase, que pode ficar no navegador. Tokens do WhatsApp e das IAs
  ficam na configuração de cada usuário (salva no Supabase).

## 3. API de leads (repositório `google-maps-scraper`)

Arquivos principais: `api.py` (API e limpeza), `scrape.py` (funções de site/e-mail/redes, CLI local),
`web/index.html` (página do scraper), `Caddyfile` e `start.sh` (roteamento e senha), `Dockerfile`.
Documentação completa das rotas no `README.md`.

- **Rotas:** `POST /leads/buscas` (criar), `GET /leads/buscas` (listar), `GET /leads/buscas/<id>` (leads limpos e
  andamento), `DELETE /leads/buscas/<id>`, `POST /leads/buscas/<id>/refazer`, `POST /enrich` (redes sociais por site).
- **Limpeza:** tira duplicadas e junta cadastros repetidos (mesmo telefone e endereço); telefone em E.164 com
  flag de celular; WhatsApp só para celular, link de WhatsApp do Google, do site ou de agendamento; tira `utm`;
  marca resultados fora do tema (`relevante`).
- **Campos de cada lead:** nome, categoria, telefone, `telefone_e164`, celular, whatsapp, `whatsapp_origem`,
  `whatsapp_confirmado`, site, instagram, facebook, linkedin, endereço, bairro, CEP, horário (texto e por dia),
  nota, avaliações, `avaliacoes_negativas`, `ultimas_avaliacoes` (até 5, sem nome de quem avaliou),
  google_maps, place_id, relevante. **Sem e-mail** (decisão do Diego: ninguém usa, e deixava a busca 3x mais lenta).
- **Busca em grade (opcional):** `"grade": 3` divide a cidade em 9 regiões; aparece como uma busca só.
- **Cache:** busca igual (mesmos termos e cidade) feita nos últimos 7 dias volta na hora; busca igual ainda
  rodando é reaproveitada ("carona"). `forcar_nova: true` ignora o cache.
- **Confirmação de WhatsApp:** `?verificar_whatsapp=1` consulta cada número no `prospecta-whatsapp`
  (`/check-number`), em segundo plano, 2 s entre consultas, com cache por número.
- **Limites práticos:** o motor faz **uma busca por vez** (as outras ficam na fila); o Google mostra ~120 empresas
  por termo; sem e-mail uma busca leva 3–6 min. Recomendação: até ~4–5 buscas por hora; o IP é de datacenter.
- **Cuidado ao publicar:** todo deploy reinicia o motor e **interrompe a busca em andamento**. Antes de dar
  push, conferir nos logs do Railway se há `running job` sem `scraped successfully` depois, e se há consultas
  `/check-number` recentes no `prospecta-whatsapp`.

## 4. SaaS Prospecta+ (o que mudou nesta fase)

Arquivo: `index.html` (pasta `prospecta-saas`). Mudanças feitas sobre a versão "v2-config-nuvem":

1. **Escolha da fonte de leads:** "⚡ API oficial" (padrão) e "🛟 Backup (Serper)". A escolha fica salva por usuário.
2. **Painel "Minerando no Google Maps"** com mapinha animado, etapas e tempo; **leads aparecem na tabela
   enquanto minera**; se a página recarregar, retoma a busca.
3. **Conversão para o formato antigo** (o mesmo da Serper): score, CRM, campanhas e filtros não mudaram. Quando o
   WhatsApp veio do site ou do agendamento e o telefone do Maps é fixo, o telefone do lead vira o WhatsApp.
   O score "aberto agora" passou a funcionar (a Serper não mandava horário).
4. **Aviso de busca pronta:** mensagem na tela, som, vibração (Android), título da aba piscando e notificação do
   sistema (computador e Android; iPhone só com o app instalado na tela de início).
5. **Cache:** busca reaproveitada mostra "busca igual de hoje, 08:32 reaproveitada · Buscar de novo no Google Maps".
6. **Login em todas as chamadas:** um único trecho logo depois de `createClient` envolve o `fetch` e anexa o token
   do Supabase: `Authorization: Bearer` na API oficial e `access_token` (corpo ou URL) nos webhooks do n8n.

Erro conhecido que **já existia** antes destas mudanças: `carregarDoSupabase is not defined` quando a sessão
responde instantaneamente (só aparece em teste).

## 5. Segurança

**Feito (modo obrigatório desde 14/10/2026, à noite):**
- n8n: nó "Verificar Login" depois dos webhooks `iniciar-campanha`, `cancelar-campanha`, `status-campanha`,
  `responder-fila`, `responder-followup` (fluxo "My workflow", `WNS7NFoQb3y4BKac`) e `extrair-leads-serp`
  (fluxo `j8YtObCntdTeb4kf`). Confere o token em `/auth/v1/user` e se o e-mail/userId do pedido é do dono do token.
- Scraper: página e motor (`/api/*`) atrás de usuário e senha; `/leads` e `/enrich` exigem login do Prospecta+,
  token de servidor ou a senha de admin.
- **Modo obrigatório** = chamada sem login é bloqueada ("Nao autorizado: faca login de novo"). Railway:
  `AUTH_MODO=obrigatorio`; n8n: `const MODO = 'obrigatorio'` nos 6 nós. Para voltar à transição (emergência),
  desfazer os dois e publicar.
- O webhook `receber-resposta` (chamado pelo servidor do WhatsApp) **não** tem login; pendência: segredo compartilhado.

## 6. Vercel: particularidades do projeto `prospeera`

- Publicado por **upload manual** (`source: drop`), sem Git, sem framework, sem variáveis de ambiente.
- Arquivos no ar: `index.html`, `0-PROSPECTA-MAIS-sem-ANA.html` (cópia idêntica do index) e
  `vercel.json` = `{"cleanUrls": true, "trailingSlash": false}`. Com `cleanUrls`, a cópia responde em
  `/0-PROSPECTA-MAIS-sem-ANA` (links antigos podem usar esse endereço).
- Domínio: só `prospeera.vercel.app`. O código usa `https://app.prospeera.com.br/adm` (`BASE_ADM`), que **não está
  ligado a este projeto**; conferir onde esse domínio aponta antes de mudar algo.
- O `index.html` no ar (publicado em 07/10/2026 20:07) é **diferente** do arquivo "v2-config-nuvem" que serviu de base
  para estas mudanças. Antes de trocar, confirmar que a v2 é mesmo a versão mais nova.

## 7. Pendências (em ordem)

1. ~~Subir o SaaS novo, modo obrigatório, repositório e Vercel~~ (feito: `diferreira99/prospecta-saas`,
   `prospecta-saas-seven.vercel.app`, deploy automático a cada push na `main`).
2. ~~Volume do `prospecta-whatsapp`~~ (feito em 14/10). Falta proteger o webhook `receber-resposta` com um segredo
   compartilhado (o servidor do WhatsApp manda, o n8n confere).
3. **Mensagem da campanha:** deixar claro o que o produto faz (nome, canal, para quem, preço/teste) no prompt.
4. **Mover chaves escritas no código do n8n** (Supabase `service_role`, Serper) para credenciais do n8n.
5. **Revisar os fluxos da Ana** (ligações, agenda, avisos), que não passaram por esta revisão.
6. Ideias que ficaram para depois: posição na fila ("2º na fila"), vários termos separados por vírgula no campo
   Nicho, limite de buscas por usuário (para revendedores), proxies se o Google começar a bloquear.

## 8. Decisões tomadas (e por quê)

- **Sem e-mail** nos leads: pouco uso e muito tempo de busca.
- **Página do scraper com senha**, apesar de antes ter sido pedido "sem senha": o motor aberto deixava qualquer
  pessoa criar buscas e usar o WhatsApp de confirmação.
- **Um termo por busca no SaaS** (visual mais simples); o servidor continua aceitando vários.
- **Segurança em duas etapas** (transição → obrigatório) para não derrubar o SaaS em produção.
- **Cache compartilhado entre usuários**: menos fila e menos acessos ao Google quando houver revendedores.

## 9. Trabalho de 13–14/10/2026 (resumo)

- **API:** enriquecimento de redes em segundo plano (`?enriquecer=fundo`) e recuperação automática de buscas
  interrompidas por reinício (só das últimas 3 horas; ids antigos viram apelido do novo em `reiniciadas.json`).
- **SaaS:** botões de Instagram/Facebook/LinkedIn com logo, contagem correta do "Verificar WhatsApp", score com
  celular e avaliações recentes, vários termos no Nicho, aviso de "já contatado", alerta de lead interessado,
  janela recomendada (seg–sex 7h–12h), celular primeiro na campanha, follow-up padrão no dia 3 (passa pela fila
  de aprovação), CRM com Ganho/Perdido e valor (R$).
- **n8n:** IA cita elogios das avaliações; não manda para quem já recebeu em outra campanha (`status_campanha`);
  A/B que aprende (tom × gatilho, 80% vencedor depois de 60 envios); respostas de robô viram sugestão de opção do
  menu; contatos fora de campanha são ignorados.
- **Supabase:** gatilho `trg_preencher_resposta_lead` em `leads` (marca `respondeu` a partir do histórico). Foi
  criado por bloco `DO`, **fora da tabela de migrações** — recriar à mão se o banco for refeito. Colunas
  `pipeline_leads.valor` e `fechado_em`.

