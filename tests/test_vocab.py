"""Personalized speech vocabulary: screen names first, then the user's own words, filtered for privacy."""
import pytest

from jevme import ax, vocab


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(vocab, "PATH", tmp_path / "vocab.json")
    monkeypatch.setattr(vocab, "_personal", {})
    monkeypatch.setattr(vocab, "_screen", [])
    monkeypatch.setattr(vocab, "_loaded", True)          # don't seed from the real memory files
    monkeypatch.setattr(vocab, "_base", ["youtube", "Google Chrome"])
    monkeypatch.setattr(vocab, "SCREEN_ENABLED", True)


def el(role, label):
    return ax.Elem(0, role, label, 0, 0, 10, 10, None)


def test_screen_names_are_cleaned_and_come_first():
    snap = ax.Snapshot("Google Chrome", 1, None, [
        el("AXLink", "1. Two Sum 57.9% Easy"), el("AXLink", "2. Add Two Numbers 45.1% Medium"),
        el("AXButton", "Submit"), el("AXLink", "Premium")], 0, 0)
    assert vocab.set_screen(vocab.screen_labels_from(snap))
    p = vocab.phrases()
    assert p[:2] == ["Two Sum", "Add Two Numbers"]
    assert p.index("Two Sum") < p.index("youtube")


def test_private_or_dynamic_text_never_enters():
    snap = ax.Snapshot("Messages", 1, None, [
        el("AXTextField", "Message"),                                   # a field: its contents are private
        el("AXButton", "‎New photo available from Alex"),          # message content
        el("AXButton", "1 Reply"),                                      # live count
        el("AXStaticText", "hey are you coming tonight"),               # not a control
        el("AXButton", "Compose"), el("AXButton", "Info"), el("AXButton", "Emoji picker")], 0, 0)
    labels = vocab.screen_labels_from(snap)
    assert labels == ["Compose", "Info", "Emoji picker"]


def test_small_screen_changes_dont_churn_the_recognizer():
    assert vocab.set_screen(["Alpha", "Beta", "Gamma", "Delta"])
    assert not vocab.set_screen(["Alpha", "Beta", "Gamma", "Delta", "Epsilon"])


def test_names_from_successful_commands_are_remembered(tmp_path):
    vocab.learn("leetcode", "the general channel", "a very long phrase that is not a name at all really")
    assert "leetcode" in vocab.phrases() and "the general channel" in vocab.phrases()
    assert not any("very long phrase" in p for p in vocab.phrases())
    assert (tmp_path / "vocab.json").exists()


def test_screen_layer_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(vocab, "SCREEN_ENABLED", False)
    assert not vocab.set_screen(["Alpha", "Beta", "Gamma", "Delta"])
