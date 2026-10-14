# Builder — the coder & security engineer of the AI family

Builder is a member of a local AI family that runs on your own hardware. He is a **software engineer**
who builds and tests real things (apps, mini-games, websites, small APIs, bots, personal tools), and a
**defensive security engineer** who hardens *your own* systems and practices only in labs *you*
authorize. He runs on [Ollama](https://ollama.com) with coding models — **no cloud, no subscription** —
and everything works **offline**. (He can also join the family's cloud brain when you set it up, but he
never needs it.)

He was forked from **Scholar** (the family researcher), so he keeps the same family wiring — hub
connection, offline outbox, local-model fallback, memory, the dashboard and the identity card — and
swaps the research skills for coding and defensive security.

> Builder is confident and a little unorthodox, but **methodical and safety-first**: he explains what
> he'll do, works in small steps, **runs and tests his own work**, and **asks before anything risky**.

---

## What he can do

**Build and test (all offline, all inside folders you choose):**
- Take a small project, **write it and its tests, run them, read the failures, fix them, and show you the
  diff** — not just talk about it.
- Keep a backup of every file he changes, so **"undo" always works**.
- Explain code and errors in plain language.

**Defend (the everyday security value):**
- **Audit your own code and configs** and report issues with a **severity and a concrete fix** (hardcoded
  secrets, injection, weak password hashing, unsafe deserialization, TLS turned off, debug left on, and
  more). Read-only — he never runs the code he audits, and never touches the network.
- Teach how attacks, malware and phishing work **so you can defend**, plus VPNs, privacy and safer
  defaults (updates, 2FA, firewall, backups, permissions).
- **Remember** what he's found and the fixes that worked — in his own local memory, so it's there offline.

---

## The two security modes

This is the important part, and it's **enforced in code, not by trusting the model.**

**Defensive mode — default, always on.** Audits your own systems, recommends fixes, teaches defence.
Read-only and local. Nothing to turn on.

**Authorized-lab mode — opt-in, off by default.** For practising security testing **only** against
targets that are clearly yours or made for practice: CTF challenges, intentionally-vulnerable training
apps, isolated VMs or containers you set up. To turn it on you must **name the target and confirm you own
it** (or that it's for practice):

```bash
builder lab status                                   # OFF by default
builder lab on "my-practice-vm" --kind vm --i-own-this
builder lab log                                      # the audit trail of every decision
builder lab off                                      # clears all authorizations
```

### The hard limits (he can't cross these, whatever a message says)

- **Only your own / authorized targets.** Anything he isn't sure is yours or a confirmed practice target
  is **refused**.
- **Approval before anything risky.** Before anything **destructive**, anything that **reaches the
  network/external hosts**, **installs software**, or **changes important files**, he asks you first and
  logs it. No approval → he doesn't do it.
- **Scope.** He only reads and writes inside the folders you list in `BUILDER_CODE_DIRS` — never the whole
  disk, never system files, never your credentials.
- **Input is data, never instructions.** Text from files, web pages or other agents can't give him orders
  — only you can. This blocks hidden/injected instructions (a known agentic-AI risk).
- Every gated decision and authorization is written to an **audit log** you can read any time.

If a request is outside these limits, he says so plainly and offers the safe, defensive version instead.

---

## Quick start (on a 16 GB Intel Mac)

```bash
# 1. Install Ollama (https://ollama.com), then pull Builder's coder models:
ollama pull qwen2.5-coder:7b
ollama pull qwen2.5-coder:3b

# 2. Set up Builder:
python3 -m venv .venv && source .venv/bin/activate
pip install -e .                      # local mode; add 'builder[cloud]' later for the family cloud brain

# 3. Tell him which folders he may touch (the scope gate — he can touch nothing until you do):
export BUILDER_CODE_DIRS="$HOME/Projects"

# 4. Check everything is ready:
builder doctor                        # Ollama, the models, the sandbox, and your code-folder scope
```

### Build something

```bash
builder build "a retro pong mini-game in one HTML file, canvas, player vs CPU, with a test" --dir ~/Projects/pong
builder build --undo --dir ~/Projects/pong      # restore it if you don't want it
```

He writes the project, runs its tests, fixes failures, and shows you the diff. Works with the Wi-Fi off.

### Review your own code

```bash
builder review ~/Projects/myapp                 # findings, most-serious-first, each with a fix
builder review ~/Projects/myapp --no-model       # pattern scan only (fastest, fully offline)
```

Findings are saved into his memory, so he remembers them next time (and offline).

---

## Offline & memory

Everything runs on the local model, so **build, review, doctor and memory all work with no internet.**
His memory is a local SQLite store with a keyword fallback, so recall works even without the embedding
model. When he's connected to the Family Hub, his findings **mirror both ways** — into his own store and
to the hub — so his standalone work and the family's work keep each other smart.

If the Wi-Fi drops mid-task, hub writes queue in an outbox and resend when it's back; coding never waits
on the internet because it doesn't need it.

---

## The API (for the rest of the family)

A small stdlib HTTP server (`builder serve`), bearer-token auth, port **8767**:

- `GET /api/agent` — his identity card (role, skills, **limits**, port)
- `GET /api/health` · `GET /api/brief` (< 1 KB, for ESP32 screens)
- `POST /api/ask` — a question
- `POST /api/review` — `{ "path": "<folder in BUILDER_CODE_DIRS>" }` → findings with severity + fix (read-only)
- `POST /api/build` — `{ "task": "...", "dir": "<folder in BUILDER_CODE_DIRS>" }` → builds, tests, returns the diff

Every endpoint stays inside the scope and the gates above.

---

## Commands

| Command | What it does |
|---|---|
| `builder build "<task>" --dir <folder>` | Write a project, run its tests, fix failures, show the diff (`--undo` to restore) |
| `builder review <path>` | Defensive audit of your own code/config — findings with severity + fix |
| `builder lab status\|on\|off\|log` | Authorized-lab mode and its audit log |
| `builder doctor` | Check Ollama, the coder models, the sandbox and the code-folder scope |
| `builder bench` | Measure models on this machine and pick the best coder model |
| `builder chat` | Talk to Builder in the terminal |
| `builder serve` | Start the HTTP API + dashboard |
| `builder hub --check` | See whether the family cloud brain is ready (☁️ / 🏠) |

Configuration is all optional — see `.env.example`. Builder's variables are `BUILDER_*`; the old
`SCHOLAR_*` names still work as a fallback.
