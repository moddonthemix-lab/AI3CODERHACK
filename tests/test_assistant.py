import json

from builder import tools
from builder.assistant import Assistant
from builder.config import Config


class FakeLLM:
    """Replays scripted assistant messages and records what it was sent."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def chat(self, messages, tools=None, json_mode=False, schema=None, think=None, max_tokens=None):
        self.calls.append(json.loads(json.dumps(messages)))
        return self.script.pop(0)


def tool_call(name, **args):
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]}


def test_tool_loop_searches_reads_and_records(tb):
    llm = FakeLLM([
        tool_call("web_search", query="sneaker resale"),
        tool_call("read_source", source_id=1),
        tool_call("record_finding", claim="Prices rose 12%", status="reported", source_ids=[1], confidence=0.7),
        {"role": "assistant", "content": "Prices rose 12% in 2025 [S1] — reported, single source."},
    ])
    a = Assistant(tb, llm, Config())
    reply = a.ask("are sneaker prices rising?")
    assert reply.startswith("Prices rose")
    read_result = json.loads(llm.calls[2][-1]["content"])
    assert read_result["tier"] == 1 and "untrusted" in read_result["note"]
    assert tb.db.findings()[0]["source_ids"] == [1]
    assert [m["role"] for m in a.history] == ["user", "assistant"]


def test_system_prompt_has_persona_library_and_sender(tb):
    tb.db.remember("Boss wants low-startup-cost ideas")
    tb.db.add_finding("Sneaker resale margins are thin", "reported", 0.6, "sneakers", [4])
    llm = FakeLLM([{"role": "assistant", "content": "ok"}])
    Assistant(tb, llm, Config()).ask("are sneaker resale margins good? low-startup-cost?", from_agent="baby")
    system = llm.calls[0][0]["content"]
    assert "Researcher" in system and "patient" in system and "nerdy" in system
    assert "F1 [reported, 60%] Sneaker resale margins are thin (sources: S4" in system
    assert "low-startup-cost" in system
    assert "Baby (the curious youngest)" in system


def test_verified_without_sources_is_downgraded(tb):
    out = json.loads(tools.call(tb, "record_finding", {"claim": "x", "status": "verified"}))
    assert out["status"] == "assumption"


def test_tool_errors_are_returned_not_raised(tb):
    assert "error" in json.loads(tools.call(tb, "nope", {}))
    assert "error" in json.loads(tools.call(tb, "wikipedia", {"query": "x"}))  # rate-limited fake
    assert "error" in json.loads(tools.call(tb, "read_source", {"url": "https://nowhere.example/"}))
    assert "error" in json.loads(tools.call(tb, "calculate", {"expression": "open('x')"}))
    assert "error" in json.loads(tools.call(tb, "remember", "{bad json"))
    assert json.loads(tools.call(tb, "calculate", '{"expression": "2*21"}'))["result"] == 42


def test_every_tool_has_valid_schema():
    for schema in tools.schemas():
        fn = schema["function"]
        assert fn["name"] and fn["description"]
        assert set(fn["parameters"]["required"]) <= set(fn["parameters"]["properties"])


def test_new_tools(tb):
    tb.db.relate("StockX", "partners_with", "Nike", 1)
    assert json.loads(tools.call(tb, "explore_entity", {"name": "stockx"}))["relations"][0]["object"] == "Nike"
    r = json.loads(tools.call(tb, "read_source", {"url": "https://www.bls.gov/sneakers"}))
    assert r["tables"] and r["parser"] == "html"
    tid = r["tables"][0]["table_id"]
    out = json.loads(tools.call(tb, "run_python", {
        "code": "import csv\nrows=list(csv.DictReader(open('T%d.csv')))\nprint(int(rows[1]['Index'])/int(rows[0]['Index']))" % tid,
        "table_ids": [tid]}))
    assert out["ok"] and out["stdout"].strip() == "1.12"
    st = json.loads(tools.call(tb, "stress_test", {"price": [180, 220], "unit_cost": [150], "units_per_month": [5, 10, 20]}))
    assert "prob_monthly_loss" in st
    lib = json.loads(tools.call(tb, "search_library", {"query": "sneaker prices"}))
    assert set(lib) == {"findings", "passages", "past_investigations", "glossary", "lessons"}


def test_chat_sends_only_the_tools_a_message_needs():
    names = lambda text: {s["function"]["name"] for s in tools.schemas(text)}  # noqa: E731
    core = set(tools.CORE)
    assert names("hello there") == core
    assert {"search_filings", "explore_entity"} <= names("Who supplies Tesla? Check their 10-K filings")
    assert "run_python" in names("compute the median of this table") and "search_filings" not in names("hi")
    assert len(tools.schemas("hello")) < len(tools.schemas())
