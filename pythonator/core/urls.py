"""Spotting the address a web server prints when it starts ("Running on http://127.0.0.1:5000")."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from .ansi import strip_ansi

URL_RE = re.compile(r"https?://[^\s\"'<>`]+", re.IGNORECASE)
_TRAILING = ".,;:!?)]}'\""
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_ANY_ADDRESS = {"0.0.0.0", "::"}


def clean_url(url: str) -> str:
    """Drop punctuation that ends a sentence rather than the URL ("see http://x/.")."""
    while url and url[-1] in _TRAILING:
        if url[-1] == ")" and url.count("(") >= url.count(")"):
            break
        url = url[:-1]
    return url


def local_url(text: str) -> str | None:
    """The first URL in ``text`` that points at this machine, made openable in a browser.

    Servers bound to every interface print ``0.0.0.0`` or ``[::]``; those become ``localhost``.
    """
    if "://" not in text:
        return None
    if "\x1b" in text:
        text = strip_ansi(text)
    for match in URL_RE.finditer(text):
        url = clean_url(match.group(0))
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
            _ = parts.port  # raises ValueError for a malformed port
        except ValueError:
            continue
        if host in _ANY_ADDRESS:
            netloc = parts.netloc.replace(f"[{host}]" if ":" in host else host, "localhost", 1)
            return parts._replace(netloc=netloc).geturl()
        if host in _LOOPBACK or host.endswith(".localhost"):
            return url
    return None
