"""Plugin manifests, the permission catalogue, and content hashes.
A plugin is a folder with `plugin.toml` and Python files. Permissions are granted one by one and stored against a
hash of every file in the folder: new code starts with no permissions, whoever published it."""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from pythonator.safe import safefs

ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
SECRET_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,39}$")
MAX_FILES = 400
MAX_BYTES = 8 * 1024 * 1024

@dataclass(frozen=True)
class Permission:
    key: str
    title: str
    detail: str
    scoped: bool = False  # granted per service (or "all")
    available: bool = True
    why_not: str = ""
    scope_kind: str = "services"  # what a scoped grant lists: service names, or host names (net.connect)

CATALOGUE: dict[str, Permission] = {
    p.key: p
    for p in (
        Permission("services.read", "Read services", "List services, their state, mode and ports.", scoped=False),
        Permission("logs.read", "Read logs", "Read masked log lines of the services you pick.", scoped=True),
        Permission("ui.notify", "Notifications", "Show a notification in the window."),
        Permission("ui.views", "Inspector section", "Add a section to a service's inspector, drawn by Pythonator from data."),
        Permission("ui.statusbar", "Status item", "Show one short text in the window's status bar."),
        Permission("net.service", "Probe a service", "Probe a service's own port through Pythonator (TCP or HTTP GET).",
                   scoped=True),
        Permission("projects.create", "Starter projects",
                   "Offer starter projects in New service. Pythonator writes the files into an empty folder you pick."),
        Permission("policy.check", "Review changes", "See each change before it is saved, and refuse it with a reason."),
        Permission(
            "net.connect", "Connect to a host",
            "Send HTTPS requests to the hosts you pick, through Pythonator. The plugin still has no network of its own.",
            scoped=True, scope_kind="hosts",
        ),
        Permission(
            "editor.assist", "Answer in the editor",
            "When you ask, gets the open file (read-only), its problems and masked logs. Can only propose edits.",
        ),
    )
}

@dataclass(frozen=True)
class Manifest:
    id: str
    name: str
    version: str
    entry: str
    description: str = ""
    publisher: str = ""
    permissions: tuple[str, ...] = ()
    activation: tuple[str, ...] = ("startup",)
    hosts: tuple[str, ...] = field(default_factory=tuple)  # the only hosts net.connect may ever be granted
    secrets: tuple[str, ...] = field(default_factory=tuple)  # names of keys kept in the OS keychain, never shown to it

class BadPlugin(Exception):
    pass

def load_manifest(folder: Path) -> Manifest:
    try:
        raw = safefs.read_text(str(folder), "plugin.toml", limit=64 * 1024)
        data = tomllib.loads(raw)
    except (OSError, safefs.UnsafePath, tomllib.TOMLDecodeError) as e:
        raise BadPlugin(f"plugin.toml: {e}") from None
    p = data.get("plugin", data)
    pid = str(p.get("id", ""))
    if not ID_RE.fullmatch(pid):
        raise BadPlugin("id: lowercase letters, digits and '-', 2 to 40 characters")
    entry = str(p.get("entry", "main.py"))
    if "/" in entry or "\\" in entry or not entry.endswith(".py"):
        raise BadPlugin("entry must be a .py file at the top of the plugin folder")
    perms = tuple(str(x) for x in p.get("permissions", []))
    unknown = [x for x in perms if x not in CATALOGUE]
    if unknown:
        raise BadPlugin(f"unknown permission {unknown[0]!r}")
    hosts = tuple(str(x).lower() for x in p.get("hosts", []))
    bad = [h for h in hosts if not HOST_RE.fullmatch(h) or h.replace(".", "").isdigit() or "." not in h]
    if bad:
        raise BadPlugin(f"hosts: {bad[0]!r} isn't a host name (no addresses, ports or wildcards)")
    if "net.connect" in perms and not hosts:
        raise BadPlugin("net.connect needs a hosts list in plugin.toml")
    secrets = tuple(str(x) for x in p.get("secrets", []))
    if any(not SECRET_RE.fullmatch(x) for x in secrets):
        raise BadPlugin("secrets: names are letters, digits and '_', up to 40 characters")
    return Manifest(
        id=pid, name=str(p.get("name", pid))[:60], version=str(p.get("version", "0.0.0"))[:20], entry=entry,
        description=str(p.get("description", ""))[:300], publisher=str(p.get("publisher", ""))[:60], permissions=perms,
        activation=tuple(str(x) for x in p.get("activation", ["startup"])), hosts=hosts, secrets=secrets,
    )

def content_hash(folder: Path) -> str:
    """SHA-256 over every file's relative path and bytes, read through the safe reader (links are refused)."""
    h = hashlib.sha256()
    files: list[str] = []

    def walk(rel: str) -> None:
        for e in safefs.list_dir(str(folder), rel):
            path = f"{rel}/{e.name}" if rel else e.name
            if e.kind == "dir":
                if e.name in ("__pycache__",):
                    continue
                walk(path)
            elif e.kind == "file":
                files.append(path)
            else:
                raise BadPlugin(f"{path}: links and special files aren't allowed in a plugin")
            if len(files) > MAX_FILES:
                raise BadPlugin("too many files")

    walk("")
    total = 0
    for rel in sorted(files):
        data = safefs.read_bytes(str(folder), rel, limit=MAX_BYTES)
        total += len(data)
        if total > MAX_BYTES:
            raise BadPlugin("plugin is larger than 8 MB")
        h.update(rel.encode() + b"\0" + hashlib.sha256(data).digest())
    return h.hexdigest()
