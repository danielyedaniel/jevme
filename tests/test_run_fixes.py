"""Regressions from the 17:47 usage run."""
import pytest

from jevme import policy
from jevme.agent import NEEDS_ACTION, WANTS_CONTENT, infer_app, web_service


@pytest.mark.parametrize("goal,site", [
    ("Compose a new Gmail email", "gmail"), ("Open my messages on LinkedIn", "linkedin"),
    ("Let's compose a new Gmail message.", "gmail"), ("open google calendar", "google calendar"),
    ("send a message to sarah saying hi", None),
])
def test_web_services(goal, site):
    assert web_service(goal) == site
    if site:
        assert infer_app(goal) is None     # never Mail / Messages for a website


@pytest.mark.parametrize("goal,wants", [
    ("This compose a new message", False), ("Compose a new Gmail email", False),
    ("I want you to implement the solution", True), ("compose an email to bob about the meeting", True),
    ("write a reply thanking her", True),
])
def test_compose_only_when_content_is_asked_for(goal, wants):
    assert bool(WANTS_CONTENT.search(goal)) is wants


@pytest.mark.parametrize("label,goal,blocked", [
    ("Submit", "I want you to implement the solution", "submit"),
    ("Submit", "submit my solution", None),
    ("Run", "OK now run the solution", None),
    ("Delete", "Delete this email draft", None),
    ("Send", "compose a new message", "send"),
    ("Send", "send a message to sarah saying hi", None),
    ("Buy now", "add it to my cart", "buy"),
])
def test_unrequested_commit(label, goal, blocked):
    assert policy.unrequested_commit(label, goal) == blocked


def test_run_goal_needs_an_action():
    assert NEEDS_ACTION.search("OK now run the solution")
    assert not NEEDS_ACTION.search("open my messages on linkedin")


@pytest.mark.parametrize("goal,asked", [
    ("message sarah that I'm late", True), ("text mom saying on my way", True), ("reply to him", True),
    ("compose a new message", False), ("draft an email to bob", False), ("open the messages app", False),
])
def test_send_is_asked_for_only_as_a_verb(goal, asked):
    assert (policy.unrequested_commit("Send", goal) is None) is asked
