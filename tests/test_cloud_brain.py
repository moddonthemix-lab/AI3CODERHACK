"""Part 2 (ADDING_CLOUD_AI): the family cloud brain, exercised end-to-end through the connector.

These tests use the REAL FamilyBrain (not a scripted stub) with a fake Anthropic client and a fake hub
that answers budget checks. They prove the wiring the handoff doc asks for:

- research goes to Claude Sonnet WITH web search, its sources land on findings, and the cost is reported;
- a question goes to Claude Haiku (no web tool), tagged ☁️;
- a $0 / used-up budget parks research as "wait" (it never spends);
- a 🏠 local-only room never touches the cloud, even with budget to spare;
- a cloud refusal is declined and the boss is told (never retried locally);
- the API key is never required in code: a client passed in is enough, and nothing leaks it.
"""

import pytest

from family_brain import FamilyBrain
from builder.config import Config
from builder.connector import ScholarConnector
from builder.db import Store
from builder.memory import ResearchMemory


# --------------------------------------------------------------------------- fake Anthropic SDK
class _Usage:
    def __init__(self, web=0):
        self.input_tokens, self.output_tokens = 1200, 300
        self.cache_creation_input_tokens, self.cache_read_input_tokens = 0, 0
        self.server_tool_use = type("S", (), {"web_search_requests": web})() if web else None


class _Text:
    type = "text"

    def __init__(self, text):
        self.text = text


class _SearchResult:
    def __init__(self, url):
        self.url = url


class _SearchBlock:
    type = "web_search_tool_result"

    def __init__(self, urls):
        self.content = [_SearchResult(u) for u in urls]


class _Resp:
    def __init__(self, model, content, stop_reason="end_turn", explanation=""):
        self.model, self.content, self.stop_reason = model, content, stop_reason
        self.usage = _Usage(web=sum(len(b.content) for b in content if isinstance(b, _SearchBlock)))
        self.stop_details = type("D", (), {"explanation": explanation})() if explanation else None


class FakeAnthropic:
    """Mimics just enough of anthropic.Anthropic for FamilyBrain._cloud. Records every call."""

    def __init__(self, text="", urls=(), stop_reason="end_turn", explanation=""):
        self.text, self.urls, self.stop_reason, self.explanation = text, list(urls), stop_reason, explanation
        self.calls = []
        self.messages = type("M", (), {"create": self._create})()
        self.beta = type("B", (), {"messages": type("M", (), {"create": self._create})()})()

    def _create(self, **kw):
        self.calls.append(kw)
        content = [_Text(self.text)]
        if self.urls:
            content.append(_SearchBlock(self.urls))
        return _Resp(kw.get("model"), content, self.stop_reason, self.explanation)


# --------------------------------------------------------------------------- fake hub (with a budget)
class FakeHub:
    def __init__(self, allowed=True, remaining=5.0, threads=None):
        self.allowed, self.remaining, self._threads = allowed, remaining, threads or {}
        self.brain = None
        self.results, self.findings, self.posts, self.asks, self.updates, self.usage = [], [], [], [], [], []

    def me(self):
        return {"member": {"id": "scholar", "name": "Scholar"}, "channels": ["discoveries", "business"], "unread": 0}

    def memories(self):
        return []

    def budget_check(self, estimate_usd):
        return {"allowed": self.allowed, "remaining": self.remaining,
                "reason": "" if self.allowed else "the cloud budget is $0 or used up"}

    def report_usage(self, model, input_tokens, output_tokens, cost_usd, note=None):
        self.usage.append({"model": model, "cost_usd": cost_usd, "note": note})

    def thread(self, mid):
        return {"root": self._threads.get(mid, {"id": mid, "type": "question", "channel": "general", "text": "?"})}

    def tasks(self, assignee=None, status=None):
        return []

    def update_task(self, tid, status, note=""):
        self.updates.append({"task": tid, "status": status, "note": note})

    def post_result(self, tid, summary, artifacts=None, files=None, channel="tasks"):
        self.results.append({"task": tid, "summary": summary, "artifacts": artifacts or [], "channel": channel})

    def post_finding(self, channel, claim, status, confidence, sources=None, reply_to=None, files=None):
        self.findings.append({"channel": channel, "claim": claim, "status": status,
                              "confidence": confidence, "sources": sources or []})

    def post(self, channel, text, reply_to=None, files=None):
        self.posts.append({"channel": channel, "text": text, "reply_to": reply_to})

    def ask(self, channel, text, to=None, reply_to=None):
        self.asks.append({"channel": channel, "text": text, "to": to})


@pytest.fixture
def db():
    return Store()


def make(hub, cloud, db, tmp_path, online=True):
    cfg = Config(home=tmp_path)
    cfg.ensure_home()
    brain = FamilyBrain(hub, system="You are Scholar.", cloud_client=cloud, internet_check=lambda: online)
    return ScholarConnector(hub, brain, db, ResearchMemory(db, None, cfg), cfg, log=lambda m: None)


RESEARCH = {"id": 7, "status": "open", "title": "studio backup market", "channel": "tasks",
            "details": "How do small studios back up sessions?"}
ANSWER = ('Most studios use external SSDs, then cloud [S1].\nFINDINGS_JSON: '
          '[{"claim":"Studios back up to external SSDs","status":"verified","confidence":0.8}]')


def test_cloud_research_uses_sonnet_with_web_search_and_reports_cost(db, tmp_path):
    hub = FakeHub(allowed=True, remaining=5.0)
    cloud = FakeAnthropic(text=ANSWER, urls=["https://www.sweetwater.com/x"])
    conn = make(hub, cloud, db, tmp_path)
    conn.work_on_task(RESEARCH, {"local_only": False})

    call = cloud.calls[0]
    assert call["model"] == "claude-sonnet-5-5"                    # research -> Sonnet
    assert call["tools"][0]["name"] == "web_search"               # with web search
    assert hub.brain == "cloud"                                   # the post is tagged ☁️
    assert hub.results and "external SSDs" in hub.results[0]["summary"]
    assert "FINDINGS_JSON" not in hub.results[0]["summary"]
    assert hub.findings[0]["sources"] == ["https://www.sweetwater.com/x"]   # cloud sources on the finding
    assert hub.usage and hub.usage[0]["cost_usd"] > 0            # the spend was reported to the hub
    # and mirrored into Scholar's own library, with the cloud source saved too
    local = db.findings()
    assert local and local[0]["claim"] == "Studios back up to external SSDs"
    assert db.get_source(local[0]["source_ids"][0])["domain"] == "sweetwater.com"


def test_chat_question_uses_haiku_without_web(db, tmp_path):
    hub = FakeHub(threads={5: {"id": 5, "type": "question", "channel": "general", "text": "How do studios back up?"}})
    cloud = FakeAnthropic(text="On external SSDs, then cloud.")
    conn = make(hub, cloud, db, tmp_path)
    conn.answer({"id": 5, "local_only": False})

    call = cloud.calls[0]
    assert call["model"] == "claude-haiku-5-5"                    # chat -> Haiku
    assert "tools" not in call                                    # Haiku gets no web search tool
    assert hub.brain == "cloud" and hub.posts[0]["reply_to"] == 5


def test_zero_budget_parks_research_without_spending(db, tmp_path):
    hub = FakeHub(allowed=False)                                  # $0 / used-up budget
    cloud = FakeAnthropic(text=ANSWER)
    conn = make(hub, cloud, db, tmp_path)
    conn.work_on_task(RESEARCH, {"local_only": False})

    assert cloud.calls == []                                      # never called the cloud
    assert hub.usage == [] and hub.results == []                 # nothing spent, nothing posted
    assert hub.updates[-1]["status"] == "blocked" and "Waiting" in hub.updates[-1]["note"]


def test_offline_parks_research_without_spending(db, tmp_path):
    hub = FakeHub(allowed=True, remaining=5.0)
    cloud = FakeAnthropic(text=ANSWER)
    conn = make(hub, cloud, db, tmp_path, online=False)           # no internet
    conn.work_on_task(RESEARCH, {"local_only": False})

    assert cloud.calls == [] and hub.updates[-1]["status"] == "blocked"


def test_local_only_room_never_touches_the_cloud(db, tmp_path):
    hub = FakeHub(allowed=True, remaining=5.0)                    # budget to spare...
    cloud = FakeAnthropic(text=ANSWER)
    conn = make(hub, cloud, db, tmp_path)
    conn.work_on_task(RESEARCH, {"local_only": True})            # ...but this room is 🏠 local-only

    assert cloud.calls == []                                      # cloud never called
    assert hub.brain != "cloud" and hub.usage == []             # not tagged ☁️, nothing spent


def test_cloud_refusal_is_declined_and_boss_told_not_retried(db, tmp_path):
    hub = FakeHub(allowed=True, remaining=5.0)
    cloud = FakeAnthropic(stop_reason="refusal", explanation="outside research policy")
    conn = make(hub, cloud, db, tmp_path)
    conn.work_on_task(RESEARCH, {"local_only": False})

    assert hub.updates[-1]["status"] == "declined"
    assert hub.asks and hub.asks[0]["to"] == "boss" and "7" in hub.asks[0]["text"]
    assert hub.results == [] and hub.findings == []              # not retried, nothing posted


def test_cloud_status_is_safe_and_reports_not_ready_without_the_sdk(db, tmp_path):
    # anthropic isn't installed in the test env -> not ready, and the probe must never raise or leak.
    hub = FakeHub(allowed=True, remaining=5.0)
    conn = make(hub, FakeAnthropic(), db, tmp_path)
    cs = conn.cloud_status()
    assert cs["sdk"] is False and cs["ready"] is False
    assert set(cs) >= {"sdk", "key", "online", "allowed", "ready"}
