"""Webhooks: tell Discord, Slack, Telegram, Teams, ntfy or your own endpoint when a service crashes or recovers.

Shows how to write an event plugin: react to ``on_event``, keep a little state, and do slow work (HTTP) on a
thread of its own so the plugin keeps answering. Standard library only.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

from pythonator.sdk import Plugin, ServiceEvent, ServiceInfo

TITLES = {"crashed": "crashed", "gave_up": "gave up restarting", "recovered": "is running again",
          "started": "started", "stopped": "stopped"}


def payload(url: str, title: str, body: str) -> tuple[bytes, str]:
    """The request body for ``url`` and its content type."""
    host = urllib.parse.urlsplit(url).hostname or ""
    text = f"**{title}**\n{body}".strip() if body else f"**{title}**"
    if host.endswith(("discord.com", "discordapp.com")):
        # allowed_mentions: output can't ping @everyone
        return json.dumps({"content": text[:1990], "allowed_mentions": {"parse": []}}).encode(), "application/json"
    if host == "hooks.slack.com":
        return json.dumps({"text": text.replace("**", "*")[:3900]}).encode(), "application/json"
    if host == "api.telegram.org":
        chat = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("chat_id", [""])[0]
        return json.dumps({"chat_id": chat, "text": text.replace("**", "")[:4000]}).encode(), "application/json"
    if host == "ntfy.sh" or host.startswith("ntfy."):
        return text.replace("**", "").encode()[:4000], "text/plain; charset=utf-8"
    if "webhook.office.com" in host or "logic.azure.com" in host:
        return json.dumps({"text": text[:20000]}).encode(), "application/json"
    return json.dumps({"title": title, "text": body, "source": "pythonator"}).encode(), "application/json"


def allowed(url: str) -> bool:
    parts = urllib.parse.urlsplit(url.strip())
    local = parts.hostname in ("localhost", "127.0.0.1", "::1")
    return bool(parts.hostname) and (parts.scheme == "https" or (parts.scheme == "http" and local))


class Webhooks(Plugin):
    def on_start(self) -> None:
        self.recent: dict[str, deque[str]] = {}
        self.last_sent: dict[str, float] = {}
        self.crashed: set[str] = set()
        self.outbox: queue.SimpleQueue[tuple[str, str]] = queue.SimpleQueue()
        threading.Thread(target=self._deliver, daemon=True).start()

    def urls(self) -> list[str]:
        out = []
        for line in str(self.settings.get("urls", "")).splitlines():
            if line.strip() and not line.strip().startswith("#"):
                if allowed(line):
                    out.append(line.strip())
                else:
                    self.log(f"Skipped {line.strip()[:60]}: use https://")
        return out

    def on_output(self, service: ServiceInfo, lines: list[str]) -> None:
        keep = max(0, min(50, int(self.settings.get("log_lines", 10))))
        self.recent.setdefault(service.id, deque(maxlen=keep or 1)).extend(lines)

    def on_event(self, event: ServiceEvent) -> None:
        kind, sid = event.kind, event.service.id
        if kind == "crashed":
            self.crashed.add(sid)
        elif kind == "started" and sid in self.crashed:
            self.crashed.discard(sid)
            kind = "recovered"
        wanted = {"crashed": "on_crash", "gave_up": "on_gave_up", "recovered": "on_recover",
                  "started": "on_stop", "stopped": "on_stop"}.get(kind)
        if not wanted or not self.settings.get(wanted, False):
            return
        gap = max(0, int(self.settings.get("min_gap", 60)))
        now = time.monotonic()
        if kind != "gave_up" and now - self.last_sent.get(sid, -1e9) < gap:
            return
        self.last_sent[sid] = now
        title = f"{event.service.name} {TITLES[kind]}"
        body = event.note
        if kind in ("crashed", "gave_up") and self.recent.get(sid):
            tail = "\n".join(line[:300] for line in self.recent[sid])
            body = f"{body}\n```\n{tail[-1500:]}\n```"
        for url in self.urls():
            self.outbox.put((url, f"{title}\n{body}"))

    def _deliver(self) -> None:
        while True:
            url, message = self.outbox.get()
            title, _, body = message.partition("\n")
            data, ctype = payload(url, title, body)
            for attempt in range(3):
                try:
                    req = urllib.request.Request(url, data=data, headers={"Content-Type": ctype,  # noqa: S310
                                                                          "User-Agent": "Pythonator-Webhooks"})
                    with urllib.request.urlopen(req, timeout=15):  # noqa: S310 - https only, checked above
                        break
                except urllib.error.HTTPError as exc:
                    if exc.code == 429:
                        time.sleep(min(60.0, float(exc.headers.get("Retry-After", "5") or 5)))
                        continue
                    self.log(f"{urllib.parse.urlsplit(url).hostname} refused the alert: HTTP {exc.code}")
                    break
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    if attempt == 2:
                        self.log(f"Couldn't reach {urllib.parse.urlsplit(url).hostname}: {exc}")
                    time.sleep(2 ** attempt)
