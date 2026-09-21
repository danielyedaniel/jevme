"""UI memory: learning a case and resolving it against a fresh tree (no network)."""
from __future__ import annotations

from jevme import ui_memory


def _mem(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_memory, "PATH", tmp_path / "ui.json")
    ui_memory._singleton = None
    return ui_memory.memory()


def test_remember_and_persist(tmp_path, monkeypatch):
    m = _mem(tmp_path, monkeypatch)
    m.remember("Spotify", "the search box", role="AXComboBox", label="What do you want to play?")
    cases = m.cases("Spotify")
    assert len(cases) == 1 and cases[0].label == "What do you want to play?"
    # reload from disk
    ui_memory._singleton = None
    m2 = ui_memory.memory()
    assert m2.cases("Spotify")[0].role == "AXComboBox"


def test_hits_increment_not_duplicate(tmp_path, monkeypatch):
    m = _mem(tmp_path, monkeypatch)
    for _ in range(3):
        m.remember("Notes", "new note", role="AXButton", label="New Note")
    cases = m.cases("Notes")
    assert len(cases) == 1 and cases[0].hits == 3


def test_ignores_empty_target(tmp_path, monkeypatch):
    m = _mem(tmp_path, monkeypatch)
    m.remember("Mail", "compose", role="AXButton", label="")  # no label, no rel → not stored
    assert m.cases("Mail") == []


def test_resolve_matches_live_element(tmp_path, monkeypatch):
    from jevme import see, ax
    _mem(tmp_path, monkeypatch)
    ui_memory.memory().remember("Spotify", "search", role="AXComboBox", label="What do you want to play?")
    case = ui_memory.memory().cases("Spotify")[0]
    elem = ax.Elem(1, "AXComboBox", "What do you want to play?", 100, 60, 200, 30, ref=None)
    snap = ax.Snapshot("Spotify", 1, (0, 0, 1440, 900), [elem], 10, 5)
    assert see._resolve(case, snap) is elem
    # a different label does not resolve
    case2 = ui_memory.Case(intent="x", role="AXButton", label="Gone")
    assert see._resolve(case2, snap) is None


def test_task_recipe_persist_and_match(tmp_path, monkeypatch):
    from jevme import ui_memory as U
    monkeypatch.setattr(U, "TASK_PATH", tmp_path / "task.json")
    U._task_singleton = None
    tm = U.task_memory()
    steps = [U.RecipeStep(op="open_app", arg="Spotify"),
             U.RecipeStep(op="click", role="AXComboBox", label="What do you want to play?")]
    tm.remember("open spotify and go to search", "Google Chrome", steps)
    # single-step recipes are not stored
    tm.remember("just one", "X", [U.RecipeStep(op="key", arg="enter")])
    assert len(tm.all()) == 1
    # reload from disk
    U._task_singleton = None
    tm2 = U.task_memory()
    r = tm2.all()[0]
    assert r.goal == "open spotify and go to search" and len(r.steps) == 2
    assert r.steps[1].label == "What do you want to play?"


def test_rejects_dynamic_and_volatile(tmp_path, monkeypatch):
    from jevme import ui_memory as U
    monkeypatch.setattr(U, "PATH", tmp_path / "ui.json"); U._singleton = None
    m = U.memory()
    # dynamic labels / content roles are refused
    m.remember("Messages", "message to nikki", role="AXCell", label="Nicky, Idk lol, 12:23 PM")
    m.remember("Messages", "the field", role="AXTextField", label='field = "Did u do 343"')
    m.remember("X", "reply to this", role="AXButton", label="Reply")   # volatile intent
    m.remember("Notes", "empty", role="AXButton", label="")            # empty label
    assert m.cases("Messages") == [] and m.cases("X") == [] and m.cases("Notes") == []
    # a real stable control is kept
    m.remember("Spotify", "search", role="AXComboBox", label="What do you want to play?")
    assert len(m.cases("Spotify")) == 1


def test_recipe_rejects_volatile_and_terminal(tmp_path, monkeypatch):
    from jevme import ui_memory as U
    monkeypatch.setattr(U, "TASK_PATH", tmp_path / "t.json"); U._task_singleton = None
    tm = U.task_memory()
    steps = [U.RecipeStep(op="open_app", arg="Messages"), U.RecipeStep(op="click", role="AXButton", label="Send")]
    tm.remember("message johnny saying hi", "Messages", steps)      # volatile goal
    tm.remember("go to settings", "iTerm2", steps)                  # terminal context
    tm.remember("open profile", "Google Chrome",
                [U.RecipeStep(op="click", role="AXCell", label="Nicky, 12:23 PM")])  # dynamic step (also <2)
    assert tm.all() == []
    tm.remember("go to my network", "Google Chrome", steps)
    assert len(tm.all()) == 1
