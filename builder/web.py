"""Reading the outside world (standard library only).

Everything here is read-only: Scholar can search and fetch pages, never
post, log in or download programs. Fetched text is untrusted data — the
assistant prompt says so, and pages on your own network (localhost, LAN
addresses) are refused so a malicious page can't point him at your devices.

Sources:
- web search: your own SearXNG if ``SCHOLAR_SEARXNG_URL`` is set, else DuckDuckGo
- Wikipedia (good map of a topic; follow its citations)
- papers: arXiv + Crossref (journal articles with DOIs)
- any URL: HTML pages, plain text/JSON, PDFs (with the optional ``pypdf`` extra)
- .onion sites, only when a Tor SOCKS proxy is configured (``SCHOLAR_TOR_PROXY``)
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import re
import socket
import ssl
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from html import unescape

from . import credibility
from .documents import Document, DocumentError, parse as parse_document, parse_html

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) Scholar/0.1 (personal research assistant)"
# Public APIs (Wikipedia, arXiv, Crossref) ask bots to identify themselves honestly.
API_AGENT = "Scholar/0.1 (personal research assistant; https://github.com/moddonthemix-lab/AI2-researcher)"
MAX_BYTES = 4_000_000
MAX_TEXT = 60_000       # characters of readable text kept per web page
MAX_DOC_TEXT = 400_000  # whole documents (reports, filings, papers) are kept in full
DOI_PREFIX = re.compile(r"^(https?://(dx\.)?doi\.org/|doi:)", re.I)


class WebError(RuntimeError):
    pass


class OfflineError(WebError):
    """No internet right now — Scholar answers from his library and queues the rest."""


def html_to_text(html: str) -> dict:
    d = parse_html(html)
    return {"title": d.title, "published": d.published, "description": d.description, "text": d.text}


def _strip_tags(s: str) -> str:
    # Some publishers' metadata is HTML-escaped twice ("SEC&amp;nbsp;Filings"), so decode twice.
    text = unescape(unescape(re.sub(r"<[^>]+>", "", s or "")))
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


# ------------------------------------------------------------------ Tor / SOCKS
def _socks5_connect(proxy: str, host: str, port: int, timeout: float) -> socket.socket:
    """Open a TCP connection to host:port through a SOCKS5 proxy (remote DNS, so .onion works)."""
    phost, _, pport = proxy.rpartition(":")
    sock = socket.create_connection((phost or "127.0.0.1", int(pport or 9050)), timeout=timeout)
    try:
        sock.sendall(b"\x05\x01\x00")
        if sock.recv(2) != b"\x05\x00":
            raise WebError("Tor proxy refused the handshake")
        h = host.encode("idna")
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(h)]) + h + struct.pack(">H", port))
        reply = sock.recv(4)
        if len(reply) < 4 or reply[1] != 0:
            raise WebError(f"Tor could not reach {host} (SOCKS error {reply[1] if len(reply) > 1 else '?'})")
        skip = {1: 4, 4: 16}.get(reply[3])
        skip = skip if skip is not None else sock.recv(1)[0]
        sock.recv(skip + 2)
        return sock
    except Exception:
        sock.close()
        raise


# ------------------------------------------------------------------------ Web
class Web:
    def __init__(self, searxng_url: str = "", tor_proxy: str = "", timeout: float = 25,
                 allow_private: bool = False, openalex_key: str = "", contact: str = "",
                 offline: bool = False):
        self.searxng_url = searxng_url.rstrip("/")
        self.force_offline = offline        # SCHOLAR_OFFLINE=1: never touch the internet
        self._online: tuple[float, bool] = (0.0, True)
        self.openalex_key = openalex_key
        self.contact = contact
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self.tor_proxy = tor_proxy
        self.timeout = timeout
        self.allow_private = allow_private
        web = self

        class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
            # A public page must not be able to bounce Scholar onto your own network.
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                web._check_target(newurl)
                return super().redirect_request(req, fp, code, msg, headers, newurl)

        self._opener = urllib.request.build_opener(_CheckedRedirects)

    # ---- connectivity
    CHECK_URLS = ("https://www.gstatic.com/generate_204", "https://cloudflare.com/cdn-cgi/trace")

    def online(self, max_age: float = 60) -> bool:
        """Is the internet reachable? Checked quickly and remembered for a minute."""
        if self.force_offline:
            return False
        checked, ok = self._online
        if time.monotonic() - checked < max_age:
            return ok
        ok = False
        for url in self.CHECK_URLS:
            try:
                with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=4):
                    ok = True
            except urllib.error.HTTPError:
                ok = True   # any answer means we're online
            except (urllib.error.URLError, OSError):
                continue
            if ok:
                break
        self._online = (time.monotonic(), ok)
        return ok

    def _went_offline(self) -> None:
        self._online = (time.monotonic(), False)

    # ---- low-level GET
    def _check_target(self, url: str) -> urllib.parse.ParseResult:
        u = urllib.parse.urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise WebError(f"only http(s) URLs can be read, not {url!r}")
        if u.hostname.endswith(".onion"):
            if not self.tor_proxy:
                raise WebError(".onion sites need Tor: run Tor and set SCHOLAR_TOR_PROXY=127.0.0.1:9050")
            return u
        if not self.allow_private:
            try:
                infos = socket.getaddrinfo(u.hostname, None)
            except OSError:
                infos = []  # can't resolve locally (e.g. behind a proxy) — the proxy decides
            for info in infos:
                ip = ipaddress.ip_address(info[4][0].split("%")[0])
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                    raise WebError(f"refusing to read {u.hostname}: it is on your private network")
        return u

    def get(self, url: str, accept: str = "*/*", agent: str = USER_AGENT,
            headers: dict | None = None) -> tuple[str, str, bytes]:
        """Return (final_url, content_type, body)."""
        u = self._check_target(url)
        if u.hostname.endswith(".onion"):
            return self._get_via_tor(url, accept)
        if self.force_offline:
            raise OfflineError("offline mode is on (SCHOLAR_OFFLINE=1)")
        checked, ok = self._online
        if not ok and time.monotonic() - checked < 60:  # known offline: fail now, don't wait on timeouts
            raise OfflineError("no internet connection right now")
        req = urllib.request.Request(url, headers={"User-Agent": agent, "Accept": accept,
                                                   "Accept-Language": "en-US,en;q=0.8", **(headers or {})})
        for attempt in (1, 2):
            try:
                with self._opener.open(req, timeout=self.timeout) as resp:
                    return resp.geturl(), resp.headers.get("Content-Type", ""), resp.read(MAX_BYTES)
            except urllib.error.HTTPError as e:
                if e.code in (429, 503) and attempt == 1:  # rate limited: wait once, politely
                    retry = e.headers.get("Retry-After", "")
                    time.sleep(min(float(retry) if retry.isdigit() else 2.0, 8.0))
                    continue
                raise WebError(f"{url} returned HTTP {e.code}"
                               + (" (rate limited — try again in a minute)" if e.code == 429 else "")) from e
            except (urllib.error.URLError, OSError) as e:
                if not self.online(max_age=0):
                    raise OfflineError(f"no internet connection (couldn't reach {u.hostname})") from e
                raise WebError(f"could not reach {url}: {getattr(e, 'reason', e)}") from e
        raise WebError(f"could not read {url}")

    def _get_via_tor(self, url: str, accept: str, redirects: int = 3) -> tuple[str, str, bytes]:
        u = urllib.parse.urlparse(url)
        port = u.port or (443 if u.scheme == "https" else 80)
        try:
            sock = _socks5_connect(self.tor_proxy, u.hostname, port, self.timeout)
            if u.scheme == "https":
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
            conn = http.client.HTTPConnection(u.hostname, port, timeout=self.timeout)
            conn.sock = sock
            path = (u.path or "/") + (f"?{u.query}" if u.query else "")
            conn.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept": accept,
                                               "Host": u.netloc})
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308) and redirects and resp.getheader("Location"):
                nxt = urllib.parse.urljoin(url, resp.getheader("Location"))
                conn.close()
                self._check_target(nxt)
                if urllib.parse.urlparse(nxt).hostname.endswith(".onion"):
                    return self._get_via_tor(nxt, accept, redirects - 1)
                return self.get(nxt, accept)
            if resp.status >= 400:
                raise WebError(f"{url} returned HTTP {resp.status}")
            body = resp.read(MAX_BYTES)
            ctype = resp.getheader("Content-Type", "")
            conn.close()
            return url, ctype, body
        except (OSError, http.client.HTTPException) as e:
            raise WebError(f"could not reach {url} over Tor: {e}") from e

    def get_json(self, url: str, agent: str = API_AGENT, headers: dict | None = None) -> dict:
        _, _, body = self.get(url, "application/json", agent, headers)
        try:
            return json.loads(body)
        except ValueError as e:
            raise WebError(f"{url} did not return JSON") from e

    # ---- search
    def search(self, query: str, limit: int = 8) -> list[dict]:
        """General web search. Each result: title, url, snippet, domain, tier."""
        results = self._searxng(query, limit) if self.searxng_url else self._duckduckgo(query, limit)
        return [_with_rating(r) for r in results[:limit]]

    def _searxng(self, query: str, limit: int) -> list[dict]:
        data = self.get_json(f"{self.searxng_url}/search?" + urllib.parse.urlencode(
            {"q": query, "format": "json"}))
        return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
                for r in data.get("results", [])[:limit] if r.get("url")]

    def _duckduckgo(self, query: str, limit: int) -> list[dict]:
        _, _, body = self.get("https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query}),
                              "text/html")
        results = parse_duckduckgo(body.decode("utf-8", "replace"))
        if not results and "result__a" not in body.decode("utf-8", "replace"):
            raise WebError("DuckDuckGo sent no results page (it rate-limits automated searches). "
                           "Try again shortly, or run your own SearXNG and set SCHOLAR_SEARXNG_URL.")
        return results[:limit]

    def wikipedia(self, query: str, limit: int = 3) -> list[dict]:
        data = self.get_json("https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode({
            "action": "query", "generator": "search", "gsrsearch": query, "gsrlimit": limit,
            "prop": "extracts|info", "exintro": 1, "explaintext": 1, "exlimit": "max",
            "inprop": "url", "format": "json"}))
        pages = sorted(data.get("query", {}).get("pages", {}).values(), key=lambda p: p.get("index", 0))
        return [_with_rating({"title": p.get("title", ""), "url": p.get("fullurl", ""),
                              "snippet": (p.get("extract") or "")[:1500]}) for p in pages]

    def papers(self, query: str, limit: int = 6) -> list[dict]:
        """Academic papers from OpenAlex, arXiv and Crossref, interleaved and de-duplicated by DOI."""
        lists, errors = [], []
        for fn in (self.openalex, self._arxiv, self._crossref):
            try:
                lists.append(fn(query, limit))
            except WebError as e:
                errors.append(str(e))
        merged, seen = [], set()
        for i in range(max((len(x) for x in lists), default=0)):
            for lst in lists:
                if i < len(lst):
                    key = (lst[i].get("doi") or lst[i]["url"]).lower()
                    if key not in seen:
                        seen.add(key)
                        merged.append(lst[i])
        if not merged and errors:
            raise WebError("; ".join(errors))
        return merged[:limit]

    # ---- OpenAlex: 250M+ works, with citation links both ways
    def _openalex(self, path: str, params: dict) -> dict:
        if self.openalex_key:
            params = {**params, "api_key": self.openalex_key}
        elif self.contact:
            params = {**params, "mailto": self.contact}
        return self.get_json("https://api.openalex.org" + path + "?" + urllib.parse.urlencode(params))

    def openalex(self, query: str, limit: int = 6) -> list[dict]:
        data = self._openalex("/works", {"search": query, "per_page": limit})
        return [parse_openalex_work(w) for w in data.get("results", [])]

    def openalex_work(self, ident: str) -> dict:
        """Look up one work by DOI, OpenAlex id (W123…) or URL."""
        ident = ident.strip()
        if DOI_PREFIX.match(ident):
            ident = "doi:" + DOI_PREFIX.sub("", ident)
        elif ident.lower().startswith("10."):
            ident = "doi:" + ident
        elif "openalex.org/" in ident:
            ident = ident.rsplit("/", 1)[-1]
        return parse_openalex_work(self._openalex(f"/works/{urllib.parse.quote(ident, safe=':/')}", {}))

    def references_of(self, ident: str, limit: int = 15) -> list[dict]:
        """The works this paper cites — most-cited first, so the original studies surface."""
        work = self.openalex_work(ident)
        ids = [r.rsplit("/", 1)[-1] for r in work.get("referenced_works", [])][:100]
        if not ids:
            return []
        data = self._openalex("/works", {"filter": "openalex:" + "|".join(ids),
                                          "sort": "cited_by_count:desc", "per_page": limit})
        return [parse_openalex_work(w) for w in data.get("results", [])]

    def cited_by(self, ident: str, limit: int = 10) -> list[dict]:
        """Newer works that cite this one — how the finding held up later."""
        work = self.openalex_work(ident)
        wid = work["openalex_id"].rsplit("/", 1)[-1]
        data = self._openalex("/works", {"filter": f"cites:{wid}", "sort": "publication_date:desc",
                                          "per_page": limit})
        return [parse_openalex_work(w) for w in data.get("results", [])]

    # ---- SEC EDGAR: company filings (10-K, 10-Q, 8-K, S-1, exhibits like EX-21 subsidiaries)
    def _sec_agent(self) -> str:
        if not self.contact:
            raise WebError("SEC EDGAR requires contact info: set SCHOLAR_CONTACT to your email")
        return f"Scholar personal research {self.contact}"

    def filings(self, query: str, forms: str = "", limit: int = 8) -> list[dict]:
        """Full-text search across SEC filings since 2001."""
        params = {"q": query}
        if forms:
            params["forms"] = forms
        data = self.get_json("https://efts.sec.gov/LATEST/search-index?" + urllib.parse.urlencode(params),
                             agent=self._sec_agent())
        return parse_edgar(data)[:limit]

    # ---- documents
    def allowed_by_robots(self, url: str) -> bool:
        """Respect robots.txt before downloading whole documents."""
        u = urllib.parse.urlparse(url)
        if u.hostname and u.hostname.endswith(".onion"):
            return True
        base = f"{u.scheme}://{u.netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                _, _, body = self.get(base + "/robots.txt", "text/plain")
                rp.parse(body.decode("utf-8", "replace").splitlines())
                self._robots[base] = rp
            except WebError:
                self._robots[base] = None  # no robots.txt (or unreachable): nothing forbidden
        rp = self._robots[base]
        return rp is None or rp.can_fetch("Scholar", url)

    def _arxiv(self, query: str, limit: int) -> list[dict]:
        _, _, body = self.get("https://export.arxiv.org/api/query?" + urllib.parse.urlencode(
            {"search_query": " AND ".join(f"all:{w}" for w in re.findall(r"\w+", query)[:8]),
             "max_results": limit}), "application/atom+xml", API_AGENT)
        return parse_arxiv(body)

    def _crossref(self, query: str, limit: int) -> list[dict]:
        data = self.get_json("https://api.crossref.org/works?" + urllib.parse.urlencode(
            {"query": query, "rows": limit,
             "select": "DOI,title,abstract,published,container-title,author,URL,type"}))
        return parse_crossref(data)

    # ---- read one page or document
    def read(self, url: str, respect_robots: bool = False) -> dict:
        """Fetch a URL and return readable text, tables, references and credibility info.

        ``respect_robots`` is used when downloading whole documents for analysis.
        """
        if respect_robots and not self.allowed_by_robots(url):
            raise WebError(f"{url} is disallowed by the site's robots.txt — not downloading it")
        agent = self._sec_agent() if "sec.gov" in (urllib.parse.urlparse(url).hostname or "") else USER_AGENT
        final, ctype, body = self.get(url, agent=agent)
        try:
            doc = parse_document(body, urllib.parse.urlparse(final).path.rsplit("/", 1)[-1] or "page", ctype)
        except DocumentError as e:
            raise WebError(str(e)) from e
        if _is_bot_wall(doc):
            raise WebError(f"{final} showed a bot-check page instead of the content")
        rating = credibility.rate(final)
        is_doc = doc.parser not in ("html", "text")
        kind = "onion" if rating["domain"].endswith(".onion") else (
            "filing" if rating["domain"] == "sec.gov" else
            "paper" if rating["domain"] in ("arxiv.org", "doi.org") or doc.parser in ("docling", "pypdf") else
            "reference" if rating["domain"].endswith("wikipedia.org") else "document" if is_doc else "web")
        return {"url": final, "title": doc.title or final, "published": doc.published,
                "text": doc.text[:MAX_TEXT if not is_doc else MAX_DOC_TEXT], "kind": kind,
                "content_type": ctype, "raw": body, "document": doc, **rating}


# ------------------------------------------------------------------- parsers
def parse_duckduckgo(html: str) -> list[dict]:
    out = []
    blocks = re.split(r'<div[^>]+class="[^"]*result results_links', html)[1:]
    for b in blocks:
        a = re.search(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
        if not a:
            continue
        href = unescape(a.group(1))
        q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        url = q.get("uddg", [href])[0]
        if url.startswith("//"):
            url = "https:" + url
        if "duckduckgo.com/y.js" in url:  # ads
            continue
        snip = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', b, re.S)
        out.append({"title": _strip_tags(a.group(2)), "url": url,
                    "snippet": _strip_tags(snip.group(1)) if snip else ""})
    return out


_ATOM = "{http://www.w3.org/2005/Atom}"


def parse_arxiv(xml: bytes) -> list[dict]:
    root = ET.fromstring(xml)
    out = []
    for e in root.iter(f"{_ATOM}entry"):
        get = lambda tag: re.sub(r"\s+", " ", (e.findtext(f"{_ATOM}{tag}") or "")).strip()  # noqa: E731
        authors = [a.findtext(f"{_ATOM}name") or "" for a in e.findall(f"{_ATOM}author")]
        out.append(_with_rating({
            "title": get("title"), "url": get("id"), "snippet": get("summary")[:1500],
            "published": get("published")[:10], "authors": ", ".join(authors[:4]),
            "venue": "arXiv preprint (not peer-reviewed)"}))
    return out


def parse_crossref(data: dict) -> list[dict]:
    out = []
    for it in data.get("message", {}).get("items", []):
        parts = (it.get("published") or {}).get("date-parts") or [[]]
        authors = [f"{a.get('given', '')} {a.get('family', '')}".strip() for a in it.get("author", [])]
        url = f"https://doi.org/{it['DOI']}" if it.get("DOI") else it.get("URL", "")
        if not url:
            continue
        out.append({
            "title": _strip_tags(" ".join(it.get("title") or [])), "url": url,
            "snippet": _strip_tags(it.get("abstract", ""))[:1500],
            "published": "-".join(str(p) for p in parts[0]), "authors": ", ".join(authors[:4]),
            "venue": " ".join(it.get("container-title") or []), "domain": "doi.org",
            # A DOI alone isn't peer review: software, datasets and reports get DOIs too.
            **_doi_tier(it.get("type", ""))})
    return out


def _abstract(inverted: dict | None) -> str:
    if not inverted:
        return ""
    words = sorted((pos, w) for w, positions in inverted.items() for pos in positions)
    return " ".join(w for _, w in words)


def parse_openalex_work(w: dict) -> dict:
    doi = (w.get("doi") or "").replace("https://doi.org/", "")
    loc = w.get("primary_location") or {}
    oa = w.get("best_oa_location") or {}
    url = f"https://doi.org/{doi}" if doi else (loc.get("landing_page_url") or w.get("id", ""))
    venue = ((loc.get("source") or {}).get("display_name")) or ""
    authors = [(a.get("author") or {}).get("display_name", "") for a in w.get("authorships", [])]
    out = {
        "title": _strip_tags(w.get("display_name") or w.get("title") or ""), "url": url, "doi": doi,
        "snippet": _abstract(w.get("abstract_inverted_index"))[:1500],
        "published": w.get("publication_date") or str(w.get("publication_year") or ""),
        "authors": ", ".join(a for a in authors[:4] if a), "venue": venue,
        "cited_by_count": w.get("cited_by_count", 0), "openalex_id": w.get("id", ""),
        "pdf_url": oa.get("pdf_url") or (w.get("open_access") or {}).get("oa_url") or "",
        "is_retracted": bool(w.get("is_retracted")), "type": w.get("type", ""),
        "referenced_works": w.get("referenced_works", []),
    }
    rating = credibility.rate(url)
    if doi and w.get("type") in ("article", "review", "book-chapter", "book"):
        rating = {"domain": "doi.org", "tier": 1, "tier_label": credibility.TIER_LABELS[1]}
    if out["is_retracted"]:  # a retracted paper is not evidence
        rating = {**rating, "tier": 4, "tier_label": "RETRACTED — do not rely on this"}
    return {**out, **rating}


_PEER_REVIEWED = {"journal-article", "proceedings-article", "book-chapter", "book", "monograph", "article",
                  "review", "reference-entry"}


def _doi_tier(kind: str) -> dict:
    t = 1 if kind in _PEER_REVIEWED else 2 if kind in ("posted-content", "preprint", "report", "dissertation") else 3
    return {"tier": t, "tier_label": credibility.TIER_LABELS[t]}


def parse_edgar(data: dict) -> list[dict]:
    out = []
    for h in data.get("hits", {}).get("hits", []):
        src = h.get("_source", {})
        adsh, _, filename = h.get("_id", "").partition(":")
        ciks = src.get("ciks") or []
        if not (adsh and filename and ciks):
            continue
        url = f"https://www.sec.gov/Archives/edgar/data/{int(ciks[0])}/{adsh.replace('-', '')}/{filename}"
        names = src.get("display_names") or []
        out.append({
            "title": f"{(names[0] if names else '').split('(CIK')[0].strip()} — {src.get('form', '')} "
                     f"({src.get('file_date', '')})",
            "url": url, "snippet": f"{src.get('file_type') or src.get('form', '')} filed {src.get('file_date', '')}; "
                                   f"{', '.join(src.get('biz_locations') or [])}",
            "published": src.get("file_date", ""), "company": names[0] if names else "",
            "cik": ciks[0], "form": src.get("form", ""), "domain": "sec.gov", "tier": 1,
            "tier_label": credibility.TIER_LABELS[1]})
    return out


def _with_rating(r: dict) -> dict:
    return {**r, **credibility.rate(r.get("url", ""))}


_BOT_WALLS = ("client challenge", "just a moment", "attention required", "verify you are human",
              "are you a robot", "access denied", "enable javascript and cookies")


def _is_bot_wall(doc: Document | dict) -> bool:
    get = doc.get if isinstance(doc, dict) else lambda k, d="": getattr(doc, k, d)
    title, text = (get("title", "") or "").lower(), get("text", "") or ""
    return any(w in title for w in _BOT_WALLS) or (
        len(text) < 600 and any(w in text.lower() for w in _BOT_WALLS))


def _charset(ctype: str) -> str:
    m = re.search(r"charset=([\w-]+)", ctype)
    return m.group(1) if m else "utf-8"
