"""What the speech recognizer should expect to hear: personalized and live.

Apple's recognizer only knows common English. The names that matter most are the ones it has never heard:
"Two Sum" (heard as "Tucson", "to some", "twosome"), a Discord channel, a site, a coworker's project. So
the recognizer is biased (contextualStrings) with three layers, most specific first:

  1. screen   — names of the controls on screen right now (refreshed when the window changes)
  2. personal — words from commands that worked before, learned click targets, saved recipes, learned tools
  3. base     — site names, installed apps, command words

Privacy: only short labels of standard controls (buttons, links, tabs, menu items…) are used, never field
contents, message text, or anything dynamic, and nothing here is written to disk except the personal
phrases you spoke. Turn the screen layer off with JEVME_SCREEN_VOCAB=0.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path

log = logging.getLogger("jevme.vocab")

PATH = Path.home() / ".config" / "jevme" / "vocab.json"
MAX_SCREEN, MAX_PERSONAL, MAX_TOTAL = 80, 120, 400
SCREEN_ENABLED = os.environ.get("JEVME_SCREEN_VOCAB", "1") != "0"

_lock = threading.Lock()
_screen: list[str] = []
_personal: dict[str, list[float]] = {}     # phrase -> [count, last_used]
_base: list[str] | None = None
_loaded = False
version = 0                                 # bumps when the vocabulary changes; speech re-biases on it


def _phrase_ok(p: str) -> bool:
    words = p.split()
    return 1 <= len(words) <= 4 and 2 <= len(p) <= 32 and bool(re.search(r"[A-Za-z]", p))


def _clean(p: str) -> str:
    p = re.sub(r"[\u200e\u200f\u202a-\u202e]", "", str(p))      # invisible direction marks
    p = re.sub(r"^\d+[.)]\s*", "", p.strip())          # "1. Two Sum" -> "Two Sum"
    p = re.sub(r"\s+\d+(\.\d+)?%.*$", "", p)            # "Two Sum 57.9% Easy" -> "Two Sum"
    return " ".join(p.split()).strip(" .,:;·•|")


# ---------- base ----------

def _base_vocab() -> list[str]:
    global _base
    if _base is None:
        from . import actions as A
        sites = list(A.WELL_KNOWN_SITES) + list(A.SITE_SEARCH)
        dotted = [f"{s} dot com" for s in ("leetcode", "piazza", "github", "amazon", "youtube")]
        words = ["open", "close", "search", "scroll", "click", "type", "paste", "new tab", "new note", "dark mode"]
        try:
            apps = A.installed_apps()
        except Exception:  # noqa: BLE001
            apps = []
        _base = list(dict.fromkeys(sites + dotted + words + apps))
    return _base


# ---------- personal ----------

def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        if PATH.exists():
            _personal.update({k: v for k, v in json.loads(PATH.read_text()).items() if _phrase_ok(k)})
    except Exception as e:  # noqa: BLE001
        log.warning("vocab unreadable: %s", e)
    # Seed from what jevme already learned about this user.
    try:
        from . import ui_memory
        mem = ui_memory.memory()
        for app, cases in list(mem._apps.items()):
            for c in cases:
                for p in (c.label, c.intent):
                    p = _clean(p or "")
                    if _phrase_ok(p) and not ui_memory.is_dynamic_label(p):
                        _personal.setdefault(p, [1, 0.0])
        for r in ui_memory.task_memory().all():
            for s in r.steps:
                p = _clean(s.label or (s.arg if s.op == "open_app" else ""))
                if _phrase_ok(p):
                    _personal.setdefault(p, [1, 0.0])
        from . import tools as T
        for t in T.TOOLS:
            if getattr(t, "learned", None) is not None:
                for ex in t.examples:
                    if _phrase_ok(ex):
                        _personal.setdefault(ex, [1, 0.0])
    except Exception as e:  # noqa: BLE001
        log.debug("vocab seed skipped: %s", e)


def learn(*phrases: str) -> None:
    """Remember names the user said in a command that worked (a site, app, click target, search, title)."""
    global version
    changed = False
    with _lock:
        _load()
        for p in phrases:
            p = _clean(p or "")
            if not _phrase_ok(p):
                continue
            n, _ = _personal.get(p, [0, 0.0])
            _personal[p] = [n + 1, time.time()]
            changed = changed or n == 0
        if changed:
            version += 1
            data = dict(sorted(_personal.items(), key=lambda kv: (-kv[1][0], -kv[1][1]))[:500])
            try:
                PATH.parent.mkdir(parents=True, exist_ok=True)
                tmp = PATH.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, indent=1))
                tmp.replace(PATH)
            except Exception as e:  # noqa: BLE001
                log.warning("vocab save failed: %s", e)


# ---------- screen ----------

def set_screen(labels: list[str]) -> bool:
    """Replace the on-screen layer. Returns True if it changed meaningfully (worth re-biasing)."""
    global _screen, version
    if not SCREEN_ENABLED:
        return False
    out: list[str] = []
    for label in labels:
        p = _clean(label)
        if _phrase_ok(p) and p.lower() not in {x.lower() for x in out}:
            out.append(p)
        if len(out) >= MAX_SCREEN:
            break
    with _lock:
        old = {x.lower() for x in _screen}
        new = {x.lower() for x in out}
        if len(new ^ old) < 3:          # the same screen give or take a label: don't churn the recognizer
            return False
        _screen = out
        version += 1
    return True


def screen_labels_from(snap) -> list[str]:
    """Short names of standard controls in an accessibility snapshot. Never field values or message text."""
    from . import ui_memory
    labels = []
    for e in snap.elems:
        if e.role in ui_memory.STABLE_ROLES and e.role not in ("AXTextField", "AXSearchField", "AXComboBox"):
            p = _clean(e.label)
            if _phrase_ok(p) and not ui_memory.is_dynamic_label(p):
                labels.append(p)
    return labels


def screen_names() -> list[str]:
    with _lock:
        return list(_screen)


# ---------- combined ----------

def phrases() -> list[str]:
    """Contextual strings for the recognizer, most specific first."""
    with _lock:
        _load()
        personal = [p for p, _ in sorted(_personal.items(), key=lambda kv: (-kv[1][0], -kv[1][1]))][:MAX_PERSONAL]
        screen = list(_screen)
    return list(dict.fromkeys(screen + personal + _base_vocab()))[:MAX_TOTAL]
