"""Guard: checks every change against your rules before Pythonator saves it.

It sees the change (mode, folders, traffic, ports, limits) and answers allow, or refuse with a reason and a fix.
It can't change anything itself, and it has no files or network.
"""

from pythonator_sdk import plugin


def _inside(path, roots):
    path = path.rstrip("/") + "/"
    return any(path.startswith(r.rstrip("/") + "/") for r in roots)


def _summary(s):
    parts = []
    if s.get("port_rule", True):
        lo, hi = s.get("port_range", [1024, 65535])
        parts.append(f"ports {lo}–{hi}")
        if not s.get("lan_ports", False):
            parts.append("no ports from the network")
    if "max_memory_mb" in s:
        parts.append(f"memory up to {s['max_memory_mb']} MB")
    if s.get("full_access_rule", True):
        roots = s.get("full_access_only_inside", [])
        parts.append(f"Full access only inside {', '.join(roots)}" if roots else "no Full access")
    if s.get("exposure_rule", False):
        parts.append(f"exposure at most {s.get('max_exposure', 6)}")
    if s.get("block_lan", False):
        parts.append("no local network")
    return ", ".join(parts) or "no rules on"


@plugin.hook("policy.check")
def review(change):
    s = plugin.settings
    name = change.get("name", "?")
    verdict = _review(change, s, name)
    verdict["summary"] = _summary(s)
    return verdict


def _review(change, s, name):
    if s.get("full_access_rule", True) and change.get("mode") == "full":
        roots = s.get("full_access_only_inside", [])
        if not _inside(change.get("folder", ""), roots):
            return {
                "allow": False,
                "reason": f"{name}: Full access is only allowed inside {', '.join(roots) or 'nowhere'}.",
                "fix": "Use Sandbox mode, or move the project into an allowed folder.",
            }
    lo, hi = s.get("port_range", [1024, 65535]) if s.get("port_rule", True) else (1, 65535)
    lan_ok = s.get("lan_ports", False) or not s.get("port_rule", True)
    for port, reach in change.get("ports", []):
        if not lo <= port <= hi:
            return {
                "allow": False, "reason": f"{name}: port {port} is outside {lo}–{hi}.",
                "fix": f"Pick a port between {lo} and {hi}.",
            }
        if reach == "lan" and not lan_ok:
            return {
                "allow": False, "reason": f"{name}: ports reachable from the local network are off.",
                "fix": "Make the port reachable from this computer only.",
            }
    if s.get("block_lan", False) and change.get("lan"):
        return {
            "allow": False, "reason": f"{name}: the local network is blocked for every service.",
            "fix": "Turn off Allow local network in the service's Network settings.",
        }
    cap = float(s.get("max_exposure", 10))
    if s.get("exposure_rule", False) and float(change.get("exposure", 0)) > cap and change.get("widens"):
        return {
            "allow": False, "reason": f"{name}: exposure {change.get('exposure')} is over the cap of {cap:g}.",
            "fix": "Narrow it: Sandbox instead of Full access, fewer hosts or ports.",
        }
    mem = (change.get("limits") or [0])[0]
    if mem > s.get("max_memory_mb", 1 << 30):
        return {
            "allow": False, "reason": f"{name}: {mem} MB is over the {s['max_memory_mb']} MB limit.",
            "fix": "Lower the memory limit.",
        }
    return {"allow": True}


@plugin.on("service.state")
def state(event):
    if event.get("phase") == "failed":
        try:
            plugin.notify(f"{event.get('name')} gave up after repeated crashes", level="warn")
        except PermissionError:
            pass
