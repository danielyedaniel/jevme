"""Demonstration recording: synthetic events in, replayable recipe (or nothing) out."""
from jevme.agent import Agent, template_fill
from jevme.ui_memory import RecipeStep
from jevme.watch import Demo


def test_menu_choice_becomes_a_one_step_recipe():
    d = Demo("zoom in", "Preview", "Preview")
    d.click("Preview", "AXMenuItem", "Zoom In", menu_path=["View", "Zoom In"])
    steps = d.recipe()
    assert [(s.op, s.arg) for s in steps] == [("menu", "View > Zoom In")]


def test_typed_text_kept_only_when_it_was_spoken():
    d = Demo("search discord for cats", "Discord", "Discord")
    d.click("Discord", "AXButton", "Search")
    d.typed("Discord", "AXTextField", "Search", "ca")
    d.key("Discord", "enter", [], field_value="cats")
    steps = d.recipe()
    assert [(s.op, s.arg) for s in steps] == [("click", ""), ("type", "cats"), ("key", "enter")]
    assert steps[1].text_from_goal


def test_private_text_is_never_kept_and_voids_the_recipe():
    d = Demo("log into canvas", "Google Chrome", "Google Chrome")
    d.click("Google Chrome", "AXTextField", "Username")
    d.typed("Google Chrome", "AXTextField", "Username", "jdoe.42")
    d.key("Google Chrome", "tab", [], field_value="jdoe.42")
    assert d.recipe() is None
    assert all("jdoe" not in s.arg for s in d.steps)


def test_password_field_voids_the_recipe():
    d = Demo("sign in", "Safari", "Safari")
    d.typed("Safari", "AXSecureTextField", "", None)
    assert d.recipe() is None


def test_app_switch_during_failed_attempt_is_prepended():
    d = Demo("open my cs 343 notes", "iTerm2", "Notes")
    d.click("Notes", "AXRow", "CS 343")
    assert [(s.op, s.arg or s.label) for s in d.recipe()] == [("open_app", "Notes"), ("click", "CS 343")]


def test_nothing_demonstrated_is_nothing_saved():
    assert Demo("zoom in", "Preview", "Preview").recipe() is None


def test_idle_ends_demo():
    d = Demo("zoom in", "Preview", "Preview")
    assert not d.is_over(d.started + 5)
    assert d.is_over(d.started + 21)
    d.click("Preview", "AXButton", "Zoom")
    assert not d.is_over(d.last_event + 3)
    assert d.is_over(d.last_event + 8)


def test_replayed_demo_types_the_new_value():
    step = RecipeStep("type", "AXTextField", "Search", "cats", text_from_goal=True)
    assert Agent._replay_text(step, "search discord for dogs", "search discord for cats") == "dogs"
    assert template_fill("find cats in notes", "cats", "open notes") is None


def test_goal_named_row_is_a_templated_click():
    d = Demo("open my cs 343 notes", "Notes", "Notes")
    d.click("Notes", "AXRow", "CS 343")
    (step,) = d.recipe()
    assert step.op == "click" and step.text_from_goal
    assert template_fill("open my cs 343 notes", step.label, "open my cs 341 notes") == "cs 341"


def test_clicking_message_content_is_not_replayable():
    d = Demo("zoom in", "Messages", "Messages")
    d.click("Messages", "AXButton", "‎New photo available from Alex")
    assert d.recipe() is None
    d2 = Demo("show replies", "Messages", "Messages")
    d2.click("Messages", "AXButton", "‎1 Reply")
    assert d2.recipe() is None
