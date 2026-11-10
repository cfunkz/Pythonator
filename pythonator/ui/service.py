"""The Services page: the list on the left, the selected service in the middle, the inspector on the right.
The inspector shows what really wraps the service (security stack, from its plan), its traffic with Allow, its
resources and what plugins said about it. At medium width the inspector becomes a drawer; at narrow width the page
shows one pane at a time."""

from __future__ import annotations

import time
from collections import deque
from typing import TYPE_CHECKING, Any

from PyQt6.QtCore import QPointF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import QFrame, QLineEdit, QMenu, QScrollArea, QStackedWidget, QVBoxLayout, QWidget

from pythonator.ui import theme as th
from pythonator.ui.console import Console
from pythonator.ui.kit import (
    Btn,
    Chip,
    Dot,
    Flow,
    Icon,
    InfoBtn,
    Meter,
    Panel,
    Search,
    Spark,
    Tabs,
    clear,
    esc,
    frame,
    hbox,
    label,
    lbl,
    show_if,
    splitter,
    vbox,
)
from pythonator.ui.widgets import PHASE_TEXT, PHASE_TONE, JailStack, ServiceList, group_color, mode_label, needs_you, stack_rows

if TYPE_CHECKING:
    from pythonator.ui.app import Main

TRAFFIC_CARD = {
    "off": ("Traffic: off", "This service can't open any network connection. It has its own empty network namespace."),
    "hosts": (
        "Traffic: allowed hosts only",
        "This service can only open connections to the hosts on its list. Everything else is blocked and shows up "
        "here with an Allow button.",
    ),
    "any": (
        "Traffic: anywhere on the internet",
        "This service can reach the internet. Programs on this computer (127.0.0.1), your local network and cloud "
        "metadata stay blocked.",
    ),
}

class GroupDot(Chip):
    """A group's filter: its colour only, to save room; the name is in the tooltip and read out by screen readers."""

    def __init__(self, group: str) -> None:
        super().__init__("", "line", h=22)
        self.group = group
        self.setToolTip(f"Group {group}: show only its services")
        self.setAccessibleName(f"Group {group}")

    def sizeHint(self) -> QSize:
        return QSize(22, 22)

    def paintEvent(self, e: Any) -> None:
        super().paintEvent(e)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(group_color(self.group))
        p.drawEllipse(QPointF(11, 11), 5, 5)

class ListPane(QFrame):
    def __init__(self, main: Main) -> None:
        super().__init__()
        self.setObjectName("ListPane")
        self.main = main
        self.count = Chip("0")
        self.find = Btn(ico="search", kind="ghost", size="sm", tip="Find a service", on=self._toggle_find)
        self.start_all = Btn(ico="play", kind="ghost", size="sm", tip="Start all", on=lambda: main.all_services("start"))
        self.stop_all = Btn(ico="stop", kind="ghost", size="sm", tip="Stop all", on=lambda: main.all_services("stop"))
        self.up = Btn(ico="up", kind="ghost", size="sm", tip="Move up in its group (Alt+Up, or drag it)",
                      on=lambda: self.list.move(main.current, -1))
        self.down = Btn(ico="down", kind="ghost", size="sm", tip="Move down in its group (Alt+Down, or drag it)",
                        on=lambda: self.list.move(main.current, 1))
        self.dense = Btn(ico="list", kind="ghost", size="sm", tip="Compact list: one short row per service")
        self.dense.setCheckable(True)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by name")
        show_if(self.search, False)
        self.list = ServiceList()
        self.list.picked.connect(main.pick)
        self.dense.setChecked(self.list.cards.compact)
        self.dense.toggled.connect(self.list.set_compact)
        self.search.textChanged.connect(self.list.set_text)
        self.chips: dict[str, Chip] = {}
        self.flow = Flow(8)
        chips = QWidget()
        chips.setLayout(self.flow)
        for key, text, tone in (("all", "All", "acc"), ("running", "Running", ""), ("needs", "Action required", "bad")):
            c = Chip(text, tone).on_click(lambda k=key: self.set_filter(k))
            self.chips[key] = c
            self.flow.add(c)
        self.key, self.tags = "all", set[str]()
        self.setLayout(vbox(
            frame("ListHead", hbox(lbl("Services"), 6, self.count, 1, self.start_all, self.stop_all, self.up, self.down,
                                   self.dense, self.find, spacing=3, margins=(14, 10, 8, 8))),
            hbox(self.search, margins=(14, 0, 14, 8)), hbox(chips, margins=(14, 0, 14, 10)), self.list, spacing=0,
        ))

    def _toggle_find(self) -> None:
        show_if(self.search, not self.search.isVisible())
        if self.search.isVisible():
            self.search.setFocus()
        else:
            self.search.clear()

    def set_filter(self, key: str) -> None:
        """All / Running / Action required pick one; group chips add up, so several groups show at once."""
        if key.startswith("tag:"):
            self.tags ^= {key[4:]}
        else:
            self.key = key
            self.tags = set() if key == "all" else self.tags
        fns = {"running": lambda s: s["observed"]["phase"] in ("running", "starting", "backoff"), "needs": needs_you}
        fn, tags = fns.get(self.key, lambda s: True), set(self.tags)
        self.list.set_filter(lambda s: fn(s) and (not tags or s["spec"].get("group") in tags))
        for k, c in self.chips.items():
            on = k == self.key and (k != "all" or not self.tags) or k[4:] in self.tags and k.startswith("tag:")
            c.set(c.text_, "acc" if on else {"all": "", "running": "", "needs": "bad"}.get(k, "line"))

    def update_services(self, services: list[dict[str, Any]]) -> None:
        self.count.set(str(len(services)))
        running = sum(1 for s in services if s["observed"]["phase"] in ("running", "starting", "backoff"))
        need = sum(1 for s in services if needs_you(s))
        self.start_all.setEnabled(any(s["approved"] and not s["spec"].get("schedule") for s in services) and running < len(services))
        self.stop_all.setEnabled(running > 0)
        for b in (self.up, self.down):
            b.setEnabled(len(services) > 1)
        self.chips["running"].set(f"Running {running}")
        self.chips["needs"].set(f"Action required {need}")
        show_if(self.chips["needs"], need > 0)
        for g in sorted({s["spec"].get("group") for s in services if s["spec"].get("group")}):
            if f"tag:{g}" not in self.chips:
                c = GroupDot(g).on_click(lambda k=f"tag:{g}": self.set_filter(k))
                self.chips[f"tag:{g}"] = c
                self.flow.add(c)
        self.list.update_services(services)

class Inspector(QScrollArea):
    """Security stack, Traffic, Resources and Notify cards for the selected service."""

    allow = pyqtSignal(str)

    def __init__(self, main: Main) -> None:
        super().__init__()
        self.setObjectName("Inspector")
        self.main = main
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("Inspector")
        self.os_chip = Chip("Linux", "line")
        self.jail_info = InfoBtn("Security stack", "", foot="From this machine's EnforcementPlan", ico="layers")
        self.stack = JailStack()
        self.jail = Panel("Security stack", "layers", self.jail_info, [self.os_chip], card=True)
        self.jail.box.addWidget(self.stack)
        self.live = Chip("live", "ok")
        self.traffic_info = InfoBtn("Traffic", "", foot="From this machine's EnforcementPlan", ico="globe")
        self.traffic = Panel("Traffic", "globe", self.traffic_info, [self.live], card=True)
        self.traffic_rows = QVBoxLayout()
        self.traffic_rows.setSpacing(0)
        self.traffic.box.addLayout(self.traffic_rows)
        self.res_spark = Spark("accent", 18, fill=False)
        self.res_spark.setFixedWidth(90)
        self.res = Panel("Resources", "gauge", right=[self.res_spark], card=True)
        self.cpu, self.mem, self.tasks = Meter("CPU"), Meter("Memory"), Meter("Tasks")
        for m in (self.cpu, self.mem, self.tasks):
            self.res.box.addWidget(m)
        self.notify = Panel("Notify", "bell", right=[Chip("official · sandboxed", "line", h=18)], card=True)
        self.notify_rows = QVBoxLayout()
        self.notify_rows.setSpacing(6)
        self.notify.box.addLayout(self.notify_rows)
        self.views = Panel("From plugins", "plug", card=True)
        self.views_lab = label("", "Small", wrap=True, rich=True)
        self.views.box.addWidget(self.views_lab)
        self.views.hide()
        lay = vbox(self.jail, self.traffic, self.res, self.notify, self.views, 1, spacing=12, margins=(14, 14, 14, 14))
        body.setLayout(lay)
        self.setWidget(body)
        self.cpu_hist: dict[str, deque[float]] = {}

    def show_service(self, s: dict[str, Any] | None, host_os: str) -> None:
        if s is None:
            self.stack.set_data([], "", None)
            return
        spec = s["spec"]
        self.os_chip.set({"linux": "Linux", "darwin": "macOS", "windows": "Windows"}.get(host_os, host_os))
        cmd = " ".join(([spec["runtime"]] if spec["runtime"] != "binary" else []) + list(spec["command"]))
        self.stack.set_data(stack_rows(s, host_os), cmd, s.get("refused"))
        plan = s.get("plan") or {}
        self.jail_info.set_card(
            "What really wraps this service on this machine, outermost first. Each box is enforced by the named "
            "part of the OS; a yellow box is reduced, a red one isn't enforced.",
            [(x["title"], f"{x['by']} · {x['status']}") for x in plan.get("layers", []) if x["status"] != "off"],
        )
        mode = spec["network"]["traffic"]["mode"]
        title, text = TRAFFIC_CARD[mode]
        net = next((x for x in plan.get("layers", []) if x["key"] == "network"), None)
        self.traffic_info.title = title
        self.traffic_info.set_card(text, [
            ("Enforced by", net["by"] if net else "—"), ("On Windows", "AppContainer + gate, else refused"),
            ("Not proxy-aware?", "fails closed"),
        ])
        show_if(self.live, mode == "hosts")
        self.update_resources(s)

    def set_traffic(self, s: dict[str, Any] | None, rows: list[dict[str, Any]]) -> None:
        clear(self.traffic_rows)
        if s is None:
            return
        mode = s["spec"]["network"]["traffic"]["mode"]
        if mode != "hosts":
            msg = {"off": "Traffic is off: nothing leaves this service.",
                   "any": "Anywhere on the internet. Switch to Allowed hosts to see and approve each host."}[mode]
            self.traffic_rows.addWidget(label(msg, "Small", wrap=True))
            return
        seen: dict[str, dict[str, Any]] = {}
        for ev in reversed(rows):
            seen.setdefault(ev["host"], ev)
        if not seen:
            hosts = s["spec"]["network"]["traffic"]["hosts"]
            self.traffic_rows.addWidget(label(
                "No connections yet. Allowed: " + (", ".join(hosts) if hosts else "nothing"), "Small", wrap=True))
            return
        allowed = set(s["spec"]["network"]["traffic"]["hosts"])
        for host, ev in list(seen.items())[:8]:
            when = label(time.strftime("%H:%M", time.localtime(ev["ts"])), "MonoMuted")
            when.setFixedWidth(44)
            h = label(host, "Mono")
            h.setToolTip(f"{host}:{ev['port']} · {ev['verdict']}" + (f" · {ev['why']}" if ev.get("why") else ""))
            if ev["verdict"] == "blocked" and host not in allowed and ev.get("why") == "not in the allowed hosts":
                right: QWidget = Btn("Allow", "check", "primary", "sm", on=lambda hh=host: self.allow.emit(hh))
            elif ev["verdict"] == "allowed" or host in allowed:
                right = Chip("allowed", "ok")
            else:
                right = Chip(ev["verdict"], "bad")
                right.setToolTip(ev.get("why", ""))
            row = frame("Row", hbox(when, h, 1, right, spacing=8, margins=(0, 6, 0, 6)))
            self.traffic_rows.addWidget(row)

    def update_resources(self, s: dict[str, Any]) -> None:
        ob, lim = s["observed"], s["spec"]["limits"]
        mem = ob.get("memory_bytes", 0) / 1048576
        cpu = ob.get("cpu_percent", 0.0)
        self.cpu.set(cpu / (100 * lim["cpu"]), f"{cpu:.1f} %")
        self.mem.set(mem / lim["memory_mb"], f"{mem:.0f}/{lim['memory_mb']} MB")
        self.tasks.set(ob.get("tasks", 0) / lim["tasks"], f"{ob.get('tasks', 0)}/{lim['tasks']}")
        self.res_spark.set(list(self.main.services_page.list.list.sparks.get(s["spec"]["name"], ())))

    def set_notes(self, notes: list[dict[str, Any]]) -> None:
        clear(self.notify_rows)
        if not notes:
            self.notify_rows.addWidget(label("Nothing to report.", "Small"))
        for n in notes[-3:][::-1]:
            dot = Dot({"error": "bad", "warn": "warn"}.get(n.get("level", ""), "run"))
            text = label(n["text"], wrap=True)
            text.setStyleSheet("font-size: 12px;")
            when = label(time.strftime("%H:%M", time.localtime(n["ts"])), "MonoMuted")
            self.notify_rows.addWidget(frame("", hbox(dot, text, 1, when, spacing=8)))
            if n.get("via"):
                self.notify_rows.addWidget(frame("", hbox(Icon("send", "muted", 13), label(n["via"], "Small"), 1, spacing=8)))

    def set_views(self, rows: list[tuple[str, dict[str, Any]]]) -> None:
        show_if(self.views, bool(rows))
        t = th.current()
        self.views_lab.setText("<br>".join(
            f"<span style='color:{t.faint}'>{esc(p)} · {esc(r['label'])}</span>&nbsp; {esc(r['value'])}" for p, r in rows))

class Work(QFrame):
    """The middle: title, phase, actions, enforcement chips, tabs, console and the stdin box."""

    def __init__(self, main: Main) -> None:
        super().__init__()
        self.setObjectName("Work")
        self.main = main
        self.back = Btn(ico="back", kind="ghost", tip="Services", on=main.show_list)
        self.back.hide()
        self.title = label("No services yet", "H1")
        self.phase = Chip("", "ok", "pulse")
        self.restart = Btn(ico="restart", tip="Restart (Ctrl R)", on=lambda: main.act("restart"))
        self.stop = Btn(ico="stop", kind="danger", tip="Stop", on=lambda: main.act("stop"))
        self.start = Btn(ico="play", kind="primary", tip="Start", on=lambda: main.act("start"))
        self.more = Btn(ico="more", tip="More")
        menu = QMenu(self.more)
        menu.addAction("Review plan", main.show_plan)
        menu.addAction("Try in a copy", main.try_copy)
        menu.addAction("Settings", lambda: main.open_settings())
        menu.addAction("Rename…", main.rename_service)
        menu.addSeparator()
        menu.addAction("Remove", main.remove_service)
        self.more.setMenu(menu)
        self.drawer = Btn(ico="drawer", tip="Inspector", on=main.toggle_drawer)
        self.drawer.hide()
        self.enf = Flow(8)
        enf = QWidget()
        enf.setLayout(self.enf)
        self.enf_info = InfoBtn("What protects it here", "", foot="From this machine's EnforcementPlan", ico="shield")
        self.message = label("", "Small", wrap=True)
        self.tabs = Tabs()
        self.tabs.add("console", "Console", "console")
        self.tabs.add("editor", "Editor", "code")
        self.tabs.add("network", "Network", "globe")
        self.tabs.add("settings", "Settings", "sliders")
        self.tabs.add("notify", "Notify", "bell")
        self.tabs.changed.connect(main.tab)
        self.console = Console(main.link)
        self.stdin = Search("send a line to stdin…")
        self.stdin.layout().itemAt(0).widget().set("next", "faint")  # type: ignore[union-attr]
        self.stdin.edit.returnPressed.connect(self._send)
        self.find = Btn(ico="search", tip="Search the whole log", on=self._find)
        self.save = Btn(ico="download", tip="Save the log to a file", on=main.save_log)
        self.pages = QStackedWidget()
        con = QWidget()
        con.setLayout(vbox(
            hbox(self.console, margins=(16, 12, 16, 0)),
            hbox(self.stdin, self.find, self.save, spacing=8, margins=(16, 10, 16, 12)), spacing=0,
        ))
        self.notify_page = QWidget()
        self.notify_lay = vbox(spacing=8, margins=(16, 14, 16, 14))
        self.notify_page.setLayout(self.notify_lay)
        self.pages.addWidget(con)
        self.pages.addWidget(self.notify_page)
        head = hbox(self.back, self.title, self.phase, 1, self.restart, self.stop, self.start, self.more, self.drawer,
                    spacing=10, margins=(16, 14, 16, 10))
        self.setLayout(vbox(
            head, hbox(enf, margins=(16, 0, 16, 10)), hbox(self.message, margins=(16, 0, 16, 10)), self.tabs, self.pages,
            spacing=0,
        ))
        self._enf_widget = enf

    def _send(self) -> None:
        text = self.stdin.edit.text()
        if text and self.main.current:
            self.main.link.call("stdin", self.main.after_err, name=self.main.current, text=text)
            self.stdin.edit.clear()

    def _find(self) -> None:
        from PyQt6.QtWidgets import QInputDialog

        text, ok = QInputDialog.getText(self, "Search the log", "Find lines containing:", text=self.console.find)
        if ok:
            self.console.search(text)

    def show_service(self, s: dict[str, Any] | None) -> None:
        for b in (self.restart, self.stop, self.start, self.more):
            b.setEnabled(s is not None)
        if s is None:
            self.title.setText("No services yet")
            self.phase.hide()
            self.enf.clear()
            self.message.setText("Add a folder and Pythonator runs it in an isolated environment. Nothing runs until you approve its plan.")
            return
        spec, ob = s["spec"], s["observed"]
        self.title.setText(spec["name"])
        ph = ob["phase"]
        self.phase.show()
        self.phase.set(PHASE_TEXT.get(ph, ph), PHASE_TONE.get(ph, ""), "pulse" if ph == "running" else "clock")
        self.phase.setToolTip(ob.get("message", "") or PHASE_TEXT.get(ph, ph))
        running = ph in ("running", "starting", "backoff")
        show_if(self.stop, running)
        show_if(self.start, not running)
        show_if(self.restart, running)
        self._chips(s)
        msg = ob.get("message", "")
        if s.get("refused"):
            r = s["refused"]
            msg = f"{r['code']}: {r['message']}" + (f"  Fix: {r['fix']}" if r.get("fix") else "")
        elif not s["approved"]:
            msg = "Awaiting approval. Press Start to review the plan."
        self.message.setText(msg)
        show_if(self.message, bool(msg) and ph != "running")
        tone = th.tone("warn" if (ph in ("blocked", "failed") or not s["approved"] or s.get("refused")) else "muted")
        self.message.setStyleSheet(f"color: {tone};")

    def _chips(self, s: dict[str, Any]) -> None:
        self.enf.clear()
        spec = s["spec"]
        layers = {x["key"]: x for x in (s.get("plan") or {}).get("layers", [])}

        def tone(key: str, default: str = "acc") -> str:
            st = layers.get(key, {}).get("status", "enforced")
            return {"reduced": "warn", "not-enforced": "bad"}.get(st, default)

        def tip(*keys: str) -> str:
            return "\n".join(f"{layers[k]['title']}: {layers[k]['by']} ({layers[k]['status']})"
                             + (f"\n{layers[k]['detail']}" if layers[k]["detail"] else "") for k in keys if k in layers)

        text, ico, mtone = mode_label(spec)
        chips = [(text, mtone, ico, tip("tree"))]
        extra = len(spec.get("folders") or [])
        files = "everything you can" if spec["mode"] == "full" else "folder only" if not extra else f"folder + {extra}"
        chips.append((f"Files: {files}", tone("files"), "folder", tip("files")))
        traffic = spec["network"]["traffic"]
        n = len(traffic["hosts"])
        tt = {"off": "off", "hosts": f"{n} host{'s' if n != 1 else ''}", "any": "anywhere"}[traffic["mode"]]
        chips.append((f"Traffic: {tt}", tone("network"), "out", tip("network", "loopback")))
        ports = spec["network"]["ports"]
        pt = ", ".join(f":{p['port']} {'local' if p['reach'] == 'this-computer' else 'LAN'}" for p in ports) or "closed"
        chips.append((f"Ports: {pt}", tone("ports"), "in", tip("ports")))
        lim = spec["limits"]
        chips.append((f"{lim['memory_mb']} MB · {lim['cpu']:g} CPU", tone("limits"), "cpu", tip("limits")))
        for text, tn, ic, tp in chips:
            c = Chip(text, tn, ic)
            c.setToolTip(tp)
            self.enf.add(c)
        plan = s.get("plan") or {}
        info = InfoBtn("What protects it here", plan.get("first_line", ""),
                       [(x["title"], f"{x['by']} · {x['status']}") for x in plan.get("layers", []) if x["status"] != "off"],
                       "From this machine's EnforcementPlan", "shield")
        self.enf.add(info)

    def set_notify(self, notes: list[dict[str, Any]]) -> None:
        clear(self.notify_lay)
        if not notes:
            self.notify_lay.addWidget(label("No messages for this service yet. The Notify plugin sends crashes, "
                                            "give-ups and blocked traffic to the channels you choose.", "Small", wrap=True))
        for n in notes[::-1][:50]:
            when = label(time.strftime("%H:%M:%S", time.localtime(n["ts"])), "MonoMuted")
            self.notify_lay.addWidget(frame("Row", hbox(when, label(f"{n['plugin']} · {n['text']}", wrap=True), 1,
                                                        spacing=10, margins=(0, 6, 0, 6))))
        self.notify_lay.addStretch(1)

class ServicesPage(QFrame):
    def __init__(self, main: Main) -> None:
        super().__init__()
        self.setObjectName("Page")
        self.list = ListPane(main)
        self.work = Work(main)
        self.insp = Inspector(main)
        self.split = splitter("services", (self.list, 296, 220, 560), (self.work, 800, 360, 16777215), (self.insp, 336, 280, 640))
        self.setLayout(hbox(self.split, spacing=0))
        self.mode = "wide"
        self.task: QWidget | None = None
        self.main = main

    def show_task(self, w: QWidget) -> None:
        """A page about the selected service (the plan sheet) takes the work and inspector panes; the list stays."""
        self.close_task()
        self.task = w
        self.split.insertWidget(1, w)
        self.split.setStretchFactor(1, 1)
        self.work.hide()
        self.insp.hide()
        fn = getattr(w, "layout_for", None)
        if fn:
            fn(self.width() + 56)

    def close_task(self, w: QWidget | None = None) -> None:
        if self.task is None or (w is not None and w is not self.task):
            return
        self.task.hide()
        self.task.deleteLater()
        self.task = None
        self.main.show_work = True
        self.main._layout()

    def layout_for(self, width: int, show_work: bool) -> None:
        """wide: three panes · medium (<1180): inspector as a drawer · narrow (<640): one pane."""
        mode = "narrow" if width < 640 else "medium" if width < 1180 else "wide"
        self.mode = mode
        if self.task is not None:
            show_if(self.list, mode != "narrow")
            self.work.hide()
            self.insp.hide()
            fn = getattr(self.task, "layout_for", None)
            if fn:
                fn(width)
            return
        self.list.setMinimumWidth(0 if mode == "narrow" else 220)
        self.list.setMaximumWidth(16777215 if mode == "narrow" else 560)
        if mode == "narrow":
            show_if(self.list, not show_work)
            show_if(self.work, show_work)
        else:
            self.list.show()
            self.work.show()
        if mode != "wide":
            show_if(self.insp, self.insp.property("drawer") is True and mode == "medium")
        else:
            self.insp.show()
            self.insp.setProperty("drawer", False)
        show_if(self.work.drawer, mode != "wide")
        show_if(self.work.back, False)  # the top bar has the one back button
        self.work.tabs.set_compact(mode == "narrow")

