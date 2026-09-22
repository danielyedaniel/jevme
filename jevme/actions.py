"""Low-level Mac actions: launch apps, run AppleScript, press keys, open URLs.

Everything here is synchronous and fast (tens of ms). Called from a worker thread.
"""
from __future__ import annotations

import logging
import re
import os
import subprocess
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from AppKit import NSWorkspace
from Foundation import NSURL

log = logging.getLogger("jevme.actions")

APP_DIRS = ["/Applications", "/System/Applications", "/System/Applications/Utilities",
            "/System/Library/CoreServices", str(Path.home() / "Applications")]

# Name spoken → real app name, for apps people call by another name.
ALIASES = {
    "chrome": "Google Chrome", "google chrome": "Google Chrome",
    "vscode": "Visual Studio Code", "vs code": "Visual Studio Code", "code": "Visual Studio Code",
    "terminal": "iTerm", "iterm": "iTerm", "iterm2": "iTerm",
    "settings": "System Settings", "system preferences": "System Settings",
    "teams": "Microsoft Teams (work or school)", "word": "Microsoft Word",
    "zoom": "zoom.us", "find my": "FindMy", "voice memos": "VoiceMemos",
}

WELL_KNOWN_SITES = {
    "youtube": "https://www.youtube.com", "gmail": "https://mail.google.com",
    "google": "https://www.google.com", "google calendar": "https://calendar.google.com",
    "calendar": "https://calendar.google.com", "google drive": "https://drive.google.com",
    "drive": "https://drive.google.com", "google docs": "https://docs.google.com",
    "docs": "https://docs.google.com", "maps": "https://maps.google.com",
    "google maps": "https://maps.google.com", "github": "https://github.com",
    "x": "https://x.com", "twitter": "https://x.com", "x.com": "https://x.com",
    "linkedin": "https://www.linkedin.com", "reddit": "https://www.reddit.com",
    "wikipedia": "https://www.wikipedia.org", "amazon": "https://www.amazon.com",
    "netflix": "https://www.netflix.com", "spotify": "https://open.spotify.com",
    "leetcode": "https://leetcode.com", "leet code": "https://leetcode.com",
    "neetcode": "https://neetcode.io", "codeforces": "https://codeforces.com", "hackerrank": "https://www.hackerrank.com",
    "piazza": "https://piazza.com", "gradescope": "https://www.gradescope.com",
    "waterloo learn": "https://learn.uwaterloo.ca", "uwaterloo learn": "https://learn.uwaterloo.ca",
    "waterloo quest": "https://quest.pecs.uwaterloo.ca", "private email": "https://privateemail.com",
    "icloud": "https://www.icloud.com",
    "chatgpt": "https://chatgpt.com", "claude": "https://claude.ai", "notion": "https://www.notion.so",
    "figma": "https://www.figma.com", "hacker news": "https://news.ycombinator.com",
    "stack overflow": "https://stackoverflow.com", "instagram": "https://www.instagram.com",
    "facebook": "https://www.facebook.com", "tiktok": "https://www.tiktok.com",
    "outlook": "https://outlook.live.com", "whatsapp": "https://web.whatsapp.com",
    "piazza": "https://piazza.com", "canvas": "https://canvas.instructure.com",
}


# ---------- apps ----------

def installed_apps() -> list[str]:
    names: set[str] = set()
    for d in APP_DIRS:
        try:
            for entry in os.listdir(d):
                if entry.endswith(".app"):
                    names.add(entry[:-4])
        except FileNotFoundError:
            pass
    return sorted(names)


def running_apps() -> list[str]:
    """Regular apps that are running. The NSWorkspace list only refreshes on the main run loop, so also
    include every app that currently owns an on-screen window (read fresh from the window server)."""
    out = []
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        if app.activationPolicy() == 0:  # NSApplicationActivationPolicyRegular
            name = app.localizedName()
            if name:
                out.append(str(name))
    try:
        import Quartz
        wins = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
        for w in wins or []:
            if int(w.get("kCGWindowLayer", 1)) == 0 and w.get("kCGWindowOwnerName"):
                out.append(str(w["kCGWindowOwnerName"]))
    except Exception:  # noqa: BLE001
        pass
    return sorted(set(out))


def frontmost_app() -> str:
    from . import ax   # fresh (window-server) read; NSWorkspace's value goes stale off the main thread
    app = ax.frontmost_running()
    return str(app.localizedName()) if app else ""


def screen_locked() -> bool:
    try:
        import Quartz
        d = Quartz.CGSessionCopyCurrentDictionary() or {}
        return bool(d.get("CGSSessionScreenIsLocked", 0))
    except Exception:  # noqa: BLE001
        return False


def front_window_title() -> str:
    """Title of the focused window (a browser's tab title, a document name). Cheap: two AX calls."""
    try:
        import ApplicationServices as AS
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return ""
        el = AS.AXUIElementCreateApplication(int(app.processIdentifier()))
        AS.AXUIElementSetMessagingTimeout(el, 0.15)
        err, win = AS.AXUIElementCopyAttributeValue(el, "AXFocusedWindow", None)
        if err != 0 or win is None:
            return ""
        err, title = AS.AXUIElementCopyAttributeValue(win, "AXTitle", None)
        return str(title)[:120] if err == 0 and title else ""
    except Exception:  # noqa: BLE001
        return ""


def resolve_app(name: str, catalog: list[str]) -> str:
    n = name.strip().lower()
    alias = ALIASES.get(n)
    # An alias only counts if that app is actually present ("terminal" → iTerm only when iTerm exists;
    # otherwise the catalog's own Terminal matches below).
    if alias and any(c.lower() == alias.lower() for c in catalog):
        return alias
    for c in catalog:
        if c.lower() == n:
            return c
    for c in catalog:
        if n in c.lower() or c.lower() in n:
            return c
    return name


def running_app(name: str):
    from . import ax
    return ax.resolve_target(name)


def open_app(name: str, wait: float = 3.0) -> str:
    """Launch or bring an app to the front. `open -a` does the activation; we just wait for it to land.
    Returns the app's real name, or "" if there is no such app (callers must not report success then)."""
    real = resolve_app(name, installed_apps())
    r = subprocess.run(["open", "-a", real], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if r.returncode != 0:
        log.info("open_app: no app %r (resolved %r)", name, real)
        return ""
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if frontmost_app().lower() == real.lower():
            _ensure_window(real)
            return real
        time.sleep(0.1)
    # Didn't come forward on its own (cold launch, or focus stolen): activate explicitly, once.
    try:
        osascript(f'tell application "{esc(real)}" to activate', timeout=3)
        time.sleep(0.3)
    except Exception:  # noqa: BLE001
        pass
    _ensure_window(real)
    return real


def _ensure_window(app: str) -> None:
    """Some apps activate with no window (Finder, and apps whose last window was closed). Open one."""
    try:
        import ApplicationServices as AS
        ra = running_app(app)
        if ra is None:
            return
        el = AS.AXUIElementCreateApplication(int(ra.processIdentifier()))
        AS.AXUIElementSetMessagingTimeout(el, 0.3)
        err, wins = AS.AXUIElementCopyAttributeValue(el, "AXWindows", None)
        has = err == 0 and wins and len(wins) > 0
        if has:
            return
        if app == "Finder":
            osascript('tell application "Finder" to make new Finder window', timeout=3)
        elif frontmost_app().lower() == app.lower():
            # Cmd-N is the near-universal "new window/document". Only when `app` is really in front:
            # otherwise the keystroke lands in whatever app is (a new iTerm tab, a new email, …).
            osascript('tell application "System Events" to keystroke "n" using command down', timeout=3)
    except Exception:  # noqa: BLE001
        pass


def quit_app(name: str) -> str:
    """Ask the app to quit. Non-blocking: an app that raises a "save changes?" / "quit with remote
    session?" dialog made the old AppleScript `quit` hang for 8 s and then report failure."""
    real = resolve_app(name, running_apps())
    ra = running_app(real)
    if ra is None:
        raise RuntimeError(f"{real} isn't running")
    ra.terminate()
    return real


# ---------- applescript / keys ----------

def esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def osascript(script: str, timeout: float = 8.0) -> str:
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "osascript failed")
    return r.stdout.strip()


# Guard: never let a bug machine-gun keystrokes (each rejected key beeps). Cap the rate.
_last_keys: list[float] = []


def _key_guard() -> bool:
    now = time.monotonic()
    _last_keys.append(now)
    del _last_keys[:-12]
    recent = [t for t in _last_keys if now - t < 2.0]
    if len(recent) >= 10:
        log.warning("keystroke rate guard tripped — suppressing to avoid alert-sound spam")
        return False
    return True


MODS = {"cmd": "command down", "shift": "shift down", "alt": "option down", "ctrl": "control down"}
KEY_CODES = {"enter": 36, "return": 36, "escape": 53, "tab": 48, "space": 49, "delete": 51,
             "up": 126, "down": 125, "left": 123, "right": 124, "pageup": 116, "pagedown": 121,
             "home": 115, "end": 119}


def keystroke(key: str, *mods: str) -> None:
    if not _key_guard():
        return
    using = ", ".join(MODS[m] for m in mods)
    using_clause = f" using {{{using}}}" if using else ""
    if key in KEY_CODES:
        osascript(f'tell application "System Events" to key code {KEY_CODES[key]}{using_clause}')
    else:
        osascript(f'tell application "System Events" to keystroke "{esc(key)}"{using_clause}')


def type_text(text: str) -> None:
    osascript(f'tell application "System Events" to keystroke "{esc(text)}"')


def set_clipboard(text: str) -> None:
    p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
    p.communicate(text.encode("utf-8"))


def paste_text(text: str, *, replace_all: bool = False) -> None:
    """Insert text via the clipboard (Cmd+V). Robust for long/multiline content and code editors, which
    auto-indent and auto-close brackets when text is typed key by key. replace_all selects all first."""
    set_clipboard(text)
    time.sleep(0.05)
    if replace_all:
        keystroke("a", "cmd")
        time.sleep(0.05)
    keystroke("v", "cmd")


def click_menu(app: str, menu: str, item: str) -> None:
    osascript(
        f'tell application "System Events" to tell process "{esc(app)}" '
        f'to click menu item "{esc(item)}" of menu "{esc(menu)}" of menu bar 1'
    )


# ---------- urls ----------

def open_url(url: str) -> None:
    NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url))


def web_search(query: str) -> str:
    url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(query.strip())
    open_url(url)
    return url


def _site_key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _site_index() -> dict[str, str]:
    return {_site_key(k): v for k, v in WELL_KNOWN_SITES.items()}


def _near_known_site(key: str) -> str | None:
    """A known site the (mis)heard name is almost certainly meant to be: 'leetcod' → leetcode (typo-level
    edit), 'etcod' → leetcode (a large, distinctive fragment of it). Only for names of 5+ letters."""
    import difflib
    if len(key) < 5:
        return None
    best, best_r = None, 0.0
    for k in _site_index():
        if len(k) < 4:
            continue
        r = difflib.SequenceMatcher(None, key, k).ratio()
        if key in k and len(key) / len(k) >= 0.6:
            r = max(r, 0.9)
        if r > best_r:
            best, best_r = k, r
    return best if best_r >= 0.82 else None


def resolve_site(target: str) -> str | None:
    """URL for a spoken site name, or None if it isn't recognisable as one (then we search instead)."""
    t = target.strip().strip(".?!,").lower()
    t = re.sub(r"\s+dot\s+", ".", t).replace("dot com", ".com")
    if t.startswith(("http://", "https://")):
        return t
    index = _site_index()
    key = _site_key(t)
    if key in index:                                   # "google calendar", "hacker news", "leet code"
        return index[key]
    host = t.replace(" ", "")
    if "." in host:
        bare = host[4:] if host.startswith("www.") else host
        name, _, rest = bare.partition(".")
        # Only a plain "<name>.com" can be a mishearing of a known site. Real multi-part domains
        # (docs.python.org, github.io) are always taken literally.
        if rest == "com":
            if _site_key(name) in index:
                return index[_site_key(name)]
            near = _near_known_site(_site_key(name))
            if near:
                return index[near]
        return "https://" + host
    near = _near_known_site(key)
    return index[near] if near else None


def open_site(target: str) -> str:
    url = resolve_site(target)
    if url is None:
        # Not a site we recognise: fall back to search, which lands one click from the site.
        return web_search(target)
    open_url(url)
    return url


SITE_SEARCH = {
    "amazon": "https://www.amazon.com/s?k={q}", "youtube": "https://www.youtube.com/results?search_query={q}",
    "google": "https://www.google.com/search?q={q}", "ebay": "https://www.ebay.com/sch/i.html?_nkw={q}",
    "reddit": "https://www.reddit.com/search/?q={q}", "github": "https://github.com/search?q={q}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={q}", "twitter": "https://x.com/search?q={q}",
    "x": "https://x.com/search?q={q}", "linkedin": "https://www.linkedin.com/search/results/all/?keywords={q}",
    "google maps": "https://www.google.com/maps/search/{q}", "maps": "https://www.google.com/maps/search/{q}",
    "spotify": "https://open.spotify.com/search/{q}", "netflix": "https://www.netflix.com/search?q={q}",
    "google images": "https://www.google.com/search?tbm=isch&q={q}", "stack overflow": "https://stackoverflow.com/search?q={q}",
    "best buy": "https://www.bestbuy.com/site/searchpage.jsp?st={q}", "walmart": "https://www.walmart.com/search?q={q}",
    "leetcode": "https://leetcode.com/problemset/?search={q}", "piazza": "https://piazza.com",
}


def site_search(site: str, query: str) -> str:
    """Search a known site directly by its search URL (instant, no clicking)."""
    import re as _re
    s = site.strip().lower().replace(".com", "")
    tpl = SITE_SEARCH.get(s)
    query = _re.sub(r"\s+(?:on|in|at|from)\s+(?:the\s+)?[\w.]*" + _re.escape(s.split()[0]) + r"[\w.]*\s*$", "",
                    query.strip().strip(".?!,"), flags=_re.I).strip() or query
    query = _re.sub(r"\s+(?:on|in)\s+(?:this|the)\s+(?:page|site|tab|website)\s*$|\s+here\s*$", "", query, flags=_re.I).strip() or query
    q = urllib.parse.quote_plus(query)
    if tpl and "{q}" in tpl:
        open_url(tpl.format(q=q))
        return f"Searched {site} for {query}"
    return web_search(f"{query} site:{s}.com" if "." not in s else f"{query} site:{s}")


# ---------- youtube / page control ----------

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/153.0.0.0 Safari/537.36")


def clean_media_query(query: str) -> str:
    """'some Lofi on YouTube for me' → 'Lofi': drop filler the span picker can leave around the title."""
    q = query.strip().strip(" .?!,")
    for _ in range(3):
        q = re.sub(r"\s+(for me|please|for us|real quick|now)$", "", q, flags=re.I)
        q = re.sub(r"\s*\b(on|in|from)\s+you\s?tube$", "", q, flags=re.I)
        q = re.sub(r"^(some|a|an|the|me|us)\s+", "", q, flags=re.I)
        q = q.strip(" .?!,")
    return q or query


def youtube_play(query: str) -> str:
    """Open the first YouTube result for `query` directly (no browser round-trip to find it)."""
    import re as _re
    import httpx
    query = clean_media_query(query)
    url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query)
    try:
        r = httpx.get(url, headers={"user-agent": _UA, "accept-language": "en-US"}, follow_redirects=True, timeout=6)
        ids = list(dict.fromkeys(_re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', r.text)))
    except Exception:  # noqa: BLE001
        ids = []
    if ids:
        open_url(f"https://www.youtube.com/watch?v={ids[0]}")
        return f"Playing {query}"
    open_url(url)
    return f"YouTube: {query}"


# ---------- system ----------

def screenshot() -> str:
    path = str(Path.home() / "Desktop" / f"Screenshot {datetime.now():%Y-%m-%d at %H.%M.%S}.png")
    subprocess.run(["screencapture", "-x", path], check=False)
    return path


def set_volume(level: str) -> None:
    if level == "mute":
        osascript("set volume with output muted")
    elif level == "unmute":
        osascript("set volume without output muted")
    elif level == "max":
        osascript("set volume without output muted\nset volume output volume 100")
    else:
        cur = int(osascript("output volume of (get volume settings)") or "50")
        delta = 15 if level == "louder" else -15
        osascript(f"set volume without output muted\nset volume output volume {max(0, min(100, cur + delta))}")


def media(cmd: str) -> str:
    """play_pause | next | previous, sent to Spotify if running, else Music."""
    player = "Spotify" if "Spotify" in running_apps() else "Music"
    verb = {"play_pause": "playpause", "next": "next track", "previous": "previous track"}[cmd]
    osascript(f'tell application "{player}" to {verb}')
    return player


def toggle_dark_mode() -> None:
    osascript('tell application "System Events" to tell appearance preferences to set dark mode to not dark mode')


def notes_new_note(title: str | None) -> None:
    body = esc(title.strip().strip(".?!,")) if title else "New Note"
    osascript(
        'tell application "Notes"\n'
        '  activate\n'
        f'  set n to make new note at default folder of default account with properties {{body:"{body}"}}\n'
        '  show n\n'
        'end tell'
    )


def photo_booth_take_picture() -> None:
    open_app("Photo Booth")
    time.sleep(0.9)
    try:
        click_menu("Photo Booth", "File", "Take Photo")
    except Exception:
        keystroke("enter", "cmd")


def scroll(direction: str) -> None:
    from Quartz import (CGEventCreateScrollWheelEvent, CGEventPost, kCGHIDEventTap,
                        kCGScrollEventUnitLine)
    amount = -12 if direction == "down" else 12
    ev = CGEventCreateScrollWheelEvent(None, kCGScrollEventUnitLine, 1, amount)
    CGEventPost(kCGHIDEventTap, ev)


def open_folder(which: str) -> str:
    home = Path.home()
    path = {"desktop": home / "Desktop", "downloads": home / "Downloads", "documents": home / "Documents",
            "home": home, "applications": Path("/Applications"), "pictures": home / "Pictures"}[which]
    subprocess.Popen(["open", str(path)])
    return str(path)
