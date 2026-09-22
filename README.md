# jevme

[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![macOS 14+](https://img.shields.io/badge/macOS-14%2B-black?logo=apple)
![Python 3.12](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)
[![GitHub stars](https://img.shields.io/github/stars/danielyedaniel/jevme?style=social)](https://github.com/danielyedaniel/jevme/stargazers)

https://github.com/user-attachments/assets/e458d454-8149-4679-891e-3f07fe927f6b

**Talk to your Mac and it does it — in any app, while you're still talking.**

> "open chrome and go to youtube and play lofi beats"
> "in discord go to the general channel"
> "open the second tab" · "click the subscribe button" · "zoom in" · "implement the solution"

A floating pill shows what it hears. Commands fire as each clause lands, not after you stop. Every routine
decision is made by a fast classifier (TypeSafe **Jev**, ~200 ms); an LLM (Claude **Haiku** / **Opus**) is
only called when something genuinely needs reasoning — and whatever it figures out is **memoized**, so the
next time that same thing is a 200 ms classification instead of a multi-second model call. It gets faster and
cheaper the more you use it.

---

## Setup

### Requirements

- macOS 14+ (Apple Silicon or Intel)
- [uv](https://docs.astral.sh/uv/) — `curl -LsSf https://astral.sh/uv/install.sh | sh` (installs Python 3.12 for you)
- A **TypeSafe API key** (required) — [typesafe.ai](https://typesafe.ai)
- An **Anthropic API key** (recommended) — [console.anthropic.com](https://console.anthropic.com).
  Without it jevme falls back to the signed-in `claude` CLI if you have Claude Code installed (slower).

### Install

```bash
git clone https://github.com/danielyedaniel/jevme.git
cd jevme
./run.sh setup          # installs deps, creates .env from .env.example, checks your keys
```

Open `.env` and paste your keys:

```ini
TYPESAFE_API_KEY=ts_...
ANTHROPIC_API_KEY=sk-ant-...
```

`.env` is git-ignored. If you'd rather keep keys out of the checkout entirely, put the same file at
`~/.config/jevme/.env` instead — jevme reads both (the repo `.env` first), and real environment variables
override either.

### Permissions

Grant these to the app you launch jevme from (Terminal, iTerm, VS Code…) in
**System Settings ▸ Privacy & Security**:

| Permission | Why |
|---|---|
| Microphone, Speech Recognition | live transcription (macOS prompts on first run) |
| Accessibility | reading on-screen controls, clicking, typing, key presses |
| Screen Recording | the screenshot fallback, when an app exposes no controls |

Prefer jevme to own its permissions instead of your terminal? Build an app bundle:
`./scripts/make_app.sh && open dist/Jevme.app`.

### Run

```bash
./run.sh start     # start in the background
./run.sh log       # tail the log (kept across restarts in ~/Library/Logs/jevme/)
./run.sh stop
uv run jevme       # or run in the foreground
```

A waveform icon in the menu bar pauses / resumes / quits. The pill's ■ button pauses too; drag it anywhere.
Say **"stop"** at any time to cancel what's queued and running.

### Try it without a microphone

```bash
uv run jevme-replay                                        # a demo sentence through real Jev, tools dry-run
uv run jevme-replay "open spotify and play drake"          # your own sentence, dry-run
uv run jevme-replay --live "open notes and make a new note called groceries"
uv run python -m jevme.evalrun                             # routing eval: 68 utterances against real Jev
uv run pytest                                              # unit tests (no network, no mic)
```

---

## How it works

```
 mic ─► Apple Speech ─► Router ───────────────► ordered task queue ─► executors
        streaming        DECIDE: what did they    EXECUTE, in spoken order:
        partials         say, and is it done?       1. shortcut tool      (~0 ms after decide)
        ~150 ms          one Jev call per partial   2. replay a memo      (Jev match + tree lookups)
                         ~200 ms                    3. agent loop         (Jev per step, ~0.5 s/step)
                                                    4. write a new tool   (Opus, once; then it's a shortcut)
                                                    ↳ vision fallback     (Haiku, only when the tree is blind)
```

### 1. Listening (`jevme/speech.py`)

Apple's `SFSpeechRecognizer` streams partial transcripts every ~150 ms. The recognizer is biased with a
vocabulary of your installed apps and common sites (`contextualStrings`) — this is what makes "leetcode.com"
come out as `leetcode.com` rather than "lico.com". Sessions roll over before Apple's ~60 s cap.

### 2. Deciding (`jevme/router.py`)

Every partial is sent to Jev as **one request** containing several typed questions at once:

- **intent** — a Choice over every tool in the catalog, plus `not_yet` (still talking), `chat`,
  `general` (a screen task for the agent), `unsupported` (needs a new tool) and `cancel`.
- **args** — for each tool's enum arguments (which app? which site?), asked speculatively in the same call.
- **text spans** — free-text arguments are *selected* from the utterance ("search for **cats**"), never
  generated. Jev picks the span; no model writes it.

A decision **commits** when it's confident (≥ 0.8) and stable: instant tools fire when the same answer repeats
on two partials; tools that carry text wait for a short pause or the end of the sentence. Guards learned from
real usage logs:

- **Incomplete phrases wait** — "send a message to…", "search for…", a lone "open" are held until more
  arrives; "zoom in", "bold that", "send it" are recognized as complete.
- **Stale decisions don't fire** — if you kept talking while Jev was answering ("and play" → "and play
  Drake"), the decision is about old text and is re-asked instead of acting.
- **Tails are absorbed** — "close this" can commit a beat before "tab" arrives; a short trailing fragment
  that isn't a command of its own is folded into what just ran instead of pressing Tab.
- **Transcript revisions are survived** — Apple rewrites earlier words; the cursor is anchored by word
  count and the last consumed words, not a character offset.

### 3. Acting while you talk

Say "open chrome **and** go to youtube **and** search for lofi". The router splits at conjunctions followed by a
command verb and **streams** each completed leading clause into the queue *while you're still speaking*:
Chrome opens on "and go", YouTube on "and search". A single ordered worker (`jevme/main.py`) runs units one at
a time, so commands execute in the order spoken. "Stop" bumps a cancel epoch that drops everything queued so
far — and nothing said afterwards.

### 4. Executing — cheapest tier first

**Shortcuts** (`jevme/tools.py`) — 35 built-in tools: open/quit apps, open sites, Google or site search,
play a YouTube video, click/type by description, media keys, tabs, volume, dark mode, screenshot… Each is a
name, a one-line description, spoken examples, and a `run`. Add one and it's in the next Jev request.

**The agent** (`jevme/agent.py`) — anything else done through app windows. Each step:

1. Read the front window through macOS Accessibility (`jevme/ax.py`): every visible button, link, field, row
   and tab with label and position, in 20–250 ms. **No screenshot.** Chrome and Electron apps (Discord,
   Slack, VS Code, Spotify, Notion) are switched into full accessibility automatically.
2. One Jev request picks the next operation (click / type / key / scroll / open app / open URL / done /
   stuck), the target element, the key, and the words to type — all in one call.
3. Do it, look again. Up to 12 steps, ~0.5 s each.

Before reasoning, deterministic goal understanding puts the task in the right place: a message with no app
named goes to Messages; "compose a Gmail email" or "my messages on LinkedIn" goes to the **website**, never
the look-alike native app. If the agent makes no progress for 5 steps it escalates to vision, then stops.

**Writing new tools** (`jevme/generator.py`, `jevme/learned.py`) — requests no window can satisfy ("make the
screen brighter", "delete the selected note") make Opus write a small AppleScript / JXA / shell tool. It must
pass a hard policy (no sudo, wiping, credentials, system paths), a syntax compile, and a Jev review ("would this
really do it?") before it runs. It's then saved and becomes a shortcut forever.

**Vision fallback** (`jevme/vision.py`) — when an app exposes no usable controls (canvas apps, games, remote
desktops) or the agent is stuck, Haiku looks at a screenshot of the front window and returns one action with
pixel coordinates.

**Composing** — only when you ask for content ("implement the solution", "reply thanking her", "summarize
this"), the agent reads the *full* window text (`ax.deep_text`: the actual problem, email or code, not just
labels) and Opus writes it; long or multi-line output is pasted so editors don't mangle indentation. "Compose
a new message" opens an empty one — it never invents words you didn't ask for.

---

## Memoization: the slow path teaches the fast path

The core design rule: **Jev classifies, models reason, and every reasoning result is stored as a case Jev can
classify next time.** There are four memories, all local in `~/.config/jevme/`:

| Memory | Learned from | Replayed by | Cost the 2nd time |
|---|---|---|---|
| **UI cases** `ui_memory.json` | a Jev pick over the whole tree, or a Haiku screenshot click | one Jev Choice matching your words to known intents in this app, then a live-tree lookup | ~130 ms instead of ~1.5–6 s |
| **Task recipes** `task_memory.json` | an agent run that *verifiably* finished | one Jev Choice matching the goal to past goals; each step resolved from the live tree | no per-step reasoning |
| **Learned tools** `learned.json` | Opus writing a script | Jev routes to it like any built-in tool | 0 model calls |
| **Demonstrations** → `task_memory.json` | you doing it by hand after a command failed | same as recipes | no model ever involved |

### What makes a memo trustworthy

A memory that replays the wrong thing is worse than none, so everything is filtered on the way in:

- **Only verified successes** become recipes (the agent explicitly chose done, or was ≥ 0.9 sure). A run
  that merely ran out of ideas teaches nothing.
- **Only stable targets** are memorized: named controls (buttons, links, tabs, fields…). Chat bubbles,
  timestamps, field values, live counts ("1 Reply"), and long descriptions are rejected.
- **Volatile goals aren't replayed** — messaging/sending, where the recipient and content differ each time.
- **Page-relative requests are never cached** — "the second video" is resolved fresh every time.
- **Replays are checked live** — a memo is used only if Jev says the request matches *and* the element is on
  screen now. If a step can't be resolved (the UI changed), the agent takes over from that point and re-learns.

### Templates, not transcripts

Recipes generalize. Text you typed is stored as a slot tied to the words of the goal, so a recipe learned for
"search discord for **cats**" replays "search discord for **dogs**" by reading the new value off the new goal.
The same applies to targets you named: "open my **cs 343** notes" → clicks the **CS 341** row when you say it.

### Learning from demonstration (`jevme/watch.py`)

When the agent fails, the pill says *"show me, I'll learn it"* and jevme watches you do it by hand — the
cleanest possible training pair (exact goal → exact steps, no model guessing). It records the **role and label**
of each control you click, menu paths ("View > Zoom In"), and named keys/shortcuts. It **never records what you
type**: a field's text is kept only if it's words you just spoke in the command; otherwise the demonstration is
discarded. Password fields are never read. Recording ends after 7 s of inactivity, when you speak the next
command, or on "stop" (which discards it).

---

## Model optimization

Each decision goes to the cheapest thing that can make it correctly.

| Job | Model | Why | Typical latency |
|---|---|---|---|
| Routing every partial, arg extraction, span selection | **Jev** (TypeSafe System One) | calibrated probabilities for typed Choice / yes-no questions; many questions batched in one request | 150–400 ms |
| Next agent step, element picks, memo matching | **Jev** | same — picking among listed options is classification, not generation | ~200 ms |
| "Where do I click?" when the tree is blind | **Claude Haiku 4.5** + screenshot | fast, cheap vision; used only as a fallback | ~1.5 s (first call ~6 s) |
| Writing code, replies, summaries; writing new tools | **Claude Opus 5** | real reasoning over page content; called once per request | 5–20 s |

Techniques that keep it fast and cheap:

- **Memoize every model result into a Jev case** (above) — the expensive path runs once per distinct thing.
- **Batch questions.** Intent, every enum arg, and the text span are one Jev request, not a chain.
- **Speculative args.** Args for every tool are asked alongside the intent, so a commit needs no second call.
- **Accessibility tree before pixels.** Reading controls takes 20–250 ms and costs nothing; a screenshot + vision
  call is the exception.
- **Structured outputs for vision.** Haiku replies against a strict JSON schema (`output_config.format`), so
  there's no text repair and no slow retry path.
- **Deterministic rules before any model** — app inference, site-name snapping ("leetcod.com" → leetcode.com),
  incomplete-phrase detection, and safety policy are plain code.
- **Stream, don't wait.** Acting on completed clauses mid-sentence hides most latency behind your own speech.
- **Warm apps ahead of time.** Chromium/Electron accessibility is switched on when an app comes to the front,
  so its tree is ready before you speak.

Models are configurable: `JEVME_VISION_MODEL`, `JEVME_CODEGEN_MODEL` (see `.env.example`).

---

## Safety

- **Nothing consequential without asking.** The agent and vision won't click Submit, Send, Post, Buy/Pay,
  Delete or Sign out unless your words ask for it. Learned tools that delete, send, pay, or change settings
  wait for a spoken "yes" — every time they run.
- **Generated scripts are gated** by a hard policy, a compile check, and a Jev review, and re-checked on load.
  Spoken values are passed to scripts as data (environment variables / escaped literals), never spliced in as code.
- **Keystroke rate guard** caps synthetic key presses so a runaway loop can't flood the machine.
- **Everything is local** except the API calls themselves. Delete `~/.config/jevme/` to forget everything learned.

## Configuration

All optional, via `.env` or environment: `JEVME_COMMIT_CONFIDENCE` (0.80), `JEVME_STABLE_PARTIALS` (2),
`JEVME_TEXT_PAUSE_S` (0.65), `JEVME_LOCALE` (en-US), `JEVME_VISION_MODEL`, `JEVME_CODEGEN_MODEL`, `JEVME_LOG`.

## Project layout

```
jevme/
  main.py        menu-bar app, pill, ordered task queue, wiring
  speech.py      Apple streaming speech + vocabulary biasing
  router.py      DECIDE: partials → Jev → commit / stream / hand off
  tools.py       built-in shortcut catalog
  agent.py       general agent loop, recipes, replay, compose
  ax.py          accessibility snapshots, clicking, menus
  see.py         "click the X": recall → ordinal → Jev pick → vision
  vision.py      Haiku screenshot fallback (structured outputs)
  ui_memory.py   UI cases + task recipes, with poisoning filters
  watch.py       learning from demonstration
  generator.py   Opus writes new tools; learned.py stores/renders them; policy.py gates them
  jev.py         minimal TypeSafe Jev client
  evalrun.py     routing eval · replay.py  microphone-free replay harness
tests/           unit tests (pytest)
```

## License

MIT — see [LICENSE](LICENSE).
