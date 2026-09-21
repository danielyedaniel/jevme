import pytest

from jevme import actions as A


@pytest.mark.parametrize("spoken,url", [
    ("google calendar", "https://calendar.google.com"),
    ("hacker news", "https://news.ycombinator.com"),
    ("stack overflow", "https://stackoverflow.com"),
    ("leetcode", "https://leetcode.com"),
    ("leet code", "https://leetcode.com"),
    ("leetcod.com", "https://leetcode.com"),      # misheard: snaps to the known site
    ("etcod.com", "https://leetcode.com"),        # clipped start of the word
    ("github dot com", "https://github.com"),
    ("docs.python.org", "https://docs.python.org"),  # real multi-part domains are taken literally
    ("example.com", "https://example.com"),
])
def test_resolve_site(spoken, url):
    assert A.resolve_site(spoken) == url


def test_unknown_phrase_is_searched():
    assert A.resolve_site("cooking recipes") is None
