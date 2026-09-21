"""Goal understanding in the agent: app inference, message bodies, send decisions (no network, no UI)."""
from __future__ import annotations

import pytest

from jevme import actions as A
from jevme import agent as G


@pytest.fixture(autouse=True)
def _apps(monkeypatch):
    apps = ["Messages", "Mail", "Discord", "Slack", "Notes", "Reminders", "Calendar", "Spotify", "Google Chrome"]
    monkeypatch.setattr(A, "installed_apps", lambda: apps)
    monkeypatch.setattr(A, "running_apps", lambda: ["Google Chrome"])


@pytest.mark.parametrize("goal, app", [
    ("Send a message to Sarah saying hi", "Messages"),       # logged: flailed in Chrome with open_url
    ("go to my DMs in discord", "Discord"),                  # logged: stuck from the wrong app
    ("send an email to my prof asking for an extension", "Mail"),
    ("text mom that I'm on my way", "Messages"),
    ("add buy milk to my reminders", "Reminders"),
    ("post in slack that the build is green", "Slack"),
    ("go to amazon.com and add a rice cooker to my cart", None),   # web task: stay in the browser
    ("search youtube for lofi", None),
    ("open the second tab in chrome", None),
])
def test_infer_app(goal, app):
    assert G.infer_app(goal) == app


def test_infer_app_skips_uninstalled(monkeypatch):
    monkeypatch.setattr(A, "installed_apps", lambda: ["Google Chrome"])
    assert G.infer_app("go to my DMs in discord") is None


@pytest.mark.parametrize("goal, body", [
    ("Send a message to Sarah saying hi", "hi"),
    ("send a message saying OK ha", "OK ha"),
    ("text mom that I'm on my way", "I'm on my way"),
    ("tell alex that the demo moved", "the demo moved"),
    ("Send the message that drafted under the conversation with Sarah", None),   # not a body
    ("send it", None),
])
def test_message_body(goal, body):
    assert G.message_body(goal) == body


def test_same_text_ignores_case_and_punctuation():
    assert G._same_text("OK ha", "ok ha.")
    assert not G._same_text("hi", "hello")
    assert not G._same_text("", "")


def test_draft_goals_are_not_auto_sent():
    assert G.DRAFT_ONLY.search("draft a reply to alex saying sounds good")
    assert not G.DRAFT_ONLY.search("send a message to sarah saying hi")


def test_replay_types_the_new_message_not_the_old_one():
    from jevme.agent import Agent
    from jevme.ui_memory import RecipeStep
    step = RecipeStep("type", "AXTextArea", "Message", "running late", text_from_goal=True)
    old = "text sam saying running late"
    assert Agent._replay_text(step, old, old) == "running late"
    assert Agent._replay_text(step, "text sam saying see you at 5", old) == "see you at 5"
    assert Agent._replay_text(step, "text sam", old) is None          # unknown text: reason instead
    fixed = RecipeStep("type", "AXTextField", "Search", "general", text_from_goal=False)
    assert Agent._replay_text(fixed, "go to general in discord", "open the general channel") == "general"
