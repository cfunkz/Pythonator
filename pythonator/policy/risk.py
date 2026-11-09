"""Risk projection: the safety-relevant part of a spec, hashed.
Your approval is stored as this hash. Any change to a field here produces a different hash, so an approval can't be
reused for a wider spec (whether the change came from you, the editor or a plugin). `widenings()` lists, in words,
what a change would add; an empty list means the change can apply without asking."""

from __future__ import annotations

import hashlib
import json

from pythonator.model.service import Mode, Reach, ServiceSpec

MODE_ORDER = {Mode.SANDBOX: 0, Mode.CONTAINER: 1, Mode.WSL: 1, Mode.FULL: 3}
TRAFFIC_ORDER = {"off": 0, "hosts": 1, "any": 2}

def projection(spec: ServiceSpec) -> dict[str, object]:
    n = spec.network
    return {
        "mode": spec.mode.value,
        "folder": spec.folder,
        "folders": sorted([g.path, g.write] for g in spec.folders),
        "traffic": n.traffic.mode,
        "hosts": list(n.traffic.hosts),
        "lan": n.traffic.allow_lan,
        "ports": [[p.port, p.reach.value] for p in n.ports],
        "limits": [spec.limits.memory_mb, spec.limits.cpu, spec.limits.tasks],
        "secrets": list(spec.secrets),
        "image": spec.container.image if spec.container else "",
        **({"engine": spec.container.engine} if spec.container else {}),  # rootful Docker is weaker than Podman
        **({"gui": True} if spec.gui else {}),  # only when on, so specs saved before it keep their approval
    }

def risk_hash(spec: ServiceSpec) -> str:
    blob = json.dumps(projection(spec), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()

MODE_EXPOSURE = {Mode.SANDBOX: 1.0, Mode.CONTAINER: 1.6, Mode.WSL: 1.6, Mode.FULL: 6.0}

def exposure(spec: ServiceSpec) -> float:
    """A 0–10 score of how much a service can reach if its code turns hostile. Higher is wider.
    Mode is most of it (Full access starts at 6), then traffic (any host 2, each allowed host 0.15 up to 1),
    the local network (0.6), ports (0.3 each from this computer, 1 each from the network), extra folders
    (0.2 read, 0.5 write) and secrets (0.1 each). Shown on the plan sheet and the Security page."""
    t = spec.network.traffic
    x = MODE_EXPOSURE[spec.mode]
    x += 2.0 if t.mode == "any" else min(1.0, 0.15 * max(1, len(t.hosts))) if t.mode == "hosts" else 0.0
    x += 0.6 if t.allow_lan else 0.0
    x += sum(1.0 if p.reach is Reach.LAN else 0.3 for p in spec.network.ports)
    x += sum(0.5 if g.write else 0.2 for g in spec.folders)
    x += 0.1 * len(spec.secrets)
    return round(min(10.0, x), 1)

def safe_part(approved: ServiceSpec, wanted: ServiceSpec) -> ServiceSpec:
    """The approved spec plus every change from `wanted` that doesn't widen access ("Apply safe steps only").
    Each field (and traffic, ports separately) is taken over only if taking it adds no widening line."""
    merged = approved
    keys = [k for k in ServiceSpec.model_fields if k not in ("name", "generation", "desired", "network")]
    for k in keys:
        if getattr(wanted, k) != getattr(merged, k):
            cand = merged.evolve(**{k: getattr(wanted, k)})
            if not widenings(approved, cand):
                merged = cand
    for part in ("traffic", "ports"):
        if getattr(wanted.network, part) != getattr(merged.network, part):
            net = merged.network.model_dump()
            net[part] = getattr(wanted.network, part).model_dump() if part == "traffic" else [
                p.model_dump() for p in wanted.network.ports]
            cand = merged.evolve(network=net)
            if not widenings(approved, cand):
                merged = cand
            elif part == "traffic":  # take the hosts that were removed, keep the new ones waiting
                kept = [h for h in merged.network.traffic.hosts if h in wanted.network.traffic.hosts]
                net = merged.network.model_dump()
                net["traffic"]["hosts"] = kept
                cand = merged.evolve(network=net)
                if not widenings(approved, cand):
                    merged = cand
    return merged

def widenings(old: ServiceSpec | None, new: ServiceSpec) -> list[str]:
    """Human-readable reasons the new spec reaches further than the old one. A new service is all widening."""
    if old is None:
        first = [f"new service in {new.mode.value} mode with access to {new.folder}"]
        if new.mode is Mode.FULL:
            first.append("runs as you (Full access)")
        if new.network.traffic.mode == "any":
            first.append("traffic: anywhere on the internet")
        elif new.network.traffic.mode == "hosts":
            first += [f"new outbound host {h}" for h in new.network.traffic.hosts] or ["traffic: allowed hosts (none yet)"]
        if new.network.traffic.allow_lan:
            first.append("may reach your local network")
        if new.gui:
            first.append("may open windows on your screen")
        first += [
            f"opens port {p.port} to {'this computer' if p.reach is Reach.THIS_COMPUTER else 'your local network'}"
            for p in new.network.ports
        ]
        return first
    out: list[str] = []
    if MODE_ORDER[new.mode] > MODE_ORDER[old.mode] or (new.mode != old.mode and new.mode is Mode.FULL):
        out.append(f"mode {old.mode.value} → {new.mode.value}")
    if new.folder != old.folder:
        out.append(f"folder {old.folder} → {new.folder}")
    old_f = {g.path: g.write for g in old.folders}
    for g in new.folders:
        if g.path not in old_f:
            out.append(f"new folder {g.path} ({'read-write' if g.write else 'read-only'})")
        elif g.write and not old_f[g.path]:
            out.append(f"{g.path} becomes writable")
    ot, nt = old.network.traffic, new.network.traffic
    if TRAFFIC_ORDER[nt.mode] > TRAFFIC_ORDER[ot.mode]:
        out.append(f"traffic {ot.mode} → {nt.mode}")
    if nt.mode == "hosts":
        added = sorted(set(nt.hosts) - set(ot.hosts if ot.mode == "hosts" else ()))
        out += [f"new outbound host {h}" for h in added]
    if nt.allow_lan and not ot.allow_lan:
        out.append("may reach your local network")
    old_p = {p.port: p.reach for p in old.network.ports}
    for p in new.network.ports:
        if p.port not in old_p:
            out.append(f"opens port {p.port} to {'this computer' if p.reach is Reach.THIS_COMPUTER else 'your local network'}")
        elif p.reach is Reach.LAN and old_p[p.port] is Reach.THIS_COMPUTER:
            out.append(f"port {p.port} becomes reachable from your local network")
    ol, nl = old.limits, new.limits
    if nl.memory_mb > 2 * ol.memory_mb or nl.cpu > 2 * ol.cpu or nl.tasks > 2 * ol.tasks:
        out.append("limits more than doubled")
    for s in sorted(set(new.secrets) - set(old.secrets)):
        out.append(f"new secret {s}")
    if new.gui and not old.gui:
        out.append("may open windows on your screen")
    if (new.container.image if new.container else "") != (old.container.image if old.container else "") and new.container:
        out.append(f"image {new.container.image}")
    return out

def changes(old: ServiceSpec | None, new: ServiceSpec) -> list[dict[str, object]]:
    """Every line of a plan: what changes from the approved spec to this one. `widens` lines wait for your OK.
    kind: "+" added, "-" removed, "~" changed, "!" widens access."""
    widen = set(widenings(old, new))
    out: list[dict[str, object]] = []

    def line(kind: str, text: str, why: str) -> None:
        out.append({"kind": kind, "text": text, "why": why, "widens": kind == "!"})

    if old is None:
        for w in widenings(None, new):
            line("!", w.replace("new outbound host ", "traffic.allow + "),
                 "new outbound host" if w.startswith("new outbound host") else "new port" if "port" in w else "new service")
        line("+", f"run: {' '.join(new.command)}", "safe")
        return out
    if new.command != old.command or new.runtime != old.runtime:
        line("~", f"run: {' '.join(old.command)} → {' '.join(new.command)}", "safe")
    for w in widenings(old, new):
        why = ("new outbound host" if w.startswith("new outbound host") else "new port" if "port" in w
               else "more access" if w.startswith(("mode", "folder", "new folder", "traffic")) else "needs your OK")
        line("!", w.replace("new outbound host ", "traffic.allow + "), why)
    ot, nt = old.network.traffic, new.network.traffic
    for h in sorted(set(ot.hosts) - set(nt.hosts)):
        line("-", f"traffic.allow − {h}", "narrower")
    if TRAFFIC_ORDER[nt.mode] < TRAFFIC_ORDER[ot.mode]:
        line("~", f"traffic {ot.mode} → {nt.mode}", "narrower")
    for p in old.network.ports:
        if p.port not in {q.port for q in new.network.ports}:
            line("-", f"close port {p.port}", "narrower")
    ol, nl = old.limits, new.limits
    if (ol.memory_mb, ol.cpu, ol.tasks) != (nl.memory_mb, nl.cpu, nl.tasks) and "limits more than doubled" not in widen:
        line("~", f"limits {ol.memory_mb} MB · {ol.cpu:g} CPU · {ol.tasks} → {nl.memory_mb} MB · {nl.cpu:g} CPU · {nl.tasks}", "safe")
    if (old.schedule.cron if old.schedule else "") != (new.schedule.cron if new.schedule else ""):
        line("~", f"schedule → {new.schedule.cron if new.schedule else 'keep running'}", "safe")
    if old.restart != new.restart:
        line("~", f"restart {new.restart.policy}, give up after {new.restart.give_up_after}", "safe")
    for k in sorted(set(new.env) ^ set(old.env)):
        line("+" if k in new.env else "-", f"variable {k}", "safe")
    for k in sorted(set(old.secrets) - set(new.secrets)):
        line("-", f"secret {k}", "narrower")
    if old.health != new.health:
        h = new.health
        line("~", "health check off" if h.kind == "off" else
             f"health check {h.kind} :{h.port}{h.path if h.kind == 'http' else ''} every {h.every_s:g} s", "safe")
    if old.group != new.group:
        line("~", f"group → {new.group or 'none'}", "safe")
    if MODE_ORDER[new.mode] < MODE_ORDER[old.mode]:
        line("~", f"mode {old.mode.value} → {new.mode.value}", "narrower")
    return out
