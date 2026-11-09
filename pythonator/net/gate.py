"""The egress gate: the only way out for a service whose traffic is "Allowed hosts", and the way in for its ports.
Out: the jail has no network of its own. Inside it, the launcher's relay listens on 127.0.0.1:3128 (the service's
HTTP(S)_PROXY) and passes each connection to this gate over a Unix socket (Linux) or a per-service port on
127.0.0.1 that only that Seatbelt profile may reach (macOS). The gate reads `CONNECT host:port` (or an
absolute-form HTTP request), checks the host against the service's list, resolves it once, refuses loopback,
link-local (cloud metadata), multicast and, unless the service may use the local network, private addresses, then
connects to the address it checked (so DNS can't swap it afterwards) and pipes the bytes. Every attempt is an event
the window shows as live traffic, with Allow for a blocked host.
In: for each open port, the gate listens on 127.0.0.1 (or every address, for the local network) and passes each
connection to the relay's Unix socket for that port inside the jail."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

MAX_HEADER = 16 * 1024
CONNECT_TIMEOUT_S = 10.0
Emit = Callable[[str, dict[str, Any]], None]

def host_allowed(host: str, port: int, allowed: tuple[str, ...]) -> bool:
    """`api.example.com`, `*.example.com` (any subdomain, not the bare domain) or `host:port`."""
    host = host.lower().rstrip(".")
    for rule in allowed:
        name, _, p = rule.partition(":")
        if p and int(p) != port:
            continue
        if name.startswith("*.") and host.endswith(name[1:]) and host != name[2:]:
            return True
        if host == name:
            return True
    return False

def address_allowed(ip: str, allow_lan: bool) -> bool:
    a = ipaddress.ip_address(ip)
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    if a.is_loopback or a.is_link_local or a.is_multicast or a.is_unspecified or a.is_reserved:
        return False  # 127.0.0.1, 169.254.169.254 (cloud metadata), fe80::, 0.0.0.0
    if str(a) in ("100.100.100.200", "fd00:ec2::254"):  # Alibaba and AWS IPv6 metadata
        return False
    return allow_lan or a.is_global

def literal_why(host: str, allow_lan: bool) -> str:
    """For an IP written straight into the request: why it is refused (cloud metadata, this computer, your network)."""
    try:
        a = ipaddress.ip_address(host)
    except ValueError:
        return ""
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    if str(a) in ("169.254.169.254", "100.100.100.200", "fd00:ec2::254", "169.254.170.2"):
        return "cloud metadata"
    if a.is_loopback or a.is_unspecified:
        return "this computer"
    if a.is_link_local or a.is_multicast or a.is_reserved:
        return "link-local or reserved address"
    if not a.is_global and not allow_lan:
        return "your local network"
    return ""

@dataclass
class Rule:
    hosts: tuple[str, ...]
    allow_lan: bool

@dataclass
class ServiceGate:
    name: str
    rule: Rule
    servers: list[asyncio.base_events.Server] = field(default_factory=list)
    recent: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=200))

class Gate:
    def __init__(self, emit: Emit | None = None) -> None:
        self.emit: Emit = emit or (lambda t, d: None)
        self.services: dict[str, ServiceGate] = {}

    async def open(
        self, name: str, rule: Rule, *, unix: str = "", tcp: int = 0, ports: tuple[tuple[int, bool, str], ...] = ()
    ) -> None:
        """Start the gate for one run. `ports`: (port, from the local network, the relay's Unix socket in the jail)."""
        await self.close(name)
        sg = self.services[name] = ServiceGate(name, rule)

        async def out(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await self._outbound(sg, r, w)

        if unix:
            with contextlib.suppress(FileNotFoundError):
                import os

                os.unlink(unix)
            sg.servers.append(await asyncio.start_unix_server(out, unix))
        if tcp:
            sg.servers.append(await asyncio.start_server(out, "127.0.0.1", tcp))
        for port, lan, inner in ports:

            async def inbound(r: asyncio.StreamReader, w: asyncio.StreamWriter, inner: str = inner) -> None:
                for attempt in range(20):  # the jail's relay socket appears a moment after the start
                    try:
                        ir, iw = await asyncio.wait_for(asyncio.open_unix_connection(inner), CONNECT_TIMEOUT_S)
                        break
                    except (FileNotFoundError, ConnectionRefusedError):
                        await asyncio.sleep(0.1 * (attempt + 1) if attempt < 5 else 0.5)
                    except (OSError, TimeoutError):
                        break
                else:
                    ir = None
                if ir is None:
                    w.close()
                    return
                await _pipe(r, w, ir, iw)

            sg.servers.append(await asyncio.start_server(inbound, "0.0.0.0" if lan else "127.0.0.1", port))  # noqa: S104 - LAN only when the service says so

    def external(self, name: str, rule: Rule) -> None:
        """A gate that runs elsewhere (inside WSL): kept here only for its recent attempts and the live traffic events."""
        self.services[name] = ServiceGate(name, rule)

    def record(self, name: str, host: str, port: int, verdict: str, why: str = "") -> None:
        sg = self.services.get(name)
        if sg is not None:
            self._note(sg, host[:253], port, verdict if verdict in ("allowed", "blocked", "failed") else "failed", why[:120])

    def update(self, name: str, rule: Rule) -> None:
        if name in self.services:
            self.services[name].rule = rule

    async def close(self, name: str) -> None:
        sg = self.services.pop(name, None)
        if sg:
            for s in sg.servers:
                s.close()

    def recent(self, name: str) -> list[dict[str, Any]]:
        sg = self.services.get(name)
        return list(sg.recent) if sg else []

    def _note(self, sg: ServiceGate, host: str, port: int, verdict: str, why: str = "") -> None:
        ev = {"name": sg.name, "host": host, "port": port, "verdict": verdict, "why": why, "ts": time.time()}
        sg.recent.append(ev)
        self.emit("traffic", ev)

    async def _outbound(self, sg: ServiceGate, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), CONNECT_TIMEOUT_S)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, OSError):
            w.close()
            return
        if len(head) > MAX_HEADER:
            w.close()
            return
        line, _, rest = head.decode("latin-1").partition("\r\n")
        parts = line.split(" ")
        if len(parts) != 3:
            await _reply(w, 400, "Bad request")
            return
        method, target, version = parts
        connect = method.upper() == "CONNECT"
        if connect:
            host, _, p = target.rpartition(":")
            port = int(p) if p.isdigit() and len(p) <= 5 else 443
        else:
            from urllib.parse import urlsplit

            u = urlsplit(target)
            if u.scheme != "http" or not u.hostname:
                await _reply(w, 400, "Only http:// requests and CONNECT go through the gate")
                return
            try:
                host, port = u.hostname, u.port or 80
            except ValueError:  # a port outside 0-65535
                await _reply(w, 400, "Bad port")
                return
        if not 0 < port < 65536:
            await _reply(w, 400, "Bad port")
            return
        host = host.strip("[]")
        why = literal_why(host, sg.rule.allow_lan)
        if why:
            self._note(sg, host, port, "blocked", why)
            await _reply(w, 403, f"Pythonator blocked {host}: {why}")
            return
        if not host_allowed(host, port, sg.rule.hosts):
            self._note(sg, host, port, "blocked", "not in the allowed hosts")
            await _reply(w, 403, f"Pythonator blocked {host}: it isn't in this service's allowed hosts")
            return
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError, ValueError):
            self._note(sg, host, port, "failed", "no such host")
            await _reply(w, 502, f"Can't resolve {host}")
            return
        ip = next((str(i[4][0]) for i in infos if address_allowed(str(i[4][0]), sg.rule.allow_lan)), "")
        if not ip:
            self._note(sg, host, port, "blocked", "resolves to this computer, your network or cloud metadata")
            await _reply(w, 403, f"Pythonator blocked {host}: it resolves to a private address")
            return
        try:
            ur, uw = await asyncio.wait_for(asyncio.open_connection(ip, port), CONNECT_TIMEOUT_S)
        except (OSError, TimeoutError, OverflowError, ValueError):
            self._note(sg, host, port, "failed", "connection failed")
            await _reply(w, 502, f"Can't connect to {host}")
            return
        self._note(sg, host, port, "allowed")
        if connect:
            w.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        else:
            from urllib.parse import urlsplit

            u = urlsplit(target)
            path = (u.path or "/") + (f"?{u.query}" if u.query else "")
            headers = [h for h in rest.split("\r\n") if h and not h.lower().startswith(("proxy-", "connection:"))]
            uw.write(f"{method} {path} {version}\r\n".encode("latin-1"))
            uw.write(("\r\n".join([*headers, "Connection: close"]) + "\r\n\r\n").encode("latin-1"))
        await _pipe(r, w, ur, uw)

async def _reply(w: asyncio.StreamWriter, code: int, text: str) -> None:
    body = (text + "\n").encode()
    w.write(f"HTTP/1.1 {code} {text[:60]}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
    with contextlib.suppress(OSError):
        await w.drain()
    w.close()

async def _pipe(r1: asyncio.StreamReader, w1: asyncio.StreamWriter, r2: asyncio.StreamReader, w2: asyncio.StreamWriter) -> None:
    async def copy(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            while data := await r.read(65536):
                w.write(data)
                await w.drain()
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            with contextlib.suppress(OSError):
                w.close()

    await asyncio.gather(copy(r1, w2), copy(r2, w1))
