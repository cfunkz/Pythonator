"""Notify: tells you when a service crashes, gives up, is blocked or fails its health check.

In the window always (ui.notify). To a webhook only if you turn it on in settings, grant net.connect for that host
and set webhook_url: the address stays in the OS keychain and Pythonator fills it in on the way out.
"""

import json
import time

from pythonator_sdk import plugin

LAST = {}
QUIET = {}
WORDS = {
    "backoff": "crashed and will restart", "failed": "gave up after repeated crashes", "blocked": "can't start",
    "unhealthy": "fails its health check",
}
COUNT = {"alerts": 0}


def _body(host_hint, text):
    """Service text is data: it never pings a channel or renders as markup in the chat it lands in."""
    text = "".join(c for c in text if c in "\n\t" or c.isprintable())[:1800]
    if "discord" in host_hint:  # no @everyone, @here, role or user pings, whatever the text says
        return json.dumps({"content": text, "allowed_mentions": {"parse": []}})
    if "ntfy" in host_hint:
        return text
    # Slack: & < > are its control characters (<!channel>, <@U123>, <url|label>)
    return json.dumps({"text": text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")})


def _send(text):
    s = plugin.settings
    try:
        plugin.notify(text, level="warn")
    except PermissionError:
        pass
    if s.get("webhook"):
        hint = str(s.get("webhook_kind", "slack"))
        try:
            res = plugin.request("{secret:webhook_url}", headers={"content-type": "application/json"},
                                 body=_body(hint, f"Pythonator: {text}"), timeout=15)
            if res.get("status", 0) >= 300:
                print(f"webhook answered {res.get('status')}")
        except (PermissionError, RuntimeError) as e:
            print(f"webhook not sent: {e}")
    COUNT["alerts"] += 1
    try:
        plugin.status(f"{COUNT['alerts']} alert{'s' if COUNT['alerts'] != 1 else ''}")
    except (PermissionError, RuntimeError):
        pass


@plugin.on("service.state")
def changed(ev):
    name = ev.get("name", "?")
    on = set(plugin.settings.get("on", WORDS))
    state = ev.get("phase", "")
    if str(ev.get("health") or "").startswith("failing"):
        state = "unhealthy"
    if LAST.get(name) == state:
        return
    LAST[name] = state
    if state == "backoff":  # the first crash, then at most once per 10 minutes; giving up always
        now = time.monotonic()
        if now - QUIET.get(name, -1e9) < 600:
            return
        QUIET[name] = now
    if state in on and state in WORDS:
        msg = ev.get("message") or ""
        _send(f"{name} {WORDS[state]}" + (f": {msg}" if msg else ""))
