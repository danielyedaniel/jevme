"""Read the front window as a list of on-screen controls via macOS Accessibility.

This is Jevme's eyes for native apps, browsers and most Electron apps: no pixels, no vision model.
A snapshot walks the focused window's element tree with a time budget, keeps the visible,
interactive (or readable) elements, and returns them top-to-bottom, left-to-right with stable
short ids so Jev can pick one by a Choice.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import ApplicationServices as AS
from AppKit import NSWorkspace
from Quartz import (CGEventCreateMouseEvent, CGEventCreateScrollWheelEvent, CGEventPost, CGEventSetIntegerValueField,
                    kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGEventRightMouseDown, kCGEventRightMouseUp,
                    kCGHIDEventTap, kCGMouseButtonLeft, kCGMouseButtonRight, kCGMouseEventClickState,
                    kCGScrollEventUnitLine)

log = logging.getLogger("jevme.ax")

INTERACTIVE = {
    "AXButton", "AXLink", "AXTextField", "AXTextArea", "AXSearchField", "AXCheckBox", "AXRadioButton",
    "AXPopUpButton", "AXMenuButton", "AXComboBox", "AXTab", "AXTabButton", "AXMenuItem", "AXSlider",
    "AXIncrementor", "AXDisclosureTriangle", "AXCell", "AXRow", "AXImage", "AXToggle", "AXSwitch",
    "AXColorWell", "AXDateField", "AXTimeField", "AXHeading", "AXStaticText", "AXWebArea", "AXGroup",
}
CLICKABLE = INTERACTIVE - {"AXStaticText", "AXHeading", "AXWebArea", "AXGroup"}
CONTAINERS = {"AXGroup", "AXScrollArea", "AXWebArea", "AXList", "AXTable", "AXOutline", "AXSplitGroup",
              "AXToolbar", "AXTabGroup", "AXLayoutArea", "AXLayoutItem", "AXUnknown", "AXGenericElement",
              "AXWindow", "AXSheet", "AXDrawer", "AXRow", "AXCell", "AXColumn", "AXBrowser", "AXPopover",
              "AXMenu", "AXMenuBar", "AXRadioGroup", "AXDisclosureTriangle", "AXSection", "AXLandmark"}
ATTRS = ["AXRole", "AXSubrole", "AXTitle", "AXDescription", "AXValue", "AXPosition", "AXSize", "AXEnabled",
         "AXChildren", "AXPlaceholderValue"]

_enabled_pids: set[int] = set()


@dataclass
class Elem:
    idx: int
    role: str
    label: str
    x: float
    y: float
    w: float
    h: float
    ref: object = field(repr=False)
    actions: list[str] = field(default_factory=list)
    value: str = ""
    enabled: bool = True
    region: str = ""     # "page" for web content, "browser" for the browser's own toolbar/tabs, "" otherwise

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def short_role(self) -> str:
        r = self.role[2:] if self.role.startswith("AX") else self.role
        return {"StaticText": "text", "TextField": "field", "TextArea": "textarea", "PopUpButton": "popup",
                "MenuButton": "menu", "CheckBox": "checkbox", "RadioButton": "radio", "SearchField": "search",
                "DisclosureTriangle": "expander", "WebArea": "page"}.get(r, r.lower())

    def describe(self) -> str:
        s = f"{self.short_role()}: {self.label}" if self.label else self.short_role()
        if self.region:
            s = f"[{self.region}] {s}"
        return s[:96]


@dataclass
class Snapshot:
    app: str
    pid: int
    window: tuple[float, float, float, float] | None
    elems: list[Elem]
    ms: int
    visited: int

    def clickable(self) -> list[Elem]:
        return [e for e in self.elems if e.role in CLICKABLE or "AXPress" in e.actions]

    def criteria(self, only_clickable: bool = True) -> dict[str, str]:
        src = self.clickable() if only_clickable else self.elems
        return {f"e{e.idx}": e.describe() for e in src}


# ---------- low level ----------

def _attr(el, name):
    err, val = AS.AXUIElementCopyAttributeValue(el, name, None)
    return val if err == 0 else None


def _multi(el, names):
    err, vals = AS.AXUIElementCopyMultipleAttributeValues(el, names, 0, None)
    if err != 0 or vals is None:
        return {}
    out = {}
    for n, v in zip(names, vals):
        if v is None:
            continue
        # A missing attribute comes back as an AXValue carrying an error code.
        if n not in ("AXPosition", "AXSize") and _is_ax_error(v):
            continue
        out[n] = v
    return out


def _is_ax_error(v) -> bool:
    try:
        return AS.CFGetTypeID(v) == AS.AXValueGetTypeID() and AS.AXValueGetType(v) == AS.kAXValueAXErrorType
    except Exception:  # noqa: BLE001
        return False


def _actions(el) -> list[str]:
    err, names = AS.AXUIElementCopyActionNames(el, None)
    return [str(n) for n in names] if err == 0 and names else []


def _point(v):
    if v is None:
        return None
    ok, pt = AS.AXValueGetValue(v, AS.kAXValueCGPointType, None)
    return (float(pt.x), float(pt.y)) if ok else None


def _size(v):
    if v is None:
        return None
    ok, sz = AS.AXValueGetValue(v, AS.kAXValueCGSizeType, None)
    return (float(sz.width), float(sz.height)) if ok else None


def _s(v, n=80) -> str:
    if v is None:
        return ""
    try:
        s = str(v)
    except Exception:  # noqa: BLE001
        return ""
    s = " ".join(s.split())
    return s[:n]


def _screens_ax() -> list[tuple[float, float, float, float]]:
    """Screen rects in Accessibility coordinates (origin top-left of the main display)."""
    from AppKit import NSScreen
    screens = NSScreen.screens()
    if not screens:
        return [(0, 0, 10000, 10000)]
    main_h = screens[0].frame().size.height
    out = []
    for s in screens:
        f = s.frame()
        out.append((f.origin.x, main_h - (f.origin.y + f.size.height), f.size.width, f.size.height))
    return out


def _on_screen(x: float, y: float, w: float, h: float, screens) -> bool:
    for sx, sy, sw, sh in screens:
        if x + w > sx and y + h > sy and x < sx + sw and y < sy + sh:
            return True
    return False


def _enable_web_ax(app_el, pid: int) -> bool:
    """Chrome / Electron only publish content once an assistive client asks. Returns True the first time.

    Setting AXManualAccessibility makes Chromium-based apps (Chrome, Slack, Discord, Spotify, VS Code,
    Notion, Electron) build their accessibility tree; it takes a moment to populate.
    """
    if pid in _enabled_pids:
        return False
    _enabled_pids.add(pid)
    from CoreFoundation import kCFBooleanTrue
    for attr in ("AXEnhancedUserInterface", "AXManualAccessibility"):
        try:
            AS.AXUIElementSetAttributeValue(app_el, attr, kCFBooleanTrue)
        except Exception:  # noqa: BLE001
            pass
    return True


_bundle_ids: dict[str, str | None] = {}


def _bundle_id(app_name: str) -> str | None:
    if app_name not in _bundle_ids:
        from AppKit import NSBundle
        path = NSWorkspace.sharedWorkspace().fullPathForApplication_(app_name)
        b = NSBundle.bundleWithPath_(path) if path else None
        _bundle_ids[app_name] = str(b.bundleIdentifier()) if b is not None and b.bundleIdentifier() else None
    return _bundle_ids[app_name]


def frontmost_running():
    """The frontmost app, read fresh from the window server.

    NSWorkspace.frontmostApplication / runningApplications are refreshed only by the MAIN thread's run loop,
    so from the task-queue worker (or a headless test) they go stale — measured: a freshly launched Discord
    never appeared. The window server's front-to-back list and pid lookups are always current."""
    from AppKit import NSRunningApplication
    import Quartz
    wins = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID)
    for w in wins or []:
        if int(w.get("kCGWindowLayer", 1)) == 0 and w.get("kCGWindowOwnerPID"):
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(int(w["kCGWindowOwnerPID"]))
            if app is not None and app.activationPolicy() == 0:
                return app
    return NSWorkspace.sharedWorkspace().frontmostApplication()


def resolve_target(app_name: str | None, wait_s: float = 0.0):
    """The running app to read: the named one (waiting up to `wait_s` for a cold launch), or the frontmost
    app when no name is given. None if a name was given and that app isn't running."""
    from AppKit import NSRunningApplication
    if not app_name:
        return frontmost_running()
    end = time.monotonic() + wait_s
    while True:
        for a in NSWorkspace.sharedWorkspace().runningApplications():
            if str(a.localizedName() or "").lower() == app_name.lower():
                return a
        bid = _bundle_id(app_name)                  # fresh LaunchServices lookup (cache may be stale)
        if bid:
            fresh = NSRunningApplication.runningApplicationsWithBundleIdentifier_(bid)
            if fresh:
                return fresh[0]
        if time.monotonic() >= end:
            return None
        time.sleep(0.2)


def wait_until_ready(app_name: str, timeout: float = 10.0, min_elems: int = 4) -> bool:
    """After launching an app, wait until it is running AND its window has settled.

    "Settled" = two consecutive reads with a similar element count. A plain threshold fires too early:
    measured on Discord, a cold start shows an updater window (7 → 10 → 6 controls) before the real one
    (207) at ~3 s, and the updater may even restart the process. An already-open app is ready in ~0.4 s."""
    end = time.monotonic() + timeout
    prev = -1
    while time.monotonic() < end:
        if resolve_target(app_name) is None:      # not up yet, or restarting after an update
            prev = -1
            time.sleep(0.3)
            continue
        n = len(snapshot(budget_s=0.3, app_name=app_name).elems)
        if n >= min_elems and prev >= min_elems and abs(n - prev) <= max(3, int(prev * 0.15)):
            return True
        prev = n
        time.sleep(0.35)
    return False


def deep_text(app_name: str | None = None, limit: int = 9000, budget_s: float = 1.0) -> str:
    """Collect the readable text of the front window (problem statements, articles, code, messages).

    Unlike snapshot(), which keeps interactive controls, this gathers the actual text nodes
    (AXStaticText, AXHeading, AXTextArea/AXTextField values) in reading order. Works over the accessibility
    tree, so no Chrome 'JavaScript from Apple Events' toggle is needed.
    """
    t0 = time.monotonic()
    front = resolve_target(app_name)
    if front is None:
        return ""          # the named app isn't running: never read some other app's text instead
    pid = int(front.processIdentifier())
    app_el = AS.AXUIElementCreateApplication(pid)
    AS.AXUIElementSetMessagingTimeout(app_el, 0.3)
    _enable_web_ax(app_el, pid)
    win = _attr(app_el, "AXFocusedWindow") or _attr(app_el, "AXMainWindow") or app_el

    out: list[tuple[float, float, str]] = []
    seen: set[str] = set()
    queue = [(win, 0)]
    visited = 0
    while queue and visited < 8000 and sum(len(t) for _, _, t in out) < limit:
        if time.monotonic() - t0 > budget_s:
            break
        el, depth = queue.pop(0)
        visited += 1
        a = _multi(el, ["AXRole", "AXValue", "AXTitle", "AXDescription", "AXPosition", "AXChildren"])
        role = _s(a.get("AXRole"))
        text = ""
        if role in ("AXStaticText", "AXHeading", "AXTextArea", "AXTextField", "AXCell", "AXLink"):
            text = _s(a.get("AXValue"), 2000) or _s(a.get("AXTitle"), 500) or _s(a.get("AXDescription"), 500)
        if text and len(text) > 1 and text not in seen:
            seen.add(text)
            pos = _point(a.get("AXPosition")) or (0.0, float(depth))
            out.append((pos[1], pos[0], text))
        kids = a.get("AXChildren") or []
        if not isinstance(kids, (list, tuple)):
            kids = list(kids) if hasattr(kids, "__iter__") else []
        for k in list(kids)[:120]:
            queue.append((k, depth + 1))

    out.sort(key=lambda r: (round(r[0] / 12), r[1]))
    joined = "\n".join(t for _, _, t in out)
    return joined[:limit]


def warm(pid: int) -> None:
    """Ask an app to build its accessibility tree ahead of time (call on launch / app-switch)."""
    try:
        el = AS.AXUIElementCreateApplication(pid)
        AS.AXUIElementSetMessagingTimeout(el, 0.2)   # called on the main thread: a hung app can't stall the UI
        _enable_web_ax(el, pid)
    except Exception:  # noqa: BLE001
        pass


# ---------- snapshot ----------

def snapshot(budget_s: float = 0.6, max_elems: int = 220, max_visited: int = 4000,
             app_name: str | None = None) -> Snapshot:
    """Read the front window of the frontmost app, or of `app_name` if given.

    If `app_name` is given but that app isn't running (quit, or still cold-launching), return an EMPTY
    snapshot for it. Never fall back to whatever happens to be frontmost: the agent would then read — and
    click in — the wrong app (seen live: "in Discord" while actually looking at iTerm)."""
    t0 = time.monotonic()
    front = resolve_target(app_name)
    if front is None:
        return Snapshot(app_name or "", 0, None, [], 0, 0)
    pid = int(front.processIdentifier())
    app_name = str(front.localizedName() or "")
    app_el = AS.AXUIElementCreateApplication(pid)
    AS.AXUIElementSetMessagingTimeout(app_el, 0.25)
    just_enabled = _enable_web_ax(app_el, pid)

    win = _attr(app_el, "AXFocusedWindow") or _attr(app_el, "AXMainWindow")
    wframe = None
    if win is not None:
        p, s = _point(_attr(win, "AXPosition")), _size(_attr(win, "AXSize"))
        if p and s:
            wframe = (p[0], p[1], s[0], s[1])
    root = win if win is not None else app_el
    screens = _screens_ax()
    # Clip to the screen the window lives on (Chrome's reported window frame excludes its own toolbar).
    if wframe:
        wx, wy, ww, wh = wframe
        screens = [s for s in screens if _on_screen(wx, wy, ww, wh, [s])] or screens

    is_browser = app_name in ("Google Chrome", "Safari", "Arc", "Brave Browser", "Microsoft Edge", "Firefox", "Chromium")
    elems: list[Elem] = []
    queue = [(root, 0, False)]
    visited = 0
    seen_frames: set[tuple] = set()
    while queue and visited < max_visited and len(elems) < max_elems:
        if time.monotonic() - t0 > budget_s:
            break
        el, depth, in_web = queue.pop(0)
        visited += 1
        a = _multi(el, ATTRS)
        role = _s(a.get("AXRole"))
        if not role:
            continue
        if role == "AXWebArea":
            in_web = True
        pos, size = _point(a.get("AXPosition")), _size(a.get("AXSize"))
        visible = bool(pos and size and size[0] > 1 and size[1] > 1)
        if visible:
            visible = _on_screen(pos[0], pos[1], size[0], size[1], screens)
        actions = _actions(el) if visible else []
        if visible and (role in INTERACTIVE or "AXPress" in actions):
            title = _s(a.get("AXTitle"))
            desc = _s(a.get("AXDescription"))
            value = _s(a.get("AXValue"), 60)
            ph = _s(a.get("AXPlaceholderValue"))
            label = title or desc or ph or (value if role in ("AXStaticText", "AXHeading", "AXLink", "AXButton", "AXCell", "AXRow") else "")
            if role in ("AXTextField", "AXTextArea", "AXSearchField") and value:
                label = f"{label or 'field'} = “{value[:30]}”"
            skip_empty = role in ("AXGroup", "AXWebArea", "AXImage", "AXCell", "AXRow", "AXStaticText") and not label
            key = (role, round(pos[0]), round(pos[1]), round(size[0]), round(size[1]))
            if not skip_empty and key not in seen_frames and role not in ("AXWebArea",):
                if role == "AXGroup" and "AXPress" not in actions:
                    pass
                else:
                    seen_frames.add(key)
                    enabled = a.get("AXEnabled")
                    region = ("page" if in_web else "browser") if is_browser else ""
                    elems.append(Elem(0, role, label, pos[0], pos[1], size[0], size[1], el, actions, value,
                                      True if enabled is None else bool(enabled), region))
        # Don't descend into containers that are entirely off screen (scrolled away, other tabs).
        if pos and size and size[0] > 1 and size[1] > 1 and not visible and role in CONTAINERS:
            continue
        if depth < 60:
            kids = a.get("AXChildren") or []
            if not isinstance(kids, (list, tuple)):
                kids = list(kids) if hasattr(kids, "__iter__") else []
            # Native tables/outlines with thousands of rows: only the rows on screen.
            if len(kids) > 60 and role in ("AXTable", "AXOutline", "AXList"):
                vk = _attr(el, "AXVisibleRows") or _attr(el, "AXVisibleChildren")
                if vk:
                    kids = list(vk)
            for k in list(kids)[:120]:
                queue.append((k, depth + 1, in_web))

    # An Electron/Chromium app we just switched on hasn't built its tree yet: wait once and re-walk.
    if just_enabled and len(elems) < 8 and time.monotonic() - t0 < budget_s:
        time.sleep(0.5)
        return snapshot(budget_s, max_elems, max_visited, app_name)

    elems.sort(key=lambda e: (round(e.y / 24), e.x))
    for i, e in enumerate(elems, 1):
        e.idx = i
    snap = Snapshot(app_name, pid, wframe, elems, int((time.monotonic() - t0) * 1000), visited)
    log.info("ax snapshot %s: %d elems (%d clickable), visited %d, %d ms", app_name, len(elems),
             len(snap.clickable()), visited, snap.ms)
    return snap


# ---------- act ----------

def _click_at(x: float, y: float, button: str = "left", clicks: int = 1) -> None:
    down, up, btn = ((kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGMouseButtonLeft) if button == "left"
                     else (kCGEventRightMouseDown, kCGEventRightMouseUp, kCGMouseButtonRight))
    for i in range(1, clicks + 1):
        for kind in (down, up):
            ev = CGEventCreateMouseEvent(None, kind, (x, y), btn)
            CGEventSetIntegerValueField(ev, kCGMouseEventClickState, i)
            CGEventPost(kCGHIDEventTap, ev)
        time.sleep(0.03)


def press(e: Elem, how: str = "click") -> str:
    """Activate an element: AXPress when the app offers it, else a real click at its centre."""
    if how == "click" and "AXPress" in e.actions and e.role not in ("AXTextField", "AXTextArea", "AXSearchField"):
        err = AS.AXUIElementPerformAction(e.ref, "AXPress")
        if err == 0:
            return f"Pressed {e.describe()}"
    clicks = 2 if how == "double" else 1
    button = "right" if how == "right" else "left"
    _click_at(e.cx, e.cy, button, clicks)
    return f"Clicked {e.describe()}"


def _title(el) -> str:
    return _s(_attr(el, "AXTitle")) or _s(_attr(el, "AXDescription"))


def _child_titled(el, title: str):
    for k in _attr(el, "AXChildren") or []:
        if _title(k).lower() == title.lower():
            return k
        # AXMenuBarItem / AXMenuItem hold their items inside an AXMenu child.
        if _s(_attr(k, "AXRole")) == "AXMenu":
            hit = _child_titled(k, title)
            if hit is not None:
                return hit
    return None


def press_menu(app_name: str | None, path: list[str]) -> bool:
    """Choose a menu item by its titles, e.g. ["View", "Zoom In"] from the menu bar, or ["", "Copy Link"]
    from a context menu that is open right now. Menus aren't in window snapshots, so replay needs this."""
    app = resolve_target(app_name)
    if app is None or not path:
        return False
    app_el = AS.AXUIElementCreateApplication(int(app.processIdentifier()))
    AS.AXUIElementSetMessagingTimeout(app_el, 0.5)
    if path[0] == "":           # context menu: an AXMenu open directly under the app
        roots = [k for k in (_attr(app_el, "AXChildren") or []) if _s(_attr(k, "AXRole")) == "AXMenu"]
        path = path[1:]
    else:
        bar = _attr(app_el, "AXMenuBar")
        roots = [bar] if bar is not None else []
    for root in roots:
        el = root
        for title in path:
            el = _child_titled(el, title)
            if el is None:
                break
        if el is not None and el is not root:
            return AS.AXUIElementPerformAction(el, "AXPress") == 0
    return False


def focus(e: Elem) -> None:
    try:
        AS.AXUIElementSetAttributeValue(e.ref, "AXFocused", True)
    except Exception:  # noqa: BLE001
        pass
    _click_at(e.cx, e.cy)


def scroll_at(x: float, y: float, lines: int) -> None:
    ev = CGEventCreateScrollWheelEvent(None, kCGScrollEventUnitLine, 1, lines)
    CGEventPost(kCGHIDEventTap, ev)


if __name__ == "__main__":
    logging.basicConfig(level="INFO")
    s = snapshot()
    print(f"{s.app}  window={s.window}  {len(s.elems)} elems in {s.ms} ms (visited {s.visited})")
    for e in s.elems[:60]:
        print(f"  e{e.idx:<3} {e.short_role():<10} {'*' if 'AXPress' in e.actions else ' '} ({e.x:.0f},{e.y:.0f} {e.w:.0f}x{e.h:.0f})  {e.label}")
