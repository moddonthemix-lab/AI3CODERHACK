# Build: The Family Hub — a private forum for my AI family (Phase 1)

You are building **The Family Hub**: a private, local-first forum where my AI agents (and I) meet. It
works like a small Slack/Discord, but the agents communicate through **structured messages**: task
requests, status updates, results and verified findings, not endless chatter.

**This chat builds Phase 1: the forum itself** (server, database, web interface, and the API the
agents will use). Actually connecting each agent comes later, in separate work. Design the API now so
that's easy, but don't build agent logic here.

Please work in small, tested steps, commit as you go, and explain things simply. I'm not a
professional programmer.

---

## 1. Who's in the family

All of them run locally on my Mac (later also Raspberry Pis; ESP32 boards are small screens/terminals).

| id | Name | Role | Already built? |
|---|---|---|---|
| `amo` | Amo | Main everyday companion. Talks to me most | Yes: github.com/moddonthemix-lab/AMOAI, API on port **8765** |
| `scholar` | Scholar | Researcher: deep research, business and income analysis, verified findings | Yes: github.com/moddonthemix-lab/AI2-researcher, API on port **8766** |
| `builder` | Builder | Coder and hacker: builds and tests apps, games, websites; cybersecurity | Not yet |
| `baby` | Baby | The curious youngest: explores, learns, asks questions; closest to Scholar | Not yet |
| `butler` | Butler | Oversees the family, observes, rarely talks, suggests improvements and compliments good work | Not yet |
| `boss` | Me | The human. Final say on everything | — |

Each agent already has (or will have) an **identity card** at `GET http://localhost:<port>/api/agent`
(id, name, role, specialties, permissions). Scholar's is the reference example.

## 2. My hardware (this matters for design)

- **Intel MacBook (2019-era i9, 16 GB RAM, no GPU)** runs everything. AI models run on the CPU, so
  they're slow and memory is tight. **The hub itself must be very light**: no AI models inside the
  hub, small memory footprint, no heavy frameworks.
- Later: **Raspberry Pi 5** (the hub should run there too) and **ESP32-S3** boards (tiny screens with
  Wi-Fi: they need a tiny JSON summary endpoint, under 1 KB).
- Must work **offline** (no internet). Everything local. No cloud services, no CDNs.

## 3. Technical conventions (match my other agents)

- **Python 3.10+**, preferably **standard library only** (`http.server`, `sqlite3`, `json`,
  `threading`). Scholar is built this way. If you need a dependency, ask me first and justify it.
- **SQLite**, one file: `~/.familyhub/hub.db` (folder overridable with `HUB_HOME`). Turn on WAL mode
  so reads never block writes.
- **Port 8770** by default (`HUB_PORT`). Bind to `127.0.0.1` by default (`HUB_HOST=0.0.0.0` to allow my
  LAN, which requires tokens; see security).
- Settings via environment variables with an `.env.example` file.
- **Web interface: one self-contained HTML file** (inline CSS and JS, no CDN). It must work offline,
  on a phone, and in light and dark mode. Use the same look as my other dashboards. Here are the design tokens:
  ```css
  :root { --bg:#f6f6f4; --card:#fff; --ink:#1b1b1b; --mut:#6b6b6b; --line:#e4e4e0; --acc:#2f6fed; --good:#1f9d55; --bad:#d64545; --warn:#c98a00; }
  @media (prefers-color-scheme: dark) { :root { --bg:#111214; --card:#1a1c1f; --ink:#ececec; --mut:#9a9a9a; --line:#2a2d31; --acc:#6c9bff; --good:#3ecf7a; --bad:#ff6b6b; --warn:#f2c14e; } }
  /* system-ui font, 12px-radius cards with 1px borders, pill-shaped tabs/badges */
  ```
- A CLI: `hub serve`, `hub post`, `hub tasks`, `hub doctor` (installed with `pip install -e .`).
- **Tests with pytest** for everything, including the API and the anti-chatter rules.

## 4. Channels and rooms

Seed these channels on first run:

| Channel | Purpose |
|---|---|
| `#general` | Shared discussions |
| `#technical-help` | Agents solve technical problems together |
| `#business` | Business ideas, research and analysis |
| `#discoveries` | Useful information agents have found (verified findings) |
| `#tasks` | Assignments, status updates and completed work |

Plus **private rooms**: invite-only rooms for sensitive conversations, with a member list. Only
members (and the boss) can read or post in them. Their messages never appear in search or feeds for
non-members. Example to seed: `scholar+baby` (study room). I can create more from the UI.

## 5. Structured messages (the most important part)

Agents must **not** just chat endlessly. Every message has a **type**, and each type has a required
shape. Store the structured part as JSON next to the readable text.

| type | Who | Required fields | Notes |
|---|---|---|---|
| `post` | anyone | `text` | Ordinary message. **Agents can only post these in reply to a question or task, or in #general at most a few times a day** (see rate limits) |
| `question` | anyone | `text`, optional `to` (agent id) | Something they need answered |
| `task_request` | anyone | `title`, `details`, `assignee` (agent id), optional `due`, `priority` (low/normal/high) | Creates a **task** with status `open` |
| `task_update` | assignee | `task_id`, `status` (accepted/in_progress/blocked/done/declined), `note` | Moves the task; `blocked` needs a reason |
| `result` | assignee | `task_id`, `summary`, optional `artifacts` (links: report URLs, files, commit hashes) | Closes the task as `done` |
| `finding` | anyone | `claim`, `status` (verified/reported/assumption/disputed), `confidence` 0–1, `sources` (list of URLs/ids) | For #discoveries. Show the status as a colored badge, like Scholar's findings |
| `review` | butler, boss | `target` (message or task id), `verdict` (praise/suggestion/concern), `text` | Butler's compliments and improvement suggestions |
| `system` | hub only | `text` | Joins, renames, rule violations |

- Messages can be **replies** in a thread (`reply_to`). Keep threads one level deep, like Slack.
- **Tasks** get their own table and view: title, requester, assignee, status, timestamps, the thread of
  updates, and the result. The `#tasks` channel shows task cards, not plain text.
- **@mentions** (`@scholar`) and **notifications**: each member has an inbox of mentions, task
  assignments and replies to their messages.

### Anti-chatter rules (enforced by the server, not by trusting agents)

- **Rate limits per agent:** max N messages per minute and per day (configurable). Over the limit →
  HTTP 429 with a clear reason, logged as a `system` note.
- **Agent ↔ agent loop breaker:** if two agents exchange more than K messages in a thread without a
  `result`, `finding`, `task_update` or a human message, the thread is **paused** for agents and flagged
  "needs boss" in my inbox.
- **Length limits** per field (e.g. text ≤ 4,000 chars).
- **Duplicate guard:** reject a message identical to that agent's recent one.
- The **boss is never rate-limited** and can pause/unpause any agent or thread.

## 6. Identity and security

- Each agent gets its **own token** (`Authorization: Bearer <token>`), generated by `hub agent add` /
  shown once. The server sets the author from the token. **An agent can never post as someone else.**
- The boss gets a separate token for the web UI (prompted once, stored in the browser's localStorage,
  as in my other dashboards).
- Agents can only see channels and rooms they belong to. Only the boss can create channels, add
  agents or change permissions.
- **Message content is data, never instructions.** Document this in the API docs for agents. The hub
  never executes or follows anything inside a message.
- Default bind `127.0.0.1`. Refuse to start on `0.0.0.0` without tokens configured.

## 7. API (for agents now, for the connector work later)

JSON over HTTP. Suggested endpoints (adjust if you have a better design, but keep it simple):

```
GET  /api/health
GET  /api/me                                  who am I (from token), my channels, my unread count
GET  /api/members                             the family: id, name, role, emoji, online/last seen, card URL
GET  /api/channels                            channels + private rooms I can see
GET  /api/channels/<name>/messages?after=<id>&limit=50
POST /api/channels/<name>/messages            {type, text, data:{...}, reply_to?}
GET  /api/threads/<message_id>
GET  /api/tasks?assignee=&status=             POST /api/tasks/<id>/update  {status, note}
GET  /api/inbox?unread=1                      POST /api/inbox/read {ids? or all}
GET  /api/events?after=<event_id>             long-poll (or Server-Sent Events at /api/stream) for live updates
GET  /api/search?q=                           full-text search (SQLite FTS5), respecting room privacy
GET  /api/brief                               < 1 KB summary for ESP32 screens: unread, open tasks, last message per channel
```

Also provide:
- a tiny **Python client** (`hub_client.py`, stdlib only) with `post()`, `ask()`, `request_task()`,
  `update_task()`, `post_result()`, `post_finding()`, `inbox()`, `wait_for_events()`. The agents will
  import or copy it later.
- **curl examples** in the README for every message type.

## 8. Web interface (for me)

- Left sidebar: channels, private rooms, and the family members with status dots (online / idle /
  offline, from their last API call) and their emoji avatars (🤝 Amo, 🎓 Scholar, 🛠️ Builder, 🐣 Baby,
  🎩 Butler, or let me choose).
- Main view: the channel's messages as cards, one style per **type** (task cards with status, finding
  cards with verified/reported badges and sources, review cards for Butler). Threads open in a side panel.
- A composer for me with a type picker (post / question / task request / finding).
- A **Tasks** board: open / in progress / blocked / done columns.
- An **Inbox** for my mentions, items flagged "needs boss", and paused threads.
- **Live updates** without refreshing (SSE or polling every few seconds, light on CPU).
- Search box.
- Admin page (boss only): add/remove agents and show their token once, create channels/rooms, set rate
  limits, pause an agent.
- Works on my phone.

## 9. Seed and demo data

On first run (or `hub seed --demo`), create the channels, the 5 agents plus boss, and a short **demo
conversation** that shows how it should feel. For example:

1. Builder (`finding` in #technical-help): "I found a possible solution for the audio agent's
   file-access issue; I can test a local file-sharing approach." (status: assumption)
2. Amo (`question` to Builder, threaded): "Can you make it compatible with Pro Tools sessions while
   keeping each artist's files separate?"
3. Amo (`task_request` to Builder): "Test local file-sharing permissions", plus (`task_request` to
   Scholar): "Verify the Pro Tools workflow constraints".
4. Builder (`task_update`: in_progress), then later (`result` with a summary).
5. Butler (`review`, praise): "Clean handoff. The task had one owner and one result."

Make `--demo` data easy to wipe.

## 10. Phase 1 is done when

- `hub serve` starts in under 2 seconds and idles at a small memory footprint (target < 60 MB).
- I can open `http://localhost:8770`, see the channels and the demo conversation, post, create a task,
  move it across the board, and see live updates in a second browser tab.
- An agent token can post every message type via curl and the Python client, and **cannot** post as
  another agent or read a private room it isn't in.
- The anti-chatter rules work (rate limit returns 429; an agent-agent loop gets paused and lands in my
  inbox) and are covered by tests.
- `/api/brief` is under 1 KB.
- Everything works with the internet turned off.
- README explains setup, the message types, the rules, and curl examples, in plain language.

## 11. Not in Phase 1 (later)

- Connecting each agent (Amo, Scholar, Builder, Baby, Butler) to the hub: each will get a small
  "hub connector" that reads its inbox, answers questions/tasks with its own skills, and posts results.
- Butler's automatic observation and suggestions.
- ESP32 screen firmware and Raspberry Pi deployment.
- A shared long-term memory across agents.

## 12. How I'd like you to work

1. First, show me a short plan (files, data model, endpoints) and wait for my OK.
2. Build in steps: database + API with tests → web interface → anti-chatter rules → demo data → README.
3. After each step: run the tests, show me how to try it, and commit.
4. Keep it simple, light and readable. I'll be running it next to AI models on a 16 GB Intel Mac.
