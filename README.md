# Scholar — the Researcher of the AI family

> *"A scholar and highly intelligent member — his knowledge goes beyond books and the internet.
> He does deep research, digs into the fine details, thinks before he speaks, sticks to the facts,
> sounds nerdy and professional at the same time, and uses credible sources whenever he can."*

Scholar is a private research analyst and business strategist that runs on your own machine
through [Ollama](https://ollama.com). No AI subscription, and your questions never leave the
MacBook. Only the searches and page reads he does go out to the web.

The goal isn't to retrieve *more* information. It's to find what other systems miss: he breaks
a question into dozens of smaller ones, follows connections from source to source, reads whole
documents (tables and appendices included), tries to disprove his own conclusions, checks the
numbers himself, and remembers what he learned for next time.

What a conversation looks like (illustrative — the figures are made up for the example):

```
you> is reselling refurbished iPhones still worth it in 2026?
scholar> Short answer: it can be, but the margin is narrower than the hype suggests.
         Refurbished-phone volumes grew 7% last year [S14] — verified (two independent sources).
         Typical wholesale-to-retail spread on grade-A iPhone 13s is about 18% before fees [S9] —
         reported, single source, so treat it as a lead. After eBay's 13.25% fee and shipping,
         unit_economics puts you at $21 profit per phone, 6.9% margin. Thin. My stress test gives
         a 31% chance of losing money in a given month. Shall I run a full `plan` on it?
```

## Where he fits in the family

| Member | Role | Repo |
|---|---|---|
| **Amo** | main everyday companion | [AMOAI](https://github.com/moddonthemix-lab/AMOAI) |
| **Scholar** | researcher — deep research, business & income analysis | **this repo** |
| Builder | coder & hacker — build and test | coming |
| Baby | the curious one — explores and learns (Scholar's study partner) | coming |
| Butler | oversees the family, suggests improvements | coming |

Scholar is forked from Amo's skeleton (stdlib Python, Ollama tool loop, SQLite, CLI, JSON API).
The rest of the family talks to him through his JSON API today, and through the Family Hub forum later.

## His knowledge, in four layers

Nothing here retrains the model. **Retrieval lets him consult knowledge; it doesn't change the
model's weights.** He "learns" by remembering and looking things up, which is also why you can
inspect and correct what he knows. Fine-tuning can come later, once there's a quality dataset
of his work and your corrections.

| Layer | What | Where |
|---|---|---|
| **1 · Live knowledge** | web search, SEC company filings (EDGAR), scholarly databases (OpenAlex 250M+ works, arXiv, Crossref), Wikipedia, any document or page, `.onion` sites over Tor (opt-in) | `web.py` |
| **2 · Personal library** | your PDFs, manuals, reports, books you have rights to, notes, plus every document he has read, chunked and searchable by meaning | `scholar ingest`, `memory.py` |
| **3 · Structured knowledge** | findings + their evidence (exact quotes, for *and* against), entities + relationships (the research graph), question trees, extracted tables, glossary, all with sources and dates. SQLite now; a graph DB only if research ever needs it | `db.py` |
| **4 · Experience** | lessons from **your** feedback (preferences, corrections, strategies), searches that produced evidence, hypotheses that failed | `scholar feedback`, `memory.py` |

Before every investigation he **recalls only what's relevant**: similar past investigations,
related findings, hypotheses that already failed (so he won't repeat them), searches that worked
on similar topics, glossary terms, your lessons, and matching passages from your library. Semantic
search runs on [Qdrant](https://qdrant.tech) if you run it, or inside `scholar.db` if you don't.

Lessons come only from you (CLI, or the API without a `from` agent). Never from a web page,
and never from another agent.

## How `research` finds what others miss

```
recall ─► plan question tree ─► round 1 ─► gaps + multi-hop ─► round 2 … ─► synthesize ─► skeptic ─► numbers ─► remember
```

**A. Recursive research planning.** The question becomes a tree of sub-questions (6 / 12 / 30 for
quick / standard / deep), each with its own searches. Business questions *always* cover demand,
competitors, customer complaints, costs, margins, regulations, distribution and evidence of actual
willingness to pay. After each round he reviews the tree, notes what's still unanswered and what
the sources raised, and adds follow-up questions.

**B. Multi-hop investigation.** Every source he reads yields entities and relationships (company
*subsidiary_of*, *supplies*, *customer_of*, *competes_with*, *cites*…), saved with source and date
in the research graph. Newly discovered companies, products and studies become new questions:
companies get SEC filing searches, studies get paper searches. That's how a filing leads to a
subsidiary, the subsidiary to a supplier, and the supplier to the opportunity.

**C. Full-document analysis.** PDFs, Word, PowerPoint, Excel and SEC filings are parsed in full
([Docling](https://github.com/docling-project/docling) recovers real tables and layout). Tables are saved as CSV,
appendices and methodology sections are searched, and the reference list is followed back to the
original studies through OpenAlex (most-cited first). Whole documents are downloaded only where the
site's robots.txt permits.

**D. Adversarial fact-checking.** After the preliminary conclusion, a separate skeptic pass tries to
*disprove* each key finding: it searches for contradicting evidence, newer data and methodology
criticism, reads it, and checks every quote. On top of that, rule checks flag single-source claims,
outdated numbers (more than 3 years old), self-reported claims (a company's own site praising
the company), retracted papers, and quotes that don't appear on the page. Findings get downgraded
to *disputed* or *refuted*, and the reason is kept.

**E. Persistent memory.** See the four layers above.

**F. Quantitative analysis.** A controlled Python sandbox (no network, no other programs, writes only
in its own folder, time and memory limits) for statistics, financial models, simulations, data
cleaning and charts on the tables he extracted. Business plans get computed scenarios and a
**Monte Carlo stress test** over ranges, so you see the chance of losing money instead of one
optimistic number.

### What the code enforces (so a small model can't fake it)

- A finding is **verified** only with two independent domains, or one tier-1 source whose quote was
  found in the page. One source = **reported**. No valid source = **assumption**.
- Invented quotes are caught; numbers in the answer that appear in no source are flagged.
- With no evidence, he writes no answer, rather than answering from the model's imagination.
- Every source gets a credibility tier: 1 primary/peer-reviewed/official · 2 reputable secondary ·
  3 general web · 4 unvetted (social media, forums, the dark web, retracted papers).
- Text from pages and documents is fenced off as data. Instructions hidden in a page don't steer him.

## Setup (MacBook)

1. **Ollama + models** (for the default `cpu` profile: Intel Macs and other machines without a GPU)
   ```bash
   ollama pull qwen3:8b            # the thinker: planning, synthesis, the skeptic
   ollama pull qwen3:4b-instruct   # the reader: pulls facts out of every source (the *instruct* edition — see below)
   ollama pull nomic-embed-text    # semantic memory
   ```
   Then run `scholar bench` to see how accurate and fast they are on *your* machine (see
   [Hardware](#hardware-your-mac-a-raspberry-pi-an-esp32)).
2. **Scholar** (Python 3.10+)
   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install -e '.[all]'         # Docling, pypdf, pandas/numpy/matplotlib for the sandbox
   # or just: pip install -e .     # core runs on the standard library alone
   ```
3. **Keys and contact** (both free)
   ```bash
   export SCHOLAR_OPENALEX_KEY=...           # https://openalex.org/settings/api
   export SCHOLAR_CONTACT=you@example.com    # SEC EDGAR requires contact info to search filings
   ```
4. **Qdrant** (optional; without it vectors live in `scholar.db`, which is fine for a personal library)
   ```bash
   docker run -d --name qdrant -p 6333:6333 -v ~/.scholar/qdrant:/qdrant/storage qdrant/qdrant
   export SCHOLAR_QDRANT_URL=http://localhost:6333
   scholar reindex                 # if you add Qdrant after he already has memories
   ```
5. **Check and start**
   ```bash
   scholar doctor
   scholar ingest ~/Documents/research     # optional: your own library
   scholar chat
   ```

All settings: [`.env.example`](.env.example). Everything lives in `~/.scholar/`: `scholar.db`,
`reports/`, `documents/` (downloaded originals + extracted tables as CSV), `analysis/` (sandbox runs).

### Better search: your own SearXNG (recommended)

DuckDuckGo works out of the box but rate-limits automated searches. A private
[SearXNG](https://docs.searxng.org) instance queries many engines at once and is yours:

```bash
docker run -d --name searxng -p 8888:8080 searxng/searxng
# enable the JSON format: add "- json" under search.formats in the container's settings.yml, restart
export SCHOLAR_SEARXNG_URL=http://localhost:8888
```

### Dark web sources (optional, read-only)

```bash
brew install tor && brew services start tor
export SCHOLAR_TOR_PROXY=127.0.0.1:9050
scholar read http://<something>.onion/
```

Only `.onion` addresses go through Tor. Everything from there is tier 4 and has to be corroborated
before it can count toward a *verified* finding. Scholar reads and reports; he doesn't log in, buy,
download or post anywhere. Keep what you ask him to look at legal where you live.

## His dashboard

```bash
scholar serve        # then open http://localhost:8766   (Amo's is on 8765)
```

The same look as Amo's dashboard, light and dark, and it works on a phone on your Wi-Fi
(`SCHOLAR_API_HOST=0.0.0.0` + a `SCHOLAR_API_TOKEN`). The page is one self-contained file, so it works
with no internet:

- **Home:** start research or a business plan and watch it live (current step, queue), ask him things (🎤 to
  talk, 🔊 to hear replies), an inbox for finished research and answers, and library counts.
- **Reports**, rendered with their citations, skeptic's review, scenarios and sources.
- **Findings:** filter by verified / reported / disputed…, and open one to see the evidence *for* and
  *against* (with quotes), the skeptic's flags and how his confidence changed over time.
- **Open questions** still unanswered, and what's **waiting for the internet**.
- **Connections:** the research graph, entity by entity (company → subsidiary → supplier…).
- **Library:** everything he has read, plus the tables he extracted.
- **Lessons:** teach him (preferences, corrections, strategies) and see his glossary.

### In Open WebUI, next to Amo

Scholar speaks the same OpenAI-style API as Amo (`/v1/models`, `/v1/chat/completions`,
`/v1/audio/transcriptions`, `/v1/audio/speech`). In Open WebUI: **Settings → Connections → add an OpenAI
API** with URL `http://localhost:8766/v1` and your `SCHOLAR_API_TOKEN` as the key. "scholar" then shows
up as a model next to "amo", with voice. Start a message with `research:` or `plan:` and he queues a full
investigation; the report lands in his dashboard inbox.

## Offline

His brain (Ollama), memory, library and voice all live on your machine, so **almost everything works with
no internet**: chat, voice, the dashboard, reports, findings, the graph, analysis, and research *from his
library*. What needs the internet is *new* sources, so:

- He notices when the internet is down (or set `SCHOLAR_OFFLINE=1`) and doesn't waste minutes on timeouts.
- **Research offline** answers from everything he has ever read plus your own documents. The report says
  so plainly (📴 Offline), and the full investigation is queued to run **automatically when the internet is
  back** (the server checks every couple of minutes; `scholar sync` does it now; `scholar pending` shows
  the queue).
- **Devices that were away** (an ESP32 terminal off Wi-Fi, a Pi on the road) work like Amo's pocket pet:
  when they reconnect they `POST /api/sync` with what they were asked
  (`{"device": "pocket", "items": [{"kind": "ask|research", "text": "...", "at": "..."}]}`). Questions are
  answered straight away (the brain is local, internet or not), research is queued, and he answers with a
  line to say: *"While I was offline you asked me … I'm on it — the answers will be on the dashboard."*
  Answers land in the inbox (`scholar inbox`, the dashboard, `GET /api/inbox`).

## His voice

He can talk, all locally:

```bash
pip install -e '.[voice]'          # Whisper (listening) + Piper (speaking)
scholar voice                      # press Enter, ask out loud, hear the answer
scholar voice --hands-free         # just talk
scholar say --report 3             # read a report's answer aloud
```

- **Listening:** [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (`base.en` on a Mac,
  `tiny.en` on a Pi; `SCHOLAR_WHISPER_MODEL`).
- **Speaking:** [Piper](https://github.com/rhasspy/piper) if you point `SCHOLAR_PIPER_VOICE` at a voice.
  `en_GB-alan-medium` fits a scholar:
  ```bash
  mkdir -p ~/.scholar/voices && cd ~/.scholar/voices
  for f in en_GB-alan-medium.onnx en_GB-alan-medium.onnx.json; do
    curl -LO "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium/$f"; done
  export SCHOLAR_PIPER_VOICE=~/.scholar/voices/en_GB-alan-medium.onnx
  ```
  Without Piper he uses the Mac's built-in voice (`SCHOLAR_SAY_VOICE`, default `Daniel`) or espeak-ng on a Pi.
- He **speaks the short version**: citations, links, tables and markdown are stripped, and long answers
  become a few sentences plus "the full details are on screen".
- **Voice terminal (ESP32-S3 with an I2S mic and speaker):** record a 16 kHz mono WAV and
  `POST /api/listen` (body = the WAV) → `{"heard", "reply", "speak"}`; then `POST /api/speak`
  `{"text": ...}` → a WAV to play. Speech recognition and the voice run on the Mac/Pi, not the ESP32.

## Using him

| Command | What it does |
|---|---|
| `scholar chat` | talk to him; he searches, reads, follows citations and filings, runs analyses, records findings |
| `scholar research "question" [--depth quick\|standard\|deep]` | full investigation → cited report |
| `scholar plan "business idea"` | research + startup costs, steps, milestones, scenarios, Monte Carlo, verdict |
| `scholar search "query" --in web\|papers\|wiki\|filings` | search without the model |
| `scholar read URL` | read a page/PDF/filing in full: tables, sections, references |
| `scholar citations DOI [--cited-by]` | what a paper cites (most-cited first), or who cites it since |
| `scholar ingest PATH [--tier 1-4]` | add your own documents to his library |
| `scholar investigations` · `scholar questions [ID]` | past investigations and their question trees (and what's still unanswered) |
| `scholar graph [entity]` | the research graph: top entities, or one entity's connections |
| `scholar finding list\|show\|add\|update` | conclusions with evidence for/against, skeptic flags and history |
| `scholar tables [--source ID]` · `scholar table ID` | tables extracted from documents |
| `scholar analyze script.py --table 3 --table 7` | run your (or his) analysis in the sandbox on those tables |
| `scholar stress --price 180 220 260 --cost 150 --units 5 10 25 --fees-pct 9 13` | Monte Carlo stress test |
| `scholar calc` · `scholar economics` | exact arithmetic, unit economics |
| `scholar feedback "..." --kind preference\|correction\|strategy\|source_note` · `scholar lessons` | teach him; retire a lesson with `lessons --retire ID` |
| `scholar glossary [term]` · `scholar sources` · `scholar reports` · `scholar report ID` | the library |
| `scholar serve` | the dashboard (http://localhost:8766) and the API for the family |
| `scholar inbox` · `scholar pending` · `scholar sync` | messages for you; work waiting for the internet; catch up now |
| `scholar reindex` · `scholar card` · `scholar doctor` | maintenance |

Each source he reads is one call to the reader, plus a handful of calls to the thinker for planning,
gaps, synthesis, the fact-check and the skeptic: **quick** ≈ 15 calls, **standard** ≈ 35, **deep** ≈ 90.
At the end of every run he prints how much model time it took. Docling's first run downloads its
layout models.

## Hardware: your Mac, a Raspberry Pi, an ESP32

Accuracy comes from the model, but on a machine without a GPU, **speed is set by how much text the
model has to read and write**, and writing is the slowest part. Scholar is built for that:

- **Two brains.** A small, fast *reader* pulls facts out of every source (most of the calls); a bigger
  *thinker* does planning, synthesis, the fact-check and the skeptic (few calls, where judgement matters).
- **Exact answer layouts.** Every step has a JSON schema that Ollama enforces while the model writes,
  so small models can't return broken or wrongly-shaped answers, and list sizes are capped so they
  don't write more than needed.
- **Sized prompts.** Each source is trimmed to its opening plus the passages closest *in meaning* to the
  open questions, within a budget set by the profile; only the sub-questions that source might answer are
  shown; synthesis gets the best evidence first (confirmed quotes, credible sources) up to the budget.
- **A fact-check step.** The thinker confirms each quote really says what the claim says (a real quote
  can still be beside the point); unsupported claims are downgraded.
- **Thinking only where it pays.** Reasoning models like Qwen3 can "think out loud" before answering, which
  on a CPU can mean minutes per call. By default the thinker reasons step by step only for the fact-check (one
  call per investigation, where it measurably helps); `SCHOLAR_THINK=all` uses it for every thinker step, `off` never.

| Profile | For | Thinker / reader | Context · read per source |
|---|---|---|---|
| `cpu` (default) | Intel Macs, PCs without a GPU | `qwen3:8b` / `qwen3:4b-instruct` | 8k tokens · 6,000 chars |
| `pi` | Raspberry Pi 5 (8–16GB) | `qwen3:4b-instruct` / `qwen3:1.7b` | 4k tokens · 3,500 chars |
| `gpu` | Apple Silicon 32GB+, desktop GPUs | `qwen3:14b` / `qwen3:8b` | 16k tokens · 10,000 chars |

Set `SCHOLAR_PROFILE`, or override any piece (`SCHOLAR_MODEL`, `SCHOLAR_FAST_MODEL`, `SCHOLAR_NUM_CTX`,
`SCHOLAR_READ_CHARS`). Then **measure instead of guessing**:

```bash
scholar bench                                   # your thinker and reader
scholar bench --models qwen3:4b-instruct,qwen3:8b,gpt-oss:20b
```

`bench` runs each model through Scholar's real prompts on three small documents with known answers
(made-up companies and numbers, so a model can't answer from memory, and one hides a prompt-injection
trap). It scores fact recall, real quotes, invented numbers, falling for the trap, judging whether a quote
supports a claim, and clean synthesis, plus reading and writing speed, then suggests a thinker and reader.

Measured on a 4-core CPU-only cloud machine with 15GB RAM, a close stand-in for a 16GB Intel Mac
(an 8-core i9 should be roughly 1.5–2× faster):

| Model | Score | Fell for the trap? | Quotes real | Seconds per source read | Writes (tokens/s) |
|---|---:|---|---:|---:|---:|
| qwen2.5:1.5b | 73 | **yes** — reported the planted "$9 billion" | 100% | 69 | 16.6 |
| qwen3:4b (Thinking edition) | 77 | no | 87% | 103 | 6.8 |
| qwen3:8b | 83 | no | 80% | 142 | 4.2 |

All three found every key fact; the differences were honesty and judgement. A model that obeys
instructions hidden in a page can't be trusted to read the web, so `bench` never recommends one. (The
judgement scores in that run were understated by a bug in the fact-check prompt, since fixed; source-reading
times were measured before the reader's output was trimmed: the 4B's dropped from 103s to 76s after.)

After the fix, the fact-check (does each quote really support its claim?) on the same machine:

| Model | Thinking | Correct | Time |
|---|---|---:|---:|
| qwen3:4b (Thinking edition) | off | 3/3 | 31s |
| qwen3:8b | off | 2/3 | 43s |
| qwen3:8b | on | 3/3 | 148s |

Three items is a small test; treat it as a sign, not a verdict. It's why thinking is on for this one step.

**Watch out for "thinking" editions.** Plain `qwen3:4b` is now Qwen's *Thinking* edition: it reasons out loud
before every answer even when told not to. That's fine inside research (the answer layouts keep it in check)
but in conversation one short reply took over 10 minutes on a CPU. Use `qwen3:4b-instruct` (answers in
seconds) or the hybrid `qwen3:8b`. `scholar doctor` tests your models and warns you.

**One model in memory at a time.** A model has to sit entirely in RAM while it works. On a 16GB Mac,
Scholar's thinker (~6.8 GB loaded) plus his reader (~4.3 GB) plus macOS and your apps is already at the
limit, and past it the Mac swaps memory to disk: in testing, a one-sentence answer that takes 5 seconds
took **7 minutes** with three models loaded. So Scholar keeps **one of his models loaded at a time**. Before
switching (reader → thinker) he tells Ollama to unload the other. Switching costs ~6–10 seconds to load
from disk; staying on one model costs nothing, and research batches its work so switches are few.
He never unloads other apps' models (like Amo's). To make it a rule for *every* app on the Mac:

```bash
launchctl setenv OLLAMA_MAX_LOADED_MODELS 1     # then quit and reopen Ollama
```

`scholar doctor` shows what's loaded and warns when several chat models crowd memory. On a 32GB+ machine,
`SCHOLAR_ONE_MODEL=0` lets both stay loaded. What Scholar needs: `qwen3:8b` (thinker, 5.2 GB download),
`qwen3:4b-instruct` (reader, 2.5 GB), `nomic-embed-text` (memory search, 0.3 GB): about 8 GB of disk.

**Raspberry Pi 5.** Same code. Either run small models on the Pi itself (`SCHOLAR_PROFILE=pi`: expect
slow, quick-depth research), or, better, let the Pi borrow a stronger machine's brain:
`SCHOLAR_OLLAMA_URL=http://<your-mac>.local:11434` (on the Mac: `OLLAMA_HOST=0.0.0.0 ollama serve`).
Docling is heavy for a Pi; without it Scholar falls back to lighter parsers.

**ESP32-S3.** A microcontroller can't run a language model (it has megabytes of memory; even a tiny useful
model needs a gigabyte or more), so it becomes a *terminal* for the family: a screen, buttons, maybe a mic,
talking to Scholar over Wi-Fi. For that he has:
- `POST /api/research` with `"async": true` → returns a job id right away; the investigation runs in the
  background (one at a time, queued), and the device polls `GET /api/jobs/<id>`.
- `GET /api/brief` → a status summary under 1 KB: busy or not, the current step, the last answer
  (shortened), and counts of findings and open questions. Sized for a small screen and a small buffer.

## JSON API (for the rest of the family)

```bash
SCHOLAR_API_HOST=0.0.0.0 SCHOLAR_API_TOKEN=change-me scholar serve     # port 8766 (Amo uses 8765)
```

| Method | Path | |
|---|---|---|
| GET | `/api/health` · `/api/agent` | liveness · identity card (role, skills, permissions, library stats) |
| GET | `/api/findings?q=&status=` · `/api/findings/<id>` | conclusions, with evidence for/against + history |
| GET | `/api/investigations` · `/api/investigations/<id>` | investigations with question tree + graph |
| GET | `/api/questions?status=unanswered` | open questions across investigations |
| GET | `/api/graph?entity=` · `/api/tables?source=` · `/api/glossary?q=` · `/api/lessons` | structured knowledge |
| GET | `/api/sources?q=` · `/api/reports` · `/api/reports/<id>` | sources and reports |
| POST | `/api/ask` `{"text": "...", "from": "amo"}` | ask him something |
| POST | `/api/research` `{"question": "...", "depth": "standard", "from": "baby"}` | full investigation |
| POST | `/api/plan` `{"idea": "...", "from": "amo"}` | researched business plan |
| POST | `/api/feedback` `{"text": "...", "kind": "preference"}` | a lesson. **Boss only:** refused when `from` is another agent |
| POST | `/api/research` or `/api/plan` with `"async": true` | queue it as a background job → `{"job_id", "poll"}` |
| GET | `/api/jobs` · `/api/jobs/<id>` | job status: queued (with position) / running (current step) / done (report id) / failed |
| GET | `/api/brief` | under-1KB status for small screens (ESP32): busy, online, unread news |
| POST | `/api/sync` `{"device", "items"}` | a device reconnected: what it was asked while offline |
| GET | `/api/inbox?unread=1` · POST `/api/inbox/read` · GET `/api/pending` · POST `/api/catch-up` | inbox and queue |
| GET | `/` | the dashboard |
| | `/v1/models` · `/v1/chat/completions` · `/v1/audio/transcriptions` · `/v1/audio/speech` | OpenAI-style, for Open WebUI |
| POST | `/api/listen` (body: WAV) · `/api/speak` `{"text"}` | voice terminal: speech in → answer; text → WAV |

Send `Authorization: Bearer <SCHOLAR_API_TOKEN>` when a token is set. Always set one before
binding to `0.0.0.0`.

## Development

```bash
pip install -e '.[dev]'
pytest
```

Tests use a fake model, fake web and a fake embedder, so they need neither Ollama nor the internet.

Layout: `research.py` (the investigation method) · `memory.py` (four-layer memory, recall, ingest) ·
`vectors.py` (embeddings; Qdrant or built-in store) · `documents.py` (Docling + fallbacks: tables,
sections, references) · `web.py` (search, OpenAlex, EDGAR, robots.txt, Tor) · `sandbox.py` (controlled
Python) · `calc.py` (business math, Monte Carlo) · `credibility.py` · `db.py` · `tools.py` (what the
model can call in chat) · `assistant.py` (personality) · `agent.py` (identity card) · `server.py` · `cli.py`.
