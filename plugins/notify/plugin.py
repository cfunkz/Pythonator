"""Notify: one timeline of what happens to your services, and alerts for what matters. Standard library only.

It merges the old Health checks and Webhooks plugins and adds anomaly detection:

* **Events**: a crash, Pythonator giving up restarting, a start blocked by a policy plugin (Guard), a failed install,
  a recovery (running again after a crash, answering again after failing health checks), starts and stops.
* **Health checks**: each running service with a port is asked ``GET http://127.0.0.1:<port><path>``.
* **Anomalies**: memory that keeps growing, a CPU stuck near the top, a crash loop, a burst of error lines, a server
  that stopped listening on its port while still running.

Each alert shows in the panel's timeline, as a notice in the window (and a tray pop-up while it is hidden), and goes
to the webhooks you set: Discord, Slack, Telegram, Teams, ntfy or any https endpoint. Pick the services it watches
(all of them by default) in the settings, or mute one from the panel.
"""

from __future__ import annotations

import http.client
import json
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import asdict, dataclass

from pythonator.sdk import Plugin, ServiceEvent, ServiceInfo, ui

HISTORY = 500  # events kept (and saved, so the timeline survives a restart)
SHOWN = 150  # events in the panel's table
WINDOW = 600.0  # seconds of stats kept per service for anomalies: ten minutes
CPU_SPAN = 300.0  # a CPU anomaly: over the threshold for this long
ERROR_LINE = re.compile(r"\b(error|exception|traceback|fatal|panic|critical)\b", re.IGNORECASE)
ALL, PROBLEMS = "All services", "Problems only"
SERVICES, EVENTS = "Services", "Events"  # the panel's two views
EVERYTHING = "All events"
LEVEL = {"crashed": "error", "gave_up": "error", "install_failed": "error", "blocked": "warn", "unhealthy": "warn",
         "anomaly": "warn", "recovered": "ok", "healthy": "ok", "started": "", "stopped": "", "exited": ""}
TITLES = {"crashed": "crashed", "gave_up": "gave up restarting", "blocked": "was blocked from starting",
          "install_failed": "couldn't install its packages", "recovered": "is running again",
          "unhealthy": "isn't answering", "healthy": "answers again", "anomaly": "looks wrong",
          "started": "started", "stopped": "stopped", "exited": "exited"}
SETTING = {"crashed": "on_crash", "gave_up": "on_gave_up", "blocked": "on_blocked",
           "install_failed": "on_install_failed", "recovered": "on_recover", "healthy": "on_recover",
           "unhealthy": "on_unhealthy", "anomaly": "on_anomaly", "started": "on_stop", "stopped": "on_stop",
           "exited": "on_stop"}
DEFAULT_ON = {"on_crash", "on_gave_up", "on_blocked", "on_install_failed", "on_recover", "on_unhealthy",
              "on_anomaly", "in_app"}


@dataclass
class Event:
    at: float  # time.time()
    sid: str
    name: str
    kind: str
    detail: str = ""

    @property
    def level(self) -> str:
        return LEVEL.get(self.kind, "")

    @property
    def problem(self) -> bool:
        return self.level in ("error", "warn")


# -- webhooks ------------------------------------------------------------------------------------------------


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


# -- health checks -------------------------------------------------------------------------------------------


def probe(port: int, path: str, timeout: float) -> tuple[str, float]:
    """``("200", ms)``, or ``("error text", ms)``."""
    started = time.monotonic()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", path if path.startswith("/") else f"/{path}", headers={"User-Agent": "Pythonator-Notify"})
        status = str(conn.getresponse().status)
    except (OSError, http.client.HTTPException) as exc:
        status = type(exc).__name__ if not str(exc) else str(exc)[:80]
    finally:
        conn.close()
    return status, (time.monotonic() - started) * 1000


def healthy(status: str) -> bool:
    return status.isdigit() and int(status) < 500


def target_port(s: ServiceInfo) -> int:
    """The port to ask: the one it really listens on when that isn't the declared one, else the declared one."""
    declared = s.ports[0] if s.ports else 0
    if declared and (not s.listening or declared in s.listening):
        return declared
    return s.listening[0] if s.listening else 0


# -- anomalies -----------------------------------------------------------------------------------------------


def memory_grew(samples: list[tuple[float, float, int]], percent: int) -> str:
    """``samples``: (time, cpu, memory) over the last ten minutes. A reason when memory grew by ``percent`` and
    kept growing (the last minute above the first, and above the middle): a leak, not one big allocation."""
    if percent <= 0 or len(samples) < 20 or samples[-1][0] - samples[0][0] < WINDOW * 0.8:
        return ""
    span = samples[-1][0] - samples[0][0]
    first = [m for t, _c, m in samples if t - samples[0][0] <= 60]
    middle = [m for t, _c, m in samples if abs(t - samples[0][0] - span / 2) <= 30]
    last = [m for t, _c, m in samples if samples[-1][0] - t <= 60]
    if not first or not middle or not last:
        return ""
    a, b, c = sum(first) / len(first), sum(middle) / len(middle), sum(last) / len(last)
    if c >= 100 * 1024 * 1024 and a > 0 and c >= a * (1 + percent / 100) and a < b < c:
        return f"memory grew from {a / 2**20:.0f} MB to {c / 2**20:.0f} MB in {span / 60:.0f} minutes"
    return ""


def cpu_stuck(samples: list[tuple[float, float, int]], percent: int) -> str:
    """A reason when every sample of the last five minutes is over ``percent`` of one core."""
    if percent <= 0 or not samples or samples[-1][0] - samples[0][0] < CPU_SPAN:
        return ""
    recent = [c for t, c, _m in samples if samples[-1][0] - t <= CPU_SPAN]
    if len(recent) >= 10 and min(recent) >= percent:
        return f"CPU over {percent}% of a core for {CPU_SPAN / 60:.0f} minutes (now {recent[-1]:.0f}%)"
    return ""


def count_errors(lines: list[str]) -> int:
    return sum(1 for line in lines if ERROR_LINE.search(line))


class Notify(Plugin):
    def on_start(self) -> None:
        self.lock = threading.RLock()
        self.events: deque[Event] = deque(self._load_events(), maxlen=HISTORY)
        muted = self._load("muted.json", [])
        self.muted: set[str] = {str(m) for m in muted} if isinstance(muted, list) else set()
        self.recent: dict[str, deque[str]] = {}
        self.crashed: set[str] = set()
        self.crashes: dict[str, deque[float]] = {}
        self.errors: dict[str, deque[tuple[float, int]]] = {}
        self.samples: dict[str, deque[tuple[float, float, int]]] = {}
        self.listened: dict[str, tuple[int, ...]] = {}
        self.quiet_ports: dict[str, int] = {}
        self.flagged: dict[tuple[str, str], float] = {}  # (service, anomaly) -> when it was last raised
        self.health: dict[str, tuple[str, str, float, str]] = {}  # id -> (url, status, ms, when)
        self.failing: set[str] = set()
        self.last_sent: dict[str, float] = {}
        self.sent = 0
        self.view = ALL
        self.filter = EVERYTHING
        self.page = SERVICES
        self.labels: dict[str, str] = {}
        self.next_check = 0.0
        self.busy = threading.Lock()
        self.dirty = False
        self.last_render = 0.0
        self.outbox: queue.SimpleQueue[tuple[str, str, str]] = queue.SimpleQueue()
        threading.Thread(target=self._deliver, daemon=True, name="notify-webhooks").start()
        self.render(force=True)

    # -- what it watches ----------------------------------------------------------------------------------

    def setting(self, key: str) -> bool:
        return bool(self.settings.get(key, key in DEFAULT_ON))

    def number(self, key: str, default: int) -> int:
        try:
            return int(self.settings.get(key, default))
        except (TypeError, ValueError):
            return default

    def watched(self, service: ServiceInfo) -> bool:
        names = {line.strip().lower() for line in str(self.settings.get("services", "")).splitlines()
                 if line.strip() and not line.strip().startswith("#")}
        return service.id not in self.muted and (not names or service.name.lower() in names)

    # -- from Pythonator ----------------------------------------------------------------------------------

    def on_output(self, service: ServiceInfo, lines: list[str]) -> None:
        keep = max(1, min(50, self.number("log_lines", 10)))
        self.recent.setdefault(service.id, deque(maxlen=keep)).extend(line[:300] for line in lines)
        threshold = self.number("error_burst", 30)
        if threshold <= 0:
            return
        now = time.monotonic()
        window = self.errors.setdefault(service.id, deque())
        found = count_errors(lines)
        if found:
            window.append((now, found))
        while window and now - window[0][0] > 60:
            window.popleft()
        total = sum(n for _t, n in window)
        if total >= threshold:
            window.clear()
            self.anomaly(service, "errors", f"{total} error lines in a minute")

    def on_event(self, event: ServiceEvent) -> None:
        kind, service = event.kind, event.service
        sid = service.id
        if kind == "crashed":
            self.crashed.add(sid)
            now = time.monotonic()
            times = self.crashes.setdefault(sid, deque())
            times.append(now)
            while times and now - times[0] > WINDOW:
                times.popleft()
            loop = self.number("restart_loop", 3)
            if loop > 0 and len(times) >= loop:
                times.clear()
                self.anomaly(service, "loop", f"crashed {loop} times in 10 minutes")
        elif kind == "started" and sid in self.crashed:
            self.crashed.discard(sid)
            kind = "recovered"
        elif kind == "stopped":
            self.crashed.discard(sid)
            self.samples.pop(sid, None)
            self.listened.pop(sid, None)
        if kind not in TITLES:
            return
        detail = event.note
        if event.exit_code is not None and kind in ("crashed", "exited", "gave_up"):
            detail = f"exit code {event.exit_code}" + (f" · {detail}" if detail else "")
        self.record(service, kind, detail)

    def on_stats(self) -> None:
        now = time.monotonic()
        growth, busy = self.number("memory_growth", 50), self.number("cpu_busy", 95)
        for sid, s in self.services.items():
            if s.state != "running":
                self.samples.pop(sid, None)
                continue
            samples = self.samples.setdefault(sid, deque())
            samples.append((now, s.cpu, s.memory))
            while samples and now - samples[0][0] > WINDOW:
                samples.popleft()
            data = list(samples)
            if why := memory_grew(data, growth):
                self.anomaly(s, "memory", why, every=3600)
            if why := cpu_stuck(data, busy):
                self.anomaly(s, "cpu", why, every=1800)
            if s.listening:
                self.listened[sid] = s.listening
                self.quiet_ports.pop(sid, None)
            elif sid in self.listened:
                self.quiet_ports[sid] = self.quiet_ports.get(sid, 0) + 1
                if self.quiet_ports[sid] == 3:  # about six seconds without a listening port, still running
                    ports = ", ".join(map(str, self.listened.pop(sid)))
                    self.anomaly(s, "port", f"stopped listening on {ports} but is still running", every=600)
        self.render()

    def on_tick(self) -> None:
        if time.monotonic() >= self.next_check:
            self.check_all()
        if self.dirty:
            self.dirty = False
            self._save("events.json", [asdict(e) for e in self.events])
        self.render()

    def on_settings(self) -> None:
        self.next_check = 0.0
        self.render(force=True)

    def on_action(self, action: str, value: str) -> None:
        if action == "page":
            self.page = EVENTS if value.startswith(EVENTS) else SERVICES
        elif action == "view":
            self.view = value if value == ALL or value in self.labels else ALL
        elif action == "filter":
            self.filter = value if value in (EVERYTHING, PROBLEMS) else EVERYTHING
        elif action == "check":
            self.check_all()
        elif action == "clear":
            with self.lock:
                self.events.clear()
            self.dirty = True
        elif action in ("mute", "unmute"):
            sid = self.labels.get(self.view, "")
            if sid:
                (self.muted.add if action == "mute" else self.muted.discard)(sid)
                self._save("muted.json", sorted(self.muted))
        elif action == "test":
            urls = self.urls()
            for url in urls:
                self.outbox.put((url, "Pythonator test alert", "If you can read this, alerts reach you here."))
            self.notify(f"Test alert sent to {len(urls)} webhook{'s' if len(urls) != 1 else ''}" if urls else
                        "No webhook URLs set: alerts show in Pythonator only (Plugins → Notify → Settings)", "ok")
        self.render(force=True)

    # -- recording and alerting -----------------------------------------------------------------------------

    def anomaly(self, service: ServiceInfo, what: str, why: str, every: float = 600) -> None:
        now = time.monotonic()
        if now - self.flagged.get((service.id, what), -1e9) < every:
            return
        self.flagged[(service.id, what)] = now
        self.record(service, "anomaly", why)

    def record(self, service: ServiceInfo, kind: str, detail: str = "") -> None:
        event = Event(time.time(), service.id, service.name, kind, detail[:500])
        with self.lock:
            self.events.append(event)
        self.dirty = True
        if self.watched(service) and self.setting(SETTING.get(kind, "")):
            self.alert(service, event)
        self.render(force=True)

    def alert(self, service: ServiceInfo, event: Event) -> None:
        title = f"{service.name} {TITLES.get(event.kind, event.kind)}"
        if self.setting("in_app") and event.kind not in ("started", "stopped", "exited", "healthy"):
            level = {"error": "error", "warn": "warn", "ok": "ok"}.get(event.level, "info")
            self.notify(f"{title}{': ' + event.detail if event.detail else ''}", level)
        urls = self.urls()
        if not urls:
            return
        gap = max(0, self.number("min_gap", 60))
        now = time.monotonic()
        if event.kind != "gave_up" and now - self.last_sent.get(service.id, -1e9) < gap:
            return
        self.last_sent[service.id] = now
        body = event.detail
        if event.kind in ("crashed", "gave_up") and self.recent.get(service.id):
            tail = "\n".join(self.recent[service.id])
            body = f"{body}\n```\n{tail[-1500:]}\n```"
        for url in urls:
            self.outbox.put((url, title, body))
        self.sent += 1

    def urls(self) -> list[str]:
        out = []
        for raw in str(self.settings.get("urls", "")).splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                if allowed(line):
                    out.append(line)
                else:
                    self.log(f"Skipped {line[:60]}: use https://")
        return out

    def _deliver(self) -> None:
        while True:
            url, title, body = self.outbox.get()
            data, ctype = payload(url, title, body)
            host = urllib.parse.urlsplit(url).hostname
            for attempt in range(3):
                try:
                    req = urllib.request.Request(url, data=data, headers={"Content-Type": ctype,  # noqa: S310
                                                                          "User-Agent": "Pythonator-Notify"})
                    with urllib.request.urlopen(req, timeout=15):  # noqa: S310 - https only, checked above
                        break
                except urllib.error.HTTPError as exc:
                    if exc.code == 429:
                        time.sleep(min(60.0, float(exc.headers.get("Retry-After", "5") or 5)))
                        continue
                    self.log(f"{host} refused the alert: HTTP {exc.code}")
                    break
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    if attempt == 2:
                        self.log(f"Couldn't reach {host}: {exc}")
                    time.sleep(2 ** attempt)

    # -- health checks ------------------------------------------------------------------------------------

    def check_all(self) -> None:
        self.next_check = time.monotonic() + max(5, self.number("health_interval", 30))
        if not str(self.settings.get("health_path", "/") or "").strip() or self.busy.locked():
            return
        threading.Thread(target=self._check, daemon=True, name="notify-health").start()

    def _check(self) -> None:
        with self.busy:
            path = str(self.settings.get("health_path", "/") or "/").strip()
            timeout = max(1, min(30, self.number("health_timeout", 5)))
            seen = set()
            for service in list(self.services.values()):
                port = target_port(service) if service.state == "running" else 0
                if not port:
                    continue
                seen.add(service.id)
                status, ms = probe(port, path, timeout)
                if not status.isdigit() and port in service.listening:
                    status = "listening (no HTTP answer)"
                good = healthy(status)
                self.health[service.id] = (f"http://127.0.0.1:{port}{path}", status, ms, time.strftime("%H:%M:%S"))
                if not good and service.id not in self.failing:
                    self.failing.add(service.id)
                    self.record(service, "unhealthy", f"port {port}: {status}")
                elif good and service.id in self.failing:
                    self.failing.discard(service.id)
                    self.record(service, "healthy", f"port {port}: {status} in {ms:.0f} ms")
            for sid in list(self.health):
                if sid not in seen:
                    del self.health[sid]
                    self.failing.discard(sid)
            self.render(force=True)

    # -- the panel ----------------------------------------------------------------------------------------

    def render(self, force: bool = False) -> None:
        if "ui" not in self.permissions:
            return
        now = time.monotonic()
        if not force and now - self.last_render < 2.0:
            return
        self.last_render = now
        with self.lock:
            events = list(self.events)
        self.show(self.panel(events))

    def panel(self, events: list[Event]) -> dict[str, object]:
        services = sorted(self.services.values(), key=lambda s: s.name.lower())
        self.labels = {}
        for s in services:
            self.labels[s.name if s.name not in self.labels else f"{s.name} ({s.id[:6]})"] = s.id
        if self.view != ALL and self.view not in self.labels:
            self.view = ALL
        picked = self.labels.get(self.view, "")
        day = time.time() - 86_400
        problems = [e for e in events if e.problem and e.at >= day]
        running = sum(1 for s in services if s.state == "running")
        answering = sum(1 for _u, status, _ms, _w in self.health.values() if healthy(status))
        stats = ui.row(
            ui.stat("Running", f"{running} / {len(services)}", "ok" if running else "muted"),
            ui.stat("Problems, 24 h", str(len(problems)), "error" if problems else "ok"),
            ui.stat("Health checks", f"{answering} / {len(self.health)}" if self.health else "–",
                    "ok" if answering == len(self.health) else "warn"),
            ui.stat("Alerts sent", str(self.sent)),
        )
        page = self.page
        events_tab = f"{EVENTS} ({len(problems)} problems)" if problems else EVENTS
        items: list[dict[str, object]] = [ui.tabs("page", [SERVICES, events_tab], events_tab if page == EVENTS
                                                  else SERVICES)]
        if page == SERVICES:
            items += [stats, ui.row(ui.select("view", [ALL, *self.labels], self.view, "Show"),
                                    ui.button("Check health now", "check", icon="refresh"))]
            if picked:
                items.append(self._service(self.services[picked], events))
                items.append(ui.row(ui.button("Unmute its alerts" if picked in self.muted else "Mute its alerts",
                                              "unmute" if picked in self.muted else "mute")))
            else:
                items.append(self._overview(services, events))
        else:
            shown = [e for e in reversed(events) if (not picked or e.sid == picked)
                     and (self.filter == EVERYTHING or e.problem)][:SHOWN]
            items.append(ui.row(ui.select("view", [ALL, *self.labels], self.view, "Show"),
                                ui.select("filter", [EVERYTHING, PROBLEMS], self.filter),
                                ui.button("Send a test alert", "test", icon="send"),
                                ui.button("Clear the history", "clear", icon="delete")))
            if shown:
                items.append(ui.table(["When", "Service", "Event", "Detail"],
                                      [[_when(e.at), e.name, TITLES.get(e.kind, e.kind), e.detail[:200]]
                                       for e in shown], [e.level or "muted" for e in shown]))
            else:
                items.append(ui.text("Nothing has happened yet." if not events else "No events match.", "muted"))
        urls = self.urls()
        hosts = sorted({urllib.parse.urlsplit(u).hostname or "?" for u in urls})
        where = "this window" + (f" and {len(urls)} webhook{'s' if len(urls) != 1 else ''} "
                                 f"({', '.join(hosts)})" if urls else "")
        scope = str(self.settings.get("services", "")).strip()
        items.append(ui.text(f"Alerts go to {where}. Watching {'the services in the settings' if scope else
                                                               'every service'}"
                             f"{f', {len(self.muted)} muted' if self.muted else ''}.", "muted"))
        return ui.panel("Notify", *items)

    def _overview(self, services: list[ServiceInfo], events: list[Event]) -> dict[str, object]:
        if not services:
            return ui.text("No services yet.", "muted")
        last: dict[str, Event] = {}
        count: dict[str, int] = {}
        day = time.time() - 86_400
        for e in events:
            last[e.sid] = e
            if e.problem and e.at >= day:
                count[e.sid] = count.get(e.sid, 0) + 1
        rows, tones = [], []
        for s in services[:200]:
            health = self.health.get(s.id)
            check = f"{health[1]} · {health[2]:.0f} ms" if health else "–"
            latest = last.get(s.id)
            seen = f"{TITLES.get(latest.kind, latest.kind)} {_when(latest.at)}" if latest else "–"
            rows.append([s.name, s.state, check, seen,
                         str(count.get(s.id, 0)), "muted" if s.id in self.muted or not self.watched(s) else "on"])
            bad = s.state == "failed" or (health is not None and not healthy(health[1]))
            tones.append("error" if bad else "warn" if count.get(s.id) else "ok" if s.state == "running" else "muted")
        return ui.table(["Service", "State", "Health", "Last event", "Problems 24 h", "Alerts"], rows, tones)

    def _service(self, s: ServiceInfo, events: list[Event]) -> dict[str, object]:
        health = self.health.get(s.id)
        mem = f"{s.memory / 2**20:.0f} MB" if s.memory else "–"
        parts = [ui.stat("State", s.state, "ok" if s.state == "running" else "error" if s.state == "failed" else ""),
                 ui.stat("CPU", f"{s.cpu:.0f}%" if s.state == "running" else "–"), ui.stat("Memory", mem),
                 ui.stat("Health", health[1] if health else "–",
                         "" if not health else "ok" if healthy(health[1]) else "error"),
                 ui.stat("Crashes, 24 h", str(sum(1 for e in events if e.sid == s.id and e.kind == "crashed"
                                                  and e.at >= time.time() - 86_400)))]
        return ui.row(*parts)

    # -- saved state --------------------------------------------------------------------------------------

    def _load(self, name: str, default: object) -> object:
        try:
            return json.loads((self.data_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def _load_events(self) -> list[Event]:
        data = self._load("events.json", [])
        out = []
        for item in data if isinstance(data, list) else []:
            try:
                out.append(Event(float(item["at"]), str(item["sid"]), str(item["name"]), str(item["kind"]),
                                 str(item.get("detail", ""))))
            except (KeyError, TypeError, ValueError):
                continue
        return out[-HISTORY:]

    def _save(self, name: str, data: object) -> None:
        try:
            (self.data_dir / name).write_text(json.dumps(data), encoding="utf-8")
        except OSError as exc:
            self.log(f"Couldn't save {name}: {exc}")


def _when(at: float) -> str:
    """"14:03:22" today, "Mon 14:03" this week, else the date."""
    now = time.time()
    if now - at < 86_400 and time.localtime(at).tm_mday == time.localtime(now).tm_mday:
        return time.strftime("%H:%M:%S", time.localtime(at))
    if now - at < 7 * 86_400:
        return time.strftime("%a %H:%M", time.localtime(at))
    return time.strftime("%Y-%m-%d", time.localtime(at))
