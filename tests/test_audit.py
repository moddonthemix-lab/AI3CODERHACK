"""Part 4: Builder's defensive security review — pattern scan (offline), scope, and the optional model pass."""

import pytest

from builder import audit
from builder.config import Config
from builder.projects import ScopeError


def cfg_for(*dirs, home):
    return Config(home=home, code_dirs=[str(d) for d in dirs])


def issues(findings):
    return {f.issue for f in findings}


# --------------------------------------------------------------------------- pattern checks
def test_flags_common_weaknesses_with_severity():
    code = (
        'password = "hunter2hunter2"\n'
        'import hashlib\n'
        'h = hashlib.md5(pw).hexdigest()\n'
        'subprocess.run(cmd, shell=True)\n'
        'data = yaml.load(open("c.yml"))\n'
        'r = requests.get(url, verify=False)\n'
        'app.run(debug=True)\n'
        'cursor.execute("SELECT * FROM t WHERE x=" + x)\n'
    )
    f = audit.scan_text(code, "app.py")
    found = issues(f)
    assert "Hardcoded secret in source" in found
    assert "Weak hash for a password" in found
    assert "Shell command with shell=True" in found
    assert "Unsafe deserialization" in found
    assert "TLS certificate verification disabled" in found
    assert "Debug mode enabled" in found
    assert "SQL built by string formatting" in found
    # severities are attached
    sev = {x.issue: x.severity for x in f}
    assert sev["Shell command with shell=True"] == "high" and sev["Weak hash for a password"] == "med"
    # every finding carries a concrete fix and a line number
    assert all(x.fix for x in f) and all(x.line > 0 for x in f)


def test_clean_code_has_no_findings():
    code = (
        'import os\n'
        'password = os.environ["APP_PASSWORD"]\n'       # from env -> not flagged
        'data = yaml.safe_load(open("c.yml"))\n'         # safe_load -> not flagged
        'cursor.execute("SELECT * FROM t WHERE x=?", (x,))\n'  # parameterised -> not flagged
        'r = requests.get(url)\n'
    )
    assert audit.scan_text(code, "app.py") == []


def test_expanded_rules_catch_more_weaknesses():
    checks = {
        "Credentials in a URL": 'db = "postgres://admin:hunter2@db.example.com/app"',
        "Server-side template injection risk": "return render_template_string(request.args['tpl'])",
        "Insecure temp file (mktemp)": "path = tempfile.mktemp()",
        "Weak randomness for a secret": "token = random.randint(1000, 9999)  # session token",
        "Insecure cookie flags": "response.set_cookie('sid', v, secure=False)",
        "CSRF protection disabled": "WTF_CSRF_ENABLED = False",
        "Obsolete TLS/SSL version": "ctx = ssl.SSLContext(ssl.PROTOCOL_TLSv1)",
        "Weak cipher mode (ECB)": "cipher = AES.new(key, AES.MODE_ECB)",
    }
    for want, line in checks.items():
        assert want in issues(audit.scan_text(line, "app.py")), f"missed: {want}"


def test_web_xss_rules_fire_on_js_not_python():
    js = 'el.innerHTML = userInput;\ndocument.write(location.hash);\n'
    found = issues(audit.scan_text(js, "page.js"))
    assert "Possible XSS via innerHTML" in found and "Possible XSS via document.write" in found
    # those are web rules — they must not fire on a .py file
    assert audit.scan_text("el.innerHTML = x", "app.py") == []


def test_aws_key_and_private_key_are_high():
    f = audit.scan_text('k = "AKIAIOSFODNN7EXAMPLE"\n', "settings.py")
    assert any(x.issue == "AWS access key id in source" and x.severity == "high" for x in f)
    f2 = audit.scan_text("-----BEGIN RSA PRIVATE KEY-----\nabc\n", "id_rsa")
    assert any(x.issue == "Private key committed" and x.severity == "high" for x in f2)


def test_language_scoped_rules_do_not_fire_on_other_types():
    # shell=True is a Python rule; it shouldn't be matched inside, say, a .html file
    assert audit.scan_text("shell=True", "page.html") == []


def test_unpinned_dependency_is_low():
    f = audit.scan_text("requests\nflask==2.0\n# comment\nurllib3\n", "requirements.txt")
    unpinned = [x for x in f if x.issue == "Unpinned dependency"]
    assert {x.line for x in unpinned} == {1, 4} and all(x.severity == "low" for x in unpinned)


# --------------------------------------------------------------------------- scope + folders
def test_scan_path_reads_a_folder_and_respects_scope(tmp_path):
    allowed = tmp_path / "Projects"
    proj = allowed / "app"
    proj.mkdir(parents=True)
    (proj / "main.py").write_text('token = "abcdef123456"\n', encoding="utf-8")
    (proj / "ok.py").write_text("x = 1\n", encoding="utf-8")
    (proj / "node_modules").mkdir()
    (proj / "node_modules" / "evil.py").write_text('password = "leakleak"\n', encoding="utf-8")

    cfg = cfg_for(allowed, home=tmp_path)
    findings = audit.scan_path(proj, cfg)
    assert any(x.issue == "Hardcoded secret in source" for x in findings)
    assert all("node_modules" not in x.file for x in findings)     # dependency dirs are skipped

    with pytest.raises(ScopeError):
        audit.scan_path(tmp_path / "outside", cfg)                 # outside the allowed folders


def test_findings_sorted_most_serious_first(tmp_path):
    allowed = tmp_path / "Projects"
    proj = allowed / "app"
    proj.mkdir(parents=True)
    (proj / "a.py").write_text('app.run(debug=True)\neval(x)\n', encoding="utf-8")
    cfg = cfg_for(allowed, home=tmp_path)
    sev = [f.severity for f in audit.scan_path(proj, cfg)]
    assert sev == sorted(sev, key=lambda s: audit.SEVERITY_RANK[s])  # high before med/low


# --------------------------------------------------------------------------- optional model pass
def test_model_findings_parsed_and_merged():
    reply = ('I reviewed it.\nREVIEW_JSON: '
             '[{"issue":"Missing rate limiting on login","severity":"med","fix":"Add a limiter"}]')
    f = audit.model_findings("code", "auth.py", lambda p: reply)
    assert len(f) == 1 and f[0].issue == "Missing rate limiting on login" and f[0].severity == "med"
    # a model hiccup must never break the scan
    assert audit.model_findings("code", "auth.py", lambda p: (_ for _ in ()).throw(RuntimeError("boom"))) == []


def test_review_offline_without_a_model(tmp_path):
    allowed = tmp_path / "Projects"
    proj = allowed / "app"
    proj.mkdir(parents=True)
    (proj / "main.py").write_text('secret = "s3cr3ts3cr3t"\n', encoding="utf-8")
    cfg = cfg_for(allowed, home=tmp_path)
    findings = audit.review(proj, cfg, generate=None, use_model=False)   # fully offline, no model
    assert any(x.issue == "Hardcoded secret in source" for x in findings)
