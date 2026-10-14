import re
from datetime import date

from builder.research import (Researcher, _short, grade, quote_found, skeptic_checks, unsupported_numbers)

from .conftest import PAGES, RoutingLLM


def _plan(prompt):
    return {"restated": "How fast are sneaker resale prices rising?",
            "questions": [{"q": "How much did resale sneaker prices change in 2025?", "dimension": "margins",
                           "queries": [{"q": "sneaker resale prices 2025", "where": "web"}]},
                          {"q": "What academic work studies sneaker resale?", "dimension": "demand",
                           "queries": [{"q": "sneaker resale study", "where": "papers"}]}]}


def _extract(prompt):
    """Pretend to read: quote the real page, except invent a quote for the blog; report graph + leads."""
    sid = int(re.search(r"\[S(\d+)\]", prompt).group(1))
    qids = [int(x) for x in re.findall(r"^(\d+): ", prompt, re.M)]
    if "blog.example.com" in prompt:
        return {"facts": [{"fact": "prices rose", "quote": "prices tripled overnight everywhere", "answers": qids[:1]}]}
    for url, html in PAGES.items():
        if url.split("/")[2].removeprefix("www.") in prompt:
            text = re.sub("<[^>]+>", " ", html)
            sentence = re.search(r"(The Bureau|Analysts|New data)[^.]+", text).group(0)
            return {"facts": [{"fact": f"fact from S{sid}", "quote": sentence[:120], "answers": qids[:1]}],
                    "entities": [{"name": "StockX", "type": "company"}, {"name": "Nike", "type": "company"}],
                    "relations": [{"subject": "StockX", "predicate": "partners with", "object": "Nike", "date": "2025"}],
                    "terms": [{"term": "deadstock", "definition": "unworn sneakers in original packaging"}],
                    "leads": ["How do StockX fees affect resale margins?"]}
    return {"facts": []}


def _ids(prompt):
    return {d: int(i) for i, d in re.findall(r"\[S(\d+)\] tier \d, [^,]+, (\S+):", prompt)}


def _synth(prompt):
    ids = _ids(prompt)
    return {
        "answer": f"Prices rose about 12% [S{ids['bls.gov']}].",
        "key_findings": [
            {"claim": "Resale prices rose 12% in 2025", "source_ids": [ids["bls.gov"]], "confidence": 0.99},
            {"claim": "Market is ~$10B", "source_ids": [f"S{ids['reuters.com']}"], "confidence": 0.9},
            {"claim": "Everyone gets rich", "source_ids": [], "confidence": 0.9},
            {"claim": "Made up source", "source_ids": [999], "confidence": 0.9},
        ],
        "risks": ["Hype cycles"], "assumptions": ["US market only"],
        "startup_costs": [{"item": "Inventory", "cost": 500, "source_ids": []}, {"item": "Bad", "cost": "lots"}],
        "scenarios": [{"name": "expected", "units_per_month": 10, "price": 200, "unit_cost": 150,
                       "fees_pct": 10, "fixed_costs_per_month": 50}],
        "ranges": {"price": [180, 200, 230], "unit_cost": [140, 150, 170], "units_per_month": [4, 10, 20],
                   "fees_pct": [9, 13], "startup_costs": [400, 700]},
        "verdict": "maybe — margins are thin",
    }


def _skeptic_queries(prompt):
    first = int(re.search(r"^(\d+): Resale prices", prompt, re.M).group(1))
    return {"queries": {str(first): [{"q": "sneaker resale prices fell", "where": "web"}]}}


def _skeptic_read(prompt):
    if "example.org" in prompt:
        return {"stance": "contradicts", "quote": "New data from 2026 shows resale sneaker prices fell 4 percent",
                "reason": "newer data", "methodology_concerns": "none"}
    return {"stance": "irrelevant"}


def _gaps(prompt):
    return {"followups": [{"q": "How do StockX seller fees change margins?", "parent": None,
                           "queries": [{"q": "StockX seller fee schedule", "where": "web"}]}]}


def full_llm():
    return RoutingLLM(plan=_plan, extract=_extract, synth=_synth, gaps=_gaps,
                      skeptic_queries=_skeptic_queries, skeptic_read=_skeptic_read)


# ------------------------------------------------------------------ units
def test_quote_found():
    text = "The Bureau reports that resale sneaker prices rose 12 percent in 2025."
    assert quote_found("resale sneaker prices rose 12 percent", text)
    assert quote_found("Resale  sneaker prices, rose 12 percent!", text)
    assert not quote_found("prices tripled overnight everywhere", text)
    assert not quote_found("12", text)


def test_grade_rules():
    srcs = {1: {"domain": "bls.gov", "tier": 1}, 2: {"domain": "reuters.com", "tier": 2},
            3: {"domain": "blog.com", "tier": 3}, 4: {"domain": "reddit.com", "tier": 4}}
    assert grade([], srcs, set()) == "assumption"
    assert grade([99], srcs, set()) == "assumption"
    assert grade([1], srcs, {1}) == "verified"           # primary with a checked quote
    assert grade([1], srcs, set()) == "reported"         # primary but quote unchecked
    assert grade([2, 3], srcs, {3}) == "verified"        # two independent domains, a quote that checks out
    assert grade([2, 3], srcs, set()) == "reported"      # ...but no quote could be confirmed
    assert grade([3, 4], srcs, {3, 4}) == "reported"     # tier 4 doesn't count toward corroboration
    assert grade([2, 3], srcs, {3}, conflicting=True) == "disputed"
    # Regression (live run): the model cited a paper that returned 403 and yielded no evidence.
    # It must not count as the "second independent source".
    assert grade([1, 3], srcs, {3}, evidence={3}) == "reported"
    assert grade([1], srcs, {1}, evidence=set()) == "assumption"


def test_skeptic_checks():
    today = date(2026, 10, 1)
    old_blog = {"domain": "blog.com", "tier": 3, "published": "2019-05-01"}
    assert skeptic_checks("x", [old_blog], True, today) == [
        "single source", "possibly outdated (newest source 2019)", "no primary or reputable source"]
    self_report = {"domain": "acme.com", "tier": 3, "published": "2026"}
    assert any("conflict of interest" in f for f in skeptic_checks("Acme grew sales 300%", [self_report], True, today))
    retracted = {"domain": "doi.org", "tier": 4, "tier_label": "RETRACTED — do not rely on this", "published": "2025"}
    assert "relies on a retracted paper" in skeptic_checks("x", [retracted], True, today)
    good = [{"domain": "bls.gov", "tier": 1, "published": "2026"}, {"domain": "reuters.com", "tier": 2, "published": "2025"}]
    assert skeptic_checks("Prices rose", good, True, today) == []
    assert "supporting quote not found in the source" in skeptic_checks("x", good, False, today)


def test_unsupported_numbers_are_flagged():
    ev = "The Pi 5 drew 7.2 watts under load and 2,700 mW idle in 2024."
    assert unsupported_numbers("It uses 7.2 W [S3], about 1.5 W idle, 2700 mW, since 2024.", ev) == ["1.5"]
    assert unsupported_numbers("Uses 7.2 W [S12].", ev) == []


def test_choose_prefers_credible_and_diverse_and_unread():
    hits = [{"url": f"u{i}", "domain": d, "tier": t, "rank": i}
            for i, (d, t) in enumerate([("a.com", 3), ("a.com", 3), ("a.com", 3), ("gov.gov", 1), ("b.org", 2)])]
    assert [h["domain"] for h in Researcher.choose(hits, 4)] == ["gov.gov", "b.org", "a.com", "a.com"]
    assert [h["url"] for h in Researcher.choose(hits, 4, {"u3"})] == ["u4", "u0", "u1"]


def test_short_query():
    assert _short("What is the evidence on demand for: mobile detailing in Texas?") == "demand mobile detailing Texas"


# ------------------------------------------------------------- pipeline
def test_investigate_end_to_end(tb, config):
    llm = full_llm()
    logs = []
    r = Researcher(tb, llm, config, log=logs.append).investigate("Are sneaker resale prices rising?", "standard", "plan")
    d = r["data"]
    db = tb.db

    # A. recursive planning: business dimensions are all covered; gaps add follow-ups in round 2
    dims = {q["dimension"] for q in d["questions"]}
    assert {"demand", "competitors", "customer complaints", "costs", "willingness to pay"} <= dims
    assert any(q["origin"] == "gap" and "StockX seller fees" in q["text"] for q in d["questions"])
    assert "gaps" in llm.kinds()
    # B. multi-hop: discovered companies become new questions with filings searches
    assert any(q["origin"] == "hop" for q in d["questions"])
    assert ("filings", '"StockX"') in tb.web.searches or ("filings", '"Nike"') in tb.web.searches
    assert any(r_["predicate"] == "partners_with" for r_ in d["graph"])
    assert db.neighbors("StockX")["relations"]
    # C. tables from documents are saved as CSV and listed
    assert d["tables"] and open(db.get_table(d["tables"][0]["table_id"])["csv_path"]).read().startswith("Year,Index")
    # grading by code, before and after the skeptic
    by_claim = {f["claim"]: f for f in d["findings"]}
    assert by_claim["Market is ~$10B"]["status"] == "reported"
    assert by_claim["Everyone gets rich"]["status"] == "assumption"
    assert by_claim["Made up source"]["source_ids"] == []
    # D. the skeptic found newer contradicting data → disputed, with the contradicting source recorded
    rising = by_claim["Resale prices rose 12% in 2025"]
    assert rising["status"] == "disputed" and rising["contradicting"]
    assert any("contradicting evidence" in c for c in rising["checks"])
    assert [e["stance"] for e in db.evidence(rising["id"])].count("contradicts") == 1
    assert db.finding_history(rising["id"])[-1]["reason"].startswith("skeptic review")
    # the invented blog quote is caught
    assert d["quotes_checked"].split("/")[0] != d["quotes_checked"].split("/")[1]
    # F. plan numbers computed by code, including the Monte Carlo stress test
    assert d["startup_total"] == 500 and [c["item"] for c in d["startup_costs"]] == ["Inventory"]
    assert d["scenarios"][0]["profit_per_month"] == 250
    assert 0 <= d["monte_carlo"]["prob_monthly_loss"] <= 1
    # memory: glossary, useful queries, investigation record
    assert db.glossary("deadstock")
    assert db.useful_queries()
    inv = db.get_investigation(r["investigation_id"])
    assert inv["status"] == "done" and inv["report_id"] == r["report_id"]
    md = r["markdown"]
    for heading in ("## Skeptic's review", "## Stress test (Monte Carlo, computed)", "## Connections discovered",
                    "## Tables extracted", "## Sources"):
        assert heading in md
    # web text reaches the model fenced as untrusted data
    assert all("<<<SOURCE" in p and "untrusted" in p for p in llm.prompts if "careful analyst" in p)
    assert any("skipped" in m for m in logs)  # wikipedia rate limit doesn't sink the research


def test_second_investigation_recalls_the_first(tb, config):
    Researcher(tb, full_llm(), config).investigate("Are sneaker resale prices rising?", "quick")
    tb.memory.learn("Always show prices in a table", "preference")
    llm = full_llm()
    Researcher(tb, llm, config).investigate("Is sneaker resale a good side hustle?", "quick")
    plan_prompt = next(p for p in llm.prompts if "Break this question" in p)
    assert "Always show prices in a table" in plan_prompt         # lessons from the boss
    assert "Resale prices rose 12% in 2025" in plan_prompt        # previous findings (keyword fallback)
    assert "Searches that produced evidence before" in plan_prompt  # useful queries


def test_template_echo_is_ignored_and_search_widens(tb, config):
    echo = {"questions": [{"q": "search engine query", "queries": [{"q": "search engine query", "where": "web|papers"}]}]}
    llm = RoutingLLM(plan=lambda p: echo)
    Researcher(tb, llm, config).investigate("Is X rising?", "quick")
    assert ("web", "search engine query") not in tb.web.searches
    assert any("Is X rising?" in q for _, q in tb.web.searches)


def test_no_evidence_means_no_answer_from_memory(tb, config):
    tb.web.search = lambda q, n=8: []
    tb.web.papers = lambda q, n=6: []
    llm = RoutingLLM(synth=lambda p: {"answer": "made up", "key_findings": [{"claim": "made up"}]})
    r = Researcher(tb, llm, config).investigate("Anything?", "quick")
    assert "synth" not in llm.kinds()
    assert r["data"]["answer"] == "" and r["data"]["findings"] == []
    assert "not enough to answer" in r["markdown"] and "SCHOLAR_SEARXNG_URL" in r["markdown"]
    assert tb.db.questions(r["investigation_id"], "unanswered")


def test_failed_investigation_is_marked(tb, config):
    def boom(prompt):
        raise RuntimeError("model crashed")
    try:
        Researcher(tb, RoutingLLM(plan=boom), config).investigate("x?", "quick")
    except RuntimeError:
        pass
    assert tb.db.investigations()[0]["status"] == "failed"


def test_finding_ids_in_any_form():
    from builder.research import _ids
    assert _ids([7, "8", "S9", "F12", "[S3]", "f 4", "x", None]) == [7, 8, 9, 12, 3, 4]
