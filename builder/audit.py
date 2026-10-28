"""Defensive security review: read Builder's OWN code/configs and report issues with severity + a fix.

This is Builder's everyday, always-on security value — and it is purely DEFENSIVE:

- read-only: it never runs the code it audits, never changes a file, never touches the network;
- offline: the pattern scan is pure standard library, so it works with the Wi-Fi off (like Scholar/Amo).
  An optional second pass asks the LOCAL coder model for context-aware findings — still offline;
- in scope: every path is checked against Builder's allowed folders (``projects.check_scope``).

Each finding is ``{issue, severity (high|med|low), fix, file, line}``. The patterns look for well-known
weak spots (hardcoded secrets, eval/exec on input, shell injection, weak hashing, unsafe deserialization,
TLS verification turned off, debug left on, unpinned dependencies). They are heuristics — a prompt to look
closer, not a verdict — so Builder explains each one and how to fix it rather than just flagging it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .db import SEVERITIES
from .projects import allowed_dirs, check_scope

SEVERITY_RANK = {"high": 0, "med": 1, "low": 2}

# Files/dirs that aren't the boss's own source (don't audit dependencies or build output).
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".mypy_cache",
             ".pytest_cache", "site-packages", ".tox", ".idea", ".next"}
CODE_SUFFIXES = {".py", ".js", ".ts", ".jsx", ".tsx", ".php", ".rb", ".go", ".java", ".sh", ".bash",
                 ".yaml", ".yml", ".json", ".env", ".cfg", ".ini", ".toml", ".html", ".htm", ".sql",
                 ".xml", ".gradle", ".kts", ".properties", ".css", ".lock"}
# Manifest/lock/config files to inspect for outdated or vulnerable components, by exact name.
COMPONENT_FILES = {"dockerfile", "composer.json", "package.json", "pom.xml", "build.gradle",
                   "requirements.txt", ".env", "docker-compose.yml", "docker-compose.yaml"}
MAX_FILE_BYTES = 1_000_000  # skip anything bigger than ~1 MB (likely data/minified, not hand-written source)


@dataclass
class Finding:
    issue: str
    severity: str
    fix: str
    file: str = ""
    line: int = 0

    def as_dict(self) -> dict:
        return {"issue": self.issue, "severity": self.severity, "fix": self.fix,
                "file": self.file, "line": self.line}


@dataclass
class Rule:
    name: str
    severity: str
    fix: str
    pattern: re.Pattern
    suffixes: tuple[str, ...] = ()          # only apply to these file types (empty = any)
    unless: re.Pattern | None = None        # skip a hit if this also matches the line (e.g. SafeLoader)


def _rx(p: str) -> re.Pattern:
    return re.compile(p, re.I)


# The defensive checks. Kept small, specific and explained; each maps to a concrete fix.
RULES: list[Rule] = [
    Rule("Hardcoded secret in source", "high",
         "Move secrets to environment variables or a secrets manager; never commit them. Rotate this one.",
         re.compile(r"""(password|passwd|secret|api[_-]?key|access[_-]?key|auth[_-]?token|token|private[_-]?key)\w*\s*[:=]\s*['"][^'"]{6,}['"]""", re.I),
         unless=_rx(r"(os\.environ|getenv|process\.env|example|changeme|your[_-]?|placeholder|\{\{|<%|input\(|prompt\()")),
    Rule("AWS access key id in source", "high",
         "Remove the key, rotate it immediately, and load credentials from the environment or a profile.",
         re.compile(r"AKIA[0-9A-Z]{16}")),
    Rule("Private key committed", "high",
         "Never commit private keys. Remove it, rotate the key, and add it to .gitignore.",
         re.compile(r"-----BEGIN (RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    Rule("eval() on a value", "high",
         "Avoid eval; parse the input explicitly (json.loads, int(), ast.literal_eval) instead.",
         _rx(r"\beval\s*\("), (".py", ".js", ".ts", ".jsx", ".tsx")),
    Rule("exec() of dynamic code", "high",
         "Avoid exec on anything derived from input; restructure so code isn't built from data.",
         _rx(r"\bexec\s*\("), (".py",)),
    Rule("Shell command with shell=True", "high",
         "Pass a list of args and shell=False; if a shell is unavoidable, shlex.quote every argument.",
         _rx(r"shell\s*=\s*True"), (".py",)),
    Rule("os.system / popen with a command", "high",
         "Use subprocess.run([...]) with a list of args instead of a shell string.",
         _rx(r"\bos\.(system|popen)\s*\("), (".py",)),
    Rule("SQL built by string formatting", "high",
         "Use parameterised queries (execute(sql, params)); never build SQL with f-strings, % or +.",
         _rx(r"""(execute|executemany)\s*\(\s*([f]['"].*(%s|%d|\{|\+)|['"].*['"]\s*(%|\+))"""),
         (".py",)),
    Rule("Unsafe deserialization", "high",
         "Don't unpickle/yaml.load untrusted data; use json, or yaml.safe_load / a safe schema.",
         _rx(r"(pickle\.loads?|cPickle\.loads?|marshal\.loads?|yaml\.load)\s*\("), (".py",),
         unless=_rx(r"(safe_load|SafeLoader|Loader\s*=\s*yaml\.SafeLoader)")),
    Rule("Weak hash for a password", "med",
         "Use a slow password hash (bcrypt, scrypt or argon2) with a per-user salt — not md5/sha1.",
         _rx(r"hashlib\.(md5|sha1)\s*\("), (".py",)),
    Rule("TLS certificate verification disabled", "high",
         "Never disable certificate verification in real use; keep verify=True / check the cert.",
         _rx(r"(verify\s*=\s*False|ssl\._create_unverified|rejectUnauthorized\s*:\s*false|CURLOPT_SSL_VERIFY(PEER|HOST)\s*,\s*0)")),
    Rule("Debug mode enabled", "med",
         "Turn debug off in anything reachable by others; it can leak internals and allow code execution.",
         _rx(r"(debug\s*=\s*True|app\.run\([^)]*debug\s*=\s*True|DEBUG\s*[:=]\s*True)")),
    Rule("Binding to all interfaces", "low",
         "Bind to 127.0.0.1 for local-only services; only use 0.0.0.0 when you mean to expose it.",
         _rx(r"0\.0\.0\.0")),
    Rule("Wide-open CORS", "med",
         "Restrict CORS to the specific origins you trust instead of '*', especially with credentials.",
         _rx(r"""Access-Control-Allow-Origin['"]?\s*[:,]\s*['"]?\*|origins\s*=\s*['"]\*['"]""")),
    Rule("JWT 'none' algorithm allowed", "high",
         "Never accept the 'none' algorithm; pin the expected algorithm (e.g. HS256/RS256) when decoding.",
         _rx(r"""algorithms?\s*[:=]\s*\[?\s*['"]none['"]""")),
    Rule("World-writable permissions", "med",
         "Don't grant 777 / world-write; give the narrowest permissions that work (e.g. 600/640/750).",
         _rx(r"(chmod\s+-?R?\s*0?777|0o777|S_IWOTH)")),
    # --- more defensive checks ---
    Rule("Credentials in a URL", "high",
         "Don't put a username:password in a URL; it leaks in logs and history. Use a secrets store.",
         _rx(r"[a-z][a-z0-9+.\-]*://[^/\s:@'\"]+:[^/\s@'\"]+@")),
    Rule("Server-side template injection risk", "high",
         "Never build a template from user input; use render_template with a fixed file and pass data as variables.",
         _rx(r"render_template_string\s*\("), (".py",)),
    Rule("Insecure temp file (mktemp)", "med",
         "tempfile.mktemp is racy; use tempfile.mkstemp or NamedTemporaryFile instead.",
         _rx(r"tempfile\.mktemp\s*\("), (".py",)),
    Rule("Weak randomness for a secret", "med",
         "Don't use the `random` module for tokens/passwords/keys; use the `secrets` module (secrets.token_hex).",
         _rx(r"random\.(random|randint|choice|randrange|getrandbits)\s*\(.*(token|password|passwd|secret|key|salt|otp|nonce|session)"),
         (".py",)),
    Rule("Insecure cookie flags", "med",
         "Set cookies with Secure and HttpOnly (and SameSite); don't disable them.",
         _rx(r"(secure|httponly)\s*=\s*False")),
    Rule("CSRF protection disabled", "high",
         "Keep CSRF protection on for state-changing routes; don't exempt or disable it globally.",
         _rx(r"(csrf_exempt|CSRF_ENABLED\s*=\s*False|WTF_CSRF_ENABLED\s*=\s*False|@csrf\.exempt)")),
    Rule("Obsolete TLS/SSL version", "high",
         "Don't pin SSLv2/SSLv3/TLSv1/TLSv1.1; require TLS 1.2+.",
         _rx(r"PROTOCOL_(SSLv2|SSLv3|TLSv1(_1)?)\b"), (".py",)),
    Rule("Weak cipher mode (ECB)", "high",
         "ECB leaks patterns; use an authenticated mode (AES-GCM) with a random IV/nonce.",
         _rx(r"MODE_ECB\b")),
    Rule("Django ALLOWED_HOSTS set to '*'", "med",
         "List the specific hostnames your site serves instead of '*'.",
         _rx(r"""ALLOWED_HOSTS\s*=\s*\[\s*['"]\*['"]""")),
    Rule("Possible XSS via innerHTML", "med",
         "Set text with textContent, or sanitize HTML before using innerHTML, to avoid XSS.",
         _rx(r"\.innerHTML\s*="), (".js", ".ts", ".jsx", ".tsx", ".html")),
    Rule("Possible XSS via document.write", "med",
         "Avoid document.write with any dynamic data; build DOM nodes and set textContent instead.",
         _rx(r"document\.write\s*\("), (".js", ".ts", ".jsx", ".tsx", ".html")),
    Rule("React dangerouslySetInnerHTML", "med",
         "Only use dangerouslySetInnerHTML with sanitized HTML (e.g. DOMPurify); prefer plain JSX.",
         _rx(r"dangerouslySetInnerHTML"), (".js", ".jsx", ".ts", ".tsx")),
    Rule("Shell command in Node", "high",
         "Use child_process.execFile/spawn with an args array, not exec with a built string.",
         _rx(r"child_process.*\.exec\s*\(|\brequire\(['\"]child_process['\"]\).*exec\s*\("),
         (".js", ".ts")),
    # --- more defensive checks (deeper coverage) ---
    Rule("Hardcoded framework SECRET_KEY", "high",
         "Load SECRET_KEY from the environment; a committed key lets anyone forge sessions/tokens. Rotate it.",
         _rx(r"""SECRET_KEY['"\]\s]*[:=,]\s*['"][^'"]{4,}['"]"""), (".py", ".cfg", ".ini", ".env"),
         unless=_rx(r"(os\.environ|getenv|config\(|example|changeme|your[_-]?|placeholder)")),
    Rule("Unpickling a file (code execution risk)", "high",
         "Don't unpickle data you didn't create; use json or a safe schema. pickle can execute code.",
         _rx(r"(pickle|cPickle)\.load\s*\("), (".py",)),
    Rule("Archive extractall (zip/tar slip)", "med",
         "Validate each member path before extracting (no '..' / absolute paths); extractall can overwrite files.",
         _rx(r"\.extractall\s*\("), (".py",)),
    Rule("Open redirect from user input", "med",
         "Don't redirect to a URL taken from the request; allow-list paths or use a fixed destination.",
         _rx(r"redirect\s*\(\s*request\.(args|form|values|GET|POST)"), (".py",)),
    Rule("SSRF: fetching a user-supplied URL", "high",
         "Don't fetch a URL built from request input; allow-list hosts and block internal addresses.",
         _rx(r"(requests\.(get|post|put|delete|head)|urlopen|urllib\.request\.urlopen)\s*\(\s*request\."),
         (".py",)),
    Rule("TLS cert checking disabled (CERT_NONE)", "high",
         "Don't set verify_mode = CERT_NONE in real use; verify the certificate.",
         _rx(r"CERT_NONE\b"), (".py",)),
    Rule("JWT signature verification disabled", "high",
         "Never set verify_signature False; always verify the token's signature when decoding.",
         _rx(r"""verify_signature['"]?\s*[:=]\s*(False|false)""")),
    Rule("Hardcoded Basic auth credentials", "high",
         "Don't embed a Basic auth header; load credentials from the environment or a secrets store.",
         _rx(r"""Authorization['"]?\s*[:,]\s*['"]Basic\s+[A-Za-z0-9+/=]{8,}['"]""")),
    Rule("Weak/broken cipher (DES/RC4)", "high",
         "DES and RC4 are broken; use AES-GCM (authenticated) with a random nonce.",
         _rx(r"\b(DES|RC4|ARC4)\b")),
    Rule("Math.random() for a secret/token", "med",
         "Math.random() isn't cryptographically secure; use crypto.getRandomValues / crypto.randomUUID.",
         _rx(r"Math\.random\s*\(\s*\).*(token|password|secret|key|salt|otp|nonce|session|id)"),
         (".js", ".ts", ".jsx", ".tsx")),
    Rule("Flask/Jinja autoescape turned off", "high",
         "Don't disable autoescaping; keep it on and only mark truly-safe, sanitized strings.",
         _rx(r"autoescape\s*=\s*False"), (".py", ".html")),
    Rule("Possible path traversal (open with request input)", "high",
         "Don't build a file path from request input; use a safe base dir and reject '..' / absolute paths.",
         _rx(r"open\s*\(\s*.*request\.(args|form|values|GET|POST)"), (".py",)),
    # --- outdated / vulnerable COMPONENTS (OWASP A06): the class behind many real-world breaches ---
    Rule("Outdated jQuery (1.x/2.x — known XSS)", "med",
         "Upgrade to jQuery 3.5+; 1.x/2.x have known XSS and prototype-pollution issues (CVE-2019-11358, …).",
         _rx(r"jquery[-/@]?(1|2)\.\d+(\.\d+)?(\.min)?\.js|jquery@[12]\."), (".html", ".htm", ".js", ".ts")),
    Rule("End-of-life AngularJS (1.x)", "high",
         "AngularJS 1.x is end-of-life and no longer patched; migrate to a supported framework.",
         _rx(r"angularjs|angular@1\.|angular/1\.|angular\.?js['\"?]"), (".html", ".htm", ".js", ".ts")),
    Rule("End-of-life Bootstrap (<= 3)", "low",
         "Bootstrap 3 and earlier are unsupported; upgrade to a maintained version.",
         _rx(r"bootstrap[-/@]?[0-3]\.\d"), (".html", ".htm", ".js", ".css")),
    Rule("Outdated / vulnerable WordPress core", "high",
         "This WordPress version is old and has public exploits; update core, themes and plugins now.",
         _rx(r"""\$wp_version\s*=\s*['"]([0-5])\.\d"""), (".php",)),
    Rule("Vulnerable Log4j (2.0–2.14 — Log4Shell)", "high",
         "Upgrade Log4j to 2.17.1+; 2.0–2.14 are vulnerable to Log4Shell (CVE-2021-44228).",
         _rx(r"log4j-core\D{0,8}2\.(\d|1[0-4])(\.\d+)?\b"),
         (".xml", ".gradle", ".kts", ".properties", ".txt", ".lock")),
    Rule("End-of-life Python 2", "med",
         "Python 2 is end-of-life and unpatched since 2020; move to Python 3.",
         _rx(r"#!.*\bpython2(\.\d)?\b"), (".py", ".sh", ".bash")),
]

# A dependency file is 'unpinned' when a requirement has no exact version — a supply-chain / reproducibility risk.
_REQ_UNPINNED = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*(?:$|[>~<!].*|\*)")


def scan_text(text: str, filename: str = "") -> list[Finding]:
    """Run the defensive pattern checks over one file's text. Read-only; returns findings with line numbers."""
    suffix = Path(filename).suffix.lower()
    name = Path(filename).name.lower()
    findings: list[Finding] = []
    lines = text.splitlines()
    for i, line in enumerate(lines, 1):
        if len(line) > 2000:  # minified/data line — skip to avoid noise
            continue
        for rule in RULES:
            if rule.suffixes and suffix not in rule.suffixes:
                continue
            if rule.pattern.search(line) and not (rule.unless and rule.unless.search(line)):
                findings.append(Finding(rule.name, rule.severity, rule.fix, filename, i))
    findings.extend(_scan_dependencies(name, lines, filename))
    return findings


def _scan_dependencies(name: str, lines: list[str], filename: str) -> list[Finding]:
    out = []
    if name in ("requirements.txt", "requirements.in"):
        for i, line in enumerate(lines, 1):
            s = line.strip()
            if not s or s.startswith("#") or "==" in s or "@" in s:
                continue
            if _REQ_UNPINNED.match(s):
                out.append(Finding("Unpinned dependency", "low",
                                   "Pin to an exact version (pkg==x.y.z) so builds are reproducible and "
                                   "a malicious release can't slip in.", filename, i))
    out.extend(_scan_components(name, lines, filename))
    return out


# End-of-life / unsupported base images — software built on these inherits unpatched OS/runtime CVEs.
_EOL_BASE = re.compile(
    r"^\s*FROM\s+(?:php:(?:[1-7]\.|5|7)|python:2|python:3\.[0-7]\b|node:(?:[0-9]|1[0-5])\b|"
    r"ubuntu:(?:1[0-8]|[0-9])\.|debian:(?:[0-9]|10)\b|centos:[0-8]\b|alpine:3\.(?:[0-9]|1[0-2])\b)", re.I)


def _scan_components(name: str, lines: list[str], filename: str) -> list[Finding]:
    """Flag outdated or unsupported runtimes/components in manifests — OWASP A06, a very common breach cause."""
    out: list[Finding] = []
    for i, line in enumerate(lines, 1):
        if name == "dockerfile" and _EOL_BASE.search(line):
            out.append(Finding("End-of-life base image", "high",
                               "Build on a currently-supported base image (newer PHP/Python/Node/OS tag); "
                               "EOL images stop getting security patches and carry known CVEs.", filename, i))
        if name == "composer.json" and re.search(r'"php"\s*:\s*"[^"]*(?:5\.|7\.)', line):
            out.append(Finding("Outdated PHP required (EOL)", "high",
                               "PHP 5 and 7 are end-of-life; require PHP 8.1+ and update the code to match. "
                               "Old PHP is a top real-world breach vector.", filename, i))
        if name == "package.json" and re.search(r'"node"\s*:\s*"[^"]*\b(?:[0-9]|1[0-5])\b', line):
            out.append(Finding("Outdated Node.js engine (EOL)", "med",
                               "Node 15 and below are end-of-life; target an active LTS (18/20+).", filename, i))
    return out


def scan_path(path: str | Path, config) -> list[Finding]:
    """Audit a file or folder (scope-checked). Reads only; skips binaries, huge files and dependency dirs."""
    dirs = allowed_dirs(config)
    root = check_scope(path, dirs)
    files = [root] if root.is_file() else [p for p in root.rglob("*")
                                           if p.is_file() and not (SKIP_DIRS & set(p.parts))]
    findings: list[Finding] = []
    for f in files:
        if f.suffix.lower() not in CODE_SUFFIXES and f.name.lower() not in COMPONENT_FILES:
            continue
        try:
            if f.stat().st_size > MAX_FILE_BYTES:
                continue
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # unreadable or binary — skip quietly
        rel = str(f.relative_to(root)) if root.is_dir() else f.name
        for found in scan_text(text, rel):
            findings.append(found)
    return sort_findings(findings)


# --------------------------------------------------------------------------- optional local-model pass
MODEL_REVIEW_CONTRACT = """

You are doing a DEFENSIVE security review of the code above (code the owner wrote and wants hardened).
List only real, specific weaknesses you can point to. For each, give a concrete fix. Do NOT suggest or
describe how to attack anything. End with exactly one line:
REVIEW_JSON: [{"issue": "...", "severity": "high|med|low", "fix": "..."}]  (use [] if you find nothing)."""


def model_findings(text: str, filename: str, generate, reference_note: str = "") -> list[Finding]:
    """Ask the LOCAL coder model for context-aware defensive findings. generate(prompt)->text. Offline.

    ``reference_note`` is relevant passages from Builder's security reference library (OWASP, etc.), so a
    model that has 'read' them gives sharper, better-grounded findings.
    """
    refs = f"\n\nRelevant security knowledge you've studied (apply it):\n{reference_note}\n" if reference_note else ""
    prompt = f"File: {filename}\n\n```\n{text[:8000]}\n```{refs}" + MODEL_REVIEW_CONTRACT
    try:
        reply = generate(prompt)
    except Exception:  # noqa: BLE001 — a model hiccup must never break the pattern scan
        return []
    return _parse_review(reply, filename)


def _parse_review(text: str, filename: str) -> list[Finding]:
    import json
    m = re.search(r"REVIEW_JSON:\s*(\[.*\])\s*$", text, re.S)
    if not m:
        return []
    try:
        raw = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return []
    out = []
    for r in raw if isinstance(raw, list) else []:
        if not isinstance(r, dict) or not str(r.get("issue", "")).strip():
            continue
        sev = str(r.get("severity", "med")).lower()
        out.append(Finding(str(r["issue"]).strip(), sev if sev in SEVERITIES else "med",
                           str(r.get("fix", "")).strip(), filename, 0))
    return out


# --------------------------------------------------------------------------- merge + sort
def sort_findings(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (SEVERITY_RANK.get(f.severity, 3), f.file, f.line))


def dedup(findings: list[Finding]) -> list[Finding]:
    seen, out = set(), []
    for f in findings:
        key = (f.issue.lower(), f.file, f.line)
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def review(path: str | Path, config, generate=None, use_model: bool = True, memory=None) -> list[Finding]:
    """Full defensive review of a file/folder: the always-on pattern scan, plus the local model if available.

    ``generate`` is a ``prompt -> text`` function (the local coder model). Pass ``use_model=False`` or no
    ``generate`` to run the pattern scan alone — which is the fully-offline, no-model path. ``memory`` (when
    given) supplies relevant passages from Builder's security reference library to ground the model pass.
    """
    findings = scan_path(path, config)
    if use_model and generate is not None:
        dirs = allowed_dirs(config)
        root = check_scope(path, dirs)
        targets = [root] if root.is_file() else [p for p in root.rglob("*")
                                                 if p.is_file() and p.suffix.lower() in CODE_SUFFIXES
                                                 and not (SKIP_DIRS & set(p.parts))][:8]  # cap model passes
        for f in targets:
            try:
                text = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            rel = str(f.relative_to(root)) if root.is_dir() else f.name
            ref_note = ""
            if memory is not None:
                try:
                    ref_note = memory.reference_note(f"security vulnerability review {rel} {text[:200]}",
                                                     max_chars=1200)
                except Exception:  # noqa: BLE001
                    ref_note = ""
            findings.extend(model_findings(text, rel, generate, reference_note=ref_note))
    return sort_findings(dedup(findings))
