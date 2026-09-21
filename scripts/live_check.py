"""Live end-to-end checks of the agent and plan executor on this Mac (needs an unlocked screen).

    uv run python scripts/live_check.py            # all
    uv run python scripts/live_check.py reminders  # only tasks containing the word

Only benign, reversible tasks: a reminder, a note, tab switching, a YouTube search.
"""
from __future__ import annotations

import logging
import sys
import time

from jevme import actions as A
from jevme.agent import Agent, compose_with_claude
from jevme.jev import JevClient
from jevme.router import Router

TASKS: list[tuple[str, list[str] | str]] = [
    ("reminders", "add buy milk to my reminders"),
    ("notes", "open the notes app and make a new note called live check"),
    ("youtube", ["search youtube for lofi beats", "click the first video"]),
    ("tabs", "open the second tab in chrome"),
    ("piazza", ["open chrome", "go to piazza.com"]),
]


def main() -> None:
    logging.basicConfig(level="INFO", format="%(asctime)s.%(msecs)03d %(name)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel("WARNING")
    logging.getLogger("jevme.ax").setLevel("WARNING")
    only = sys.argv[1] if len(sys.argv) > 1 else None
    if A.screen_locked():
        print("screen is locked; unlock it first")
        return
    jev = JevClient()
    agent = Agent(jev, compose=compose_with_claude)
    router = Router(jev, on_preview=lambda s: None, on_action=lambda l: print("   ✓", l),
                    on_error=lambda e: print("   ✗", e), dispatch_main=lambda fn, a: fn(*a))
    router.agent = agent
    outcomes = []
    for name, task in TASKS:
        if only and only not in name:
            continue
        print(f"\n=== {name}: {task}")
        t = time.time()
        if isinstance(task, list):
            router.run_plan(task, progress=lambda s: print("   ▸", s))
            ok = True
        else:
            res = agent.run(task, progress=lambda s: print("   ▸", s))
            ok = res.ok
            print(f"   RESULT ok={res.ok} {res.summary!r}")
        print(f"   {time.time() - t:.1f}s")
        outcomes.append((name, ok))
    print("\n" + "  ".join(f"{n}:{'ok' if ok else 'FAIL'}" for n, ok in outcomes))


if __name__ == "__main__":
    main()
