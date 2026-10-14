import socket
import threading

import pytest

from builder.web import Web, WebError, html_to_text, parse_arxiv, parse_crossref, parse_duckduckgo

DDG = """
<div class="result results_links results_links_deep web-result ">
  <h2><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.bls.gov%2Fcpi%2F&amp;rut=abc">CPI <b>Home</b></a></h2>
  <a class="result__snippet" href="x">Consumer &amp; price <b>index</b> data.</a>
</div>
<div class="result results_links results_links_deep web-result ">
  <h2><a class="result__a" href="https://duckduckgo.com/y.js?ad=1">Ad</a></h2>
</div>
"""

ARXIV = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry>
<id>http://arxiv.org/abs/2401.00001v1</id><published>2024-01-02T00:00:00Z</published>
<title>Small  Models
 at the Edge</title><summary>We study things.</summary><author><name>Ada L</name></author></entry></feed>"""


def test_parse_duckduckgo():
    r = parse_duckduckgo(DDG)
    assert r == [{"title": "CPI Home", "url": "https://www.bls.gov/cpi/", "snippet": "Consumer & price index data."}]


def test_parse_arxiv_and_crossref():
    a = parse_arxiv(ARXIV)[0]
    assert a["title"] == "Small Models at the Edge" and a["published"] == "2024-01-02" and a["tier"] == 2
    sw = parse_crossref({"message": {"items": [{"DOI": "10.5281/zenodo.1", "title": ["A package"], "type": "software"}]}})
    assert sw[0]["tier"] == 3  # a DOI alone isn't peer review
    c = parse_crossref({"message": {"items": [{"DOI": "10.1/abc", "title": ["T"], "type": "journal-article", "abstract": "<jats:p>Hi</jats:p>",
                                               "published": {"date-parts": [[2023, 5]]}, "author": [{"family": "X"}]}]}})
    assert c[0]["url"] == "https://doi.org/10.1/abc" and c[0]["snippet"] == "Hi" and c[0]["published"] == "2023-5"


def test_html_to_text_strips_chrome():
    html = """<html><head><title>Doc</title><meta property="article:published_time" content="2025-03-04T10:00">
    <script>evil()</script><style>p{}</style></head><body><nav>Home | About | Contact</nav>
    <h1>Findings</h1><p>The survey of 2,000 resellers found margins averaged 18 percent last year.</p>
    <footer>Copyright</footer></body></html>"""
    d = html_to_text(html)
    assert d["title"] == "Doc" and d["published"] == "2025-03-04"
    assert "margins averaged 18 percent" in d["text"] and "# Findings" in d["text"]
    assert "evil" not in d["text"] and "Contact" not in d["text"] and "Copyright" not in d["text"]


def test_refuses_private_network_and_odd_schemes():
    w = Web()
    for url in ("http://127.0.0.1:11434/api/tags", "http://localhost/", "file:///etc/passwd", "ftp://x.org/"):
        with pytest.raises(WebError):
            w.read(url)
    with pytest.raises(WebError, match="Tor"):
        w.read("http://exampleexampleexample.onion/")


def test_redirect_into_private_network_is_blocked():
    """A public page that redirects to localhost must not be followed."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/secret")
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        w = Web(allow_private=True)  # allow reaching the test server itself...
        w.allow_private = False      # ...but the redirect target is checked with the real rule
        w._check_target = Web(allow_private=False)._check_target
        with pytest.raises(WebError, match="private network"):
            w._opener.open(f"http://127.0.0.1:{srv.server_address[1]}/")
    finally:
        srv.shutdown()


def test_socks5_handshake_reaches_onion():
    """Minimal fake SOCKS5 proxy: checks the handshake and serves one HTTP response."""
    seen = {}
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def proxy():
        c, _ = srv.accept()
        assert c.recv(3) == b"\x05\x01\x00"
        c.sendall(b"\x05\x00")
        head = c.recv(5)
        seen["host"] = c.recv(head[4]).decode()
        c.recv(2)
        c.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
        c.recv(4096)
        body = b"<html><title>Hidden</title><p>A paragraph that is long enough to keep.</p></html>"
        c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
        c.close()

    threading.Thread(target=proxy, daemon=True).start()
    w = Web(tor_proxy=f"127.0.0.1:{srv.getsockname()[1]}")
    doc = w.read("http://exampleexampleexample.onion/page")
    assert seen["host"] == "exampleexampleexample.onion"
    assert doc["kind"] == "onion" and doc["tier"] == 4 and doc["title"] == "Hidden"
    srv.close()


def test_bot_check_pages_are_not_content():
    from builder.web import _is_bot_wall
    assert _is_bot_wall({"title": "Client Challenge", "text": "x"})
    assert _is_bot_wall({"title": "", "text": "Just a moment... Enable JavaScript and cookies to continue"})
    assert not _is_bot_wall({"title": "Pi 5 review", "text": "A long article. " * 100})
