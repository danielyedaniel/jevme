"""Learn from a demonstration: when a spoken task fails, watch the user do it by hand and keep the steps.

After the agent gives up on "zoom in" or "open my cs 343 notes", the user usually just does it themselves.
Those few seconds are the best training signal there is: an exact (spoken goal → steps) pair with no
model guessing. We record them as a task recipe, which the next time is matched by Jev and replayed with no
reasoning at all — the same memo the agent writes after a verified success.

What is recorded, and what never is:
  - clicks: the app, and the role + label of the control under the pointer (never pixels or screenshots)
  - menu choices: the menu path ("View > Zoom In")
  - named keys and shortcuts: enter, escape, tab, arrows, cmd+… (never which characters were typed)
  - typing: only the fact that a field was typed into. The field's text is kept ONLY if it is words the
    user just spoke in the goal ("search discord for cats" → "cats"); otherwise the demonstration is not
    saved as a recipe at all. Password fields are never read.
Recording starts only after a failed voice command, lasts until the user goes quiet (or speaks the next
command), and everything stays in ~/.config/jevme/task_memory.json.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

from . import ui_memory

log = logging.getLogger("jevme.watch")

IDLE_END_S = 7.0      # quiet this long after the last action = the demonstration is over
NO_START_S = 20.0     # nobody demonstrated anything: stop watching
MAX_S = 90.0

NAMED_KEYS = {36: "enter", 76: "enter", 53: "escape", 48: "tab", 51: "delete", 117: "delete",
              123: "left", 124: "right", 125: "down", 126: "up", 116: "pageup", 121: "pagedown"}
SECRET_ROLES = ("AXSecureTextField",)
TEXT_ROLES = ("AXTextField", "AXTextArea", "AXSearchField", "AXComboBox")


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^\w']+", " ", s.lower()).split())


@dataclass
class Demo:
    """Pure recording logic (no Cocoa), so it can be tested with synthetic events."""
    goal: str
    app0: str                  # front app when the failed task started (the recipe's context)
    start_app: str             # front app when the demonstration started
    steps: list[ui_memory.RecipeStep] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)
    last_event: float = 0.0
    untemplatable: bool = False     # typed text that isn't from the goal: can't be replayed, don't save
    _typing: dict | None = None
    _app: str = ""

    def __post_init__(self) -> None:
        self._app = self.app0
        if self.start_app and self.start_app.lower() != (self.app0 or "").lower():
            # The failed attempt already switched apps; replay from the original context must too.
            self.steps.append(ui_memory.RecipeStep("open_app", arg=self.start_app))
            self._app = self.start_app

    # ---------- events ----------

    def _touch(self, app: str) -> None:
        self.last_event = time.monotonic()
        if app and app.lower() != self._app.lower() and app != "Jevme":
            self.steps.append(ui_memory.RecipeStep("open_app", arg=app))
            self._app = app

    def click(self, app: str, role: str, label: str, *, menu_path: list[str] | None = None,
              how: str = "click", field_value: str | None = None) -> None:
        self.end_typing(field_value)
        self._touch(app)
        if menu_path:
            self.steps.append(ui_memory.RecipeStep("menu", role="AXMenuItem", label=menu_path[-1],
                                                   arg=" > ".join(menu_path)))
        elif role and label and role not in SECRET_ROLES and (
                ui_memory.is_stable_target(role, label) or self._from_goal(label)):
            # A non-standard target (a note row, a file) is kept only when the user named it in the goal;
            # then it's a slot that follows the words ("open my cs 341 notes" clicks the CS 341 row).
            self.steps.append(ui_memory.RecipeStep("click", role=role, label=label,
                                                   arg="" if how == "click" else how,
                                                   text_from_goal=not ui_memory.is_stable_target(role, label)))
        else:
            # An unlabeled spot: nothing a replay could find again.
            self.untemplatable = True

    def _from_goal(self, text: str) -> bool:
        return bool(_norm(text)) and f" {_norm(text)} " in f" {_norm(self.goal)} "

    def key(self, app: str, name: str, mods: list[str], field_value: str | None = None) -> None:
        self.end_typing(field_value)
        self._touch(app)
        k = "+".join([*mods, name]) if mods else name
        self.steps.append(ui_memory.RecipeStep("key", arg=k))

    def typed(self, app: str, role: str, label: str, value: str | None = None) -> None:
        """A character went into a field. Coalesced; the content is resolved in end_typing()."""
        if role in SECRET_ROLES:
            self.untemplatable = True
            return
        if self._typing is None:
            self._touch(app)
            self._typing = {"role": role, "label": label, "seen": ""}
        if value:
            self._typing["seen"] = value      # Enter may clear the field before it's read at the end
        self.last_event = time.monotonic()

    def end_typing(self, field_value: str | None) -> None:
        if self._typing is None:
            return
        t, self._typing = self._typing, None
        # Keep the text only if it is words from the spoken goal (so it's a template slot, not private text).
        value = next((v.strip() for v in (field_value, t["seen"]) if v and self._from_goal(v)), "")
        if value:
            self.steps.append(ui_memory.RecipeStep("type", role=t["role"], label=t["label"], arg=value,
                                                   text_from_goal=True))
        else:
            self.untemplatable = True

    # ---------- lifecycle ----------

    def is_over(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if now - self.started > MAX_S:
            return True
        if not self.last_event:
            return now - self.started > NO_START_S
        return now - self.last_event > IDLE_END_S     # stop() resolves any unfinished typing

    def recipe(self) -> list[ui_memory.RecipeStep] | None:
        """The demonstrated steps, or None if they can't be replayed faithfully."""
        steps = list(self.steps)
        # Only app switching and nothing else isn't a demonstration of anything.
        if self.untemplatable or not any(s.op != "open_app" for s in steps):
            return None
        return steps


class Watcher:
    """Cocoa side: global event monitors feeding a Demo. All methods run on the main thread."""

    def __init__(self, on_learned=None) -> None:
        self.demo: Demo | None = None
        self._monitor = None
        self.on_learned = on_learned     # (goal) -> None, for the overlay

    @property
    def active(self) -> bool:
        return self.demo is not None

    def start(self, goal: str, app0: str) -> None:
        from AppKit import NSEvent
        from . import actions as A
        self.stop(save=False)
        self.demo = Demo(goal, app0, A.frontmost_app())
        mask = (1 << 1) | (1 << 3) | (1 << 10)      # left mouse down, right mouse down, key down
        self._monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(mask, self._on_event)
        log.info("watching a demonstration of «%s»", goal)

    def tick(self) -> None:
        if self.demo is not None and self.demo.is_over():
            self.stop(save=True)

    def stop(self, *, save: bool) -> None:
        if self._monitor is not None:
            from AppKit import NSEvent
            NSEvent.removeMonitor_(self._monitor)
            self._monitor = None
        demo, self.demo = self.demo, None
        if demo is None or not save:
            return
        demo.end_typing(_focused_value())
        steps = demo.recipe()
        if not steps:
            log.info("demonstration of «%s» not kept (%s)", demo.goal,
                     "nothing done" if not demo.steps else "not replayable")
            return
        if ui_memory.task_memory().remember(demo.goal, demo.app0, steps, min_steps=1):
            log.info("learned from demonstration: «%s» → %s", demo.goal,
                     [f"{s.op}:{s.label or s.arg}" for s in steps])
            if self.on_learned:
                self.on_learned(demo.goal)

    # ---------- events (main thread) ----------

    def _on_event(self, ev) -> None:
        try:
            self._handle(ev)
        except Exception:  # noqa: BLE001 — a monitor must never raise into AppKit
            log.exception("watch event")

    def _handle(self, ev) -> None:
        d = self.demo
        if d is None:
            return
        t = int(ev.type())
        if t in (1, 3):                                   # left / right mouse down
            x, y = _mouse_point()
            app, role, label, menu = _element_at(x, y)
            if role == "AXMenuBarItem":                   # just opening a menu; the item click follows
                d.last_event = time.monotonic()
                return
            d.click(app, role, label, menu_path=menu, how="right" if t == 3 else "click",
                    field_value=_focused_value())
            return
        if t == 10:                                       # key down
            flags = int(ev.modifierFlags())
            mods = [m for m, bit in (("ctrl", 1 << 18), ("alt", 1 << 19), ("shift", 1 << 17), ("cmd", 1 << 20))
                    if flags & bit]
            code = int(ev.keyCode())
            app = _front_name()
            if code in NAMED_KEYS or "cmd" in mods or "ctrl" in mods:
                name = NAMED_KEYS.get(code) or str(ev.charactersIgnoringModifiers() or "").lower()
                if name:
                    d.key(app, name, [m for m in mods if m != "shift" or code in NAMED_KEYS],
                          field_value=_focused_value())
                return
            role, label = _focused_field()
            d.typed(app, role, label, _focused_value())


# ---------- accessibility helpers ----------

def _front_name() -> str:
    from . import actions as A
    return A.frontmost_app()


def _mouse_point() -> tuple[float, float]:
    from AppKit import NSEvent, NSScreen
    loc = NSEvent.mouseLocation()
    h = NSScreen.screens()[0].frame().size.height      # AX uses top-left origin of the primary screen
    return float(loc.x), float(h - loc.y)


def _app_of(el) -> str:
    import ApplicationServices as AS
    from AppKit import NSRunningApplication
    err, pid = AS.AXUIElementGetPid(el, None)
    if err != 0:
        return ""
    ra = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    return str(ra.localizedName() or "") if ra else ""


def _label(el) -> str:
    """The same label a snapshot would give this element, minus any typed value, so replay can match it."""
    from . import ax
    role = ax._s(ax._attr(el, "AXRole"))
    label = (ax._s(ax._attr(el, "AXTitle")) or ax._s(ax._attr(el, "AXDescription"))
             or ax._s(ax._attr(el, "AXPlaceholderValue")))
    if not label and role in ("AXStaticText", "AXHeading", "AXLink", "AXButton", "AXCell", "AXRow"):
        label = ax._s(ax._attr(el, "AXValue"), 60)
    return label


def _element_at(x: float, y: float) -> tuple[str, str, str, list[str] | None]:
    """(app, role, label, menu_path) for the control under a screen point, climbing from inner text to
    the button/link that owns it."""
    import ApplicationServices as AS
    from . import ax
    sys_el = AS.AXUIElementCreateSystemWide()
    AS.AXUIElementSetMessagingTimeout(sys_el, 0.3)
    err, el = AS.AXUIElementCopyElementAtPosition(sys_el, x, y, None)
    if err != 0 or el is None:
        return _front_name(), "", "", None
    app = _app_of(el) or _front_name()
    cur = el
    for _ in range(5):
        role = ax._s(ax._attr(cur, "AXRole"))
        if role in ("AXMenuItem", "AXMenuBarItem"):
            return app, role, _label(cur), (_menu_path(cur) if role == "AXMenuItem" else None)
        if role in ax.INTERACTIVE or "AXPress" in ax._actions(cur):
            if role in ("AXGroup",) and not _label(cur):
                pass
            else:
                return app, role, _label(cur), None
        parent = ax._attr(cur, "AXParent")
        if parent is None:
            break
        cur = parent
    role = ax._s(ax._attr(el, "AXRole"))
    return app, role, _label(el), None


def _menu_path(item) -> list[str]:
    """["View", "Zoom In"] for a menu-bar item; ["", "Copy Link"] for a context-menu item."""
    from . import ax
    path = [_label(item)]
    cur = ax._attr(item, "AXParent")
    for _ in range(8):
        if cur is None:
            break
        role = ax._s(ax._attr(cur, "AXRole"))
        if role in ("AXMenuItem", "AXMenuBarItem"):
            path.insert(0, _label(cur))
            if role == "AXMenuBarItem":
                return path
        elif role == "AXMenuBar":
            return path
        elif role == "AXApplication":
            return ["", *path]
        cur = ax._attr(cur, "AXParent")
    return ["", *path]


def _focused():
    import ApplicationServices as AS
    from . import ax
    sys_el = AS.AXUIElementCreateSystemWide()
    AS.AXUIElementSetMessagingTimeout(sys_el, 0.2)
    return ax._attr(sys_el, "AXFocusedUIElement")


def _focused_field() -> tuple[str, str]:
    from . import ax
    el = _focused()
    if el is None:
        return "", ""
    role = ax._s(ax._attr(el, "AXRole"))
    if ax._s(ax._attr(el, "AXSubrole")) == "AXSecureTextField":
        role = "AXSecureTextField"
    return role, _label(el)


def _focused_value() -> str | None:
    """The focused field's text — read only to check it against the spoken goal, never for secure fields."""
    from . import ax
    el = _focused()
    if el is None:
        return None
    role = ax._s(ax._attr(el, "AXRole"))
    if role not in TEXT_ROLES or ax._s(ax._attr(el, "AXSubrole")) == "AXSecureTextField":
        return None
    return ax._s(ax._attr(el, "AXValue"), 200)
