"""Screen 8: what each container engine is, and which of its containers are Pythonator's.
Everything here is read-only except Restart and Prune, and each card shows the exact command it runs. A container
is Pythonator's only when its ownership ledger lists it; anything else on the engine is shown as "not Pythonator"
and never touched (no stop, no remove, no prune), even if it carries a pythonator label someone copied.
Restart restarts Pythonator's own services on that engine (through the reconciler, so the jail and ledger stay
right); it never restarts the engine's daemon, which for Docker would need root. Prune removes only ledger rows:
stopped containers Pythonator left, and images Pythonator itself pulled (recorded when it pulled them, never an
image that was already there)."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from pythonator.ledger.ledger import Ledger
from pythonator.safe.hostenv import host_env
from pythonator.safe.process import NO_WINDOW
from pythonator.service import check

TIMEOUT_S = 15.0
INFO_CMD = {"podman": ("info", "--format", "json"), "docker": ("version", "--format", "{{json .}}")}
SHOWN = {"podman": "podman info --format json", "docker": "docker version --format '{{json .}}'"}

async def _run(exe: str, *args: str, timeout: float = TIMEOUT_S) -> tuple[int, str]:
    code, out, _err = await _run3(exe, *args, timeout=timeout)
    return code, out

async def _run3(exe: str, *args: str, timeout: float = TIMEOUT_S) -> tuple[int, str, str]:
    try:
        p = await asyncio.create_subprocess_exec(
            exe, *args, env=host_env(), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, **NO_WINDOW,
        )
        out, err = await asyncio.wait_for(p.communicate(), timeout)
    except (OSError, TimeoutError):
        return 127, "", f"{exe} didn't answer."
    return p.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

def _json_rows(text: str) -> list[dict[str, Any]]:
    """`ps --format json`: Podman prints one array, Docker one object per line."""
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        return [d for d in (data if isinstance(data, list) else [data]) if isinstance(d, dict)]
    except ValueError:
        out = []
        for line in text.splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict):
                out.append(d)
        return out

def _size(text: str | int | float) -> int:
    if isinstance(text, (int, float)):
        return int(text)
    units = {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12, "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3}
    t = str(text).strip().upper().replace(" ", "")
    for u in sorted(units, key=len, reverse=True):
        if t.endswith(u):
            try:
                return int(float(t[: -len(u)]) * units[u])
            except ValueError:
                return 0
    return 0

def _name(row: dict[str, Any]) -> str:
    n = row.get("Names") or row.get("Name") or ""
    if isinstance(n, list):
        n = n[0] if n else ""
    return str(n).lstrip("/")

def _labels(row: dict[str, Any]) -> dict[str, str]:
    lab = row.get("Labels") or {}
    if isinstance(lab, dict):
        return {str(k): str(v) for k, v in lab.items()}
    out = {}
    for part in str(lab).split(","):  # Docker: "a=b,c=d"
        k, _, v = part.partition("=")
        if k:
            out[k.strip()] = v.strip()
    return out

_UNITS = (("second", "s"), ("minute", "m"), ("hour", "h"), ("day", "d"), ("week", "w"), ("month", "mo"), ("year", "y"))

def short_age(text: str) -> str:
    """'6 days' -> '6d', 'About an hour' -> '1h', 'Less than a second' -> '0s'."""
    t = text.lower()
    if "less than" in t:
        return "0s"
    parts = ["1" if w in ("a", "an") else w for w in t.split() if w != "about"]
    for i, w in enumerate(parts):
        for word, u in _UNITS:
            if w.startswith(word):
                n = parts[i - 1] if i and parts[i - 1].isdigit() else "1"
                return f"{n}{u}"
    return text

def _state(row: dict[str, Any]) -> tuple[str, str]:
    """(short state, tone), like 'up 6d' or 'exited 3d'."""
    import re

    st = str(row.get("State") or "").lower()
    status = str(row.get("Status") or "")
    if "restart" in st or status.lower().startswith("restarting"):
        return "restarting", "warn"
    if st == "running" or status.lower().startswith("up"):
        m = re.match(r"up\s+(.*?)(\s+\(.*\))?$", status, re.I)
        return (f"up {short_age(m[1])}" if m else "running"), "ok"
    if st in ("exited", "stopped", "created", "dead"):
        m = re.search(r"\)\s+(.*?)\s+ago", status)
        return (f"{st} {short_age(m[1])}" if m else st), "muted"
    return st or "unknown", "muted"

async def engine_info(name: str, exe: str, version: str) -> dict[str, Any]:
    """One engine's card: version, rootless or rootful, socket, CVE floor."""
    floor = check.CVE_FLOORS.get(name)
    card: dict[str, Any] = {
        "engine": name, "path": exe, "version": version, "ok": check.version_ok(name, version),
        "floor": ".".join(map(str, floor[0])) if floor else "", "floor_why": floor[1] if floor else "",
        "command": SHOWN[name], "reachable": False, "rootless": None, "socket": "",
    }
    code, out, err = await _run3(exe, *INFO_CMD[name])
    lines = [x for x in err.strip().splitlines() if x.strip()]
    why = (lines[-1] if lines else f"{name} didn't answer.")[:200]
    try:
        data = json.loads(out) if out.strip() else {}
    except ValueError:
        data = {}
    if code != 0 and not data:
        card["error"] = why
        return card
    card["reachable"] = code == 0
    if name == "podman":
        host = data.get("host") or {}
        sec = host.get("security") or {}
        card["rootless"] = bool(sec.get("rootless", True))
        card["version"] = (data.get("version") or {}).get("Version") or version
        sock = (host.get("remoteSocket") or {}).get("path", "")
        card["socket"] = "user only" if card["rootless"] else "root"
        card["socket_path"] = sock
    else:
        server = data.get("Server") or {}
        if not server:
            card["reachable"] = False
            card["error"] = "The Docker daemon isn't running." if "connect" in why.lower() or not lines else why
        card["version"] = server.get("Version") or (data.get("Client") or {}).get("Version") or version
        card["rootless"] = any("rootless" in str(c.get("Name", "")).lower() or "rootless" in json.dumps(c.get("Details", {}))
                               for c in server.get("Components") or [])
        card["socket"] = "user only" if card["rootless"] else "root = you"
    card["ok"] = check.version_ok(name, card["version"])
    return card

async def containers(name: str, exe: str, ledger: Ledger, pinned: dict[str, str] | None = None) -> list[dict[str, Any]]:
    code, out = await _run(exe, "ps", "--all", "--format", "json")
    if code != 0:
        return []
    owned = {e.ref: e for e in ledger.live("container")}
    rows = []
    for r in _json_rows(out):
        cname = _name(r)
        state, tone = _state(r)
        e = owned.get(cname)
        rows.append({
            "engine": name,
            "name": cname,
            "service": e.service if e else "",
            "image": (pinned or {}).get(e.service, "") if e and (pinned or {}).get(e.service) else str(r.get("Image") or ""),
            "state": state,
            "tone": tone,
            "owner": f"ledger #{e.id}" if e else "not Pythonator · hidden",
            "ledger": e.id if e else 0,
            "labelled": _labels(r).get("pythonator.owner") == "1",
        })
    rows.sort(key=lambda x: (not x["ledger"], x["service"] or "~", x["name"]))
    return rows

async def images(exe: str) -> tuple[int, int]:
    code, out = await _run(exe, "images", "--format", "json")
    if code != 0:
        return 0, 0
    rows = _json_rows(out)
    return len(rows), sum(_size(r.get("Size") or r.get("VirtualSize") or 0) for r in rows)

async def snapshot(engines: dict[str, str], paths: dict[str, str], ledger: Ledger, wsl: dict[str, Any],
                   pinned: dict[str, str] | None = None) -> dict[str, Any]:
    cards, rows = [], []
    for name in ("podman", "docker"):
        exe = paths.get(name, "")
        if not exe:
            cards.append({"engine": name, "installed": False, "command": SHOWN[name]})
            continue
        card = await engine_info(name, exe, engines.get(name, ""))
        card["installed"] = True
        if card["reachable"]:
            cs = await containers(name, exe, ledger, pinned)
            n_img, size = await images(exe)
            card.update(containers=len(cs), mine=sum(1 for c in cs if c["ledger"]), images=n_img, image_bytes=size)
            rows += cs
        cards.append(card)
    preferred = next((c["engine"] for c in cards if c.get("installed") and c.get("rootless")), "")
    for c in cards:
        c["recommended"] = c["engine"] == preferred
    return {"engines": cards, "containers": rows, "wsl": wsl, "ts": time.time()}

def image_present_args(name: str, image: str) -> tuple[str, ...]:
    return ("image", "exists", image) if name == "podman" else ("image", "inspect", "--format", "{{.Id}}", image)

async def image_present(name: str, exe: str, image: str) -> bool:
    code, _ = await _run(exe, *image_present_args(name, image))
    return code == 0

async def prune(name: str, exe: str, ledger: Ledger, running: set[str], images_in_use: set[str]) -> dict[str, Any]:
    """Remove ledger-listed containers that aren't running a service, and images Pythonator pulled that no service
    uses any more. Returns what was removed and the commands run."""
    ran: list[str] = []
    removed: list[str] = []
    for e in ledger.live("container"):
        if e.extra.get("engine", "podman") != name or e.service in running:
            continue
        ran.append(f"{name} rm -f {e.ref}")
        await _run(exe, "rm", "-f", e.ref)
        removed.append(e.ref)
        ledger.undone(e)
    for e in ledger.live("image"):
        if e.extra.get("engine", "podman") != name or e.ref in images_in_use:
            continue
        ran.append(f"{name} rmi {e.ref}")
        code, _ = await _run(exe, "rmi", e.ref)
        if code == 0:
            removed.append(e.ref)
            ledger.undone(e)
    return {"removed": removed, "commands": ran or [f"# nothing of Pythonator's to prune on {name}"]}
