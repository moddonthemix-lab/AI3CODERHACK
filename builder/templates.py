"""Ready-made, working project templates.

Every one is a COMPLETE project that runs and passes its own test with no model involved. Builder uses
them two ways:
  1. `builder new <kind>` scaffolds one instantly (no model, never fails).
  2. Every `builder build` starts from the matching template and keeps it as a guaranteed fallback, so a
     build always produces a runnable result even when the local model can't improve it.

Keep each template small, correct, and self-contained (standard library / plain HTML-JS only).
"""

from __future__ import annotations

# ---------------------------------------------------------------- game (playable canvas)
_GAME_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Game</title>
<style>body{margin:0;background:#111;color:#eee;font-family:sans-serif;text-align:center}
canvas{background:#000;display:block;margin:12px auto;border:2px solid #333}</style></head>
<body>
<h1>Game</h1>
<p>Arrow keys to move. Eat the red dots to score.</p>
<canvas id="game" width="400" height="400"></canvas>
<script>
const canvas = document.getElementById('game');
const ctx = canvas.getContext('2d');
const SIZE = 20;
let player = {x: 200, y: 200};
let food = {x: 100, y: 100};
let dir = {x: 0, y: 0};
let score = 0;

function placeFood() {
  food.x = Math.floor(Math.random() * (canvas.width / SIZE)) * SIZE;
  food.y = Math.floor(Math.random() * (canvas.height / SIZE)) * SIZE;
}
function update() {
  player.x += dir.x; player.y += dir.y;
  if (player.x < 0) player.x = canvas.width - SIZE;
  if (player.x >= canvas.width) player.x = 0;
  if (player.y < 0) player.y = canvas.height - SIZE;
  if (player.y >= canvas.height) player.y = 0;
  if (player.x === food.x && player.y === food.y) { score += 1; placeFood(); }
}
function draw() {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = 'lime'; ctx.fillRect(player.x, player.y, SIZE, SIZE);
  ctx.fillStyle = 'red'; ctx.fillRect(food.x, food.y, SIZE, SIZE);
  ctx.fillStyle = '#fff'; ctx.font = '16px sans-serif'; ctx.fillText('Score: ' + score, 10, 20);
}
function loop() { update(); draw(); requestAnimationFrame(loop); }
document.addEventListener('keydown', function (e) {
  if (e.key === 'ArrowUp') dir = {x: 0, y: -SIZE};
  else if (e.key === 'ArrowDown') dir = {x: 0, y: SIZE};
  else if (e.key === 'ArrowLeft') dir = {x: -SIZE, y: 0};
  else if (e.key === 'ArrowRight') dir = {x: SIZE, y: 0};
});
placeFood(); loop();
</script>
</body></html>
"""
_GAME_TEST = """def test_game_has_the_essentials():
    html = open('index.html').read()
    assert '<canvas' in html
    assert 'requestAnimationFrame' in html
    assert 'addEventListener' in html
    assert 'getContext' in html
"""

# ---------------------------------------------------------------- website (landing page)
_SITE_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>My Site</title>
<style>
  :root{--bg:#0e1116;--fg:#e6edf3;--accent:#4da3ff}
  *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--fg);font-family:system-ui,sans-serif;line-height:1.6}
  .hero{padding:80px 20px;text-align:center}
  .hero h1{font-size:2.4rem;margin:0 0 10px} .hero p{color:#9aa7b5;max-width:600px;margin:0 auto}
  .btn{display:inline-block;margin-top:20px;padding:12px 22px;background:var(--accent);color:#04101f;border-radius:8px;text-decoration:none;font-weight:600}
  section{max-width:760px;margin:0 auto;padding:30px 20px} .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px}
  .card{background:#161b22;border:1px solid #273040;border-radius:12px;padding:18px}
</style></head>
<body>
  <div class="hero"><h1>My Site</h1><p>A clean starting point. Edit this file to make it yours.</p>
  <a class="btn" href="#more">Learn more</a></div>
  <section id="more"><h2>What's here</h2><div class="cards">
    <div class="card"><h3>Fast</h3><p>One file, no build step.</p></div>
    <div class="card"><h3>Responsive</h3><p>Looks right on phone and desktop.</p></div>
    <div class="card"><h3>Yours</h3><p>Change the text, colors and sections.</p></div>
  </div></section>
</body></html>
"""
_SITE_TEST = """def test_site_is_a_valid_page():
    html = open('index.html').read().lower()
    assert '<!doctype html>' in html
    assert '<title>' in html and '</body>' in html
    assert 'viewport' in html
"""

# ---------------------------------------------------------------- API (stdlib http.server)
_API_APP = """\"\"\"A tiny JSON API using only the standard library. Run: python app.py  (serves on :8000)\"\"\"
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


def health():
    return {"ok": True}


def echo(payload):
    return {"echo": payload}


class Handler(BaseHTTPRequestHandler):
    def _send(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(health() if self.path == "/health" else {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            payload = {}
        self._send(echo(payload))


if __name__ == "__main__":
    print("Serving on http://127.0.0.1:8000  (Ctrl+C to stop)")
    HTTPServer(("127.0.0.1", 8000), Handler).serve_forever()
"""
_API_TEST = """from app import health, echo

def test_health():
    assert health() == {"ok": True}

def test_echo():
    assert echo({"name": "bob"}) == {"echo": {"name": "bob"}}
"""

# ---------------------------------------------------------------- CLI tool
_CLI_TOOL = """\"\"\"A small command-line tool. Run: python tool.py <name>\"\"\"
import argparse


def greet(name: str) -> str:
    return f"Hello, {name}!"


def main() -> None:
    p = argparse.ArgumentParser(description="Say hello.")
    p.add_argument("name", help="who to greet")
    print(greet(p.parse_args().name))


if __name__ == "__main__":
    main()
"""
_CLI_TEST = """from tool import greet

def test_greet():
    assert greet("World") == "Hello, World!"
"""

# ---------------------------------------------------------------- plain Python module
_PY_MAIN = """\"\"\"A small Python module. Run: python main.py\"\"\"


def add(a, b):
    return a + b


def main() -> None:
    print("2 + 3 =", add(2, 3))


if __name__ == "__main__":
    main()
"""
_PY_TEST = """from main import add

def test_add():
    assert add(2, 3) == 5
    assert add(-1, 1) == 0
"""


TEMPLATES = {
    "game": {"desc": "A playable arrow-key canvas game (index.html) + a test",
             "files": {"index.html": _GAME_HTML, "test_game.py": _GAME_TEST}},
    "web": {"desc": "A responsive one-page website (index.html) + a test",
            "files": {"index.html": _SITE_HTML, "test_site.py": _SITE_TEST}},
    "api": {"desc": "A JSON API using the standard library (app.py) + tests",
            "files": {"app.py": _API_APP, "test_app.py": _API_TEST}},
    "cli": {"desc": "A command-line tool with argparse (tool.py) + a test",
            "files": {"tool.py": _CLI_TOOL, "test_tool.py": _CLI_TEST}},
    "default": {"desc": "A plain Python module (main.py) + a test",
                "files": {"main.py": _PY_MAIN, "test_main.py": _PY_TEST}},
}

# Friendly aliases people might type for `builder new`.
ALIASES = {"website": "web", "site": "web", "server": "api", "tool": "cli", "script": "cli",
           "python": "default", "module": "default", "library": "default", "gui": "default",
           "data": "default", "snake": "game", "pong": "game"}


def template_files(kind: str, db=None) -> dict:
    """The working files for a template name. Checks the boss's saved templates first (when a db is given),
    then the built-in ones (with aliases); falls back to the plain Python module."""
    import json
    name = (kind or "").strip()
    if db is not None:
        row = db.get_user_template(name)
        if row:
            try:
                files = json.loads(row["files"])
                if isinstance(files, dict) and files:
                    return dict(files)
            except (ValueError, TypeError):
                pass
    key = ALIASES.get(name.lower(), name.lower())
    return dict(TEMPLATES.get(key, TEMPLATES["default"])["files"])


def template_names(db=None) -> list[dict]:
    """All templates: the boss's saved ones first (source 'yours'), then the built-in ones."""
    out = []
    if db is not None:
        for t in db.user_templates():
            out.append({"name": t["name"], "desc": t.get("descr") or "(your saved template)", "source": "yours"})
    out += [{"name": k, "desc": v["desc"], "source": "built-in"} for k, v in TEMPLATES.items()]
    return out
