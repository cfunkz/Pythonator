"""New service, in five steps: Folder, Mode, Network, Limits & schedule, Review plan.
The Fit check reads the folder (never runs it) and suggests the lightest mode. Mode cards say what each mode can and
can't do on this machine. The right side always says, in plain words, what the service will be able to do. Nothing
runs until you approve the plan on the last step."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QLineEdit,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pythonator.model.service import NAME_RE
from pythonator.ui import theme as th
from pythonator.ui.kit import (
    Btn,
    Chip,
    Flow,
    Icon,
    InfoBtn,
    Note,
    Panel,
    Seg,
    Toggle,
    clear,
    esc,
    frame,
    hbox,
    label,
    lbl,
    show_if,
    vbox,
)

if TYPE_CHECKING:
    from pythonator.ui.app import Main

STEPS = ("Folder", "Mode", "Network", "Limits & schedule", "Review plan")
RUNTIMES = (("python", "Python"), ("node", "Node.js"), ("binary", "Program in the folder"))

def mode_cards(host: dict[str, Any], lim: dict[str, Any]) -> list[dict[str, Any]]:
    """What each mode can and can't do on this machine, from what the background service found here."""
    os_ = host.get("os", "linux")
    eng = host.get("engines") or {}
    engines = [f"{k.capitalize()} {eng.get(k, '')}".strip() for k in host.get("engines_found") or [k for k, v in eng.items() if v]]
    limits = f"{lim['memory_mb']} MB · {lim['cpu']:g} CPU · {lim['tasks']} tasks"
    if os_ == "linux":
        sandbox = dict(tags="bubblewrap · Landlock · seccomp", ok=bool(host.get("bwrap") and host.get("seccomp")), rows=[
            ("ok", "Files: only this folder (read-write) + runtime"),
            ("ok", "Network: off, allowed hosts, or anywhere"),
            ("ok", f"Limits by the kernel: {limits}"),
            ("ok", "Starts in ~17 ms, no engine needed"),
        ], why_not="Install bubblewrap and libseccomp (System check says how).")
    elif os_ == "darwin":
        sandbox = dict(tags="Seatbelt · run marker · watchdog", ok=bool(host.get("seatbelt")), rows=[
            ("ok", "Files: only this folder (read-write) + runtime"),
            ("ok", "Network: off, allowed hosts, or anywhere"),
            ("warn", f"Limits by a watchdog: {lim['memory_mb']} MB soft, no task cap"),
            ("ok", "Starts in well under 100 ms, no engine needed"),
        ], why_not="sandbox-exec isn't available on this Mac.")
    else:
        sandbox = dict(tags="AppContainer · job object · ACL ledger", ok=bool(host.get("appcontainer")), rows=[
            ("ok", "Files: only this folder (read-write) + runtime"),
            ("warn", "Network out: off or anywhere. No ports in; allowed hosts and ports need WSL or Container"),
            ("ok", f"Limits by the kernel (job object): {limits}"),
            ("ok", "Every grant is undone when you remove it"),
        ], why_not="The AppContainer probe failed; System check has the details.")
    container = dict(tags="cap-drop ALL · no-new-privileges", ok=bool(engines), rows=[
        ("ok", "Own image, own filesystem, read-only root"),
        ("ok", "Network: off or anywhere, published ports"),
        ("warn", "~145 ms start; image pull on first run"),
        ("ok" if engines else "bad", (", ".join(engines) + " found") if engines else "No Podman or Docker found"),
    ], why_not="install Podman or Docker")
    wsl_ok = os_ == "windows" and bool(host.get("wsl"))
    wsl = dict(tags="bubblewrap in Pythonator's own distro", ok=wsl_ok and bool(host.get("wsl_ready")), rows=[
        ("ok" if os_ == "windows" else "x", "Windows only"),
        ("ok", "Same Linux jail as Sandbox, inside WSL 2"),
        ("ok", "Interop off, your drives not mounted"),
    ], why_not="not on this machine" if os_ != "windows" else "set up in Engines" if wsl_ok else "install WSL first")
    full = dict(tags="Guard can forbid it", ok=True, rows=[
        ("warn", "Can read and write everything you can"),
        ("ok", "Stop still stops every child (" + {"linux": "cgroup", "darwin": "marker", "windows": "job object"}.get(os_, "tree") + ")"),
        ("lock", "Typed confirmation; Guard can forbid it"),
    ], why_not="")
    return [
        dict(key="sandbox", title="Kernel jail on this computer", chip=("Sandbox", "m_sandbox", "shield"), **sandbox),
        dict(key="container", title="Podman (rootless) or Docker", chip=("Container", "m_container", "box"), **container),
        dict(key="wsl", title="Linux jail inside Pythonator's own distro", chip=("WSL", "m_wsl", "console"), **wsl),
        dict(key="full", title="Runs as you, still in a kernel tree", chip=("Full access", "m_full", "unlock"), **full),
    ]

class ModeCard(QFrame):
    def __init__(self, card: dict[str, Any], recommended: bool, fits: bool, on_pick: Any) -> None:
        super().__init__()
        self.key, self.ok = card["key"], card["ok"]
        self.setObjectName("Card")
        self.setCursor(Qt.CursorShape.PointingHandCursor if self.ok else Qt.CursorShape.ForbiddenCursor)
        text, tone, ico = card["chip"]
        title = label(card["title"], "H3", wrap=True)
        title.setStyleSheet("font-size: 14.5px; font-weight: 700;")
        head = hbox(Chip(text, tone, ico), spacing=8)
        head.addWidget(title, 1)
        if recommended:
            head.addWidget(Chip("Recommended", "acc", "star"), 0, Qt.AlignmentFlag.AlignTop)
        lay = vbox(head, spacing=7, margins=(14, 14, 14, 14))
        for kind, row in card["rows"]:
            ico_name, tn = {"ok": ("check", "ok"), "warn": ("warn", "warn"), "bad": ("x", "bad"), "x": ("x", "muted"),
                            "lock": ("lock", "bad")}[kind]
            lab = label(row, wrap=True)
            r = hbox(Icon(ico_name, tn, 14), spacing=8)
            r.addWidget(lab, 1)
            lay.addLayout(r)
        foot = [Chip(card["tags"], "line")]
        if not self.ok:
            foot.append(Chip(card["why_not"] if len(card["why_not"]) < 30 else "not available here", "line"))
            self.setToolTip(card["why_not"])
        elif fits:
            foot.append(Chip("fits", "acc", "check"))
        lay.addLayout(hbox(*foot, 1, spacing=6))
        self.setLayout(lay)
        self.on_pick = on_pick
        if not self.ok:
            from PyQt6.QtWidgets import QGraphicsOpacityEffect

            fx = QGraphicsOpacityEffect(self)
            fx.setOpacity(0.42)
            self.setGraphicsEffect(fx)
        self.set_on(False)

    def set_on(self, on: bool) -> None:
        t = th.current()
        if not self.ok:
            self.setStyleSheet(f"#Card {{ background: transparent; border: 1px solid {t.border}; border-radius: 8px; }}")
            return
        border = f"2px solid {t.accent}" if on else f"1px solid {t.border}"
        self.setStyleSheet(f"#Card {{ background: {t.raised}; border: {border}; border-radius: 8px; }}")

    def mousePressEvent(self, e: Any) -> None:
        if self.ok:
            self.on_pick(self.key)

class NewService(QWidget):
    def __init__(self, main: Main, spec: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.setObjectName("Page")
        self.main = main
        self.host = main.snap.get("host") or {}
        self.fit: dict[str, Any] = {}
        self.step = 0
        self.done_upto = -1
        self.mode = "sandbox"
        self.cards: dict[str, ModeCard] = {}
        self.preview: dict[str, Any] = {}

        # left: steps
        steps = frame("Steps")
        steps.setFixedWidth(230)
        sl = vbox(spacing=4, margins=(14, 20, 14, 14))
        steps.setLayout(sl)
        sl.addWidget(label("New service", "H2"))
        sl.addSpacing(10)
        self.step_btns: list[QToolButton] = []
        for i, name in enumerate(STEPS):
            b = QToolButton()
            b.setText("  " + name.replace("&", "&&"))
            b.setIconSize(QSize(24, 24))
            b.setCheckable(True)
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            b.setSizePolicy(b.sizePolicy().horizontalPolicy().Expanding, b.sizePolicy().verticalPolicy())
            b.setFixedHeight(40)
            b.clicked.connect(lambda _=False, i=i: self.go(i) if i <= self.done_upto + 1 else None)
            self.step_btns.append(b)
            sl.addWidget(b)
        sl.addStretch(1)
        sl.addWidget(Note("Nothing runs until you approve the plan on the last step."))

        # middle: the step pages
        self.title = label("Which folder?", "H1")
        self.path_hint = label("", "MonoMuted")
        self.pages = QStackedWidget()
        for build in (self._folder_page, self._mode_page, self._network_page, self._limits_page, self._review_page):
            sc = QScrollArea()
            sc.setWidgetResizable(True)
            sc.setWidget(build())
            self.pages.addWidget(sc)
        self.error = label("", wrap=True)
        self.error.setStyleSheet(f"color: {th.current().bad};")
        self.back = Btn("Back", "back", on=self.prev)
        self.trial = Btn("Try in a copy first", "play", on=self.try_copy)
        self.next = Btn("Next: Mode", "next", "primary", on=self.forward, trailing=True)
        foot = frame("WizardFoot", hbox(self.back, Btn("Cancel", kind="ghost", on=self.cancel), 1, self.trial, self.next,
                                       spacing=8, margins=(24, 12, 24, 12)))
        mid = QWidget()
        mid.setLayout(vbox(hbox(self.title, 1, self.path_hint, margins=(24, 20, 24, 6)),
                           hbox(self.error, margins=(24, 0, 24, 0)), self.pages, foot, spacing=0))

        # right: what it can do
        self.side = frame("WizardSide")
        self.side.setFixedWidth(330)
        self.side_title = label("What it can do", "H3")
        self.cando = QVBoxLayout()
        self.cando.setSpacing(8)
        self.fit_card = Panel("Fit check", "sparkle", card=True)
        self.fit_text = label("Reads your project's files through the safe reader (no links, no special files, never runs "
                              "anything): imports, requirements and wheels. It suggests the lightest mode that can run it "
                              "and the hosts it will likely need.", "Small", wrap=True)
        self.fit_rows = label("", "Small", wrap=True, rich=True)
        self.fit_card.box.addWidget(self.fit_text)
        self.fit_card.box.addWidget(self.fit_rows)
        self.side.setLayout(vbox(
            hbox(self.side_title, 1, InfoBtn("What it can do", "Worked out from your choices and this machine's "
                                             "EnforcementPlan; the plan on the last step is the exact version."), spacing=6),
            self.cando, 4, self.fit_card, 1, spacing=12, margins=(16, 20, 16, 16)))
        lay = hbox(steps, mid, self.side, spacing=0)
        lay.setStretch(1, 1)
        self.setLayout(lay)
        if spec:
            self._load(spec)
        self.go(0)

    # pages ----------------------------------------------------------------------------------------------------
    def _folder_page(self) -> QWidget:
        w = QWidget()
        self.folder = QLineEdit()
        self.folder.setPlaceholderText(str(Path.home() / "services" / "api"))
        self.folder.editingFinished.connect(self._scan)
        pick = Btn("Choose…", "folder", on=self._pick)
        self.tpl_btn = Btn("Template…", "wrench", tip="Start a new project from a starter template (Builder plugin)",
                           on=self._templates)
        self.name = QLineEdit()
        self.name.setPlaceholderText("api")
        self.command = QLineEdit()
        self.command.setProperty("mono", True)
        self.command.setPlaceholderText("main.py --port 8000")
        self.runtime = QComboBox()
        for key, text in RUNTIMES:
            self.runtime.addItem(text, key)
        self.group = QLineEdit()
        self.group.setPlaceholderText("web, workers, data, jobs… (optional)")
        self.gui = Toggle(False, "It opens windows (PyQt, Tk, Electron…)")
        self.image = QLineEdit()
        self.image.setPlaceholderText("postgres:17 (Container mode)")
        self.image.setProperty("mono", True)
        g = QGridLayout()
        g.setHorizontalSpacing(14)
        g.setVerticalSpacing(12)
        for row, (text, field) in enumerate((("Folder", hbox(self.folder, pick, self.tpl_btn, spacing=8)), ("Name", self.name),
                                              ("Command", self.command), ("Runtime", self.runtime), ("Group", self.group),
                                              ("GUI app", self.gui))):
            g.addWidget(lbl(text), row, 0)
            if isinstance(field, QWidget):
                g.addWidget(field, row, 1)
            else:
                g.addLayout(field, row, 1)
        self.fit_summary = Note("Pick the service's folder. The Fit check reads it, never runs it, and fills in the rest.")
        w.setLayout(vbox(g, self.fit_summary, 1, spacing=16, margins=(24, 14, 24, 14)))
        return w

    def _mode_page(self) -> QWidget:
        w = QWidget()
        self.fit_panel = Panel("Fit check", "sparkle", InfoBtn("Fit check", "Reads your project's files through the safe "
                               "reader and suggests the lightest mode that can run it. Advice only: you can pick any mode."),
                               [Chip("read via safe reader · nothing was run", "line", "eye")])
        grid = QGridLayout()
        grid.setSpacing(10)
        self.fit_cells: list[tuple[QWidget, Any, Any]] = []
        for i, title in enumerate(("Runtime", "Packages", "Needs network", "Listens on a port")):
            small, big = label(title, "Small"), label("—", "H3")
            cell = frame("Surface", vbox(small, big, spacing=3, margins=(12, 10, 12, 10)))
            grid.addWidget(cell, 0, i)
            self.fit_cells.append((cell, small, big))
        self.fit_panel.box.addLayout(grid)
        self.fit_verdict = label("", wrap=True, rich=True)
        self.fit_hosts = label("", "Small", wrap=True)
        self.fit_hosts.setAlignment(Qt.AlignmentFlag.AlignRight)
        vr = hbox(Icon("check", "ok"), spacing=8)
        vr.addWidget(self.fit_verdict, 3)
        vr.addWidget(self.fit_hosts, 2)
        self.fit_panel.box.addLayout(vr)
        self.mode_grid = QGridLayout()
        self.mode_grid.setSpacing(12)
        self.image_row = QWidget()
        self.engine = Seg((("auto", "Auto"), ("podman", "Podman"), ("docker", "Docker")))
        self.image2 = QLineEdit()
        self.image2.setProperty("mono", True)
        self.image2.setPlaceholderText("python:3.13-slim, postgres:17…")
        self.image2.textChanged.connect(self.image.setText)
        self.image_row.setLayout(hbox(lbl("Image"), self.image2, self.engine, spacing=10))
        self.confirm_row = QWidget()
        self.confirm = QLineEdit()
        self.confirm.setPlaceholderText("type the service's name to confirm Full access")
        self.confirm_row.setLayout(hbox(Icon("lock", "bad"), self.confirm, spacing=8))
        w.setLayout(vbox(self.fit_panel, self.mode_grid, self.image_row, self.confirm_row, 1, spacing=14, margins=(24, 8, 24, 14)))
        return w

    def _network_page(self) -> QWidget:
        w = QWidget()
        self.traffic = Seg((("off", "Off", "x"), ("hosts", "Allowed hosts", "list"), ("any", "Anywhere", "globe")), "off", accent=True)
        self.traffic.changed.connect(lambda _k: self._refresh())
        self.hosts_flow = Flow(7)
        hosts = QWidget()
        hosts.setLayout(self.hosts_flow)
        self.host_edit = QLineEdit()
        self.host_edit.setPlaceholderText("add a host: api.example.com, *.example.net or host:443")
        self.host_edit.setProperty("mono", True)
        self.host_edit.returnPressed.connect(self._add_host)
        self.hosts_box = frame("Surface", vbox(hosts, self.host_edit, spacing=8, margins=(10, 10, 10, 10)))
        self.lan = Toggle(False)
        self.lan.toggled.connect(lambda _x: self._refresh())
        self.ports: list[tuple[int, str]] = []
        self.ports_box = QVBoxLayout()
        self.port_num = QSpinBox()
        self.port_num.setRange(1024, 65535)
        self.port_num.setValue(8000)
        self.port_reach = Seg((("this-computer", "This computer"), ("lan", "Local network")))
        self.add_port = add = Btn("Open port", "plus", on=self._add_port)
        self.win_note = label("", "Small", wrap=True)
        self.win_note.setStyleSheet(f"color: {th.current().warn};")
        w.setLayout(vbox(
            self.win_note,
            label("Traffic out", "H2"),
            label("Off is the safest. Allowed hosts lets it reach only the hosts you list; every other attempt is "
                  "blocked and shows up with an Allow button.", "Small", wrap=True),
            hbox(self.traffic, 1), self.hosts_box,
            hbox(self.lan, label("Also reach my local network (printers, NAS, other computers)"), 1, spacing=10),
            8,
            label("Ports in", "H2"),
            label("Closed by default. An open port is reachable from this computer only, or from your local network.",
                  "Small", wrap=True),
            self.ports_box,
            hbox(self.port_num, self.port_reach, add, 1, spacing=8),
            1, spacing=10, margins=(24, 8, 24, 14)))
        return w

    def _limits_page(self) -> QWidget:
        w = QWidget()
        self.memory, self.cpu, self.tasks = QSpinBox(), QSpinBox(), QSpinBox()
        for sb, lo, hi, val, suf in ((self.memory, 16, 262144, 256, " MB"), (self.cpu, 10, 25600, 100, " % CPU"),
                                     (self.tasks, 4, 65536, 128, " tasks")):
            sb.setRange(lo, hi)
            sb.setValue(val)
            sb.setSuffix(suf)
            sb.valueChanged.connect(lambda _v: self._refresh())
        self.restart = Seg((("on-failure", "On failure"), ("always", "Always"), ("never", "Never")), "on-failure")
        self.give_up = QSpinBox()
        self.give_up.setRange(1, 1000)
        self.give_up.setValue(5)
        self.give_up.setSuffix(" crashes")
        self.when = Seg((("keep", "Keep it running"), ("cron", "On a schedule")), "keep")
        self.when.changed.connect(lambda _k: show_if(self.cron, self.when.value() == "cron"))
        self.cron = QLineEdit()
        self.cron.setProperty("mono", True)
        self.cron.setPlaceholderText("0 3 * * *   (every day at 03:00)")
        self.cron.hide()
        g = QGridLayout()
        g.setHorizontalSpacing(14)
        g.setVerticalSpacing(12)
        for row, (text, field) in enumerate((("Memory", self.memory), ("CPU", self.cpu), ("Tasks", self.tasks),
                                              ("Restart", hbox(self.restart, label("give up after"), self.give_up, 1, spacing=8)),
                                              ("When", hbox(self.when, self.cron, 1, spacing=8)))):
            g.addWidget(lbl(text), row, 0)
            if isinstance(field, QWidget):
                g.addWidget(field, row, 1)
            else:
                g.addLayout(field, row, 1)
        w.setLayout(vbox(label("Limits are enforced by the kernel where this machine can (System check shows how).",
                               "Small", wrap=True), g, 1, spacing=14, margins=(24, 8, 24, 14)))
        return w

    def _review_page(self) -> QWidget:
        w = QWidget()
        self.review = QVBoxLayout()
        self.review.setSpacing(0)
        self.sheet = frame("Sheet")
        self.sheet.setLayout(self.review)
        w.setLayout(vbox(self.sheet, 1, spacing=12, margins=(24, 8, 24, 14)))
        return w

    # behaviour ------------------------------------------------------------------------------------------------
    def _pick(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Service folder", self.folder.text() or str(Path.home()))
        if d:
            self.folder.setText(d)
            self._scan()

    def _templates(self) -> None:
        from PyQt6.QtWidgets import QInputDialog, QMenu

        def got(res: Any, err: Any) -> None:
            m = QMenu(self)
            if err or not res:
                a = m.addAction("Starter projects come from the Builder plugin. Open Plugins…")
                a.triggered.connect(lambda: self.main.go("plugins"))
            for t in res or []:
                a = m.addAction(f"{t['title']} — {t['detail']}")
                a.triggered.connect(lambda _=False, t=t: create(t))
            m.exec(self.tpl_btn.mapToGlobal(self.tpl_btn.rect().bottomLeft()))

        def create(t: dict[str, Any]) -> None:
            parent = QFileDialog.getExistingDirectory(self, "Where the new project goes", str(Path.home()))
            if not parent:
                return
            name, ok = QInputDialog.getText(self, "New project", "Folder and service name", text=t["id"].split("-")[-1])
            if not ok or not name.strip():
                return
            folder = str(Path(parent) / name.strip())

            def made(res: Any, err: Any) -> None:
                if err:
                    self.fit_summary.set(esc(err["message"]) + (f"<br>{esc(err['fix'])}" if err.get("fix") else ""), "bad")
                    return
                self.folder.setText(res["folder"])
                self.name.setText(res["name"])
                self._scan()

            self.main.link.call("templates.create", made, plugin=t["plugin"], template=t["id"], folder=folder, name=name.strip())

        self.main.link.call("templates.list", got)

    def _scan(self) -> None:
        folder = self.folder.text().strip()
        if not folder:
            return
        self.fit_summary.set("Reading the folder…", "info")

        def got(res: Any, err: Any) -> None:
            if err:
                self.fit_summary.set(esc(err["message"]), "bad")
                return
            self.fit = res
            if not self.name.text():
                self.name.setText(res["name"])
            if not self.command.text() and res["command"]:
                self.command.setText(shlex.join(res["command"]))
            rt = res["runtime"] if res["runtime"] in ("python", "node") else "binary"
            self.runtime.setCurrentIndex([k for k, _ in RUNTIMES].index(rt))
            if res["image"] and not self.image.text():
                self.image2.setText(res["image"])
            if res["hosts"] and not self._hosts():
                for h in res["hosts"]:
                    self._chip_host(h)
            if res["port"]:
                self.port_num.setValue(res["port"])  # suggested on the Network step; a port opens only when you click
            if res["port"] and res["mode"] == "sandbox" and self._win_sandbox_closed("sandbox"):
                h = self.main.snap.get("host") or self.host
                res["mode"] = "wsl" if h.get("wsl_ready") else "container" if h.get("engines_found") else "sandbox"
            self.gui.setChecked(bool(res.get("gui")))
            ok = ("sandbox", "container", "wsl", "full") if res.get("gui") else ("sandbox", "container", "wsl")
            self.mode = res["mode"] if res["mode"] in ok else "sandbox"
            if res["network"]:
                self.traffic.set("hosts" if res["hosts"] else "any")
            if res["database"]:
                self.group.setText(self.group.text() or "data")
            self.fit_summary.set(self._fit_html(), "ok")
            self._refresh()

        self.main.link.call("fit", got, folder=folder)

    def _fit_html(self) -> str:
        f = self.fit
        bits = [f"<b>{esc(f['runtime_label'] or 'No runtime recognised')}</b>" + (f" · {esc(f['manifest'])}" if f["manifest"] else "")]
        if f["database"]:
            bits.append(f"a {esc(f['database'])} database: Container with <span style='font-family:monospace'>{esc(f['image'])}</span>")
        bits.append(f"{f['packages']} packages" + (f", {len(f['native'])} with native code" if f["native"] else ""))
        bits.append("talks to the network" if f["network"] else "no network use found")
        if f["port"]:
            bits.append(f"listens on {f['port']}")
        if f.get("gui"):
            bits.append("opens windows")
        return " · ".join(bits) + f"<br>Suggests <b>{esc(f['mode'].capitalize())}</b>: {esc(f['why'])}."

    def _hosts(self) -> list[str]:
        return [c.text_ for c in self.hosts_flow.chips()]

    def _chip_host(self, host: str) -> None:
        c = Chip(host, "plain", mono=True, h=28)
        c.setToolTip("Click to remove")
        c.setCursor(Qt.CursorShape.PointingHandCursor)
        c.clicked.connect(lambda c=c: (self.hosts_flow.removeWidget(c), c.deleteLater(), self._refresh()))
        self.hosts_flow.add(c)

    def _add_host(self) -> None:
        h = self.host_edit.text().strip().lower()
        if h and h not in self._hosts():
            self._chip_host(h)
        self.host_edit.clear()
        self._refresh()

    def _add_port(self) -> None:
        p = self.port_num.value()
        if all(p != x for x, _ in self.ports):
            self.ports.append((p, self.port_reach.value()))
        self._render_ports()
        self._refresh()

    def _render_ports(self) -> None:
        clear(self.ports_box)
        for port, reach in self.ports:
            rm = Btn(ico="x", kind="ghost", size="sm", tip="Close this port",
                     on=lambda p=port: (self.ports.__setitem__(slice(None), [x for x in self.ports if x[0] != p]),
                                        self._render_ports(), self._refresh()))
            text = f":{port} reachable from {'this computer' if reach == 'this-computer' else 'your local network'}"
            self.ports_box.addWidget(frame("Surface", hbox(Icon("in", "acc"), label(text, "Mono"), 1, rm, spacing=8,
                                                             margins=(10, 4, 6, 4))))

    def _mode_cards(self) -> None:
        self.host = self.main.snap.get("host") or self.host  # what the background service sees now, as Engines shows
        clear(self.mode_grid)
        self.cards = {}
        suggested = self.fit.get("mode", "sandbox")
        for i, c in enumerate(mode_cards(self.host, self._limits())):
            card = ModeCard(c, c["key"] == suggested, bool(self.fit) and c["key"] == suggested, self._set_mode)
            self.cards[c["key"]] = card
            self.mode_grid.addWidget(card, i // 2, i % 2)
        if self.mode in self.cards and not self.cards[self.mode].ok:
            self.mode = next((k for k, c in self.cards.items() if c.ok), "full")
        self._set_mode(self.mode)

    def _set_mode(self, key: str) -> None:
        self.mode = key
        for k, c in self.cards.items():
            c.set_on(k == key)
        show_if(self.image_row, key == "container")
        if key == "container" and not self.image2.text().strip():
            from pythonator.policy import runtimes

            rt = runtimes.RUNTIMES.get(self.runtime.currentData() or "")
            if rt and rt.image:
                self.image2.setText(rt.image)  # the runtime's official image; change it if you need another
        show_if(self.confirm_row, key == "full")
        self._refresh()

    def _win_sandbox_closed(self, mode: str = "") -> bool:
        """Windows Sandbox (an AppContainer) can't take ports in or an allow-list without an administrator's
        firewall change, which Pythonator never makes. Say so where you choose, not at the last step."""
        return (self.main.snap.get("host") or self.host).get("os") == "windows" and (mode or self.mode) == "sandbox"

    def _limits(self) -> dict[str, Any]:
        return {"memory_mb": self.memory.value(), "cpu": self.cpu.value() / 100, "tasks": self.tasks.value()}

    def spec(self) -> dict[str, Any]:
        spec: dict[str, Any] = {
            "name": self.name.text().strip(),
            "folder": self.folder.text().strip(),
            "command": shlex.split(self.command.text()) if self.command.text().strip() else [],
            "mode": self.mode,
            "runtime": self.runtime.currentData(),
            "group": self.group.text().strip().lower(),
            "gui": self.gui.isChecked(),
            "limits": self._limits(),
            "restart": {"policy": self.restart.value(), "give_up_after": self.give_up.value()},
            "network": {
                "traffic": {"mode": self.traffic.value(), "hosts": self._hosts() if self.traffic.value() == "hosts" else [],
                            "allow_lan": self.lan.isChecked()},
                "ports": [{"port": p, "reach": r} for p, r in self.ports],
            },
        }
        if self.when.value() == "cron" and self.cron.text().strip():
            spec["schedule"] = {"cron": self.cron.text().strip()}
        if self.mode == "container":
            spec["container"] = {"image": self.image2.text().strip(), "engine": self.engine.value()}
        return spec

    def _refresh(self) -> None:
        """The right side: what this service will be able to do, in plain words."""
        clear(self.cando)
        name = self.name.text() or "it"
        self.side_title.setText(f"What {name} can do")
        folder = self.folder.text() or "its folder"
        home = self.host.get("home", "")
        if home and folder.startswith(home):
            folder = "~" + folder[len(home):]
        if len(folder) > 40:
            folder = folder[:14] + "…" + folder[-24:]
        closed = self._win_sandbox_closed()
        self.win_note.setText("On Windows, Sandbox mode can't open ports or use Allowed hosts: Windows blocks both for an "
                              "AppContainer unless an administrator changes the firewall. Choose WSL or Container mode "
                              "on the Mode step for a web service." if closed else "")
        show_if(self.win_note, closed)
        for wdg in (self.port_num, self.port_reach, self.add_port):
            wdg.setEnabled(not closed)
        mode, traffic = self.mode, self.traffic.value()
        hosts = self._hosts()
        lim = self._limits()
        rows: list[tuple[str, str, str]] = []
        if mode == "full":
            rows.append(("warn", "warn", "Read and write everything you can, including your keys and browser data"))
        else:
            rows.append(("folder", "muted", f"Read and write {folder}, nothing else in your home"))
        if traffic == "off":
            rows.append(("x", "bad", "Open any network connection"))
        elif traffic == "hosts":
            rows.append(("globe", "muted", f"Reach {len(hosts)} host{'s' if len(hosts) != 1 else ''}" +
                         (" (you confirm on the next step)" if self.step < 2 else ": " + ", ".join(hosts[:4]))))
        else:
            rows.append(("globe", "warn", "Reach anywhere on the internet" + ("" if self.lan.isChecked() else ", not your local network")))
        for p, r in self.ports:
            rows.append(("in", "muted", f"Accept connections on :{p} from {'this computer' if r == 'this-computer' else 'your local network'}"))
        if mode != "full":
            rows.append(("x", "bad", "Reach programs on this computer (127.0.0.1)"))
            rows.append(("x", "bad", "See other processes, your keys, SSH or browser data"))
        by = "a watchdog" if self.host.get("os") == "darwin" and mode == "sandbox" else "the kernel"
        rows.append(("cpu", "acc", f"{lim['memory_mb']} MB, {lim['cpu']:g} CPU, {lim['tasks']} tasks, enforced by {by}"))
        for ico, tone, text in rows:
            lab = label(text, wrap=True)
            lab.setStyleSheet("font-size: 12.5px;")
            row = QWidget()
            lay = hbox(spacing=8)
            lay.addWidget(Icon(ico, "accent_text" if tone == "acc" else tone), 0, Qt.AlignmentFlag.AlignTop)
            lay.addWidget(lab, 1)
            row.setLayout(lay)
            self.cando.addWidget(row)
        f = self.fit
        self.fit_rows.setText(
            f"<table cellspacing=4><tr><td style='color:{th.current().muted}'>Took&nbsp;&nbsp;</td>"
            f"<td style='font-family:monospace'>{f.get('took_ms', 0):g} ms · {f.get('files', 0)} files</td></tr>"
            f"<tr><td style='color:{th.current().muted}'>Advice only&nbsp;&nbsp;</td><td style='font-family:monospace'>you can pick any mode</td></tr></table>"
            if f else "")
        self._fill_fit()

    def _fill_fit(self) -> None:
        f = self.fit
        if not f:
            return
        vals = [
            (f["runtime_label"] or "—", f["manifest"]),
            (str(f["packages"]), f"{len(f['native'])} with C code" if f["native"] else "pure"),
            ("yes" if f["network"] else "no", f["network_file"]),
            ("yes" if f["listens"] else "no", f":{f['port']}" if f["port"] else ""),
        ]
        t = th.current()
        for (_cell, _small, big), (v, sub) in zip(self.fit_cells, vals, strict=True):
            big.setTextFormat(Qt.TextFormat.RichText)
            big.setText(f"<b>{esc(v)}</b>" + (f" <span style='color:{t.muted};font-weight:400'>· {esc(sub)}</span>" if sub else ""))
        self.fit_verdict.setText(f"Fits <b>{esc(f['mode'].capitalize())}</b>: {esc(f['why'])}")
        self.fit_hosts.setText(("Suggested hosts: " + ", ".join(f["hosts"][:4])) if f["hosts"] else "")

    # navigation -----------------------------------------------------------------------------------------------
    def _check(self) -> str:
        if self.step == 0:
            if not self.folder.text().strip():
                return "Pick the service's folder."
            if not NAME_RE.fullmatch(self.name.text().strip()):
                return "Name: lowercase letters, digits and '-' only."
            try:
                if not shlex.split(self.command.text()):
                    return "Command: what should it run? For example main.py"
            except ValueError as e:
                return f"Command: {e}"
        if self.step == 1:
            if self.mode == "container" and not self.image2.text().strip():
                return "Container mode needs an image, for example python:3.13-slim."
            if self.mode == "full" and self.confirm.text().strip() != self.name.text().strip():
                return "Full access runs as you. Type the service's name to confirm."
        if self.step == 2 and self._win_sandbox_closed():
            if self.ports:
                return "Close the port, or go back and choose WSL or Container mode: Windows blocks ports into Sandbox."
            if self.traffic.value() == "hosts":
                return "Choose Off or Anywhere, or go back and choose WSL mode: Allowed hosts needs WSL on Windows."
        if self.step == 3 and self.when.value() == "cron" and not self.cron.text().strip():
            return "Schedule: write when it should run, for example 0 3 * * *."
        return ""

    def go(self, i: int) -> None:
        self.step = i
        self.pages.setCurrentIndex(i)
        self.title.setText(["Which folder?", f"How should {self.name.text() or 'it'} run?", "What may it reach?",
                            "Limits and schedule", "Review the plan"][i])
        folder = self.folder.text()
        home = self.host.get("home", "")
        from PyQt6.QtGui import QFontMetrics

        hint = "~" + folder[len(home):] if home and folder.startswith(home) else folder
        self.path_hint.setToolTip(folder)
        self.path_hint.setText(QFontMetrics(self.path_hint.font()).elidedText(hint, Qt.TextElideMode.ElideMiddle, 300))
        t = th.current()
        for j, b in enumerate(self.step_btns):
            b.setChecked(j == i)
            done = j <= self.done_upto and j != i
            b.setIcon(_step_icon(j + 1, "done" if done else "on" if j == i else "", t))
            b.setStyleSheet(f"QToolButton {{ background: {t.raised2 if j == i else 'transparent'}; color: "
                            f"{t.text if j == i else t.muted}; border: 0; border-radius: 6px; text-align: left; padding: 0 10px; }}")
        self.back.setEnabled(i > 0)
        show_if(self.trial, i in (1, 2, 3))
        self.next.setText(f"Next: {STEPS[i + 1]}".replace("&", "&&") if i < 4 else "Approve and start")  # & is a mnemonic
        self.next.set_icon("next" if i < 4 else "check")
        self.error.setText("")
        self.next.setEnabled(True)  # the review step disables it for a refused plan; any other step starts fresh
        if i == 1:
            self._mode_cards()
        if i == 4:
            self._load_review()
        self._refresh()

    def forward(self) -> None:
        err = self._check()
        if err:
            self.error.setText(err)
            return
        if self.step < 4:
            self.done_upto = max(self.done_upto, self.step)
            self.go(self.step + 1)
        else:
            self._save(start=True)

    def prev(self) -> None:
        if self.step > 0:
            self.go(self.step - 1)

    def cancel(self) -> None:
        self.main.close_overlay_page(self)

    def try_copy(self) -> None:
        from pythonator.ui.plan import TryCopy

        def allow(hosts: list[str]) -> None:
            for h in hosts:
                if h not in self._hosts():
                    self._chip_host(h)
            self.traffic.set("hosts")
            self.go(2)

        TryCopy(self.main, {"spec": {**self.spec(), "_unsaved": True}}, on_allow=allow).exec()

    def _load_review(self) -> None:
        clear(self.review)
        self.review.addWidget(label("Compiling the plan…", "Small"))

        def got(res: Any, err: Any) -> None:
            clear(self.review)
            if err:
                self.error.setText(err["message"] + (f"  Fix: {err['fix']}" if err.get("fix") else ""))
                return
            self.preview = res
            from pythonator.ui.plan import plan_rows

            for w in plan_rows(res, new=True):
                self.review.addWidget(w)
            self.next.setEnabled(not res.get("refused"))

        self.main.link.call("preview", got, spec=self.spec())

    def _save(self, start: bool) -> None:
        def saved(res: Any, err: Any) -> None:
            if err:
                self.error.setText(err["message"] + (f"  Fix: {err['fix']}" if err.get("fix") else ""))
                return
            name = res["spec"]["name"]

            def approved(_r: Any, e: Any) -> None:
                if e:
                    self.error.setText(e["message"])
                    return
                self.main.current = ""
                self.main.close_overlay_page(self)
                self.main.pick(name)  # watch its log before it starts, so the console shows the first line
                if start:
                    self.main.link.call("start", self.main.after_err, name=name)
                self.main.snapshot()

            self.main.link.call("approve", approved, name=name, hash=res["hash"])

        self.main.link.call("apply", saved, spec=self.spec())

    def _load(self, spec: dict[str, Any]) -> None:
        self.folder.setText(spec["folder"])
        self.name.setText(spec["name"])
        self.name.setReadOnly(True)
        self.command.setText(shlex.join(spec["command"]))
        self.mode = spec["mode"]
        self.done_upto = 3

def _step_icon(n: int, state: str, t: th.Tokens) -> Any:
    """The round step number (one of the few round things): done = tinted check, on = accent ring."""
    from PyQt6.QtCore import QPointF, QRectF
    from PyQt6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

    from pythonator.ui.icons import pixmap

    pm = QPixmap(48, 48)
    pm.setDevicePixelRatio(2)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    col = t.ok if state == "done" else t.accent if state == "on" else t.border_strong
    p.setPen(QPen(QColor(col), 1.5))
    p.setBrush(th.mix(t.ok, t.bg, 0.18) if state == "done" else Qt.BrushStyle.NoBrush)
    p.drawEllipse(QPointF(12, 12), 11, 11)
    if state == "done":
        p.drawPixmap(QRectF(6, 6, 12, 12).toRect(), pixmap("check", t.ok, 12))
    else:
        p.setPen(QColor(t.accent_text if state == "on" else t.muted))
        p.setFont(th.mono_font(11, 700))
        p.drawText(QRectF(0, 0, 24, 24), Qt.AlignmentFlag.AlignCenter, str(n))
    p.end()
    return QIcon(pm)
