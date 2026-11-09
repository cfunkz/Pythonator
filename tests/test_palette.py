"""Command palette matching."""

from __future__ import annotations

from pythonator.ui.palette import bold, score


def test_score_order() -> None:
    prefix, word, letters = (score("re", t) for t in ("Restart api", "Open report", "Turn off scheduler"))
    assert prefix and word and letters
    assert prefix[0] > word[0]
    assert score("rsa", "Restart api") is not None
    assert score("xyz", "Restart api") is None
    assert score("", "anything") == (0, [])


def test_bold_marks_hits() -> None:
    assert bold("a<b", [0, 1]) == "<b>a</b><b>&lt;</b>b"
