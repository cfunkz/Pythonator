"""Health checks: is each web server you run answering? Shown live in a panel.

Shows how to build a UI plugin with the design system in :class:`pythonator.sdk.ui`: stats, a table with tones,
and buttons that come back as ``on_action``. Standard library only.
"""

from __future__ import annotations

import http.client
import threading
import time

from pythonator.sdk import Plugin, ServiceInfo, ui


def probe(port: int, path: str, timeout: float) -> tuple[str, float]:
    """``("200", ms)``, or ``("error text", ms)``."""
    started = time.monotonic()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", path if path.startswith("/") else f"/{path}", headers={"User-Agent": "Pythonator-Health"})
        status = str(conn.getresponse().status)
    except (OSError, http.client.HTTPException) as exc:
        status = type(exc).__name__ if not str(exc) else str(exc)[:80]
    finally:
        conn.close()
    return status, (time.monotonic() - started) * 1000


class Health(Plugin):
    def on_start(self) -> None:
        self.results: dict[str, tuple[str, str, float, str]] = {}  # id -> (url, status, ms, when)
        self.failing: set[str] = set()
        self.next_check = 0.0
        self.busy = threading.Lock()
        self.render()

    def targets(self) -> list[tuple[ServiceInfo, int]]:
        """Each running server and the port to ask: the one it really listens on when that isn't the declared
        one (started with --port 8000 while its settings say 8764), else the declared one."""
        out = []
        for s in self.services.values():
            if s.state != "running" or not (s.ports or s.listening):
                continue
            declared = s.ports[0] if s.ports else 0
            port = declared if declared and (not s.listening or declared in s.listening) else s.listening[0]
            out.append((s, port))
        return out

    def on_tick(self) -> None:
        if time.monotonic() >= self.next_check:
            self.check_all()

    def on_action(self, action: str, value: str) -> None:
        if action == "check":
            self.check_all()

    def check_all(self) -> None:
        self.next_check = time.monotonic() + max(5, int(self.settings.get("interval", 30) or 30))
        if self.busy.locked():
            return
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        with self.busy:
            path = str(self.settings.get("path", "/") or "/")
            timeout = max(1, min(30, int(self.settings.get("timeout", 5) or 5)))
            seen = set()
            for service, port in self.targets():
                seen.add(service.id)
                status, ms = probe(port, path, timeout)
                if not status.isdigit() and port in service.listening:
                    status = "listening (no HTTP answer)"
                ok = status.isdigit() and int(status) < 500
                self.results[service.id] = (f"http://127.0.0.1:{port}{path}", status, ms, time.strftime("%H:%M:%S"))
                if not ok and service.id not in self.failing:
                    self.failing.add(service.id)
                    if self.settings.get("alert", True):
                        self.notify(f"{service.name} isn't answering on port {port}: {status}", "warn")
                elif ok:
                    self.failing.discard(service.id)
            for sid in list(self.results):
                if sid not in seen:
                    del self.results[sid]
            self.render()

    def render(self) -> None:
        rows, tones = [], []
        for sid, (url, status, ms, when) in sorted(self.results.items()):
            name = self.services[sid].name if sid in self.services else sid
            ok = status.isdigit() and int(status) < 500
            rows.append([name, url, status, f"{ms:.0f} ms", when])
            tones.append("ok" if ok else "error")
        up = sum(1 for t in tones if t == "ok")
        self.show(ui.panel(
            "Health checks",
            ui.row(ui.stat("Answering", f"{up} / {len(rows)}", "ok" if up == len(rows) else "warn"),
                   ui.stat("Every", f"{self.settings.get('interval', 30)} s")),
            ui.table(["Service", "URL", "Status", "Time", "Checked"], rows, tones) if rows
            else ui.text("No running service has a port set up. Add one under the service's Network settings.",
                         "muted"),
            ui.button("Check now", "check"),
        ))
