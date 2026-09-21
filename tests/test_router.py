"""Router decision logic with a scripted Jev: no network, no mic, no screen."""
from __future__ import annotations

import time

import pytest

from jevme import tools as T
from jevme.jev import Answer, Decision
from jevme.router import Router, split_clauses


# ---------- clause splitting ----------

@pytest.mark.parametrize("text, expected", [
    ("open chrome and go to amazon and add a rice cooker to my cart", ["open chrome", "go to amazon", "add a rice cooker to my cart"]),
    ("select all and copy", ["select all", "copy"]),
    ("add milk and eggs to my reminders", ["add milk and eggs to my reminders"]),
    ("open notes then make a new note called plans and type buy tickets", ["open notes", "make a new note called plans", "type buy tickets"]),
    ("okay now close this tab", ["close this tab"]),
    ("Open discord and go to the general channel.", ["Open discord", "go to the general channel"]),
])
def test_split_clauses(text, expected):
    assert split_clauses(text) == expected


# ---------- scripted router ----------

class FakeJev:
    """Answers `intent` by keyword; every arg question gets a fixed pick."""

    def __init__(self, table, default=("not_yet", 0.9)):
        self.table = table
        self.default = default
        self.calls: list[str] = []

    def ask_async(self, seq, state, questions, on_done, on_error):
        utt = state["utterance"]
        self.calls.append(utt)
        name, conf = self.default
        for key, val in self.table.items():
            if key in utt.lower():
                name, conf = val
        probs = {name: conf, "not_yet": max(0.0, 1 - conf) if name != "not_yet" else conf}
        answers = {"intent": Answer("choice", name, conf, probs)}
        for q in questions:
            if q.startswith("arg::"):
                crit = questions[q]["criteria"]
                answers[q] = Answer("choice", next(iter(crit)), 0.9, {})
            elif q.startswith("text::"):
                crit = [k for k in questions[q]["criteria"] if not k.startswith("__")]
                answers[q] = Answer("choice", crit[-1] if crit else "__none__", 0.9, {})
        on_done(Decision(seq, answers, 5))

    def ask(self, state, questions):
        raise NotImplementedError


class Harness:
    def __init__(self, jev):
        from jevme import config
        config.DEBOUNCE_S = 0.0          # no timers: every partial is judged synchronously
        self.events: list[tuple[str, object]] = []
        self.router = Router(
            jev, on_preview=lambda s: None,
            on_action=lambda label: self.events.append(("action", label)),
            on_error=lambda e: self.events.append(("error", e)),
            dispatch_main=lambda fn, args: fn(*args),
            on_general=lambda g: self.events.append(("general", g)),
            on_plan=lambda cs: self.events.append(("plan", cs)),
            on_learn=lambda u: self.events.append(("learn", u)),
        )
        for t in T.TOOLS:
            t.run = (lambda name: (lambda a: name))(t.name)

    def say(self, text: str, settle: float = 0.0):
        acc = ""
        for w in text.split():
            acc = (acc + " " + w).strip()
            self.router.on_partial(acc, False)
            self.router.tick()
        if settle:
            self.router.last_change_t = time.monotonic() - settle
            self.router.tick()
        return self

    def committed(self):
        return [e[1] for e in self.events if e[0] == "action"]


def test_instant_tool_commits_when_stable():
    h = Harness(FakeJev({"skip": ("media_next", 0.95)}))
    h.say("skip this song")
    assert "media_next" in h.committed()


def test_low_confidence_instant_tool_commits_after_pause():
    h = Harness(FakeJev({"close this tab": ("close_tab", 0.7)}))
    h.say("close this tab")
    assert h.committed() == []
    h.say("", settle=0.6)  # user paused
    assert "close_tab" in h.committed()


def test_compound_sentence_becomes_plan():
    h = Harness(FakeJev({}, default=("general", 0.8)))
    h.say("go to amazon and add a rice cooker to my cart", settle=1.1)
    kinds = [e[0] for e in h.events]
    assert "plan" in kinds
    plan = next(e[1] for e in h.events if e[0] == "plan")
    assert plan == ["go to amazon", "add a rice cooker to my cart"]


def test_actionable_sentence_goes_to_agent_not_bin():
    jev = FakeJev({"rice cooker": ("general", 0.8)})
    h = Harness(jev)
    h.say("look for a new rice cooker", settle=1.1)
    assert ("general", "look for a new rice cooker") in h.events


def test_mid_sentence_commit_keeps_remainder():
    h = Harness(FakeJev({"open discord": ("open_app", 0.99)}))
    h.say("open discord and go to settings")
    h.say("", settle=0.5)  # brief pause: the discord clause commits
    assert "open_app" in h.committed()
    # The discord clause is consumed; a remainder is kept for the next decision, not the whole sentence.
    pending = h.router.pending_text().lower()
    assert pending and "discord" not in pending and len(pending) < len("open discord and go to settings")


def test_locked_confirmation_waits_for_yes():
    jev = FakeJev({"yes": ("chat", 0.9)})
    h = Harness(jev)
    ran = []
    h.router.ask_confirmation("delete note", lambda: ran.append(1))
    # emulate Jev saying yes
    orig = jev.ask_async

    def yes(seq, state, questions, on_done, on_error):
        answers = {"intent": Answer("choice", "chat", 0.9, {"chat": 0.9}),
                   "confirm": Answer("noul", noul=0.95), "deny": Answer("noul", noul=0.02)}
        on_done(Decision(seq, answers, 3))
    jev.ask_async = yes
    h.say("yes do it")
    time.sleep(0.05)
    assert ran == [1]
    jev.ask_async = orig


def test_risky_learned_tool_confirms_every_run():
    # Reviewed bug: spec.risky was only honoured the first time a generated tool ran.
    from jevme import learned as L, tools as T
    ran = []
    spec = L.LearnedSpec(name="notes_delete_x", what="delete a note", examples=["delete the note"],
                         kind="applescript", script='return "x"', label="Deleted", risky=True)
    tool = L.to_tool(spec)
    tool.run = lambda a: ran.append(1) or "Deleted"
    T.BY_NAME[tool.name] = tool
    try:
        h = Harness(FakeJev({}))
        asked = []
        h.router.ask_confirmation = lambda label, run: asked.append((label, run))
        for _ in range(2):
            h.router.run_tool(tool.name, {}, "Deleted", lambda s: None)
        assert len(asked) == 2 and ran == []          # asked both times, ran neither time
        asked[0][1]()                                  # the user says yes
        assert ran == [1]
    finally:
        T.BY_NAME.pop(tool.name, None)


# ---------- cursor survives STT revisions (logged: "o Code", "s tab close this") ----------

def _router():
    return Harness(FakeJev({})).router


def test_cursor_survives_head_revision():
    r = _router()
    r.transcript = "Open Visual Studio"
    r.cursor = len(r.transcript)                     # committed open_app here
    r.transcript = "Hello, open Visual Studio Code"  # Apple rewrote the head and added a word
    assert r.pending_text() == "Code"                # a whole word, not "o Code"


def test_cursor_survives_tail_growth_after_short_commit():
    r = _router()
    r.transcript = "This. Tab"
    r.cursor = len(r.transcript)
    r.transcript = "This tab close this"
    assert r.pending_text() == "close this"          # not "s tab close this"


def test_cursor_plain_append_unchanged():
    r = _router()
    r.transcript = "open notes"
    r.cursor = len(r.transcript)
    r.transcript = "open notes and make a new note"
    assert r.pending_text() == "and make a new note"


def test_cursor_mid_word_offset_rounds_to_whole_word():
    r = _router()
    r.transcript = "open discord and go"
    r.cursor = 3                                     # inside "open": that word counts as consumed
    assert r.pending_text() == "discord and go"


def test_one_word_fragment_split_by_interim_punctuation_does_not_fire():
    # Logged: "This tab" was punctuated "This. Tab" and the lone "Tab" pressed the Tab key.
    h = Harness(FakeJev({"tab": ("press_key", 0.95)}))
    h.router.transcript = ""
    for partial in ["This", "This. Tab", "This. Tab"]:
        h.router.on_partial(partial, False)
        h.router.tick()
    assert "press_key" not in h.committed()


def test_genuine_one_word_command_still_fires():
    h = Harness(FakeJev({"pause": ("media_play_pause", 0.95)}))
    h.say("pause")
    h.router.on_partial("pause.", False)
    h.router.tick()
    assert "media_play_pause" in h.committed()


def test_one_word_fragment_not_best_guessed_on_pause():
    h = Harness(FakeJev({"tab": ("press_key", 0.6)}))
    for partial in ["This", "This. Tab"]:
        h.router.on_partial(partial, False)
        h.router.tick()
    h.router.last_change_t = time.monotonic() - 1.2     # the user pauses
    h.router.tick()
    assert "press_key" not in h.committed()


def test_words_spoken_during_flight_are_not_swallowed():
    """A decision is about the utterance Jev saw; speech that arrived while it was in flight is kept."""
    h = Harness(FakeJev({}))
    r = h.router
    r.transcript = "open spotify play drake"
    r._hand_off("open spotify")
    assert h.events[-1] == ("general", "open spotify")
    assert r.pending_text() == "play drake"


def test_commit_of_stale_proposal_keeps_later_speech():
    from jevme.router import Proposal
    h = Harness(FakeJev({}))
    r = h.router
    r.transcript = "pause and play the next track"
    r._commit(Proposal("media_play_pause", {}, 0.9, 1, "pause"))
    assert r.pending_text() == "and play the next track"


@pytest.mark.parametrize("transcript,decided", [("Close this tab", "Close this"), ("Open VS Code", "Open VS")])
def test_in_flight_words_that_finish_the_command_are_consumed(transcript, decided):
    from jevme.router import Proposal
    h = Harness(FakeJev({}))
    r = h.router
    r.transcript = transcript
    r._commit(Proposal("close_tab", {}, 0.9, 1, decided))
    assert r.pending_text() == ""


@pytest.mark.parametrize("text,incomplete", [
    ("zoom in", False), ("bold that", False), ("sign in", False), ("turn it on", False),
    ("send a message to", True), ("search for", True), ("tell him that", True), ("turn on", True),
])
def test_particles_and_objects_are_not_dangling(text, incomplete):
    from jevme.router import _looks_incomplete
    assert _looks_incomplete(text, 1.5) is incomplete


def test_tail_right_after_commit_is_absorbed_not_acted_on():
    import time as _t
    h = Harness(FakeJev({}))
    r = h.router
    r.transcript, r.cursor = "Close this", len("Close this")
    r.last_commit_t = _t.monotonic()
    r.on_partial("Close this tab.", False)
    r.last_change_t -= 1.0                      # the tail stopped growing
    r.tick()
    assert r.pending_text() == ""


def test_tail_that_keeps_growing_is_not_absorbed():
    import time as _t
    h = Harness(FakeJev({}))
    r = h.router
    r.transcript, r.cursor = "open notes", len("open notes")
    r.last_commit_t = _t.monotonic()
    r.on_partial("open notes for", False)
    r.tick()
    r.on_partial("open notes for me and", False)
    r.tick()
    assert r.pending_text() == "for me and"


def test_real_short_command_after_commit_is_kept():
    import time as _t
    h = Harness(FakeJev({}))
    r = h.router
    r.transcript, r.cursor = "Close this tab", len("Close this tab")
    r.last_commit_t = _t.monotonic()
    r.on_partial("Close this tab undo", False)
    assert r.pending_text() == "undo"
