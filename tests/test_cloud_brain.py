"""The family cloud brain, exercised end-to-end through Builder's connector.

These tests use the REAL FamilyBrain (not a scripted stub) with a fake Anthropic client and a fake hub
that answers budget checks. They prove the wiring the handoff doc asks for, as Builder:

- a build/review task goes to Claude Sonnet (job="code") WITHOUT web search, the cost is reported, the
  result lands on #technical-help, and security reviews are mirrored into Builder's store;
- a question goes to Claude Haiku (no web tool), tagged ☁️;
- coding does NOT park: with $0 budget or offline, Builder falls back to the LOCAL coder model and spends
  nothing (only research ever waits, and Builder doesn't research);
- a 🏠 local-only room never touches the cloud, even with budget to spare;
- a cloud refusal is declined and the boss is told (never retried locally);
- the API key is never required in code: a client passed in is enough, and nothing leaks it.
"""

import pytest

from family_brain import FamilyBrain, Thought
from builder.config import Config
from builder.connector import BuilderConnector
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


class _Resp:
    def __init__(self, model, content, stop_reason="end_turn", explanation=""):
        self.model, self.content, self.stop_reason = model, content, stop_reason
        self.usage = _Usage()
        self.stop_details = type("D", (), {"explanation": explanation})() if explanation else None


class FakeAnthropic:
    """Mimics just enough of anthropic.Anthropic for FamilyBrain._cloud. Records every call."""

    def __init__(self, text="", stop_reason="end_turn", explanation=""):
        self.text, self.stop_reason, self.explanation = text, stop_reason, explanation
        self.calls = []
        self.messages = type("M", (), {"create": self._create})()
        self.beta = type("B", (), {"messages": type("M", (), {"create": self._create})()})()

    def _create(self, **kw):
        self.calls.append(kw)
        return _Resp(kw.get("model"), [_Text(self.text)], self.stop_reason, self.explanation)


# --------------------------------------------------------------------------- fake hub (with a budget)
class FakeHub:
    def __init__(self, allowed=True, remaining=5.0, threads=None):
        self.allowed, self.remaining, self._threads = allowed, remaining, threads or {}
        self.brain = None
        self.results, self.findings, self.posts, self.asks, self.updates, self.usage = [], [], [], [], [], []

    def me(self):
        return {"member": {"id": "builder", "name": "Builder"}, "channels": ["technical-help"], "unread": 0}

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


def make(hub, cloud, db, tmp_path, online=True, local_text=None):
    cfg = Config(home=tmp_path)
    cfg.ensure_home()
    brain = FamilyBrain(hub, system="You are Builder.", cloud_client=cloud, internet_check=lambda: online)
    if local_text is not None:
        # Stand in for a running Ollama so the local-fallback path is exercised without a real model.
        brain._local = lambda prompt, system, history, reason: Thought(text=local_text, brain="local")
    return BuilderConnector(hub, brain, db, ResearchMemory(db, None, cfg), cfg, log=lambda m: None)


TASK = {"id": 7, "status": "open", "title": "login app", "channel": "technical-help",
        "details": "Build a tiny login page"}
ANSWER = ('Built a tiny Flask login app; its tests pass.\nREVIEW_JSON: '
          '[{"issue":"Secret key hardcoded","severity":"high","fix":"Load it from an env var"}]')


def test_cloud_code_uses_sonnet_without_web_and_reports_cost(db, tmp_path):
    hub = FakeHub(allowed=True, remaining=5.0)
    cloud = FakeAnthropic(text=ANSWER)
    conn = make(hub, cloud, db, tmp_path)
    conn.work_on_task(TASK, {"local_only": False})

    call = cloud.calls[0]
    assert call["model"] == "claude-sonnet-5-5"                   # code -> Sonnet
    assert "tools" not in call                                    # coding gets NO web search tool
    assert hub.brain == "cloud"                                   # the post is tagged ☁️
    assert hub.results and "Flask login" in hub.results[0]["summary"]
    assert hub.results[0]["channel"] == "technical-help"
    assert "REVIEW_JSON" not in hub.results[0]["summary"]
    assert hub.usage and hub.usage[0]["cost_usd"] > 0            # the spend was reported to the hub
    # the security review is mirrored into Builder's own store with severity + fix
    local = db.reviews()
    assert local and local[0]["issue"] == "Secret key hardcoded"
    assert local[0]["severity"] == "high" and "env var" in local[0]["fix"]


def test_chat_question_uses_haiku_without_web(db, tmp_path):
    hub = FakeHub(threads={5: {"id": 5, "type": "question", "channel": "technical-help", "text": "How do I hash a password?"}})
    cloud = FakeAnthropic(text="Use bcrypt or argon2 — never plain SHA.")
    conn = make(hub, cloud, db, tmp_path)
    conn.answer({"id": 5, "local_only": False})

    call = cloud.calls[0]
    assert call["model"] == "claude-haiku-5-5"                    # chat -> Haiku
    assert "tools" not in call                                    # Haiku gets no web search tool
    assert hub.brain == "cloud" and hub.posts[0]["reply_to"] == 5


def test_zero_budget_falls_back_to_local_without_spending(db, tmp_path):
    hub = FakeHub(allowed=False)                                  # $0 / used-up budget
    cloud = FakeAnthropic(text=ANSWER)
    conn = make(hub, cloud, db, tmp_path, local_text="Built it on the local coder model.\nREVIEW_JSON: []")
    conn.work_on_task(TASK, {"local_only": False})

    assert cloud.calls == []                                      # never called the cloud
    assert hub.usage == []                                        # nothing spent
    assert hub.results and hub.brain == "local"                  # coding does NOT park — it builds locally
    assert hub.updates[-1]["status"] == "in_progress"            # not blocked


def test_offline_falls_back_to_local_without_spending(db, tmp_path):
    hub = FakeHub(allowed=True, remaining=5.0)
    cloud = FakeAnthropic(text=ANSWER)
    conn = make(hub, cloud, db, tmp_path, online=False, local_text="Local build.\nREVIEW_JSON: []")
    conn.work_on_task(TASK, {"local_only": False})

    assert cloud.calls == [] and hub.brain == "local" and hub.results


def test_local_only_room_never_touches_the_cloud(db, tmp_path):
    hub = FakeHub(allowed=True, remaining=5.0)                    # budget to spare...
    cloud = FakeAnthropic(text=ANSWER)
    conn = make(hub, cloud, db, tmp_path, local_text="Local build.\nREVIEW_JSON: []")
    conn.work_on_task(TASK, {"local_only": True})                # ...but this room is 🏠 local-only

    assert cloud.calls == []                                      # cloud never called
    assert hub.brain != "cloud" and hub.usage == []             # not tagged ☁️, nothing spent


def test_cloud_refusal_is_declined_and_boss_told_not_retried(db, tmp_path):
    hub = FakeHub(allowed=True, remaining=5.0)
    cloud = FakeAnthropic(stop_reason="refusal", explanation="outside policy")
    conn = make(hub, cloud, db, tmp_path)
    conn.work_on_task(TASK, {"local_only": False})

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
