from __future__ import annotations

from pythonator.core.ansi import BASE16, PLAIN, AnsiParser, Style, strip_ansi, xterm_color


def test_plain_text_is_single_segment() -> None:
    assert AnsiParser().feed("hello") == [("hello", PLAIN)]


def test_basic_sgr_and_reset() -> None:
    p = AnsiParser()
    out = p.feed("\x1b[1;31mred\x1b[0m plain")
    assert out == [("red", Style(fg=BASE16[1], bold=True)), (" plain", PLAIN)]


def test_256_and_truecolor() -> None:
    p = AnsiParser()
    (_, s1), (_, s2) = p.feed("\x1b[38;5;196ma\x1b[48;2;1;2;3mb")
    assert s1.fg == xterm_color(196)
    assert s2.bg == (1, 2, 3) and s2.fg == xterm_color(196)


def test_style_persists_across_feeds() -> None:
    p = AnsiParser()
    p.feed("\x1b[32m")
    assert p.feed("green") == [("green", Style(fg=BASE16[2]))]


def test_escape_split_across_chunks() -> None:
    p = AnsiParser()
    assert p.feed("abc\x1b[3") == [("abc", PLAIN)]
    assert p.feed("4mblue") == [("blue", Style(fg=BASE16[4]))]


def test_non_sgr_sequences_are_dropped() -> None:
    p = AnsiParser()
    text = "".join(seg for seg, _ in p.feed("\x1b[2K\x1b[1Gline\x1b]0;title\x07 end\x1b(B"))
    assert text == "line end"


def test_strip_ansi() -> None:
    assert strip_ansi("\x1b[1m\x1b[31merror\x1b[0m: \x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\") == "error: link"
    assert strip_ansi("no escapes") == "no escapes"


def test_bright_and_inverse() -> None:
    (_, s), = AnsiParser().feed("\x1b[93;7mx")
    assert s.fg == BASE16[11] and s.inverse
