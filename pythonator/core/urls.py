"""Finding the address of a web server or API a service started, whatever language it is written in.

Three sources, from most to least exact:

* :func:`local_url` - a full link the program printed ("Running on http://127.0.0.1:5000").
* :func:`address_url` - an address without ``http://`` next to a "listening"/"running" word, as
  Go, Node, Java, Elixir, Ruby and many API frameworks print it ("Listening on :8080",
  "Tomcat started on port 8080 (http)", "listening on 127.0.0.1:3000", "tcp://0.0.0.0:3000").
* :func:`listening_url` - the TCP ports the process tree is actually listening on, for servers
  that print nothing useful at all.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
from collections.abc import Iterable
from urllib.parse import urlsplit

from .ansi import strip_ansi

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]

URL_RE = re.compile(r"https?://[^\s\"'<>`]+", re.IGNORECASE)
_TRAILING = ".,;:!?)]}'\""
_ANY_ADDRESS = {"0.0.0.0", "::"}
_HTTPS_PORTS = {443, 8443}
EPHEMERAL_PORT = 32768  # ports from here up are mostly random ones (IPC, debug adapters, socketpair)

# A word that says the program is serving, not connecting somewhere.
_SERVE_RE = re.compile(
    r"\b(?:listen\w*|serv(?:e|es|ed|er|ers|ing)|running|started|starting|bound|binding|available|ready|"
    r"accepting|launched|hosted|hosting|live|online|responding|exposed|local|url)\b",
    re.IGNORECASE,
)
# ...and words that say it is a client of something else ("connected to redis at localhost:6379").
_CLIENT_RE = re.compile(r"connect|proxy|proxies|proxying|upstream|forward|redirect|database|\bdb\b", re.IGNORECASE)
# host:port without a scheme, or just :port ("Listening and serving HTTP on :8080").
_HOSTPORT_RE = re.compile(
    r"(?:(?<=[\s(\[='\"@,])|^)"
    r"(?:(?:tcp|http|https)://)?"
    r"(?P<host>localhost|\d{1,3}(?:\.\d{1,3}){3}|\[[0-9a-fA-F:.%\w]+\]|[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)*)?"
    r":(?P<port>\d{2,5})(?![\d:])"
)
# "port 3000", "port(s): 8080", "port=4567", "PORT: 5000".
_PORT_RE = re.compile(r"\bports?(?:\(s\))?\s*(?:[:=#]|is|of|number)?\s*(?P<port>\d{2,5})\b", re.IGNORECASE)


def clean_url(url: str) -> str:
    """Drop punctuation that ends a sentence rather than the URL ("see http://x/.")."""
    while url and url[-1] in _TRAILING:
        if url[-1] == ")" and url.count("(") >= url.count(")"):
            break
        url = url[:-1]
    return url


# -- what counts as "this machine" -----------------------------------------------------------------

_own_lock = threading.Lock()
_own_cache: tuple[float, frozenset[str]] = (-1e9, frozenset())
_OWN_TTL = 30.0  # seconds; network interfaces change rarely (VPN up, Wi-Fi switch)


def _own_names() -> frozenset[str]:
    """This computer's host name and the IP addresses of its network interfaces (cached)."""
    global _own_cache
    now = time.monotonic()
    stamp, names = _own_cache
    if now - stamp < _OWN_TTL:
        return names
    with _own_lock:
        found: set[str] = set()
        try:
            host = socket.gethostname().lower()  # no DNS lookup, always fast
        except OSError:
            host = ""
        if host:
            short = host.split(".")[0]
            found.update({host, short, f"{short}.local"})
        if psutil is not None:
            try:
                for nic in psutil.net_if_addrs().values():
                    for addr in nic:
                        if addr.family in (socket.AF_INET, socket.AF_INET6):
                            found.add(addr.address.split("%")[0].lower())
            except (OSError, psutil.Error):  # pragma: no cover - sandboxed or unusual systems
                pass
        names = frozenset(found)
        _own_cache = (now, names)
    return names


def browser_host(host: str) -> str | None:
    """How to reach ``host`` from a browser on this machine, or None if it's another machine.

    ``0.0.0.0``/``::`` (bound to every interface) and an empty host (``:8080``) become ``localhost``.
    """
    host = host.strip("[]").lower()
    if not host or host in _ANY_ADDRESS:
        return "localhost"
    if host == "localhost" or host.endswith(".localhost"):
        return host
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return host if host in _own_names() else None
    if ip.is_unspecified:
        return "localhost"
    if ip.is_loopback or host.split("%")[0] in _own_names():
        return f"[{host}]" if ip.version == 6 else host
    return None


def _http_url(host: str, port: int) -> str:
    scheme = "https" if port in _HTTPS_PORTS else "http"
    return f"{scheme}://{host}:{port}/"


# -- the three sources -----------------------------------------------------------------------------


def local_url(text: str) -> str | None:
    """The first ``http(s)://`` link in ``text`` that points at this machine, made openable in a browser.

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
        if not host:
            continue
        reach = browser_host(host)
        if reach is None:
            continue
        if host in _ANY_ADDRESS:
            netloc = parts.netloc.replace(f"[{host}]" if ":" in host else host, "localhost", 1)
            return parts._replace(netloc=netloc).geturl()
        return url
    return None


def address_url(text: str) -> str | None:
    """A server address printed without ``http://``, as an openable URL.

    Only on a line that says the program is listening/running/serving and doesn't talk about
    connecting to something else, so a bot's "connected to localhost:6379" is never picked up.
    """
    if "\x1b" in text:
        text = strip_ansi(text)
    serve = _SERVE_RE.search(text)
    if serve is None or _CLIENT_RE.search(text):
        return None
    for m in _HOSTPORT_RE.finditer(text):
        port = int(m.group("port"))
        host = m.group("host") or ""
        if not 0 < port < 65536:
            continue
        reach = browser_host(host)
        if reach is not None:
            return _http_url(reach, port)
    if found := _PORT_RE.search(text):
        port = int(found.group("port"))
        if 0 < port < 65536:
            return _http_url("localhost", port)
    return None


def listening_url(addresses: Iterable[tuple[str, int]]) -> str | None:
    """A URL for the most likely web port among ``(ip, port)`` TCP sockets in LISTEN state.

    Random high ports (debug adapters, IPC) are ignored; of the rest the lowest port wins, which
    picks the app over side ports such as a debugger (9229) or metrics endpoint.
    """
    best: tuple[int, int, str] | None = None  # (port, preference, host)
    for ip, port in addresses:
        if not 0 < port < EPHEMERAL_PORT or (host := browser_host(ip)) is None:
            continue
        # Every interface beats IPv4 loopback beats IPv6 loopback beats a LAN address.
        pref = 0 if host == "localhost" else 1 if host.startswith("127.") else 2 if host == "[::1]" else 3
        rank = (port, pref, host)
        if best is None or rank < best:
            best = rank
    return _http_url(best[2], best[0]) if best else None


def url_port(url: str) -> int | None:
    """The port a URL points at, including the scheme's default."""
    try:
        parts = urlsplit(url)
        return parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return None
