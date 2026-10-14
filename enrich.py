#!/usr/bin/env python3
"""Small HTTP service the search page calls to find socials and extra emails on business websites.

POST /enrich  {"sites": ["https://example.com", ...]}   (max 20 per request)
-> {"results": {"https://example.com": {"emails": "...", "instagram": "...", "facebook": "...", "linkedin": "..."}}}

Reuses the homepage fetch, social regexes and contact-page email pass from scrape.py.
The service is reachable from the public URL with no auth, so it only fetches public http(s)
hosts: every hop (including redirects) must resolve to a public IP, and it returns only the
extracted links and emails, never page content.

Run: python3 enrich.py [--addr 127.0.0.1] [--port 8082]
"""
import argparse, ipaddress, json, os, socket, sys, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scrape  # noqa: E402  (lives next to this file)

MAX_SITES = 20
MAX_BODY = 64_000


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


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/enrich/health":
            return self._json(200, {"ok": True})
        self._json(404, {"message": "not found"})

    def do_POST(self):
        if self.path != "/enrich":
            return self._json(404, {"message": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._json(413, {"message": "request too large"})
            sites = json.loads(self.rfile.read(n) or b"{}").get("sites")
        except (ValueError, AttributeError):
            return self._json(400, {"message": "invalid JSON"})
        if not isinstance(sites, list) or not all(isinstance(s, str) for s in sites):
            return self._json(400, {"message": "sites must be a list of URLs"})
        sites = list(dict.fromkeys(s.strip() for s in sites if s.strip()))
        if len(sites) > MAX_SITES:
            return self._json(400, {"message": f"at most {MAX_SITES} sites per request"})
        with ThreadPoolExecutor(max_workers=8) as ex:
            results = dict(zip(sites, ex.map(enrich_site, sites)))
        self._json(200, {"results": results})

    def log_message(self, fmt, *args):
        sys.stderr.write("enrich: " + (fmt % args) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--addr", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8082)
    a = ap.parse_args()
    print(f"enrich: listening on {a.addr}:{a.port}", file=sys.stderr)
    ThreadingHTTPServer((a.addr, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
