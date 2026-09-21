"""Replay a spoken sentence word by word through the real router (no mic, real Jev, dry-run tools).

    uv run jevme-replay "open up the notes app for me and once you're there create a new note"
    uv run jevme-replay --live "..."   # actually run the actions
"""
from __future__ import annotations

import logging
import sys
import threading
import time

from . import tools as T
from .jev import JevClient
from .router import Router

DEFAULT = ("Alright can you open up the notes app for me and once you're there can you create a new note and "
           "inside this new note let's make the title say hello. Great great okay let's move on and can you open up "
           "the Chrome browser and once you're there can you google search Norbert Wiener? Now can you open up "
           "x.com. Nice nice okay now can you open up the photo booth and let's take a picture of me. "
           "Cool awesome thank you.")


class MainLoop:
    """A tiny main-thread dispatcher standing in for the Cocoa run loop."""

    def __init__(self) -> None:
        self.q: list = []
        self.lock = threading.Lock()

    def dispatch(self, fn, args) -> None:
        with self.lock:
            self.q.append((fn, args))

    def drain(self) -> None:
        while True:
            with self.lock:
                if not self.q:
                    return
                fn, args = self.q.pop(0)
            fn(*args)


def main() -> None:
    logging.basicConfig(level="INFO", format="%(asctime)s.%(msecs)03d %(levelname)-5s %(name)s %(message)s",
                        datefmt="%H:%M:%S")
    args = sys.argv[1:]
    live = "--live" in args
    args = [a for a in args if a != "--live"]
    sentence = " ".join(args) if args else DEFAULT
    words_per_sec = 3.0

    if not live:
        for t in T.TOOLS:
            t.run = (lambda name: (lambda a: f"[dry] {name} {a}"))(t.name)

    loop = MainLoop()
    fired: list[str] = []
    def run_clause(clause):
        fired.append(clause)
        r.execute_clause(clause, lambda s: None)

    r = Router(JevClient(), on_preview=lambda s: None,
               on_action=lambda label: (fired.append(label), print(f"    ⚡ {label}")),
               on_error=lambda e: print("    ✗", e), dispatch_main=loop.dispatch,
               on_general=lambda g: (print(f"    🤖 agent: {g}"), run_clause(g)),
               on_plan=lambda cs: [run_clause(c) for c in cs],
               on_commit=lambda name, args, label: (r.run_tool(name, args, label, lambda s: None)),
               on_stream=lambda c: (print(f"    ▶ stream: {c}"), run_clause(c)),
               on_learn=lambda u: (fired.append(f"[learn] {u}"), print(f"    🛠 learn: {u}")))

    words = sentence.split()
    t0 = time.monotonic()
    text = ""
    for i, w in enumerate(words):
        text = (text + " " + w).strip()
        r.on_partial(text, False)
        # emulate speech pacing; punctuation = a natural pause
        pause = 1 / words_per_sec + (0.9 if w.endswith((".", "?", "!")) else 0.0)
        end = time.monotonic() + pause
        while time.monotonic() < end:
            loop.drain()
            r.tick()
            time.sleep(0.03)
    for _ in range(40):
        loop.drain()
        r.tick()
        time.sleep(0.05)
    print(f"\n{len(fired)} actions in {time.monotonic() - t0:.1f}s:")
    for f in fired:
        print("  ", f)


if __name__ == "__main__":
    main()
