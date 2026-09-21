"""Point at things on screen by description: snapshot → Jev picks the element → act.

Falls back to a screenshot + Claude vision when the app exposes nothing useful.
"""
from __future__ import annotations

import logging
import re as _re
import time

from . import actions as A
from . import ax
from . import ui_memory
from .jev import JevClient, choice

log = logging.getLogger("jevme.see")

_jev: JevClient | None = None


def jev() -> JevClient:
    global _jev
    if _jev is None:
        _jev = JevClient()
    return _jev


PICK_INSTRUCTIONS = (
    "`request` is what the user said while looking at `app`. `elements` lists what is on screen right now, "
    "top to bottom, as 'id: [region] kind: label'. Which single element does the user mean? Ordinals ('the second "
    "video') count only elements of that kind in the order listed. In a browser, [page] elements are the "
    "website's own content and [browser] elements are the toolbar and tabs: 'the search box' means the "
    "website's search field when one exists, not the address bar, unless the user says address bar or URL. "
    "Choose __none__ if nothing listed fits."
)


POSITIONAL = _re.compile(
    r"\b(first|second|third|fourth|fifth|sixth|last|top|next|previous|1st|2nd|3rd|4th|5th)\b|"
    r"\bnumber\s+\w+\b", _re.I)


def is_positional(request: str) -> bool:
    """Requests like 'the first link' / 'second video' are page-relative: their target changes per page,
    so they must never be memoized or recalled as a fixed element — resolve them fresh each time."""
    return bool(POSITIONAL.search(request))


def recall(request: str, snap: ax.Snapshot) -> ax.Elem | None:
    """Try the learned cases for this app first: one Jev Choice matches the request to a known intent,
    then we resolve the live element by its stored signature. This is the fast path that vision teaches."""
    if is_positional(request) or ui_memory.is_volatile_goal(request):
        return None
    mem = ui_memory.memory()
    cases = mem.cases(snap.app)
    if not cases:
        return None
    seen: dict[str, ui_memory.Case] = {}
    for c in cases:
        seen.setdefault(c.intent, c)
    intents = list(seen)
    crit = {f"c{i}": intent for i, intent in enumerate(intents)}
    crit["__none__"] = "The request is not any of these learned actions."
    answers, ms = jev().ask(
        {"request": request, "app": snap.app, "known_actions": intents},
        {"match": choice("`request` is what the user said in `app`. `known_actions` are actions learned here "
                         "before. Which learned action means the same thing as the request? __none__ if none match.",
                         crit)})
    a = answers.get("match")
    if a is None or a.choice in (None, "__none__"):
        return None
    case = seen[intents[int(a.choice[1:])]]
    el = _resolve(case, snap)
    if el is not None:
        log.info("recall: [%s] «%s» → %s (%d ms, learned via %s)", snap.app, case.intent, el.describe(), ms, case.source)
    return el


def _resolve(case: ui_memory.Case, snap: ax.Snapshot) -> ax.Elem | None:
    if case.label:
        exact = [e for e in snap.elems if e.role == case.role and e.label == case.label]
        if exact:
            return exact[0]
        loose = [e for e in snap.elems if e.role == case.role and case.label.lower() in e.label.lower()]
        if loose:
            return loose[0]
    if case.rel and snap.window:
        wx, wy, ww, wh = snap.window
        x, y = wx + case.rel[0] * ww, wy + case.rel[1] * wh
        # nearest interactive element to the learned point
        near = min((e for e in snap.elems), key=lambda e: (e.cx - x) ** 2 + (e.cy - y) ** 2, default=None)
        if near is not None and (near.cx - x) ** 2 + (near.cy - y) ** 2 < (0.06 * ww) ** 2:
            return near
    return None


def learn(request: str, snap: ax.Snapshot, el: ax.Elem, source: str) -> None:
    """Record a resolved target so a future request can skip straight to Jev classification."""
    if is_positional(request) or source == "ordinal":
        return   # page-relative targets ("the first link") are never stable enough to memoize
    rel = None
    if snap.window:
        wx, wy, ww, wh = snap.window
        if ww > 0 and wh > 0:
            rel = (round((el.cx - wx) / ww, 3), round((el.cy - wy) / wh, 3))
    ui_memory.memory().remember(snap.app, request, role=el.role, label=el.label, rel=rel, source=source)


def pick(request: str, snap: ax.Snapshot, *, only_clickable: bool = True) -> tuple[ax.Elem | None, float]:
    crit = snap.criteria(only_clickable=only_clickable)
    if not crit:
        return None, 0.0
    crit["__none__"] = "Nothing on this list is what the user means."
    listing = [f"{k}: {v}" for k, v in crit.items() if k != "__none__"]
    answers, ms = jev().ask({"request": request, "app": snap.app, "elements": listing},
                            {"target": choice(PICK_INSTRUCTIONS, crit)})
    a = answers.get("target")
    if a is None or a.choice in (None, "__none__"):
        log.info("pick: none (%d ms) for «%s»", ms, request)
        return None, a.confidence if a else 0.0
    idx = int(a.choice[1:])
    el = next((e for e in snap.elems if e.idx == idx), None)
    log.info("pick: %s conf=%.2f (%d ms) for «%s»", el.describe() if el else a.choice, a.confidence, ms, request)
    return el, a.confidence


BROWSERS = {"Google Chrome", "Safari", "Arc", "Brave Browser", "Microsoft Edge", "Firefox", "Chromium"}


def wait_for_screen(timeout: float = 3.5, min_elems: int = 8, want_page: bool = False) -> ax.Snapshot:
    """Snapshot, but give a loading page or launching app a moment to show its controls.

    In a browser the toolbar always yields ~25 controls, so `want_page` waits for real page content
    (elements in the [page] region) rather than being satisfied by Chrome's own chrome.
    """
    end = time.monotonic() + timeout
    snap = ax.snapshot()
    prev_page = -1
    stable = 0
    prev_n = -1

    def page_count(s: ax.Snapshot) -> int:
        return sum(1 for e in s.elems if e.region == "page")

    while time.monotonic() < end:
        if snap.app == "loginwindow":
            return snap
        if snap.app in BROWSERS:
            pc = page_count(snap)
            # Ready only when the page has real content AND it has stopped growing (finished loading).
            if pc >= 6 and abs(pc - prev_page) <= 3:
                stable += 1
                if stable >= 2:
                    return snap
            else:
                stable = 0
            prev_page = pc
        else:
            n = len(snap.clickable())
            # Enough controls, or a small tree that has stopped changing (a "Don't Save" sheet has 4;
            # waiting for 8 cost 4 s every time).
            if n >= min_elems or (n > 0 and n == prev_n):
                return snap
            prev_n = n
        time.sleep(0.3)
        snap = ax.snapshot()
    return snap


import re as _re

ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4,
            "fifth": 5, "5th": 5, "sixth": 6, "last": -1, "top": 1, "next": 2}
KIND_ROLES = {
    "video": ("AXLink",), "result": ("AXLink", "AXButton", "AXCell", "AXRow"), "link": ("AXLink",),
    "item": ("AXLink", "AXCell", "AXRow", "AXButton"), "post": ("AXLink", "AXCell", "AXRow"),
    "row": ("AXRow", "AXCell"), "tab": ("AXRadioButton", "AXTab"), "message": ("AXCell", "AXRow"),
    "email": ("AXCell", "AXRow"), "song": ("AXRow", "AXCell", "AXLink"), "photo": ("AXImage", "AXLink"),
    "thumbnail": ("AXImage", "AXLink"),
}


def ordinal_pick(request: str, snap: ax.Snapshot) -> ax.Elem | None:
    """Resolve 'the second video' / 'first result' by position, without asking Jev."""
    m = _re.search(r"\b(first|second|third|fourth|fifth|sixth|last|top|next|1st|2nd|3rd|4th|5th)\b.*?\b(video|result|link|item|post|row|tab|message|email|song|photo|thumbnail)\b", request.lower())
    if not m:
        return None
    n, kind = ORDINALS[m.group(1)], m.group(2)
    roles = KIND_ROLES.get(kind, ("AXLink",))
    cands = [e for e in snap.elems if e.role in roles and e.label and (e.region != "browser")]
    if kind == "video":
        # Real result videos sit below the top nav and carry a substantial title; drop logo/nav/chips.
        nav = {"youtube home", "home", "shorts", "subscriptions", "you", "history", "trending", "view channel", "sign in"}
        cands = [e for e in cands if e.y > 130 and e.label.lower() not in nav
                 and not e.label.lower().startswith(("new content", "@")) and len(e.label) > 10]
    seen: set = set()
    uniq = []
    for e in sorted(cands, key=lambda e: (round(e.y / 20), e.x)):
        key = (round(e.y / 30), e.label[:30])
        if key not in seen:
            seen.add(key)
            uniq.append(e)
    if not uniq:
        return None
    return uniq[-1] if n == -1 else (uniq[n - 1] if n <= len(uniq) else None)


def click(request: str, how: str = "click") -> str:
    snap = wait_for_screen()
    if snap.app == "loginwindow":
        return "Screen is locked"
    # 1) Learned fast path: Jev classifies against what we already know in this app.
    el = recall(request, snap)
    if el is not None:
        learn(request, snap, el, "recall")   # reinforce
        return ax.press(el, how)
    # 2) Deterministic ordinals ("the second video").
    el = ordinal_pick(request, snap)
    if el is not None:
        log.info("ordinal pick: %s", el.describe())
        learn(request, snap, el, "ordinal")
        return ax.press(el, how)
    # 3) Jev pick over the whole tree.
    el, conf = pick(request, snap)
    if el is not None and conf >= 0.35:
        learn(request, snap, el, "pick")
        return ax.press(el, how)
    # A page may still be loading (a stale frame can pass the settle check). Retry a few times.
    if snap.app in BROWSERS:
        for _ in range(3):
            time.sleep(0.7)
            snap = ax.snapshot()
            el = ordinal_pick(request, snap)
            if el is not None:
                learn(request, snap, el, "ordinal")
                return ax.press(el, how)
            el, conf = pick(request, snap)
            if el is not None and conf >= 0.35:
                learn(request, snap, el, "pick")
                return ax.press(el, how)
    # 4) Last resort: Haiku looks at pixels. Whatever it hits, map back to a tree element and learn it,
    #    so this exact request is a fast Jev classification next time.
    from . import vision
    try:
        result, point = vision.act_located(request, snap, intent="click")
        if point is not None:
            el = _nearest(snap, point)
            if el is not None:
                learn(request, snap, el, "vision")
            else:
                _learn_point(request, snap, point)
        return result
    except Exception as e:  # noqa: BLE001
        log.warning("vision failed: %s", e)
        return "Couldn't find that on screen"


def _nearest(snap: ax.Snapshot, point: tuple[float, float]) -> ax.Elem | None:
    x, y = point
    near = min((e for e in snap.elems), key=lambda e: (e.cx - x) ** 2 + (e.cy - y) ** 2, default=None)
    if near is not None and (near.cx - x) ** 2 + (near.cy - y) ** 2 < 60 ** 2:
        return near
    return None


def _learn_point(request: str, snap: ax.Snapshot, point: tuple[float, float]) -> None:
    if snap.window:
        wx, wy, ww, wh = snap.window
        if ww > 0 and wh > 0:
            ui_memory.memory().remember(snap.app, request,
                                        rel=(round((point[0] - wx) / ww, 3), round((point[1] - wy) / wh, 3)),
                                        source="vision")


def type_into(request: str, text: str) -> str:
    """Focus the described field, then type. Empty request = type where the cursor already is."""
    if request.strip():
        snap = ax.snapshot()
        el, conf = pick(request, snap)
        if el is not None and conf >= 0.35:
            ax.focus(el)
            time.sleep(0.12)
    A.type_text(text)
    return f"Typed “{text}”"


SEND_LABELS = _re.compile(r"^(send|send message|submit|post|reply|send now)$", _re.I)


def send_draft() -> str:
    """Submit whatever is drafted in the front app: press its Send/Submit button if one is visible,
    otherwise press Return (which sends in Messages, Slack, Discord, most chat and search boxes)."""
    snap = ax.snapshot()
    if snap.app == "loginwindow":
        return "Screen is locked"
    btn = next((e for e in snap.elems if e.role == "AXButton" and SEND_LABELS.match(e.label.strip())
                and e.enabled), None)
    if btn is not None:
        ax.press(btn)
        return f"Sent ({btn.label})"
    A.keystroke("enter")
    return "Sent (Return)"


def read() -> str:
    """Everything readable on screen, for 'what does it say' style requests (logged, shown in pill)."""
    snap = ax.snapshot(max_elems=200)
    lines = [e.label for e in snap.elems if e.label and e.role in ("AXStaticText", "AXHeading", "AXLink", "AXButton")]
    return " · ".join(dict.fromkeys(lines))[:400] or "nothing readable on screen"
