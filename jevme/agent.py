"""General computer use: observe the screen, choose one action, act, repeat until the goal is met.

Each step is a single Jev request over the front window's accessibility tree:
  op        which operation (click / type / key / scroll / open app / done / stuck)
  target    which on-screen element, for click and type
  text      which span of the user's words to type (never generated), or __compose__
  key/app   speculative enum arguments
  done      has the goal been achieved on the screen as it is now?
No screenshots and no LLM in the loop. Claude is consulted only to compose free text the user did not
dictate ("reply saying I'll be late"), and, through the vision fallback, when the tree is empty.
"""
from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable

from . import actions as A
from . import ax
from . import policy
from . import ui_memory
from .see import BROWSERS
from .jev import JevClient, choice, noul

log = logging.getLogger("jevme.agent")

MAX_STEPS = 12
STALL_STEPS = 5       # this many steps without the goal getting closer = look at pixels, then give up
STEP_TIMEOUT_S = 90
SETTLE_MAX_S = 0.5      # wait at most this long for the screen to react, but proceed the moment it does

COMMON_APPS = ["Google Chrome", "Safari", "Finder", "Notes", "Reminders", "Calendar", "Messages", "Mail",
               "Spotify", "Music", "Slack", "Discord", "Visual Studio Code", "System Settings", "Photos",
               "Maps", "Terminal", "iTerm", "Preview", "Microsoft Word", "Notion", "Calculator"]

OPS = {
    "click": "Click / press one of the listed on-screen elements (buttons, links, menus, fields, rows, tabs).",
    "type": "Type text into the focused or a listed text field (the field is clicked first).",
    "key": "Press a single key such as enter, escape, tab, or a shortcut like cmd+n.",
    "scroll": "Scroll the front window to reveal more content.",
    "open_app": "Bring an application to the front (or launch it) because the task happens there.",
    "open_url": "Open a website or web search in the browser as the next step.",
    "done": "The goal has been fully achieved on the screen as it is now.",
    "stuck": "Nothing on screen leads toward the goal, the task needs information the user did not give, "
             "or it is not something done through app windows at all.",
}

OP_INSTRUCTIONS = (
    "You are driving a Mac to carry out `goal` for a user who spoke it. `app` is in front (window titled "
    "`window`, which for a browser names the page that is open), `elements` is "
    "what is on screen right now (top to bottom, 'id: [region] kind: label'; in a browser [page] is the "
    "website's content and [browser] its toolbar), and `history` lists the steps already taken. Choose the "
    "ONE next operation that makes progress. To reach a website or run a web search, use open_url; never "
    "click or type in the address bar. To search within a site, type into the site's [page] search field "
    "then press enter. Prefer clicking a listed element over keyboard shortcuts. Choose done only when the "
    "screen shows the goal is complete. Choose stuck if the same step keeps repeating without effect, or "
    "nothing on screen can lead to the goal. To switch to an existing tab ('the second tab'), click that "
    "tab in the tab strip, never open a new tab. When a text field already holds the value the goal wants, "
    "do not retype it; move on or choose done. Messages, email, notes, reminders and calendar events live in "
    "their apps (Messages, Mail, Notes, Reminders, Calendar, Discord, Slack...), not on a website: use "
    "open_app for them, never open_url. To message someone, click their conversation (or start a new one), "
    "type the message, then send it with enter or the Send button — typing alone does not send. Gmail, "
    "LinkedIn, Outlook.com, YouTube, LeetCode and other websites are used in the browser, never through the "
    "Mail or Messages apps. A goal that only asks to start a new message, email, note or document (without "
    "saying what it should say) is done once the empty compose window is open — never make up content."
)
VISION_ATTEMPTS = 2

# ---------- goal understanding (deterministic, before the loop) ----------

SEND_GOAL = re.compile(r"\b(send|message|text|reply|dm|tell|post|tweet)\b", re.I)
DRAFT_ONLY = re.compile(r"\b(draft|compose|write up|write out)\b", re.I)
WEB_CONTEXT = re.compile(r"(\.(com|org|io|net|edu|ai|dev)\b|\bwebsite\b|\bbrowser\b|\bin (chrome|safari)\b|"
                         r"\bon (youtube|reddit|amazon|twitter|x|linkedin|facebook|instagram|google)\b)", re.I)
MESSAGE_BODY = re.compile(r"\b(?:saying|that says|to say|say|telling (?:them|him|her))\s+(.+?)[.?!]*$", re.I)
# "text mom that I'm on my way" / "tell alex that the demo moved": a recipient word, then "that <body>".
MESSAGE_BODY_THAT = re.compile(r"\b(?:text|tell|message|dm)\s+(?!that\b)(?:the\s+)?[\w' ]+?\s+that\s+(.+?)[.?!]*$", re.I)
APP_HINTS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bdiscord\b", re.I), "Discord"),
    (re.compile(r"\bslack\b", re.I), "Slack"),
    (re.compile(r"\bwhats ?app\b", re.I), "WhatsApp"),
    (re.compile(r"\btelegram\b", re.I), "Telegram"),
    (re.compile(r"\bsignal\b", re.I), "Signal"),
    (re.compile(r"\bteams\b", re.I), "Microsoft Teams (work or school)"),
    (re.compile(r"\b(e-?mails?|mail|inbox)\b", re.I), "Mail"),
    (re.compile(r"\b(i ?messages?|messages app|texts?)\b", re.I), "Messages"),
    (re.compile(r"\breminders?\b", re.I), "Reminders"),
    (re.compile(r"\b(calendar|meetings?)\b", re.I), "Calendar"),
    (re.compile(r"\bnotes?\b", re.I), "Notes"),
    (re.compile(r"\bspotify\b", re.I), "Spotify"),
]


# Services that are websites. A goal naming one happens in the browser ("compose a new Gmail email" went to
# the Mail app; "open my messages on LinkedIn" went to Messages).
WEB_SERVICES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bg ?mail\b", re.I), "gmail"),
    (re.compile(r"\blinked ?in\b", re.I), "linkedin"),
    (re.compile(r"\boutlook(\.com| web)\b", re.I), "outlook"),
    (re.compile(r"\byou ?tube\b", re.I), "youtube"),
    (re.compile(r"\bleet ?code\b", re.I), "leetcode"),
    (re.compile(r"\bgoogle (docs?|drive|calendar|maps)\b", re.I), None),
    (re.compile(r"\b(reddit|github|instagram|facebook|piazza|gradescope|canvas|chatgpt|twitter|netflix|amazon)\b", re.I), None),
]
# Goals that ask for text to be written (so composing it is wanted), vs. just opening a compose window.
WANTS_CONTENT = re.compile(
    r"\b(implement\w*|solv\w*|solutions?|code|coding|function|program\w*|algorithm\w*|summar\w*|explain\w*|"
    r"translat\w*|rewrit\w*|repl(y|ies)|respond\w*|answer\w*|essay|poem|paragraph|story|about|regarding|asking|"
    r"thanking|apologi\w*|inviting|describing|finish\w*|complete|fill\s+(in|out)|fill\s+\w+(\s+\w+)?\s+(in|out)|"
    r"write (a|an|the|me|some|something|up|out))\b", re.I)
# Goals that need an action taken now; "done" before doing anything is wrong for these ("run the solution"
# was judged already done because a previous run's results were on screen).
NEEDS_ACTION = re.compile(
    r"\b(run|submit|send|click|press|delete|play|save|clear|type|search|compose|create|add|make|remove|select|"
    r"pick|choose|implement|solve|write|reply|get rid|draft|post|upload|download|rename|move|copy|paste|refresh|"
    r"reload)\b", re.I)


# "clear the code", "get rid of the text in here", "empty this field": select-all + delete inside a text
# element. Only ever applied to a text field/editor (never a list, where select-all + delete would remove
# every email or file).
CLEAR_TEXT = re.compile(r"\b(clear|delete|remove|erase|wipe|empty|get rid of)\b.*\b(code|text|editor|field|box|input|"
                        r"in here|in there|everything in (it|this|here))\b", re.I)
TEXT_ROLES = ("AXTextArea", "AXTextField", "AXSearchField", "AXComboBox")


def web_service(goal: str) -> str | None:
    """The website a goal names ('gmail', 'linkedin', …), as an open_site key; None if it names none."""
    for pat, key in WEB_SERVICES:
        m = pat.search(goal)
        if m:
            return key or m.group(0).lower()
    return None


def infer_app(goal: str) -> str | None:
    """The app a goal belongs in when it doesn't say so plainly: 'send a message to Sarah' → Messages,
    'go to my DMs in discord' → Discord. None for web tasks or when no installed app fits."""
    if WEB_CONTEXT.search(goal) or web_service(goal):
        return None
    available = set(A.installed_apps()) | set(A.running_apps())
    for pat, app in APP_HINTS:
        if pat.search(goal) and app in available:
            return app
    if SEND_GOAL.search(goal) and "Messages" in available:
        return "Messages"   # a message with no platform named: iMessage
    return None


def message_body(goal: str) -> str | None:
    m = MESSAGE_BODY.search(goal) or MESSAGE_BODY_THAT.search(goal)
    return m.group(1).strip() if m else None


def template_fill(old_goal: str, old_value: str, new_goal: str) -> str | None:
    """Treat the old goal as a template around the value that was typed, and read the new value off the new
    goal: ("search discord for cats", "cats", "search discord for dogs") → "dogs". None if the new goal
    doesn't have the same words around the slot."""
    norm = lambda s: re.sub(r"[^\w']+", " ", s.lower()).split()  # noqa: E731
    og, ov, ng = norm(old_goal), norm(old_value), norm(new_goal)
    if not ov:
        return None
    for i in range(len(og) - len(ov) + 1):
        if og[i:i + len(ov)] == ov:
            pre, post = og[:i], og[i + len(ov):]
            if ng[:len(pre)] == pre and (not post or ng[len(ng) - len(post):] == post):
                mid = ng[len(pre):len(ng) - len(post) if post else len(ng)]
                if mid:
                    # Return the new goal's own spelling of those words.
                    words = re.findall(r"[\w']+", new_goal)
                    return " ".join(words[len(pre):len(pre) + len(mid)])
    return None


def _same_text(a: str, b: str) -> bool:
    norm = lambda s: re.sub(r"[^\w']+", " ", s.lower()).strip()  # noqa: E731
    return bool(a) and bool(b) and norm(a) == norm(b)

KEYS = {"enter": "return / submit", "escape": None, "tab": None, "space": None, "delete": "backspace",
        "down": "arrow down", "up": "arrow up", "cmd+n": "new item", "cmd+t": "new tab", "cmd+w": "close tab/window",
        "cmd+s": "save", "cmd+enter": "send / confirm", "cmd+a": "select all", "cmd+f": "find", "cmd+l": "address bar"}


@dataclass
class Step:
    op: str
    detail: str


@dataclass
class Result:
    ok: bool
    summary: str
    steps: list[Step] = field(default_factory=list)


class Agent:
    def __init__(self, jev: JevClient, *, compose: Callable[[str, str], str] | None = None) -> None:
        self.jev = jev
        self.compose = compose      # (goal, screen_summary) -> text, via Claude
        self.cancelled = False
        self.running = False
        self.app: str | None = None

    def cancel(self) -> None:
        self.cancelled = True

    # ---------- one step ----------

    def _decide(self, goal: str, snap: ax.Snapshot, history: list[Step]) -> dict:
        crit = snap.criteria(only_clickable=False)
        listing = [f"{k}: {v}" for k, v in crit.items()]
        state = {"goal": goal, "app": snap.app, "window": A.front_window_title() or "untitled",
                 "elements": listing[:220],
                 "history": [f"{i+1}. {s.op}: {s.detail}" for i, s in enumerate(history)] or ["nothing yet"]}
        qs: dict = {
            "op": choice(OP_INSTRUCTIONS, OPS),
            "done": noul("Given `goal`, `history` and what `elements` show is on screen now, has the goal already been "
                         "fully achieved? Only true if the screen itself shows the finished result. A message, "
                         "email or post the goal asks to SEND is done only once it has been sent (history shows "
                         "it was sent, or the compose box is empty again) — merely typing it is not done.",
                         true="The goal is complete on screen.", false="More steps are needed, or it is unclear."),
            "key": choice(["Assume the next operation is 'key'.", "Which key or shortcut?"], KEYS),
            "app": choice(["Assume the next operation is 'open_app'.", "Which application does the goal need?"],
                          {name: None for name in self._app_options()}),
            "scroll_dir": choice("If scrolling, which way?", {"down": None, "up": None}),
        }
        click_crit = dict(snap.criteria(only_clickable=True))
        if click_crit:
            click_crit["__none__"] = "None of the listed elements is the right target."
            qs["target"] = choice(["Assume the next operation is 'click' or 'type'.",
                                   "Which listed element should be clicked, or typed into?",
                                   "For 'type', pick the text field; if the cursor is already in the right field "
                                   "and no field is listed, choose __none__."], click_crit)
        spans = self._spans(goal)
        span_crit = {s: None for s in spans}
        span_crit["__compose__"] = ("The words to type are not in the goal verbatim and must be written "
                                    "(a reply, a message body, a summary).")
        span_crit["__none__"] = "Nothing needs typing."
        qs["text"] = choice(["Assume the next operation is 'type'.",
                             "Which exact words from `goal` should be typed, and nothing else (no 'type', 'search "
                             "for', 'called', 'saying')? If the goal quotes the words (after 'type', 'say', 'write', "
                             "'called', 'titled', 'search for'), choose that span. Choose __compose__ only when the "
                             "goal describes what to write without giving the words."], span_crit)
        qs["url"] = choice(["Assume the next operation is 'open_url'.", "Which site or search from `goal`?"],
                           {**{s: None for s in spans}, "__none__": "no site named"})
        answers, ms = self.jev.ask(state, qs)
        out = {k: (a.choice if a.kind == "choice" else a.noul) for k, a in answers.items()}
        out["_conf"] = answers["op"].confidence if "op" in answers else 0.0
        out["_ms"] = ms
        return out

    @staticmethod
    def _app_options() -> list[str]:
        installed = set(A.installed_apps())
        # Running apps always exist even if not under /Applications (Finder, menu-bar apps).
        return list(dict.fromkeys(A.running_apps() + [n for n in COMMON_APPS if n in installed] + ["Finder"]))

    @staticmethod
    def _spans(goal: str) -> list[str]:
        """Candidate word runs from the goal. Runs that end a clause come first: dictated text usually does."""
        words = goal.split()
        n = len(words)
        ends = {n} | {i + 1 for i, w in enumerate(words) if w.endswith((",", ".", ";", "?", "!"))}
        out: list[str] = []

        def add(i, j):
            s = " ".join(words[i:j]).strip().strip(".?!,;\"'")
            if s and s not in out:
                out.append(s)
        for j in sorted(ends, reverse=True):
            for i in range(max(0, j - 14), j):
                add(i, j)
        for i in range(n):
            for j in range(i + 1, min(n, i + 10) + 1):
                add(i, j)
        return out[:160]

    # ---------- run ----------

    def run(self, goal: str, *, progress: Callable[[str], None] | None = None, allow_replay: bool = True) -> Result:
        self.running = True
        try:
            return self._run(goal, progress=progress, allow_replay=allow_replay)
        finally:
            self.running = False

    def _run(self, goal: str, *, progress: Callable[[str], None] | None = None, allow_replay: bool = True) -> Result:
        if allow_replay:
            # A fresh task: clear any stop aimed at the previous one BEFORE replay checks it. (The nested
            # run() a diverged replay falls into keeps the flag, so a stop said during replay still holds.)
            self.cancelled = False
            replayed = self.try_replay(goal, progress)
            if replayed is not None:
                return replayed
        t0 = time.monotonic()
        history: list[Step] = []
        self.recipe: list[ui_memory.RecipeStep] = []
        self.app: str | None = None      # the app the task is happening in, once we switched to one
        self.app0 = A.frontmost_app()
        # Put the task in the right app before reasoning: a messaging goal said while Chrome is in front
        # should start in Messages, not search Google for "Sarah".
        site = web_service(goal)
        if site:
            self._ensure_site(site, history, progress)
        target = infer_app(goal)
        if target:
            if self.app0.lower() != target.lower():
                real = A.open_app(target)
                if not real:
                    log.info("inferred app %s isn't installed; reasoning from %s", target, self.app0)
                    target = None
            if target and self.app0.lower() != target.lower():
                self.app = real or target
                ax.wait_until_ready(self.app, timeout=8.0)   # cold Electron launches take seconds
                self._record("open_app", arg=self.app)
                history.append(Step("open_app", f"opened {self.app}"))
                if progress:
                    progress(f"step 0: opened {self.app}")
                time.sleep(0.4)
            else:
                self.app = target
        self._positional = False
        if CLEAR_TEXT.search(goal):
            done = self._clear_text(progress)
            if done:
                history.append(Step("clear", done))
        # "the second video", "a random problem": resolved by position, deterministically, before reasoning.
        # (Asked to pick "a random one", the model kept picking the same item and then wandered off.)
        from . import see
        if see.ORDINAL_RE.search(goal):
            snap = ax.snapshot(app_name=self.app)
            el = see.ordinal_pick(goal, snap)
            if el is not None:
                detail = ax.press(el)
                self._positional = True       # a recipe would replay this exact item: don't memoize
                history.append(Step("click", detail))
                log.info("positional pick: %s", el.describe())
                if progress:
                    progress(f"step 0: {detail}")
                time.sleep(0.6)
        last_sig = None
        last_action = None
        best_done, best_step = 0.0, 0
        last_low = False
        repeats = 0
        looks = 0
        for step_no in range(1, MAX_STEPS + 1):
            if self.cancelled:
                return Result(False, "cancelled", history)
            if time.monotonic() - t0 > STEP_TIMEOUT_S:
                return Result(False, "took too long", history)
            snap = ax.snapshot(app_name=self.app)
            if snap.app == "loginwindow":
                return Result(False, "screen is locked", history)
            # The app the task lives in isn't running (quit, or a slow cold launch). Launch it and wait for a
            # real window; if it never comes up, fail clearly rather than act on some other app.
            if self.app and snap.pid == 0:
                A.open_app(self.app)
                if not ax.wait_until_ready(self.app, timeout=8.0):
                    return Result(False, f"{self.app} didn't open", history)
                snap = ax.snapshot(app_name=self.app)
            if len(snap.elems) < 4 and history:
                time.sleep(0.6)                      # something is probably still loading
                snap = ax.snapshot(app_name=self.app)
            sig = (snap.app, tuple((e.role, e.label) for e in snap.elems[:60]))
            try:
                d = self._decide(goal, snap, history)
            except Exception as e:  # noqa: BLE001 — the client already retried; the service is down
                log.warning("agent: jev failed: %s", str(e).splitlines()[0])
                return Result(False, "Jev unavailable — try again in a moment", history)
            op = d.get("op", "stuck")
            if op == "open_app" and site and d.get("app") not in BROWSERS:
                op, d["url"] = "open_url", site          # a web service: never its look-alike native app
            # open_url with no URL in the goal is a guaranteed no-op (the log showed three in a row). If the
            # task belongs in an app, go there; otherwise treat it as stuck so we escalate instead of looping.
            if op == "open_url" and d.get("url") in (None, "__none__"):
                target = infer_app(goal)
                if target and (self.app or "").lower() != target.lower():
                    op, d["app"] = "open_app", target
                else:
                    op = "stuck"
            log.info("step %d [%s]: op=%s conf=%.2f done=%.2f target=%s text=%s (%d elems, jev %d ms)",
                     step_no, snap.app, op, d["_conf"], d.get("done", 0), d.get("target"), d.get("text"), len(snap.elems), d["_ms"])

            done_p = d.get("done", 0) or 0
            if done_p > best_done + 0.05:
                best_done, best_step = done_p, step_no
            if op == "done" and not history:
                if NEEDS_ACTION.search(goal):
                    op = "stuck"      # nothing has been done yet: look closer rather than claim success
                else:
                    return Result(True, f"Already done: {goal}", history)
            elif step_no - best_step >= STALL_STEPS and op != "done":
                log.info("no progress in %d steps; escalating", step_no - best_step)
                op = "stuck"
                best_step = step_no   # give the pixel look a fresh window before the next escalation

            # "Verified" completion = the model explicitly chose done, or is very confident it's finished
            # after real actions. Only these teach a recipe; a loose 0.7 after one step does not.
            if op == "done" and history:
                self._save_recipe(goal, verified=d.get("done", 0) >= 0.8)
                return Result(True, f"Done: {goal}", history)
            if d.get("done", 0) >= 0.9 and history:
                self._save_recipe(goal, verified=True)
                return Result(True, f"Done: {goal}", history)
            if d.get("done", 0) >= 0.7 and history:
                return Result(True, f"Done: {goal}", history)   # done, but not confident enough to memorize

            # Escalate when the same action on the same target repeats (a click that does nothing, or a
            # toggle that flips the tree so the whole-screen signature never matches), or when the model is
            # just guessing (low confidence with no progress). Two strikes → look at pixels; then give up.
            tgt = d.get("target")
            action_key = (op, tgt, d.get("app"), d.get("key"))
            low = d["_conf"] < 0.45
            if action_key == last_action or sig == last_sig or (low and last_low):
                repeats += 1
            else:
                repeats = 0
            last_action = action_key
            last_sig = sig
            last_low = low
            if op == "stuck" or repeats >= 2:
                # The tree gave nothing useful: look at pixels (a couple of times at most), then continue.
                looks += 1
                if looks > VISION_ATTEMPTS:
                    return Result(False, "stuck: nothing on screen leads there", history)
                from . import vision
                try:
                    # Not memorized: a pixel guess partway through a multi-step goal isn't a stable mapping
                    # from the goal to one element ("select a random problem" learned one fixed problem).
                    out, _ = vision.act_located(goal, snap, intent="step")
                    history.append(Step("look", out))
                    if progress:
                        progress(f"step {step_no}: {out}")
                    if out.startswith("Done"):
                        return Result(True, f"Done: {goal}", history)
                    if out.startswith("Couldn't"):
                        return Result(False, "stuck: nothing on screen leads there", history)
                    time.sleep(0.3)
                    continue
                except Exception as e:  # noqa: BLE001
                    log.warning("vision fallback failed: %s", e)
                    return Result(False, "stuck", history)

            detail = self._act(op, d, snap, goal)
            history.append(Step(op, detail))
            if progress:
                progress(f"step {step_no}: {detail}")
            # Navigation and app launches need a real pause; in-app clicks/keys react fast.
            time.sleep(0.5 if op in ("open_app", "open_url") else 0.2)
        return Result(False, "ran out of steps", history)

    # ---------- recipes: record on success, replay when a goal recurs ----------

    def _record(self, op: str, *, el: ax.Elem | None = None, arg: str = "", text_from_goal: bool = False) -> None:
        if not hasattr(self, "recipe"):
            return
        self.recipe.append(ui_memory.RecipeStep(
            op=op, role=el.role if el else "", label=el.label if el else "", arg=arg, text_from_goal=text_from_goal))

    def _save_recipe(self, goal: str, verified: bool) -> None:
        # Only memorize a task that genuinely finished (the model clearly saw it done), never a run that
        # merely ran out of ideas. ui_memory applies the further stable-target / volatile-goal filters.
        if verified and getattr(self, "recipe", None) and not getattr(self, "_positional", False):
            ui_memory.task_memory().remember(goal, getattr(self, "app0", ""), self.recipe)

    def try_replay(self, goal: str, progress: Callable[[str], None] | None):
        """If this goal (or one meaning the same) was done before, replay the recorded steps by resolving
        each target from the live tree — no per-step reasoning. Falls back to None if it can't be replayed."""
        recipes = ui_memory.task_memory().all()
        if not recipes:
            return None
        crit = {f"r{i}": r.goal for i, r in enumerate(recipes)}
        crit["__none__"] = "None of these past tasks is what the user is asking for now."
        try:
            answers, _ = self.jev.ask({"request": goal, "past_tasks": [r.goal for r in recipes]},
                                      {"match": choice("`request` is a task the user just spoke. `past_tasks` are tasks "
                                                       "done before. Which past task means the same thing (same action, "
                                                       "even if worded differently, and any specific value like a name "
                                                       "can differ)? __none__ if none.", crit)})
        except Exception:  # noqa: BLE001
            return None
        a = answers.get("match")
        if a is None or a.choice in (None, "__none__"):
            return None
        recipe = recipes[int(a.choice[1:])]
        log.info("replay recipe «%s» (%d steps) for «%s»", recipe.goal, len(recipe.steps), goal)
        self.app = None
        for i, step in enumerate(recipe.steps):
            if self.cancelled:
                return Result(False, "cancelled")
            if not self._replay_step(step, goal, progress, recipe.goal):
                # Resolution failed — hand the rest to the reasoning loop, which also re-learns the recipe.
                log.info("replay diverged at step %d (%s); reasoning instead", i + 1, step.op)
                return self.run(goal, progress=progress, allow_replay=False)
            time.sleep(0.3 if step.op in ("open_app", "open_url") else 0.15)
        return Result(True, f"Done: {goal}")

    @staticmethod
    def _replay_text(step: ui_memory.RecipeStep, goal: str, recipe_goal: str) -> str | None:
        """The text a replayed 'type' step should enter. Text that came from the spoken goal ('text sam
        saying running late') must come from the NEW goal, not be retyped from the old one. None = can't
        tell, so reason instead."""
        if not step.text_from_goal or _same_text(goal, recipe_goal):
            return step.arg
        body = message_body(goal)
        if body:
            return body
        return template_fill(recipe_goal, step.arg, goal)

    def _replay_step(self, step: ui_memory.RecipeStep, goal: str, progress, recipe_goal: str = "") -> bool:
        if progress:
            progress(f"↺ {step.op} {step.label or step.arg}")
        if step.op == "open_app":
            self.app = A.open_app(step.arg) or None
            return True
        if step.op == "open_url":
            A.open_site(step.arg)
            self._wait_for_page()
            return True
        if step.op == "menu":
            return ax.press_menu(self.app, step.arg.split(" > "))
        if step.op in ("key",):
            k = step.arg
            (A.keystroke(k.split("+")[-1], *k.split("+")[:-1]) if "+" in k else A.keystroke(k))
            return True
        if step.op == "scroll":
            A.scroll(step.arg or "down")
            return True
        snap = ax.snapshot(app_name=self.app)
        if step.op == "click":
            if step.text_from_goal and not _same_text(goal, recipe_goal):
                # The click target was named in the goal: follow the new goal's words.
                label = template_fill(recipe_goal, step.label, goal)
                if not label:
                    return False
                step = ui_memory.RecipeStep(step.op, step.role, label, step.arg)
            el = self._match(step, snap)
            if el is None:
                return False
            ax.press(el, step.arg or "click")      # arg: "" | "right" | "double" (demonstrations)
            return True
        if step.op == "type":
            text = self._replay_text(step, goal, recipe_goal)
            if text is None:
                return False
            if step.role:
                el = self._match(step, snap)
                if el is not None:
                    ax.focus(el)
                    time.sleep(0.12)
            A.type_text(text)
            return True
        return False

    @staticmethod
    def _match(step: ui_memory.RecipeStep, snap: ax.Snapshot) -> ax.Elem | None:
        exact = [e for e in snap.elems if e.role == step.role and e.label == step.label]
        if exact:
            return exact[0]
        loose = [e for e in snap.elems if e.role == step.role and step.label and step.label.lower() in e.label.lower()]
        return loose[0] if loose else None

    # ---------- act ----------

    def _act(self, op: str, d: dict, snap: ax.Snapshot, goal: str) -> str:
        def target_el() -> ax.Elem | None:
            t = d.get("target")
            if not t or t == "__none__":
                return None
            return next((e for e in snap.elems if f"e{e.idx}" == t), None)

        if op == "click":
            el = target_el()
            if el is None:
                # No listed element fit but the model still said click: look at pixels rather than stall.
                from . import vision
                try:
                    return vision.act(goal, snap, intent="click")
                except Exception:  # noqa: BLE001
                    return "nothing to click"
            if (what := policy.unrequested_commit(el.label, goal)):
                return f"did not {what} ({el.label}): the goal didn't ask to"
            self._record("click", el=el)
            return ax.press(el)
        if op == "type":
            el = target_el()
            text = d.get("text")
            if text in (None, "__none__"):
                return "nothing to type"
            if text == "__compose__":
                if not WANTS_CONTENT.search(goal):
                    # "compose a new message" asks for a compose window, not for us to invent its words.
                    return "nothing to write: the goal doesn't say what to write"
                if not self.compose:
                    return "cannot compose text"
                content = ax.deep_text(app_name=self.app)   # full page/app text, not just labels
                text = self.compose(goal, content)
            # Typing a site into the address bar is really navigation: do it properly.
            if el is not None and el.region == "browser" and "address" in el.label.lower():
                A.open_site(text)
                time.sleep(0.8)
                return f"opened {text}"
            if el is not None and el.role in ("AXTextField", "AXTextArea", "AXSearchField", "AXComboBox"):
                ax.focus(el)
                time.sleep(0.15)
                if el.value:
                    A.keystroke("a", "cmd")   # replace what is there rather than appending
            composed = d.get("text") == "__compose__"
            self._record("type", el=el, arg=text, text_from_goal=not composed)
            # Long or multiline content (code, a drafted reply, a summary) is pasted, not typed, so editors
            # don't mangle it with auto-indent. "solve/implement/fill/rewrite" replaces the field's contents.
            if composed and ("\n" in text or len(text) > 120):
                replace = bool(re.search(r"\b(solve|implement|fill|rewrite|replace|the whole|entire)\b", goal, re.I))
                A.paste_text(text, replace_all=replace)
                return f"wrote {len(text)} chars"
            A.type_text(text)
            # The user dictated a message and asked to send it; this was the body. Send it now instead of
            # leaving it sitting in the compose box (the model kept stopping at "typed").
            body = message_body(goal)
            if (not composed and body and _same_text(text, body) and SEND_GOAL.search(goal)
                    and not DRAFT_ONLY.search(goal)):
                time.sleep(0.25)
                from . import see
                sent = see.send_draft()
                self._record("key", arg="enter")
                return f"typed “{text[:40]}” and {sent.lower()}"
            return f"typed “{text[:40]}”"
        if op == "key":
            key = d.get("key") or "enter"
            if "+" in key:
                mods, k = key.split("+")[:-1], key.split("+")[-1]
                A.keystroke(k, *mods)
            else:
                A.keystroke(key)
            self._record("key", arg=key)
            return f"pressed {key}"
        if op == "scroll":
            direction = d.get("scroll_dir") or "down"
            A.scroll(direction)
            self._record("scroll", arg=direction)
            return f"scrolled {direction}"
        if op == "open_app":
            app = d.get("app") or ""
            if app and self.app and app.lower() == self.app.lower() and A.frontmost_app().lower() == app.lower():
                return f"{app} already in front"
            real = A.open_app(app) if app else ""
            if not real:
                return f"there is no app called {app!r}; try a website or another app"
            self.app = real
            self._record("open_app", arg=real)
            if self.app:
                ax.wait_until_ready(self.app, timeout=8.0)
            return f"opened {real or app}"
        if op == "open_url":
            if self.app is None:
                self.app = A.frontmost_app() or None
            u = d.get("url")
            if not u or u == "__none__":
                return "no url"
            self._record("open_url", arg=u)
            A.open_site(u)
            self._wait_for_page()
            return f"opened {u}"
        return "no-op"

    def _clear_text(self, progress) -> str | None:
        """Select everything in the focused text element (or the largest editor on screen) and delete it."""
        import ApplicationServices as AS
        sys_el = AS.AXUIElementCreateSystemWide()
        AS.AXUIElementSetMessagingTimeout(sys_el, 0.3)
        focused = ax._attr(sys_el, "AXFocusedUIElement")
        role = ax._s(ax._attr(focused, "AXRole")) if focused is not None else ""
        if role not in TEXT_ROLES or ax._s(ax._attr(focused, "AXSubrole")) == "AXSecureTextField":
            snap = ax.snapshot(app_name=self.app)
            fields = [e for e in snap.elems if e.role in TEXT_ROLES]
            if not fields:
                return None                     # no editor on screen: let the agent reason instead
            el = max(fields, key=lambda e: e.w * e.h)
            ax.focus(el)
            time.sleep(0.2)
        A.keystroke("a", "cmd")
        time.sleep(0.1)
        A.keystroke("delete")
        self._record("key", arg="cmd+a")
        self._record("key", arg="delete")
        if progress:
            progress("step 0: cleared the editor")
        log.info("cleared text (select all + delete)")
        return "selected all the text and deleted it"

    def _ensure_site(self, site: str, history: list, progress) -> None:
        """Be on `site` in the browser before reasoning. Stay if the front tab is already on it."""
        front = A.frontmost_app()
        title = re.sub(r"\s+", "", A.front_window_title().lower())
        if front in BROWSERS and re.sub(r"\s+", "", site) in title:
            self.app = front
            return
        url = A.open_site(site)
        self.app = A.frontmost_app() or None
        self._wait_for_page()
        self._record("open_url", arg=site)
        history.append(Step("open_url", f"opened {url}"))
        if progress:
            progress(f"step 0: opened {site}")

    def _wait_for_page(self, timeout: float = 2.5) -> None:
        """After navigation, wait until the page exposes a reasonable number of elements."""
        end = time.monotonic() + timeout
        time.sleep(0.5)
        while time.monotonic() < end:
            s = ax.snapshot(budget_s=0.3, app_name=self.app)
            if sum(1 for e in s.elems if e.region == "page") >= 8:
                return
            time.sleep(0.3)


def compose_with_claude(goal: str, content: str) -> str:
    """Reason over the on-screen content and produce the text to insert: a reply, a summary, or code.

    `content` is the full readable text of the window (from ax.deep_text), so the model can work from the
    actual problem/email/article, not just control labels. Uses the Anthropic API when a key is present
    (faster, and Opus reasons well), else the signed-in Claude Code CLI.
    """
    import os
    coding = bool(re.search(r"\b(solv\w*|implement\w*|solutions?|code|coding|function|leetcode|bug|fix|program\w*|algorithm\w*)\b", goal, re.I))
    prompt = (
        f'A Mac voice assistant is carrying out this spoken request: "{goal}".\n'
        f"It will insert your reply into the focused field/editor. Here is the full readable text on "
        f"screen (accessibility tree; sidebars and menus may be mixed in — focus on the relevant part):\n\n"
        f"{content[:9000]}\n\n"
        + ("Write ONLY the code to insert (the complete solution in the language the page expects, matching "
           "any starter signature). No markdown fences, no explanation."
           if coding else
           "Write ONLY the text to insert: the reply, summary, note, or content the request asks for. "
           "No preamble, no quotes, no sign-off unless asked."))
    try:
        import anthropic
        if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            r = anthropic.Anthropic().messages.create(
                model="claude-opus-5", max_tokens=2000, output_config={"effort": "medium"},
                messages=[{"role": "user", "content": prompt}])
            text = "".join(b.text for b in r.content if b.type == "text").strip()
            return _strip_fences(text)
    except Exception as e:  # noqa: BLE001
        log.warning("compose via sdk failed: %s", e)
    r = subprocess.run(["claude", "-p", prompt, "--model", "opus", "--output-format", "text"],
                       capture_output=True, text=True, timeout=120)
    return _strip_fences(r.stdout.strip()) if r.returncode == 0 else ""


def _strip_fences(text: str) -> str:
    m = re.match(r"^```[a-zA-Z0-9]*\n(.*)\n```\s*$", text, re.S)
    return m.group(1) if m else text
