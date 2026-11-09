"""net.connect: HTTPS requests a plugin asks Pythonator to make for it.
A plugin's jail has no network. With net.connect granted for named hosts, it can ask the host to send one HTTPS
request to one of them. Pythonator checks every request:
* https only, port 443, no user:password in the URL, and the host must be one you granted (exact name; the grant
  can only list hosts the plugin declared in plugin.toml);
* the name is resolved here and every address must be public: no cloud metadata, this computer or your network,
  and the connection goes to the address that was checked (no second lookup to rebind);
* the certificate is verified for that name;
* request bodies pass the same secret masker as every log, so a service's secret can't leave in one;
* a header may say {secret:NAME} for a key the plugin declared, and the whole URL may be {secret:NAME} (a webhook
  address); Pythonator fills it in from the OS keychain on the way out, so the plugin never sees it, and checks the
  filled-in URL like any other;
* 2 MB out, 4 MB back, 120 seconds at most.
Each request is counted and kept in the plugin's call list (host, size, status), shown on the Plugins page."""

from __future__ import annotations

import http.client
import os
import re
import socket
import ssl
import time
import urllib.parse
from collections.abc import Callable
from typing import Any

from pythonator.net.gate import literal_why

MAX_OUT = 2 * 1024 * 1024
MAX_IN = 4 * 1024 * 1024
MAX_TIMEOUT_S = 120.0
SECRET_SLOT = re.compile(r"\{secret:([A-Za-z_][A-Za-z0-9_]{0,39})\}")
DROP_HEADERS = frozenset({"host", "connection", "proxy-authorization", "proxy-connection", "content-length",
                          "transfer-encoding", "upgrade", "te"})

class NetRefused(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

def check_url(url: str, granted: list[str] | tuple[str, ...]) -> tuple[str, str]:
    """(host, path) when the URL is allowed, else NetRefused."""
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        raise NetRefused("NET-PLG-001", "That isn't a URL.") from None
    if u.scheme != "https":
        raise NetRefused("NET-PLG-001", "Only https:// requests are allowed.")
    if u.username or u.password:
        raise NetRefused("NET-PLG-002", "A user name or password in the URL isn't allowed.")
    host = (u.hostname or "").lower().rstrip(".")
    if u.port not in (None, 443):
        raise NetRefused("NET-PLG-003", "Only port 443 is allowed.")
    if not host or literal_why(host, False) or _is_ip(host):
        raise NetRefused("NET-PLG-004", "Use a host name you granted, not an address.")
    if host not in granted:
        raise NetRefused("NET-PLG-005", f"{host} isn't one of the hosts you granted.")
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    return host, path

def _is_ip(host: str) -> bool:
    import ipaddress

    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False

def _proxy() -> tuple[str, int] | None:
    raw = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    if not raw:
        return None
    u = urllib.parse.urlsplit(raw if "://" in raw else f"http://{raw}")
    return (u.hostname or "", u.port or 3128) if u.hostname else None

def resolve(host: str) -> str:
    """One checked public address for host, or NetRefused."""
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as e:
        raise NetRefused("NET-PLG-006", f"Couldn't look up {host}: {e}.") from None
    addrs = [str(i[4][0]) for i in infos]
    for a in addrs:
        why = literal_why(a, False)
        if why:
            raise NetRefused("NET-PLG-007", f"{host} points at {why} ({a}), so the request was refused.")
    if not addrs:
        raise NetRefused("NET-PLG-006", f"{host} has no address.")
    return addrs[0]

class _Pinned(http.client.HTTPSConnection):
    """HTTPS to a checked address, with the certificate verified for the host name."""

    def __init__(self, host: str, addr: str, timeout: float, context: ssl.SSLContext) -> None:
        super().__init__(host, 443, timeout=timeout, context=context)
        self._addr = addr
        self._ctx = context

    def connect(self) -> None:
        sock = socket.create_connection((self._addr, 443), self.timeout)
        self.sock = self._ctx.wrap_socket(sock, server_hostname=self.host)

def _context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    extra = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if extra and os.path.isfile(extra):
        ctx.load_verify_locations(extra)
    return ctx

def fill_secrets(headers: dict[str, str], declared: tuple[str, ...], lookup: Callable[[str], str | None]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in headers.items():
        if not isinstance(k, str) or not isinstance(v, str) or k.lower() in DROP_HEADERS or k.lower().startswith("proxy-"):
            continue
        if "\r" in k + v or "\n" in k + v:
            raise NetRefused("NET-PLG-008", "Header names and values can't contain line breaks.")

        def put(m: re.Match[str]) -> str:
            name = m[1]
            if name not in declared:
                raise NetRefused("NET-PLG-009", f"{name} isn't a secret this plugin declared.")
            val = lookup(name)
            if not val:
                raise NetRefused("NET-PLG-010", f"Set the plugin's {name} first (Plugins → this plugin → Secrets).")
            return val

        out[k] = SECRET_SLOT.sub(put, v)
    return out

def request(
    url: str,
    *,
    method: str,
    headers: dict[str, str],
    body: str,
    timeout: float,
    granted: list[str] | tuple[str, ...],
    declared: tuple[str, ...],
    lookup: Callable[[str], str | None],
    mask: Callable[[str], str],
) -> dict[str, Any]:
    """Blocking; the plugin host runs it in a thread."""
    whole = SECRET_SLOT.fullmatch(url)
    if whole:  # a whole URL kept as a secret (a webhook address): filled in here, then checked like any other
        if whole[1] not in declared:
            raise NetRefused("NET-PLG-009", f"{whole[1]} isn't a secret this plugin declared.")
        url = lookup(whole[1]) or ""
        if not url:
            raise NetRefused("NET-PLG-010", f"Set the plugin's {whole[1]} first (Plugins → this plugin → Secrets).")
    host, path = check_url(url, granted)
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        raise NetRefused("NET-PLG-011", f"Method {method} isn't allowed.")
    data = mask(body).encode("utf-8") if body else b""
    if len(data) > MAX_OUT:
        raise NetRefused("NET-PLG-012", "Requests are limited to 2 MB.")
    hdrs = fill_secrets(headers, declared, lookup)
    timeout = max(1.0, min(MAX_TIMEOUT_S, timeout))
    ctx = _context()
    proxy = _proxy()
    t0 = time.monotonic()
    if proxy:  # the proxy connects for us; the name is still checked against your grant and the cert is verified
        try:
            resolve(host)  # refuse names that point inside, when we can see that from here
        except NetRefused as e:
            if e.code != "NET-PLG-006":
                raise
        conn = http.client.HTTPSConnection(proxy[0], proxy[1], timeout=timeout, context=ctx)
        conn.set_tunnel(host, 443)
    else:
        conn = _Pinned(host, resolve(host), timeout, ctx)
    try:
        conn.request(method, path, body=data or None, headers={**hdrs, "Content-Length": str(len(data))})
        resp = conn.getresponse()
        raw = resp.read(MAX_IN + 1)
    except (OSError, http.client.HTTPException, ssl.SSLError) as e:
        raise NetRefused("NET-PLG-013", f"{host}: {e}") from None
    finally:
        conn.close()
    if len(raw) > MAX_IN:
        raise NetRefused("NET-PLG-014", "The answer was over 4 MB.")
    return {
        "status": resp.status,
        "headers": {k.lower(): v for k, v in resp.getheaders() if k.lower() in ("content-type", "retry-after", "request-id")},
        "body": raw.decode("utf-8", "replace"), "host": host, "sent": len(data),
        "ms": round((time.monotonic() - t0) * 1000),
    }
