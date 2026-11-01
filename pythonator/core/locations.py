"""Finding "file:line" references in error output, for every supported language."""

from __future__ import annotations

import re

_EXT = r"(?:py|pyw|[cm]?[jt]sx?|go|rb|php|java|kts?|cs|rs|sh|bash|ps1|psm1|lua|pl|ex|exs)"
_PATH = r"(?:[A-Za-z]:)?[^\s:\"'<>()]+"
_PATTERNS = tuple(
    re.compile(p)
    for p in (
        r'File "(?P<p>[^"]+)", line (?P<l>\d+)',  # Python traceback
        rf"\bin (?P<p>{_PATH}\.cs):line (?P<l>\d+)",  # .NET stack trace
        rf"(?P<p>{_PATH}\.php)(?: on line |\()(?P<l>\d+)",  # PHP errors and traces
        rf"(?P<p>{_PATH}\.(?:sh|bash)): line (?P<l>\d+)",  # bash errors
        rf"(?:file://)?(?P<p>{_PATH}\.{_EXT}):(?P<l>\d+)",  # Node, Go, Ruby, Java, Rust, PowerShell, linters ...
    )
)


def find_location(text: str) -> tuple[str, int] | None:
    """The first ``(path, line)`` mentioned in ``text``, if any."""
    best: re.Match[str] | None = None
    for pattern in _PATTERNS:
        m = pattern.search(text)
        if m and (best is None or m.start() < best.start()):
            best = m
    if best is None:
        return None
    return best.group("p"), int(best.group("l"))
