#!/usr/bin/env python3
"""Clean leads API on top of the scraper engine, plus the socials lookup the search page uses.

Clean leads (for Prospecta, n8n or any other integration):
  POST   /leads/buscas          {"busca": "contabilidade", "cidade": "São José dos Campos, SP", ...}
  GET    /leads/buscas          list searches and their status
  GET    /leads/buscas/{id}     status + cleaned leads (JSON). Filters: ?celular=1 ?whatsapp=1
                                ?relevantes=0 (include off-topic results) ?enriquecer=1 (visit sites
                                for socials and WhatsApp buttons; cached on disk after the first call)
  DELETE /leads/buscas/{id}

Socials lookup used by the page:
  POST   /enrich                {"sites": ["https://example.com", ...]}   (max 20 per request)

Every endpoint is reachable from the public URL with no auth, so site visits only go to public
http(s) hosts: every hop (including redirects) must resolve to a public IP, and only the extracted
links are returned, never page content.

Run: python3 api.py [--addr 127.0.0.1] [--port 8082]
WhatsApp check (optional): ?verificar_whatsapp=1 on GET /leads/buscas/{id} starts asking the
Prospecta WhatsApp server (/check-number) about each lead's number, one at a time, in the
background; the response's "verificacao" field shows progress until "rodando" is false.

Env: ENGINE_URL (default http://127.0.0.1:8081), CACHE_DIR (default /gmapsdata/leads-cache),
     WHATSAPP_CHECK_URL + WHATSAPP_CHECK_TOKEN (Prospecta server and its API_TOKEN),
     WHATSAPP_CHECK_DELAY_S (pause between checks, default 2)
"""
import argparse, csv, io, ipaddress, json, os, re, socket, sys, threading, time, unicodedata, uuid
from datetime import datetime, timezone
import urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scrape  # noqa: E402  (lives next to this file)

MAX_SITES = 20
MAX_BODY = 64_000
ENGINE = os.environ.get("ENGINE_URL", "http://127.0.0.1:8081").rstrip("/")
CACHE_DIR = os.environ.get("CACHE_DIR", "/gmapsdata/leads-cache")
WA_CHECK_URL = os.environ.get("WHATSAPP_CHECK_URL", "").rstrip("/")
WA_CHECK_TOKEN = os.environ.get("WHATSAPP_CHECK_TOKEN", "")
WA_CHECK_DELAY = float(os.environ.get("WHATSAPP_CHECK_DELAY_S", "2"))
STATUS_PT = {"pending": "na_fila", "working": "buscando", "ok": "pronta", "failed": "falhou"}
csv.field_size_limit(10**9)


# ── Public-only fetching ─────────────────────────────────────────────────────
def is_public_url(url):
    """True if url is http(s) and every address its host resolves to is a public IP."""
    try:
        u = urllib.parse.urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            return False
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80))
    except (ValueError, OSError):
        return False
    return bool(infos) and all(ipaddress.ip_address(i[4][0].split("%")[0]).is_global for i in infos)


class PublicOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_public_url(newurl):
            raise urllib.error.URLError("redirect to a non-public address")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(PublicOnlyRedirect)


def safe_fetch_html(url, timeout=10):
    """Drop-in for scrape._fetch_html that refuses non-public hosts."""
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    if not is_public_url(url):
        return ""
    try:
        r = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; " + scrape.UA + ")", "Accept": "text/html"})
        with _opener.open(r, timeout=timeout) as resp:
            return resp.read(400_000).decode("utf-8", "replace")
    except Exception:
        return ""


# Route every fetch in scrape.py (homepage cache and contact pages) through the safe version.
scrape._fetch_html = safe_fetch_html


# WhatsApp buttons on business sites: wa.me/5512..., api.whatsapp.com/send?phone=..., whatsapp://send?phone=...
WA_LINK_RE = re.compile(r"(?:wa\.me/|whatsapp\.com/send/?\?(?:[^\"'\s<>]*?&(?:amp;)?)?phone=|whatsapp://send/?\?phone=)"
                        r"(?:%2B|\+)?([\d\s().-]{10,20})", re.I)


def find_whatsapp(html):
    """First Brazilian-looking WhatsApp number linked on the page, as E.164 digits, or ''."""
    for m in WA_LINK_RE.finditer(html or ""):
        digits, _ = br_phone(m.group(1))
        if digits:
            return digits
    return ""


def enrich_site(site):
    home = scrape._fetch_cached(site)
    out = scrape._find_socials(home)
    out["whatsapp"] = find_whatsapp(home)
    return out


# ── Cleaning ─────────────────────────────────────────────────────────────────
def br_phone(raw):
    """-> (e164 digits like '5512978113132' or '', is_mobile). Brazilian numbers only get the
    mobile flag: 55 + 2-digit area code + 9 + 8 digits."""
    d = re.sub(r"\D", "", raw or "")
    if re.match(r"0?[3589]00", d):   # 0800/0300/0500/0900: national service numbers, no WhatsApp
        return "", False
    d = d.lstrip("0")
    if len(d) in (10, 11):
        d = "55" + d
    if not 10 <= len(d) <= 15:
        return "", False
    return d, d.startswith("55") and len(d) == 13 and d[4] == "9"


_WA_HOSTS = ("wa.me", "api.whatsapp.com", "whatsapp.com", "web.whatsapp.com")
_SOCIAL_HOSTS = {"instagram": ("instagram.com",), "facebook": ("facebook.com", "fb.com"),
                 "linkedin": ("linkedin.com",)}


def clean_site(raw):
    """-> (site, extras). Strips tracking params; moves WhatsApp and social links out of the
    site field (Google Maps often has them as the 'website')."""
    extras = {}
    if not raw:
        return "", extras
    url = raw.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    u = urllib.parse.urlparse(url)
    host = (u.hostname or "").lower().removeprefix("www.")
    if any(host == h or host.endswith("." + h) for h in _WA_HOSTS):
        phone = urllib.parse.parse_qs(u.query).get("phone", [""])[0] or u.path.strip("/")
        if re.fullmatch(r"\+?\d{10,15}", phone):
            extras["whatsapp_numero"] = re.sub(r"\D", "", phone)
        else:
            extras["whatsapp_link"] = url
        return "", extras
    for key, hosts in _SOCIAL_HOSTS.items():
        if any(host == h or host.endswith("." + h) for h in hosts):
            extras[key] = url
            return "", extras
    query = [(k, v) for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True)
             if not k.lower().startswith(("utm_", "gclid", "fbclid"))]
    return urllib.parse.urlunparse(u._replace(query=urllib.parse.urlencode(query), fragment="")), extras


_STOP = {"para", "com", "sem", "dos", "das", "nos", "nas", "por", "uma", "loja", "lojas",
         "empresa", "empresas", "servico", "servicos"}
# Words too generic to say what a business does ("Clínica odontológica" is about "odontológica").
_GENERIC = _STOP | {"clinica", "clinicas", "centro", "escritorio", "consultorio", "assessoria",
                    "agencia", "estudio", "studio", "instituto", "casa", "grupo", "rede", "loja",
                    "comercio", "especialista", "profissional", "certificado", "certificada"}


def _fold(s):
    return unicodedata.normalize("NFD", (s or "").lower()).encode("ascii", "ignore").decode()


def term_stems(keywords):
    """Stems of the business words in each search term ('contabilidade em SJC' -> {'contabi'})."""
    stems = set()
    for k in keywords or []:
        what = re.split(r"\s+(?:em|in|no|na|perto de)\s+", _fold(k), maxsplit=1)[0]
        stems |= _word_stems(what, _STOP)
    return stems


def _word_stems(text, skip):
    return {w if len(w) < 5 else w[:max(4, int(len(w) * 0.5))]
            for w in re.findall(r"[a-z]{3,}", _fold(text)) if w not in skip}


def relevance_stems(rows, keywords):
    """Search-term stems plus the stems of categories Google returned often for this search.
    Google ranks by relevance, so a category that shows up in several results is on topic
    ("Clínica odontológica" for "dentista") even when it shares no word with the query."""
    stems = term_stems(keywords)
    counts = {}
    for r in rows:
        c = (r.get("category") or "").strip()
        if c:
            counts[c] = counts.get(c, 0) + 1
    floor = max(3, len(rows) // 20)
    for c, n in counts.items():
        if n >= floor:
            stems |= _word_stems(c, _GENERIC)
    return stems


def is_relevant(row, stems):
    """A stem matches the start of a word ('dent' -> 'dentários', not 'multicontas' for 'cont');
    stems of 6+ letters also match inside a word ('odonto' -> 'ortodontista' needs 'odont...')."""
    if not stems:
        return True
    words = re.findall(r"[a-z]+", _fold(row.get("title", "") + " " + row.get("category", "")))
    return any(w.startswith(s) or (len(s) >= 6 and s in w) for s in stems for w in words)


def clean_address(address, name):
    """The engine often prefixes the address with the business name: 'Name - R. X, 10 - ...'."""
    a, n = (address or "").strip(), (name or "").strip()
    if n and a.lower().startswith(n.lower() + " - "):
        a = a[len(n) + 3:]
    return a.removesuffix(", Brasil").removesuffix(", Brazil")


def clean_rows(rows, keywords):
    rows = list(rows)
    stems, seen, out = relevance_stems(rows, keywords), set(), []
    for r in rows:
        key = r.get("place_id") or r.get("cid") or (r.get("title", "") + "|" + r.get("address", ""))
        if key in seen:
            continue
        seen.add(key)
        site, extras = clean_site(r.get("website", ""))
        e164, mobile = br_phone(r.get("phone", ""))
        wa = extras.get("whatsapp_numero") or (e164 if mobile else "")
        origin = "link_google" if extras.get("whatsapp_numero") else ("celular" if wa else "")
        try:
            rating = float(r.get("review_rating") or 0) or None
        except ValueError:
            rating = None
        try:
            reviews = int(float(r.get("review_count") or 0))
        except ValueError:
            reviews = 0
        out.append({
            "nome": (r.get("title") or "").strip(),
            "categoria": (r.get("category") or "").strip(),
            "telefone": (r.get("phone") or "").strip(),
            "telefone_e164": "+" + e164 if e164 else "",
            "celular": mobile,
            "whatsapp": "https://wa.me/" + wa if wa else extras.get("whatsapp_link", ""),
            "whatsapp_origem": origin if wa else ("link_google" if extras.get("whatsapp_link") else ""),
            "whatsapp_confirmado": None,
            "site": site,
            "instagram": extras.get("instagram", ""),
            "facebook": extras.get("facebook", ""),
            "linkedin": extras.get("linkedin", ""),
            "endereco": clean_address(r.get("address"), r.get("title")),
            "nota": rating,
            "avaliacoes": reviews,
            "google_maps": r.get("link", ""),
            "place_id": r.get("place_id", ""),
            "relevante": is_relevant(r, stems),
        })
    return merge_same_business(out)


def _street(address):
    """'R. Gladiolo, 85 - Monte Castelo, ...' -> 'r gladiolo 85' (street and number only)."""
    return " ".join(re.findall(r"[a-z0-9]+", _fold(address.split(" - ")[0])))


def merge_same_business(leads):
    """Google often has two listings for one business (the firm and the accountant's own name).
    Same phone at the same street address is the same contact: keep the listing with more reviews
    and fill its blanks from the other, so a campaign does not message the same person twice."""
    kept, by_key = [], {}
    for lead in leads:
        key = (lead["telefone_e164"], _street(lead["endereco"])) if lead["telefone_e164"] and lead["endereco"] else None
        other = by_key.get(key) if key else None
        if other is None:
            if key:
                by_key[key] = lead
            kept.append(lead)
            continue
        main, extra = (other, lead) if other["avaliacoes"] >= lead["avaliacoes"] else (lead, other)
        for k, v in extra.items():
            if not main.get(k) and v:
                main[k] = v
        main["relevante"] = main["relevante"] or extra["relevante"]
        main.setdefault("outros_nomes", []).append(extra["nome"])
        if main is lead:
            kept[kept.index(other)] = lead
        by_key[key] = main
    return kept


def apply_enrichment(leads, found):
    for lead in leads:
        x = found.get(lead["site"]) if lead["site"] else None
        if not x:
            continue
        for k in ("instagram", "facebook", "linkedin"):
            if x.get(k) and not lead[k]:
                lead[k] = x[k]
        if x.get("whatsapp") and not lead["whatsapp"]:
            lead["whatsapp"], lead["whatsapp_origem"] = "https://wa.me/" + x["whatsapp"], "site"


# ── WhatsApp check through the Prospecta WhatsApp server ─────────────────────
_wa_lock = threading.Lock()
WA_CACHE_FILE = "whatsapp-check.json"


def _wa_cache():
    try:
        with open(os.path.join(CACHE_DIR, WA_CACHE_FILE)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _wa_cache_save(cache):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(os.path.join(CACHE_DIR, WA_CACHE_FILE), "w") as f:
            json.dump(cache, f)
    except OSError:
        pass


def check_whatsapp(number):
    """True/False from the Prospecta server's /check-number for E.164 digits."""
    req = urllib.request.Request(WA_CHECK_URL + "/check-number", method="POST",
                                 data=json.dumps({"phone": number}).encode(),
                                 headers={"Content-Type": "application/json", "x-api-token": WA_CHECK_TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return bool(json.loads(r.read()).get("exists"))
    except urllib.error.HTTPError as e:
        if e.code == 503:
            raise EngineError(503, "o WhatsApp do Prospecta não está conectado; conecte pelo /qr e tente de novo")
        if e.code == 401:
            raise EngineError(502, "o token do Prospecta (WHATSAPP_CHECK_TOKEN) foi recusado")
        raise EngineError(502, f"o servidor do Prospecta respondeu HTTP {e.code}")
    except OSError as e:
        raise EngineError(502, f"não consegui falar com o servidor do Prospecta: {e}")


def _wa_digits(lead):
    m = re.search(r"wa\.me/(\d{10,15})", lead["whatsapp"] or "")
    return m.group(1) if m else lead["telefone_e164"].lstrip("+")


# One check run at a time (it goes through the user's own WhatsApp number). It runs in a
# background thread so callers (a phone browser, n8n) poll for progress instead of holding one
# HTTP request open for minutes.
_wa_run = {"busca": None, "feitos": 0, "total": 0, "rodando": False, "erro": None}


def _wa_worker(job_id, todo):
    cache = _wa_cache()
    try:
        for i, n in enumerate(todo):
            if i:
                time.sleep(WA_CHECK_DELAY)
            cache[n] = check_whatsapp(n)
            _wa_cache_save(cache)   # small file; saving each answer lets polls show it right away
            with _wa_lock:
                _wa_run["feitos"] = i + 1
    except EngineError as e:
        with _wa_lock:
            _wa_run["erro"] = str(e)
    except Exception as e:   # keep the error visible to the page instead of dying silently
        with _wa_lock:
            _wa_run["erro"] = f"erro interno: {e}"
    finally:
        _wa_cache_save(cache)
        with _wa_lock:
            _wa_run["rodando"] = False


def start_whatsapp_check(job_id, leads):
    """Start checking this search's unchecked numbers in the background, unless a run is going."""
    if not (WA_CHECK_URL and WA_CHECK_TOKEN):
        raise EngineError(501, "verificação de WhatsApp não configurada (WHATSAPP_CHECK_URL e WHATSAPP_CHECK_TOKEN)")
    cache = _wa_cache()
    todo = list(dict.fromkeys(n for n in map(_wa_digits, leads) if n and n not in cache))
    with _wa_lock:
        if _wa_run["rodando"]:
            if _wa_run["busca"] != job_id:
                raise EngineError(409, "já existe uma confirmação de WhatsApp em andamento em outra busca; espere terminar")
            return
        _wa_run.update(busca=job_id, feitos=0, total=len(todo), rodando=bool(todo), erro=None)
    if todo:
        threading.Thread(target=_wa_worker, args=(job_id, todo), daemon=True).start()


def whatsapp_check_status(job_id):
    with _wa_lock:
        return dict(_wa_run) if _wa_run["busca"] == job_id else None


def apply_whatsapp_cache(leads):
    """Fill whatsapp_confirmado from what was checked so far. A landline that turns out to have
    WhatsApp (WhatsApp Business) gets its wa.me link."""
    cache = _wa_cache()
    for lead in leads:
        n = _wa_digits(lead)
        if n in cache:
            lead["whatsapp_confirmado"] = cache[n]
            if cache[n] and not lead["whatsapp"]:
                lead["whatsapp"], lead["whatsapp_origem"] = "https://wa.me/" + n, "verificado"


# ── Engine access ────────────────────────────────────────────────────────────
_engine = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class EngineError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def engine(method, path, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(ENGINE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with _engine.open(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            msg = json.loads(raw).get("message") or raw.decode()
        except ValueError:
            msg = raw.decode(errors="replace")
        raise EngineError(e.code, msg.strip() or f"HTTP {e.code}")
    except OSError as e:
        raise EngineError(503, f"scraper engine unavailable: {e}")


def _parse_date(value):
    """Engine dates carry up to nanoseconds ('...27.123456789Z'); Python takes microseconds."""
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?$", value or "")
    if not m:
        return None
    frac = (m.group(2) or "0")[:6].ljust(6, "0")
    tz = m.group(3) or "Z"
    return datetime.fromisoformat(f"{m.group(1)}.{frac}{'+00:00' if tz == 'Z' else tz}")


def job_status(j):
    """Engine status in Portuguese. A job still 'working' well past its time limit was cut off by a
    server restart (the engine never picks it up again), so it is reported as 'interrompida'."""
    st = j.get("Status")
    if st == "working":
        started = _parse_date(j.get("Date"))
        limit_s = ((j.get("Data") or {}).get("max_time") or 0) / 1e9 or 1200
        if started and (datetime.now(timezone.utc) - started).total_seconds() > limit_s + 600:
            return "interrompida"
    return STATUS_PT.get(st, st)


def job_summary(j):
    d = j.get("Data") or {}
    return {"id": j.get("ID"), "nome": j.get("Name"), "status": job_status(j),
            "criada_em": j.get("Date"), "termos": d.get("keywords") or [], "profundidade": d.get("depth"),
            "tempo_limite_min": round((d.get("max_time") or 0) / 60e9) or None, "grade": None, "regioes": 1}


# ── Grid searches: one search split into N x N map regions, run as N*N engine jobs ──
# The group lives in a small JSON file on the volume; the engine only knows the member jobs.
_group_lock = threading.Lock()


def _group_dir():
    return os.path.join(CACHE_DIR, "grades")


def _group_path(gid):
    return os.path.join(_group_dir(), re.sub(r"[^A-Za-z0-9-]", "", gid) + ".json")


def load_group(gid):
    try:
        with open(_group_path(gid)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_group(g):
    with _group_lock:
        os.makedirs(_group_dir(), exist_ok=True)
        with open(_group_path(g["id"]), "w") as f:
            json.dump(g, f, ensure_ascii=False)


def list_groups():
    try:
        names = os.listdir(_group_dir())
    except OSError:
        return []
    return [g for g in (load_group(n.removesuffix(".json")) for n in names if n.endswith(".json")) if g]


def group_summary(g, jobs_by_id):
    statuses = [job_status(jobs_by_id[j]) if j in jobs_by_id else "interrompida" for j in g["jobs"]]
    done = statuses.count("pronta")
    bad = sum(s in ("falhou", "interrompida") for s in statuses)
    if done == len(statuses):
        status = "pronta"
    elif any(s in ("na_fila", "buscando") for s in statuses):
        status = "buscando" if (done or bad or "buscando" in statuses) else "na_fila"
    elif done:
        status = "pronta"   # finished, some regions failed: results cover the others
    else:
        status = "falhou" if "falhou" in statuses else "interrompida"
    return {"id": g["id"], "nome": g["nome"], "status": status, "criada_em": g["criada_em"],
            "termos": g["termos"], "profundidade": g.get("profundidade"), "tempo_limite_min": g.get("tempo_limite_min"),
            "grade": g["grade"], "regioes": len(g["jobs"]), "regioes_prontas": done, "regioes_com_erro": bad}


def _engine_jobs():
    _, raw = engine("GET", "/api/v1/jobs")
    return {j.get("ID"): j for j in (json.loads(raw) or [])}


def list_searches():
    jobs = _engine_jobs()
    groups = list_groups()
    members = {j for g in groups for j in g["jobs"]}
    out = [group_summary(g, jobs) for g in groups]
    out += [job_summary(j) for jid, j in jobs.items() if jid not in members]
    return sorted(out, key=lambda x: x.get("criada_em") or "", reverse=True)


_cache_lock = threading.Lock()


def cache_path(job_id):
    return os.path.join(CACHE_DIR, re.sub(r"[^A-Za-z0-9-]", "", job_id) + ".json")


def load_cache(job_id):
    try:
        with open(cache_path(job_id)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_cache(job_id, found):
    with _cache_lock:
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(cache_path(job_id), "w") as f:
                json.dump(found, f)
        except OSError:
            pass


def _job_rows(job_id):
    """Raw CSV rows the engine has written for a job so far ([] if nothing yet)."""
    try:
        _, csv_raw = engine("GET", "/api/v1/jobs/" + urllib.parse.quote(job_id) + "/download", timeout=120)
    except EngineError as e:
        if e.code != 404:   # 404 = nothing written yet
            raise
        return []
    return list(csv.DictReader(io.StringIO(csv_raw.decode("utf-8", "replace"))))


def get_leads(job_id, q):
    group = load_group(job_id)
    if group:
        jobs = _engine_jobs()
        summary = group_summary(group, jobs)
        rows = [r for jid in group["jobs"] if jid in jobs for r in _job_rows(jid)]
        # Region searches leak past the city edge; keep what falls inside the city box (+10%).
        rows = [r for r in rows if scrape.in_bbox(r, group["bbox"])]
    else:
        _, raw = engine("GET", "/api/v1/jobs/" + urllib.parse.quote(job_id))
        summary = job_summary(json.loads(raw))
        rows = _job_rows(job_id)
    leads = clean_rows(rows, summary["termos"]) if rows else []
    found = load_cache(job_id)
    if q.get("enriquecer") == "1" and leads:
        sites = sorted({lead["site"] for lead in leads if lead["site"]} - set((found or {}).keys()))
        if sites:
            with ThreadPoolExecutor(max_workers=8) as ex:
                found = {**(found or {}), **dict(zip(sites, ex.map(enrich_site, sites)))}
            if summary["status"] == "pronta":
                save_cache(job_id, found)
    if found:
        apply_enrichment(leads, found)
    if q.get("verificar_whatsapp") == "1" and leads:
        start_whatsapp_check(job_id, leads)
    apply_whatsapp_cache(leads)
    total = len(leads)
    if q.get("relevantes", "1") != "0":
        leads = [x for x in leads if x["relevante"]]
    if q.get("celular") == "1":
        leads = [x for x in leads if x["celular"]]
    if q.get("whatsapp") == "1":   # has a WhatsApp link that was not ruled out by the check
        leads = [x for x in leads if x["whatsapp"] and x["whatsapp_confirmado"] is not False]
    return {**summary, "enriquecida": bool(found), "verificacao_whatsapp": bool(WA_CHECK_URL and WA_CHECK_TOKEN),
            "verificacao": whatsapp_check_status(job_id),
            "total_bruto": total, "total": len(leads), "leads": leads}


def _search_terms(body):
    terms = body.get("buscas") or ([body["busca"]] if body.get("busca") else [])
    if isinstance(terms, str):
        terms = [terms]
    return [t.strip() for t in terms if isinstance(t, str) and t.strip()]


def create_grid_search(body, terms, city, grade):
    """Split the city's bounding box into grade x grade regions and queue one engine job per
    region, searching the bare terms around each region's center. Google caps a search at about
    120 results, so a big city needs several viewports to be covered."""
    if not city:
        raise EngineError(400, 'a busca em grade precisa de "cidade"')
    geo = scrape.geocode(city, with_bbox=True)
    if not geo or not geo[2]:
        raise EngineError(400, f'não encontrei os limites da cidade "{city}"; use o formato "Cidade, UF"')
    bbox = [float(x) for x in geo[2]]
    short = city.split(",")[0].strip()
    name = body.get("nome") or (terms[0] + (f" +{len(terms) - 1}" if len(terms) > 1 else "") + f" · {short} (cidade inteira)")
    minutes = int(body.get("tempo_limite_min", 20))
    cells = scrape.grid_cells(bbox, grade)
    ids = []
    for i, cell in enumerate(cells, 1):
        job = {"name": f"{name} · região {i}/{len(cells)}", "keywords": terms, "lang": body.get("idioma", "pt"),
               "zoom": cell["zoom"], "lat": cell["lat"], "lon": cell["lon"], "fast_mode": False,
               "radius": cell["radius"], "depth": int(body.get("profundidade", 10)), "email": False,
               "max_time": minutes * 60}
        if body.get("proxies"):
            job["proxies"] = body["proxies"]
        _, raw = engine("POST", "/api/v1/jobs", job)
        ids.append(json.loads(raw).get("id"))
    group = {"id": str(uuid.uuid4()), "nome": name, "termos": terms, "cidade": city, "bbox": bbox, "grade": grade,
             "jobs": ids, "profundidade": int(body.get("profundidade", 10)), "tempo_limite_min": minutes,
             "criada_em": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "pedido": body}
    save_group(group)
    return {"id": group["id"], "status": "na_fila", "nome": name, "termos": terms, "grade": grade, "regioes": len(ids)}


def create_search(body):
    terms = _search_terms(body)
    city = (body.get("cidade") or "").strip()
    if not terms:
        raise EngineError(400, 'informe "busca" (texto) ou "buscas" (lista), por exemplo "contabilidade"')
    grade = body.get("grade")
    if grade:
        try:
            grade = int(grade)
        except (TypeError, ValueError):
            grade = 0
        if not 2 <= grade <= 5:
            raise EngineError(400, '"grade" vai de 2 a 5 (3 divide a cidade em 9 regiões)')
        return create_grid_search(body, terms, city, grade)
    lat, lon = body.get("lat"), body.get("lon")
    if not (lat and lon):
        if not city:
            raise EngineError(400, 'informe "cidade", por exemplo "São José dos Campos, SP", ou "lat" e "lon"')
        geo = scrape.geocode(city)
        if not geo:
            raise EngineError(400, f'não encontrei a cidade "{city}"; use o formato "Cidade, UF"')
        lat, lon = geo
    short = city.split(",")[0].strip()
    keywords = [t if not short or _fold(short) in _fold(t) else f"{t} em {city}" for t in terms]
    job = {
        "name": body.get("nome") or (terms[0] + (f" +{len(terms) - 1}" if len(terms) > 1 else "") + (f" · {short}" if short else "")),
        "keywords": keywords, "lang": body.get("idioma", "pt"), "zoom": 15, "lat": str(lat), "lon": str(lon),
        "fast_mode": False, "radius": int(body.get("raio_m", 10000)), "depth": int(body.get("profundidade", 5)),
        "email": False, "max_time": int(body.get("tempo_limite_min", 20)) * 60,
    }
    if body.get("proxies"):
        job["proxies"] = body["proxies"]
    _, raw = engine("POST", "/api/v1/jobs", job)
    return {"id": json.loads(raw).get("id"), "status": "na_fila", "nome": job["name"], "termos": keywords}


def delete_search(sid):
    group = load_group(sid)
    for jid in (group["jobs"] if group else [sid]):
        try:
            engine("DELETE", "/api/v1/jobs/" + urllib.parse.quote(jid))
        except EngineError as e:
            if not group or e.code != 404:
                raise
    for path in ([_group_path(sid)] if group else []) + [cache_path(sid)]:
        try:
            os.remove(path)
        except OSError:
            pass


def redo_search(sid):
    """Queue the same search again and delete the old one (for failed or interrupted searches)."""
    group = load_group(sid)
    if group:
        new = create_search(group["pedido"])
    else:
        _, raw = engine("GET", "/api/v1/jobs/" + urllib.parse.quote(sid))
        j = json.loads(raw)
        d = j.get("Data") or {}
        job = {"name": j.get("Name"), "keywords": d.get("keywords"), "lang": d.get("lang") or "pt",
               "zoom": d.get("zoom") or 15, "lat": d.get("lat"), "lon": d.get("lon"), "fast_mode": False,
               "radius": d.get("radius") or 10000, "depth": d.get("depth") or 10, "email": False,
               "max_time": round((d.get("max_time") or 1200e9) / 1e9)}
        if d.get("proxies"):
            job["proxies"] = d["proxies"]
        _, raw = engine("POST", "/api/v1/jobs", job)
        new = {"id": json.loads(raw).get("id"), "status": "na_fila", "nome": job["name"]}
    delete_search(sid)
    return new


# ── HTTP ─────────────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass   # the client gave up waiting (e.g. a phone that locked its screen)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise EngineError(413, "request too large")
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise EngineError(400, "invalid JSON")
        if not isinstance(body, dict):
            raise EngineError(400, "body must be a JSON object")
        return body

    def _route(self, method):
        u = urllib.parse.urlparse(self.path)
        path, q = u.path.rstrip("/"), dict(urllib.parse.parse_qsl(u.query))
        m = re.fullmatch(r"/leads/buscas/([A-Za-z0-9-]{1,64})", path)
        try:
            if method == "GET" and path == "/enrich/health":
                return self._json(200, {"ok": True})
            if method == "POST" and path == "/enrich":
                return self._enrich()
            if path == "/leads/buscas" and method == "GET":
                return self._json(200, {"buscas": list_searches()})
            if path == "/leads/buscas" and method == "POST":
                return self._json(201, create_search(self._body()))
            if m and method == "GET":
                return self._json(200, get_leads(m.group(1), q))
            if m and method == "DELETE":
                delete_search(m.group(1))
                return self._json(200, {"apagada": m.group(1)})
            redo = re.fullmatch(r"/leads/buscas/([A-Za-z0-9-]{1,64})/refazer", path)
            if redo and method == "POST":
                return self._json(201, redo_search(redo.group(1)))
            self._json(404, {"erro": "rota não encontrada"})
        except EngineError as e:
            self._json(e.code if 400 <= e.code < 600 else 502, {"erro": str(e)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:   # never leave the client hanging on an unexpected error
            self._json(500, {"erro": f"erro interno: {e}"})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _enrich(self):
        sites = self._body().get("sites")
        if not isinstance(sites, list) or not all(isinstance(s, str) for s in sites):
            raise EngineError(400, "sites must be a list of URLs")
        sites = list(dict.fromkeys(s.strip() for s in sites if s.strip()))
        if len(sites) > MAX_SITES:
            raise EngineError(400, f"at most {MAX_SITES} sites per request")
        with ThreadPoolExecutor(max_workers=8) as ex:
            results = dict(zip(sites, ex.map(enrich_site, sites)))
        self._json(200, {"results": results})

    def log_message(self, fmt, *args):
        sys.stderr.write("api: " + (fmt % args) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--addr", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8082)
    a = ap.parse_args()
    print(f"api: listening on {a.addr}:{a.port}, engine {ENGINE}", file=sys.stderr)
    ThreadingHTTPServer((a.addr, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
