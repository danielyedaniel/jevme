"""Routing eval: everyday phrases through the real router (dry-run tools, real Jev).

    uv run python -m jevme.evalrun            # all cases
    uv run python -m jevme.evalrun spotify    # cases whose text contains 'spotify'

Each case says what outcome is acceptable: a tool name, 'agent', 'plan', 'learn', or several.
"""
from __future__ import annotations

import logging
import sys
import time

from . import tools as T
from .jev import JevClient
from .replay import MainLoop
from .router import Router

CASES: list[tuple[str, set[str]]] = [
    ("skip this song", {"media_next"}),
    ("turn the volume down", {"set_volume"}),
    ("mute", {"set_volume"}),
    ("pause", {"media_play_pause"}),
    ("open spotify and play my liked songs", {"plan", "agent"}),
    ("open a new tab and go to piazza", {"plan", "new_tab", "open_site"}),
    ("search leetcode for two sum", {"site_search"}),
    ("go back", {"go_back"}),
    ("close all the other tabs", {"agent", "learn"}),
    ("reply to alex on discord saying I'll be there in five", {"agent"}),
    ("text mom that I'm on my way", {"agent"}),
    ("make a new note called cs 343 lecture", {"notes_new_note"}),
    ("take a screenshot", {"take_screenshot"}),
    ("open my downloads folder", {"open_folder"}),
    ("switch to discord", {"open_app"}),
    ("what's on my screen", {"read_screen"}),
    ("scroll down", {"scroll"}),
    ("click the first result", {"click_thing"}),
    ("play lofi hip hop on youtube", {"youtube_play"}),
    ("google how tall is mount everest", {"web_search"}),
    ("open chatgpt", {"open_site", "open_app"}),
    ("select all and copy", {"plan", "select_all"}),
    ("paste it here", {"paste"}),
    ("make chrome full screen", {"fullscreen_window"}),
    ("hide everything else", {"hide_others"}),
    ("quit spotify", {"quit_app"}),
    ("reload the page", {"reload_page"}),
    ("type hello world", {"type_text"}),
    ("open the terminal and run ls", {"plan", "agent"}),
    ("lock my screen", {"lock_screen"}),
    ("look for a new rice cooker", {"agent", "web_search", "site_search"}),
    ("go to amazon.com and add a rice cooker to my cart", {"plan", "agent"}),
    ("delete the groceries note", {"learn", "agent"}),
    ("make the screen brighter", {"learn"}),
    ("send an email to my prof asking for an extension", {"agent"}),
    ("open the second tab", {"agent", "click_thing"}),
    ("zoom in", {"agent", "learn"}),
    ("add buy milk to my reminders", {"agent"}),
    ("what time is it", {"chat", "agent", "learn"}),
    ("thanks that's all", {"chat"}),
    # second batch: the shape of a student's day
    ("open piazza and check the new posts", {"plan", "agent", "open_site", "open_app"}),  # piazza opens first
    ("go to the next tab", {"agent", "click_thing", "press_key"}),
    ("close the other tabs", {"agent", "learn"}),
    ("open leetcode", {"open_site", "open_app"}),
    ("bring up the terminal", {"open_app"}),
    ("run npm test in the terminal", {"agent", "plan"}),
    ("open discord and go to the general channel", {"plan", "agent", "open_app"}),  # open_app fires mid-sentence, rest → agent
    ("message the group chat that I'm running late", {"agent"}),
    ("open my cs 343 notes", {"agent", "notes_new_note", "open_app"}),
    ("new note", {"notes_new_note"}),
    ("start a new word document", {"agent", "open_app", "plan"}),
    ("bold that", {"agent", "learn", "press_key"}),
    ("undo", {"undo"}),
    ("play the next episode", {"agent", "media_next", "click_thing", "media_play_pause"}),
    ("turn it up a bit", {"set_volume"}),
    ("what does this page say", {"read_screen"}),
    ("open the link in a new tab", {"agent", "click_thing"}),
    ("sign out", {"agent", "click_thing", "learn"}),
    ("scroll to the bottom", {"scroll", "agent", "press_key"}),
    ("copy the link", {"agent", "copy", "learn"}),
    ("switch to light mode", {"toggle_dark_mode"}),
    ("show me the desktop", {"open_folder", "hide_others", "agent", "learn"}),
    ("open chrome go to youtube and play lofi", {"plan", "open_app"}),  # chrome fires first, rest plans
    ("um okay so", {"chat", "nothing"}),
    # from the logged session
    ("send", {"send_draft"}),
    ("send it", {"send_draft"}),
    ("send the message", {"send_draft"}),
    ("send a message to sarah saying hi", {"agent"}),
]


def run(filter_text: str | None = None, words_per_sec: float = 6.0) -> None:
    logging.basicConfig(level="WARNING")
    for t in T.TOOLS:
        t.run = (lambda name: (lambda a: f"{name} {a}"))(t.name)
    jev = JevClient()
    results = []
    cases = [c for c in CASES if not filter_text or filter_text in c[0]]
    for text, ok in cases:
        loop = MainLoop()
        got: list[str] = []
        r = Router(jev, on_preview=lambda s: None,
                   on_action=lambda label: got.append(label.split(" ")[0]),
                   on_error=lambda e: got.append(f"error:{e}"), dispatch_main=loop.dispatch,
                   on_general=lambda g: got.append("agent"), on_plan=lambda cs: got.append("plan"),
                   on_commit=lambda name, args, label: got.append(name),
                   on_stream=None,  # streaming is a live behavior; tested separately
                   on_learn=lambda u: got.append("learn"))
        words = text.split()
        acc = ""
        for w in words:
            acc = (acc + " " + w).strip()
            r.on_partial(acc, False)
            end = time.monotonic() + 1 / words_per_sec
            while time.monotonic() < end:
                loop.drain(); r.tick(); time.sleep(0.02)
        r.on_partial(acc + ".", False)
        end = time.monotonic() + 2.2
        while time.monotonic() < end:
            loop.drain(); r.tick(); time.sleep(0.03)
            if got:
                break
        loop.drain()
        outcome = got[0] if got else ("chat" if r.last_probs.get("chat", 0) > 0.5 else "nothing")
        passed = outcome in ok
        results.append(passed)
        print(f"{'PASS' if passed else 'FAIL'}  {text!r:60} -> {outcome:16} (want {'/'.join(sorted(ok))})")
    print(f"\n{sum(results)}/{len(results)} passed")


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else None)
