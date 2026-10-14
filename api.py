#!/usr/bin/env python3
"""Clean leads API on top of the scraper engine, plus the socials/email lookup the search page uses.

Clean leads (for Prospecta, n8n or any other integration):
  POST   /leads/buscas          {"busca": "contabilidade", "cidade": "São José dos Campos, SP", ...}
  GET    /leads/buscas          list searches and their status
  GET    /leads/buscas/{id}     status + cleaned leads (JSON). Filters: ?celular=1 ?email=1
                                ?relevantes=0 (include off-topic results) ?enriquecer=1 (visit sites
                                for socials and extra emails; cached on disk after the first call)
  DELETE /leads/buscas/{id}

Socials lookup used by the page:
  POST   /enrich                {"sites": ["https://example.com", ...]}   (max 20 per request)

Every endpoint is reachable from the public URL with no auth, so site visits only go to public
http(s) hosts: every hop (including redirects) must resolve to a public IP, and only the extracted
links and emails are returned, never page content.

Run: python3 api.py [--addr 127.0.0.1] [--port 8082]
Env: ENGINE_URL (default http://127.0.0.1:8081), CACHE_DIR (default /gmapsdata/leads-cache)
"""
import argparse, csv, io, ipaddress, json, os, re, socket, sys, threading, unicodedata
import urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scrape  # noqa: E402  (lives next to this file)

MAX_SITES = 20
MAX_BODY = 64_000
ENGINE = os.environ.get("ENGINE_URL", "http://127.0.0.1:8081").rstrip("/")
CACHE_DIR = os.environ.get("CACHE_DIR", "/gmapsdata/leads-cache")
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


def enrich_site(site):
    home = scrape._fetch_cached(site)
    out = scrape._find_socials(home)
    emails = scrape.extract_emails(home, site)
    if home and not emails:
        pages = "".join(safe_fetch_html(u, timeout=8) for u in scrape.contact_links(home, site))
        emails = scrape.extract_emails(pages, site)
    out["emails"] = ", ".join(emails)
    return out


# ── Cleaning ─────────────────────────────────────────────────────────────────
_PLACEHOLDER_LOCAL = {"first.last", "firstname.lastname", "nome", "seunome", "exemplo", "example", "test", "teste"}
_JOB_LOCAL = re.compile(r"^(vagas?|curriculos?|curriculum|rh|trabalheconosco|jobs?|careers?)[._-]?", re.I)


def clean_emails(raw, site=""):
    """Split, decode and validate the engine's comma-joined emails. Drops placeholders and
    junk domains, puts recruiting addresses last and addresses on the site's domain first."""
    out = []
    for e in re.split(r"[,;\s]+", raw or ""):
        e = urllib.parse.unquote(e).strip().strip(".<>()[]'\"").lower()
        e = e.removeprefix("mailto:")
        local, _, dom = e.rpartition("@")
        if (not scrape.EMAIL_RE.fullmatch(e) or e.endswith(scrape._JUNK_EXT) or local in scrape._JUNK_LOCAL
                or local in _PLACEHOLDER_LOCAL or any(j in dom for j in scrape._JUNK_DOMAIN) or e in out):
            continue
        out.append(e)
    host = scrape._host(site) if site else ""
    on_site = lambda e: bool(host) and (e.endswith("@" + host) or e.endswith("." + host))
    return sorted(out, key=lambda e: (bool(_JOB_LOCAL.match(e)), not on_site(e)))


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
        emails = clean_emails(r.get("emails", ""), site)
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
            "email": emails[0] if emails else "",
            "emails": emails,
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
    return out


def apply_enrichment(leads, found):
    for lead in leads:
        x = found.get(lead["site"]) if lead["site"] else None
        if not x:
            continue
        for k in ("instagram", "facebook", "linkedin"):
            if x.get(k) and not lead[k]:
                lead[k] = x[k]
        if x.get("emails"):
            extra = [e for e in clean_emails(x["emails"], lead["site"]) if e not in lead["emails"]]
            lead["emails"] = clean_emails(", ".join(lead["emails"] + extra), lead["site"])
            lead["email"] = lead["emails"][0] if lead["emails"] else ""


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


def job_summary(j):
    d = j.get("Data") or {}
    return {"id": j.get("ID"), "nome": j.get("Name"), "status": STATUS_PT.get(j.get("Status"), j.get("Status")),
            "criada_em": j.get("Date"), "termos": d.get("keywords") or [], "profundidade": d.get("depth"),
            "emails": d.get("email"), "tempo_limite_min": round((d.get("max_time") or 0) / 60e9) or None}


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


def get_leads(job_id, q):
    _, raw = engine("GET", "/api/v1/jobs/" + urllib.parse.quote(job_id))
    job = json.loads(raw)
    summary = job_summary(job)
    leads = []
    try:
        _, csv_raw = engine("GET", "/api/v1/jobs/" + urllib.parse.quote(job_id) + "/download", timeout=120)
        leads = clean_rows(csv.DictReader(io.StringIO(csv_raw.decode("utf-8", "replace"))), summary["termos"])
    except EngineError as e:
        if e.code != 404:   # 404 = nothing written yet
            raise
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
    total = len(leads)
    if q.get("relevantes", "1") != "0":
        leads = [x for x in leads if x["relevante"]]
    if q.get("celular") == "1":
        leads = [x for x in leads if x["celular"]]
    if q.get("email") == "1":
        leads = [x for x in leads if x["email"]]
    return {**summary, "enriquecida": bool(found), "total_bruto": total, "total": len(leads), "leads": leads}


def create_search(body):
    terms = body.get("buscas") or ([body["busca"]] if body.get("busca") else [])
    if isinstance(terms, str):
        terms = [terms]
    terms = [t.strip() for t in terms if isinstance(t, str) and t.strip()]
    city = (body.get("cidade") or "").strip()
    if not terms:
        raise EngineError(400, 'informe "busca" (texto) ou "buscas" (lista), por exemplo "contabilidade"')
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
        "email": bool(body.get("emails", True)), "max_time": int(body.get("tempo_limite_min", 20)) * 60,
    }
    if body.get("proxies"):
        job["proxies"] = body["proxies"]
    _, raw = engine("POST", "/api/v1/jobs", job)
    return {"id": json.loads(raw).get("id"), "status": "na_fila", "nome": job["name"], "termos": keywords}


# ── HTTP ─────────────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

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
                _, raw = engine("GET", "/api/v1/jobs")
                jobs = sorted(json.loads(raw) or [], key=lambda j: j.get("Date") or "", reverse=True)
                return self._json(200, {"buscas": [job_summary(j) for j in jobs]})
            if path == "/leads/buscas" and method == "POST":
                return self._json(201, create_search(self._body()))
            if m and method == "GET":
                return self._json(200, get_leads(m.group(1), q))
            if m and method == "DELETE":
                engine("DELETE", "/api/v1/jobs/" + m.group(1))
                try:
                    os.remove(cache_path(m.group(1)))
                except OSError:
                    pass
                return self._json(200, {"apagada": m.group(1)})
            self._json(404, {"erro": "rota não encontrada"})
        except EngineError as e:
            self._json(e.code if 400 <= e.code < 600 else 502, {"erro": str(e)})
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
