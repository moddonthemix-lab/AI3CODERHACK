# Builder — first day, from scratch

A plain-language, do-this-in-order guide to getting Builder running on your Mac the first time.
Everything runs on **your own machine** — no cloud, no subscription. Written for a 16 GB Intel Mac.

You'll spend most of the time waiting for two downloads (Ollama + the model). The rest is quick.

---

## 1. Install Ollama (Builder's local "brain")

1. Go to **https://ollama.com** and download the Mac app.
2. Open it. That's it — it runs quietly in the background.

Check it worked, in **Terminal** (press ⌘-Space, type "Terminal", Enter):
```bash
ollama --version
```
If you see a version number, you're good.

## 2. Download Builder's coder models

Still in Terminal (these are a few GB each, so they take a while on the first run):
```bash
ollama pull qwen2.5-coder:7b
ollama pull qwen2.5-coder:3b
```
These are the models Builder thinks with. `7b` is the main one; `3b` is the fast one.

## 3. Get Builder's code

```bash
git clone https://github.com/moddonthemix-lab/AI3CODERHACK.git
cd AI3CODERHACK
git checkout claude/inspiring-bohr-o2l0p9
```

## 4. Set Builder up (one-time)

You need Python 3.10 or newer. Check with `python3 --version`. If it's older, install Python from
**https://python.org**, then use `python3.12` in place of `python3` below.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```
After this, the `builder` command exists **whenever the environment is active** (step 7 reminds you).

## 5. Give Builder a workspace (this is his access to your computer)

Builder can only ever touch the folders you list here — nothing else on your Mac. Make him a space and
tell him about it, once:

```bash
mkdir -p ~/BuilderSpace
cp .env.example .env
```
Now open `.env` in any text editor and set this one line (leave the rest as-is for now):
```
BUILDER_CODE_DIRS=~/BuilderSpace
```
That folder is his sandbox. He can create files, build projects and use its space — and he **cannot**
reach anything outside it. (You can list more folders with commas, e.g. `~/BuilderSpace,~/Projects`.)

## 6. Check everything is ready

```bash
builder doctor
```
You want ✓ next to Ollama, both models, the sandbox, and your code folder. If a model shows ✗, re-run its
`ollama pull` from step 2. If the folder shows ✗, re-check step 5.

## 7. Use him

**Every new Terminal window**, first activate the environment (so the `builder` command works):
```bash
cd ~/AI3CODERHACK && source .venv/bin/activate
```

**Build something** (he writes it, runs its tests, fixes failures, shows you the diff):
```bash
builder build "a retro snake game in one HTML file, canvas, with a test" --dir ~/BuilderSpace/snake
```
Don't like it? Undo it:
```bash
builder build --undo --dir ~/BuilderSpace/snake
```

**Review your own code for security problems** (read-only, offline):
```bash
builder review ~/BuilderSpace/snake
```

**Talk to him:**
```bash
builder chat
```

**Teach him** (he remembers this for every future build):
```bash
builder feedback "always add type hints and a README"
```

> On a 16 GB Intel Mac the model runs on the CPU, so builds take **minutes, not seconds**. That's normal —
> he's actually writing and testing, not guessing.

## 8. The dashboard (optional, nice to have)

```bash
builder serve
```
Then open **http://localhost:8767** in your browser — his security findings, the projects he's built, and
an "Ask Builder" box. Leave that Terminal window running; stop it with Ctrl-C.

To reach the dashboard from your phone on the same Wi-Fi, set these in `.env` first:
```
BUILDER_API_HOST=0.0.0.0
BUILDER_API_TOKEN=pick-a-password
```
then `builder serve` and open `http://<your-mac-IP>:8767` (it'll ask for the password).

---

## If something's off

- **`builder: command not found`** → you forgot `source .venv/bin/activate` (step 7).
- **"local coder model isn't reachable"** → the Ollama app isn't running, or the model isn't pulled (step 2).
- **"outside Builder's allowed folders"** → the `--dir` isn't inside `BUILDER_CODE_DIRS` (step 5). That's the
  safety gate doing its job.
- **Everything's slow** → expected on a CPU; try the `3b` model as the main one by setting
  `BUILDER_MODEL=qwen2.5-coder:3b` in `.env`, or run `builder bench` to compare.

## What he will never do (even if a file or message tells him to)

Touch anything outside your listed folders · reach the network · install software · run destructive
commands — **without asking you first**. Security practice against anything other than targets you've
confirmed are yours stays off by default (`builder lab status`). That's all enforced in the code, not left
up to the model.
