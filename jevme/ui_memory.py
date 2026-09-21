"""Self-learning UI memory: turn expensive reasoning (Haiku vision, hard picks) into cheap Jev cases.

Jev is a classifier, not a reasoner. So every time something slow — a screenshot sent to Haiku, or a
low-confidence element pick — figures out what to click, we record it as a *case*: in this app, this
intent maps to an element with this signature (role + label), or to a point at this relative spot. Next
time, Jev classifies the spoken request against the app's learned intents (one Choice, ~250 ms) and we
resolve the live element by its signature. The slow path teaches the fast path, so common actions in an
app get faster and cheaper the more they are used.

Stored at ~/.config/jevme/ui_memory.json. Safe by construction: a learned case is only reused when Jev
says the request matches it AND the element (or a same-size window) is actually present right now.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger("jevme.ui_memory")

PATH = Path.home() / ".config" / "jevme" / "ui_memory.json"
MAX_PER_APP = 60


@dataclass
class Case:
    intent: str                 # a spoken phrasing that led here, e.g. "the search box"
    role: str = ""              # tree signature: element role...
    label: str = ""             # ...and label (stable controls only; dynamic labels just won't re-match)
    rel: tuple[float, float] | None = None   # fallback: point as a fraction of the window, for no-tree apps
    source: str = "pick"        # how it was learned: pick | vision | ordinal
    hits: int = 1
    last: float = field(default_factory=lambda: time.time())


class UIMemory:
    def __init__(self) -> None:
        self._apps: dict[str, list[Case]] = {}
        self._lock = threading.Lock()
        self._load()

    # ---------- persistence ----------

    def _load(self) -> None:
        if not PATH.exists():
            return
        try:
            data = json.loads(PATH.read_text())
            for app, cases in data.items():
                self._apps[app] = [Case(**c) for c in cases]
        except Exception as e:  # noqa: BLE001
            log.warning("ui_memory unreadable: %s", e)

    def _save(self) -> None:
        try:
            PATH.parent.mkdir(parents=True, exist_ok=True)
            data = {app: [asdict(c) for c in cases] for app, cases in self._apps.items()}
            tmp = PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(PATH)
        except Exception as e:  # noqa: BLE001
            log.warning("ui_memory save failed: %s", e)

    # ---------- learning ----------

    def remember(self, app: str, intent: str, *, role: str = "", label: str = "",
                 rel: tuple[float, float] | None = None, source: str = "pick") -> None:
        intent = _norm(intent)
        if not app or not intent:
            return
        # "red button we can click that to delete the draft" is a one-off description, not a name the user
        # will say again; memorizing it (from an unverified vision guess) only poisons recall.
        if len(intent.split()) > 6:
            return
        # Only memorize a stable, named control. Refuse dynamic content and volatile intents so the memory
        # can't poison itself with chat bubbles, timestamps, or one-off targets.
        if label:
            if not is_stable_target(role, label) or is_volatile_goal(intent):
                return
        elif rel is None:
            return
        with self._lock:
            cases = self._apps.setdefault(app, [])
            for c in cases:
                same_target = (role and c.role == role and c.label == label) or (rel and c.rel and _close(c.rel, rel))
                if _norm(c.intent) == intent and same_target:
                    c.hits += 1
                    c.last = time.time()
                    c.source = source
                    self._save()
                    return
            cases.append(Case(intent=intent, role=role, label=label, rel=rel, source=source))
            if len(cases) > MAX_PER_APP:
                cases.sort(key=lambda c: (c.hits, c.last))
                del cases[: len(cases) - MAX_PER_APP]
            self._save()
        log.info("learned UI case: [%s] «%s» -> %s", app, intent, label or f"point{rel}")

    # ---------- recall ----------

    def cases(self, app: str) -> list[Case]:
        with self._lock:
            return list(self._apps.get(app, []))

    def has(self, app: str) -> bool:
        return bool(self._apps.get(app))


_norm = lambda s: " ".join(str(s).lower().strip().strip(".?!,").split())  # noqa: E731

# Controls whose label is stable across visits (a button stays "Submit"); worth memorizing.
STABLE_ROLES = {"AXButton", "AXLink", "AXMenuItem", "AXMenuButton", "AXPopUpButton", "AXTab", "AXTabButton",
                "AXCheckBox", "AXRadioButton", "AXComboBox", "AXSearchField", "AXTextField", "AXToggle",
                "AXSwitch", "AXDisclosureTriangle", "AXImage"}
# Goals whose target/content changes every time — never replay these from memory.
VOLATILE_GOAL = re.compile(r"\b(message|text|email|reply|dm|dms|send|post|tweet|whatsapp|imessage|slack|"
                           r"compose|draft|call)\b", re.I)


def is_dynamic_label(label: str) -> bool:
    """A label that names specific content (a chat bubble, a timestamp, a field's value) rather than a
    stable control. Memorizing these poisons the memory, so we refuse them."""
    if not label:
        return True
    if re.search(r"\d{1,2}:\d{2}", label):        # a time / timestamp
        return True
    if any(q in label for q in ('"', '"', '"', "'", " = ")):   # quoted content or a field value
        return True
    if len(label) > 45 or len(label.split()) > 7:  # a sentence, not a control name
        return True
    if re.search("[\u200e\u200f\u202a-\u202e]", label):   # direction marks: Messages wraps message content in them
        return True
    if re.match(r"\d+\s+\w", label):               # "1 Reply", "3 new messages": a live count
        return True
    return False


def is_volatile_goal(goal: str) -> bool:
    return bool(VOLATILE_GOAL.search(goal))


def is_stable_target(role: str, label: str) -> bool:
    return role in STABLE_ROLES and not is_dynamic_label(label)


def _close(a: tuple[float, float], b: tuple[float, float], tol: float = 0.04) -> bool:
    return abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol


_singleton: UIMemory | None = None


def memory() -> UIMemory:
    global _singleton
    if _singleton is None:
        _singleton = UIMemory()
    return _singleton


# ---------- task recipes: memoize a whole multi-step task, replay via Jev matching ----------

TASK_PATH = Path.home() / ".config" / "jevme" / "task_memory.json"


@dataclass
class RecipeStep:
    op: str
    role: str = ""
    label: str = ""
    arg: str = ""          # key name, app name, url, scroll direction, or a text value
    text_from_goal: bool = False   # if the typed text should be taken from the new goal's words


@dataclass
class Recipe:
    goal: str
    app0: str               # front app when the task started (matching context)
    steps: list[RecipeStep] = field(default_factory=list)
    hits: int = 1
    last: float = field(default_factory=lambda: time.time())


class TaskMemory:
    def __init__(self) -> None:
        self._recipes: list[Recipe] = []
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not TASK_PATH.exists():
            return
        try:
            for r in json.loads(TASK_PATH.read_text()):
                steps = [RecipeStep(**s) for s in r.pop("steps", [])]
                self._recipes.append(Recipe(steps=steps, **r))
        except Exception as e:  # noqa: BLE001
            log.warning("task_memory unreadable: %s", e)

    def _save(self) -> None:
        try:
            TASK_PATH.parent.mkdir(parents=True, exist_ok=True)
            data = [{**{k: v for k, v in asdict(r).items() if k != "steps"},
                     "steps": [asdict(s) for s in r.steps]} for r in self._recipes]
            tmp = TASK_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(TASK_PATH)
        except Exception as e:  # noqa: BLE001
            log.warning("task_memory save failed: %s", e)

    def remember(self, goal: str, app0: str, steps: list[RecipeStep], *, min_steps: int = 2) -> bool:
        # Agent runs: single-step tasks are already fast via shortcuts / ui_memory. A demonstration of a
        # single menu choice or click (min_steps=1) is still worth keeping: the agent failed at it.
        if len(steps) < min_steps or not steps:
            return False
        if is_volatile_goal(goal):          # messaging/sending: recipient and content differ every time
            return False
        if app0.lower() in ("loginwindow", ""):
            return False
        if min_steps > 1 and app0.lower() in ("iterm2", "iterm", "terminal"):
            return False                    # agent run started from the terminal: not a real task context
        if any(s.label and is_dynamic_label(s.label) for s in steps if s.op != "type"):
            return False                    # a step targets one-off content; would replay wrongly
        g = _norm(goal)
        with self._lock:
            for r in self._recipes:
                if _norm(r.goal) == g and r.app0 == app0:
                    r.steps, r.hits, r.last = steps, r.hits + 1, time.time()
                    self._save()
                    return True
            self._recipes.append(Recipe(goal=goal, app0=app0, steps=steps))
            self._recipes.sort(key=lambda r: (r.hits, r.last), reverse=True)
            del self._recipes[40:]
            self._save()
        log.info("learned task recipe: «%s» (%d steps)", goal, len(steps))
        return True

    def all(self) -> list[Recipe]:
        with self._lock:
            return list(self._recipes)


_task_singleton: TaskMemory | None = None


def task_memory() -> TaskMemory:
    global _task_singleton
    if _task_singleton is None:
        _task_singleton = TaskMemory()
    return _task_singleton
