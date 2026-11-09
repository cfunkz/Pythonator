"""Screen 4: a service's settings. Sections on the left, the section in the middle, and on the right what is
enforced on this machine (and on macOS, Windows or in Podman: the same compiler, a reference machine) with Guard's
verdict, recompiled as you type. Nothing changes until Save; a save that widens access goes to the plan sheet."""

from __future__ import annotations

import copy
import shlex
import socket
import time
from typing import TYPE_CHECKING, Any

from PyQt6.QtCore import QSize, Qt, QTimer
from PyQt6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QInputDialog,
    QLineEdit,
    QMessageBox,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pythonator.policy.runtimes import RUNTIMES
from pythonator.ui import theme as th
from pythonator.ui.icons import icon
from pythonator.ui.kit import (
    Btn,
    Chip,
    Flow,
    Icon,
    InfoBtn,
    Note,
    Seg,
    Toggle,
    clear,
    esc,
    frame,
    hbox,
    label,
    lbl,
    primary,
    show_if,
    splitter,
    vbox,
)

if TYPE_CHECKING:
    from pythonator.ui.app import Main

SECTIONS = (
    ("general", "General", "sliders"), ("isolation", "Isolation", "shield"), ("network", "Network", "globe"),
    ("limits", "Limits", "cpu"), ("schedule", "Schedule", "calendar"), ("variables", "Variables", "key"),
    ("git", "Git", "git"), ("health", "Health check", "pulse"),
)
OSES = (("linux", "Linux", "monitor"), ("darwin", "macOS", ""), ("windows", "Windows", ""), ("podman", "Podman", "box"))
MODES = (("sandbox", "Sandbox", "shield"), ("container", "Container", "box"), ("wsl", "WSL", "console"),
         ("full", "Full access", "unlock"))
TRAFFIC_INFO = (
    "Traffic out",
    "Off: no network at all. Allowed hosts: only the hosts you list, through the egress gate; every attempt is logged "
    "and a blocked one gets an Allow button. Anywhere: the internet, but never this computer's 127.0.0.1 or cloud "
    "metadata.",
)



def section_head(title: str, ico: str, info: tuple[str, str] | None = None, sub: str = "", right: QWidget | None = None) -> QWidget:
    head = hbox(Icon(ico, "text", 16), label(title, "H2"), spacing=9)
    if info:
        head.addWidget(InfoBtn(info[0], info[1], ico=ico))
    head.addStretch(1)
    if right is not None:
        head.addWidget(right)
    box = vbox(head, spacing=4)
    if sub:
        box.addWidget(label(sub, "Muted", wrap=True))
    return frame("", box)

def port_status(port: int) -> tuple[str, str]:
    """free / in use, by trying to bind it on this computer (the service's own run holds it through the gate)."""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return "free", "ok"
    except OSError:
        return "in use", "warn"
    finally:
        s.close()

class Field(QGridLayout):
    """A compact form: the label, then its field with the short hint beside it, all on one line."""

    def __init__(self) -> None:
        super().__init__()
        self.setHorizontalSpacing(14)
        self.setVerticalSpacing(8)
        self.setColumnMinimumWidth(0, 120)
        self.setColumnStretch(0, 0)
        self.setColumnStretch(1, 1)

    def row(self, text: str, w: QWidget | Any, hint: str = "") -> None:
        r = self.rowCount()
        self.addWidget(lbl(text), r, 0, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        line = hbox(spacing=12)
        fixed = isinstance(w, Seg | Toggle) or (isinstance(w, QWidget) and w.maximumWidth() < 2000)
        if isinstance(w, QWidget):
            line.addWidget(w, 0 if fixed else 1)
        else:
            line.addLayout(w, 1)
        if hint:
            h = label(hint, "Small", wrap=True)
            h.setMinimumWidth(240)
            h.setMaximumWidth(420 if fixed else 300)
            line.addWidget(h)
        if fixed:
            line.addStretch(1)
        self.addLayout(line, r, 1)

class SettingsPage(QWidget):
    def __init__(self, main: Main, s: dict[str, Any], section: str = "general") -> None:
        super().__init__()
        self.setObjectName("Page")
        self.main = main
        self.s = s
        self.spec: dict[str, Any] = copy.deepcopy(s["spec"])
        self.name = self.spec["name"]
        self.host = main.snap.get("host") or {}
        self.os = self.host.get("os", "linux")
        self.dirty = False
        self.last: dict[str, Any] = {}
        self._timer = QTimer(self, singleShot=True, interval=300, timeout=self._recompile)

        # left: sections ------------------------------------------------------------------------------------------
        nav = frame("SideNav")
        nav.setFixedWidth(220)
        nl = vbox(spacing=4, margins=(12, 16, 12, 14))
        nav.setLayout(nl)
        back = Btn(self.name, "back", "ghost", on=self.close_page, tip="Back to the service")
        back.setStyleSheet("font-size: 15px; font-weight: 700; text-align: left;")
        nl.addWidget(back)
        nl.addSpacing(10)
        self.nav_group = QButtonGroup(self)
        self.nav_btns: dict[str, QToolButton] = {}
        for i, (key, text, ico) in enumerate(SECTIONS):
            b = QToolButton()
            b.setText(f"  {text}")
            b.setProperty("ico", ico)
            b.setCheckable(True)
            b.setIconSize(QSize(17, 17))
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            b.setFixedHeight(38)
            b.setSizePolicy(b.sizePolicy().horizontalPolicy().Expanding, b.sizePolicy().verticalPolicy())
            b.clicked.connect(lambda _=False, i=i: self.show_section(i))
            self.nav_group.addButton(b)
            self.nav_btns[key] = b
            nl.addWidget(b)
        self.net_badge = Chip("", "bad", h=18)
        self.net_badge.setParent(self.nav_btns["network"])
        self.net_badge.hide()
        nl.addStretch(1)
        nl.addWidget(Note("Changes are compiled into a new plan. Nothing changes until you save."))

        # middle: the sections ---------------------------------------------------------------------------------------
        self.pages = QStackedWidget()
        builders = (self._general, self._isolation, self._network, self._limits, self._schedule, self._variables,
                    self._git, self._health)
        for b in builders:
            inner = QWidget()
            lay = vbox(spacing=16, margins=(28, 22, 28, 28))
            b(lay)
            lay.addStretch(1)
            inner.setLayout(lay)
            sc = QScrollArea()
            sc.setWidgetResizable(True)
            sc.setFrameShape(QFrame.Shape.NoFrame)
            sc.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            sc.setWidget(inner)
            self.pages.addWidget(sc)
        self.toast = frame("Sheet")
        self.toast_text = label("", rich=True)
        self.toast_undo = Btn("Undo", "undo", size="sm", on=self._undo_allow)
        self.toast_restart = Btn("Restart", "restart", "primary", "sm",
                                 on=lambda: (self.main.act("restart", self.name), self.toast.hide()))
        self.toast.setLayout(hbox(Icon("check", "ok", 16), self.toast_text, 1, self.toast_undo, self.toast_restart,
                                  spacing=10, margins=(14, 10, 12, 10)))
        self.toast.hide()
        mid = QWidget()
        ml = vbox(self.pages, spacing=0)
        tl = hbox(1, self.toast, margins=(28, 0, 28, 16))
        ml.addLayout(tl)
        mid.setLayout(ml)

        # right: enforced on this machine -----------------------------------------------------------------------
        side = frame("SidePanel")
        self.os_seg = Seg([(k, t, i) if i else (k, t) for k, t, i in OSES], self.os if self.os != "linux" else "linux")
        self.os_seg.changed.connect(lambda _k: self._recompile())
        self.rows = QVBoxLayout()
        self.rows.setSpacing(0)
        self.guard = Note("", "info", "shield")
        self.save = primary("Save && recompile", "check", self.save_spec)
        cancel = Btn("Cancel", on=self.close_page)
        info = InfoBtn("Enforced on this machine", "What the compiler turns these settings into, layer by layer, and "
                       "which part of the OS enforces each. The other tabs show the same spec compiled for a typical "
                       "Mac, Windows PC or rootless Podman, so you can see what would change.",
                       foot="Same compiler, golden-tested per OS", ico="layers")
        sl = vbox(hbox(label("Enforced on this machine", "H2"), info, 1, spacing=8), 6, self.os_seg, 4, self.rows, 1,
                  hbox(Icon("shield", "muted", 13), lbl("Guard policy"), 1, spacing=6), self.guard, 10,
                  hbox(1, cancel, self.save, spacing=8), spacing=8, margins=(20, 20, 20, 20))
        side.setLayout(sl)

        self.nav, self.side = nav, side
        self.setLayout(hbox(nav, splitter("settings", (mid, 700, 360, 16777215), (side, 380, 300, 680)), spacing=0))
        self.layout().setStretch(1, 1)  # type: ignore[union-attr]
        idx = next((i for i, (k, _t, _i) in enumerate(SECTIONS) if k == section), 0)
        self.show_section(idx)
        self.retheme()
        self._recompile()
        self.main.link.call("traffic", self._got_traffic, name=self.name)

    # framing ------------------------------------------------------------------------------------------------------
    def retheme(self) -> None:
        t = th.current()
        for b in self.nav_btns.values():
            b.setIcon(icon(str(b.property("ico")), t.text if b.isChecked() else t.muted, 17))

    def layout_for(self, w: int) -> None:
        show_if(self.side, w >= 1180)
        self.nav.setFixedWidth(220 if w >= 900 else 64)

    def show_section(self, i: int) -> None:
        if SECTIONS[i][0] == "git" and not self.git_loaded:
            self.git_loaded = True
            self._load_git()
        self.pages.setCurrentIndex(i)
        list(self.nav_btns.values())[i].setChecked(True)
        self.retheme()

    def changed(self) -> None:
        self.dirty = True
        self._timer.start()

    def close_page(self) -> None:
        if self.dirty and QMessageBox.question(self, "Settings", "Leave without saving your changes?") != \
                QMessageBox.StandardButton.Yes:
            return
        self.main.close_overlay_page(self)

    # sections -----------------------------------------------------------------------------------------------------
    def _general(self, lay: QVBoxLayout) -> None:
        sp = self.spec
        lay.addWidget(section_head("General", "sliders", sub="What runs, from which folder."))
        f = Field()
        self.folder = QLineEdit(sp["folder"])
        self.folder.setProperty("mono", True)
        self.folder.textEdited.connect(lambda _t: self.changed())
        pick = Btn("Choose…", "folder", on=self._pick_folder)
        f.row("Folder", hbox(self.folder, pick, spacing=8), "Read-write for the service. Nothing else of your home.")
        self.command = QLineEdit(shlex.join(sp["command"]))
        self.command.setProperty("mono", True)
        self.command.textEdited.connect(lambda _t: self.changed())
        self.runtime = QComboBox()
        for k, rt in RUNTIMES.items():
            self.runtime.addItem(rt.label, k)
        self.runtime.setCurrentIndex(max(0, self.runtime.findData(sp["runtime"])))
        self.runtime.currentIndexChanged.connect(lambda _i: (self._runtime_hint(), self.changed()))
        self.rt_hint = label("", "Small", wrap=True)
        f.row("Command", self.command)
        f.row("Runtime", vbox(self.runtime, self.rt_hint, spacing=4))
        self.pyver = QLineEdit(sp.get("runtime_version", ""))
        self.pyver.setPlaceholderText("3.13 (installed by uv), empty for this computer's Python")
        self.pyver.textEdited.connect(lambda _t: self.changed())
        f.row("Python version", self.pyver)
        self.group = QLineEdit(sp.get("group", ""))
        self.group.setPlaceholderText("web, workers, data, jobs… (optional)")
        self.group.textEdited.connect(lambda _t: self.changed())
        f.row("Group", self.group)
        self.gui = Toggle(bool(sp.get("gui")), "GUI app")
        self.gui.toggled.connect(lambda _on: self.changed())
        f.row("GUI app", self.gui, "It opens windows (PyQt, Tk, Electron…). Full access, or Sandbox on Windows.")
        lay.addLayout(f)
        self._runtime_hint()

    def _runtime_hint(self) -> None:
        k = self.runtime.currentData()
        rt = RUNTIMES[k]
        self.pyver.setEnabled(k == "python")
        self.rt_hint.setText(f"For example: {rt.example}" + (f" · the command is {rt.label}'s arguments" if k not in
                                                              ("python", "node", "binary", "ruby", "powershell") else ""))

    def _pick_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Service folder", self.folder.text())
        if d:
            self.folder.setText(d)
            self.changed()

    def _isolation(self, lay: QVBoxLayout) -> None:
        sp = self.spec
        lay.addWidget(section_head("Isolation", "shield", ("Modes", "Sandbox: kernel-enforced isolation on this computer. Container: "
                                   "its own image in rootless Podman or Docker. WSL: the Sandbox isolation inside Pythonator's own "
                                   "Linux on Windows. Full access: runs as you, still in a process tree Stop can end."),
                                   sub="How it is kept apart from the rest of your computer."))
        self.mode = Seg(MODES, sp["mode"], accent=True)
        self.mode.changed.connect(lambda _k: (self._mode_fields(), self.changed()))
        lay.addWidget(self.mode)
        f = Field()
        c = sp.get("container") or {}
        self.image = QLineEdit(c.get("image", ""))
        self.image.setProperty("mono", True)
        self.image.setPlaceholderText(RUNTIMES[sp["runtime"]].image or "postgres:17")
        self.image.textEdited.connect(lambda _t: self.changed())
        self.engine = Seg((("auto", "Auto"), ("podman", "Podman"), ("docker", "Docker")), c.get("engine", "auto"))
        self.engine.changed.connect(lambda _k: self.changed())
        self.ctr_box = QWidget()
        cf = Field()
        cf.row("Image", self.image, "An official image such as python:3.13-slim, or one you trust.")
        cf.row("Engine", self.engine)
        self.ctr_box.setLayout(cf)
        lay.addWidget(self.ctr_box)
        self.confirm = QLineEdit()
        self.confirm.setPlaceholderText(f"type {self.name} to confirm")
        self.confirm.textEdited.connect(lambda _t: self.changed())
        self.full_box = QWidget()
        self.full_box.setLayout(vbox(Note("Full access runs as you: it can read and write everything you can. Stop still "
                                          "ends every process it starts. Guard can forbid it.", "warn"), self.confirm, spacing=8))
        lay.addWidget(self.full_box)
        lay.addWidget(section_head("Extra folders", "folder", sub="Other folders it may read (or write). Your keys, "
                                   "browser data and Pythonator's own data are refused whatever you pick."))
        self.folder_rows = QVBoxLayout()
        self.folder_rows.setSpacing(6)
        lay.addLayout(self.folder_rows)
        lay.addWidget(Btn("Add folder", "plus", "ghost", on=self._add_folder), 0, Qt.AlignmentFlag.AlignLeft)
        lay.addLayout(f)
        for g in sp.get("folders", []):
            self._folder_row(g["path"], g["write"])
        self._mode_fields()

    def _mode_fields(self) -> None:
        m = self.mode.value()
        show_if(self.ctr_box, m == "container")
        show_if(self.full_box, m == "full" and self.s["spec"]["mode"] != "full")

    def _folder_row(self, path: str, write: bool) -> None:
        row = QWidget()
        tog = Toggle(write, "Read-write")
        tog.toggled.connect(lambda _on: self.changed())
        rm = Btn(ico="trash", kind="ghost", tip="Remove")
        row.setLayout(hbox(Icon("folder", "muted"), label(path, "Mono"), 1, label("read-write", "Small"), tog, rm, spacing=8))
        row.setProperty("kind", "folder")
        row.setProperty("path", path)
        row.setProperty("tog", tog)
        rm.clicked.connect(lambda _=False: (row.hide(), row.deleteLater(), self.changed()))
        self.folder_rows.addWidget(row)

    def _add_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Extra folder")
        if d:
            self._folder_row(d, False)
            self.changed()

    def _network(self, lay: QVBoxLayout) -> None:
        t = self.spec["network"]["traffic"]
        lay.addWidget(section_head("Traffic out", "out", TRAFFIC_INFO,
                                   "Where this service may connect. Blocked attempts appear below with an Allow button."))
        self.traffic = Seg((("off", "Off", "x"), ("hosts", "Allowed hosts", "list"), ("any", "Anywhere", "globe")),
                           t["mode"], accent=True)
        self.traffic.changed.connect(lambda _k: (self._traffic_fields(), self.changed()))
        lay.addWidget(self.traffic, 0, Qt.AlignmentFlag.AlignLeft)
        self.hosts_flow = Flow(7)
        hw = QWidget()
        hw.setLayout(self.hosts_flow)
        self.host_edit = QLineEdit()
        self.host_edit.setObjectName("Bare")
        self.host_edit.setPlaceholderText("add host, *.domain or host:port")
        self.host_edit.returnPressed.connect(self._add_host)
        add = Btn("Add", "plus", size="sm", on=self._add_host)
        self.hosts_box = frame("Surface", vbox(hw, hbox(self.host_edit, add, spacing=8), spacing=8, margins=(10, 10, 10, 10)))
        lay.addWidget(self.hosts_box)
        for h in t["hosts"]:
            self._host_chip(h)
        self.lan = Toggle(t.get("allow_lan", False))
        self.lan.toggled.connect(lambda _on: self.changed())
        lan_info = InfoBtn("Local network", "Off by default: printers, NAS boxes, routers and other computers on your network "
                           "are unreachable. This computer's own 127.0.0.1 and cloud metadata stay blocked even when it "
                           "is on.", [("Linux", "pasta --no-map-gw + gate"), ("macOS", "Seatbelt + gate"),
                                      ("Windows", "AppContainer capability")], ico="shield")
        lay.addLayout(hbox(self.lan, label("Let it reach your local network (printers, NAS, other computers)"),
                           Chip("off by default", "warn"), lan_info, 1, spacing=10))
        self.live = Chip("live", "ok", "pulse")
        lay.addWidget(section_head("Recent attempts", "history", ("Recent attempts", "The last connections this service "
                                   "tried through the gate. Allow adds that one host; your click approves exactly that line."),
                                   right=self.live))
        self.attempts = QVBoxLayout()
        self.attempts.setSpacing(0)
        self.attempts_box = frame("Card", self.attempts)
        lay.addWidget(self.attempts_box)
        lay.addWidget(section_head("Ports in", "in", ("Ports in", "No ports listed means the service can't listen at all. "
                                   "Each port goes through the gate (Sandbox), pasta, Seatbelt bind rules, AppContainer or the "
                                   "engine; PORT is passed to the program."),
                                   "No ports listed means the service can't listen at all. PORT is passed to the program."))
        head = hbox(lbl("Port"), 70, lbl("Who can connect"), 1, lbl("Status"), 90, spacing=8, margins=(12, 0, 12, 0))
        lay.addLayout(head)
        self.port_rows = QVBoxLayout()
        self.port_rows.setSpacing(0)
        lay.addLayout(self.port_rows)
        for p in self.spec["network"]["ports"]:
            self._port_row(p["port"], p.get("reach", "this-computer"))
        lay.addWidget(Btn("Add port", "plus", "ghost", on=self._add_port), 0, Qt.AlignmentFlag.AlignLeft)
        self._traffic_fields()

    def _traffic_fields(self) -> None:
        show_if(self.hosts_box, self.traffic.value() == "hosts")
        show_if(self.live, self.traffic.value() == "hosts")

    def _hosts(self) -> list[str]:
        return [c.text_ for c in self.hosts_flow.chips()]

    def _host_chip(self, h: str) -> None:
        c = Chip(h, "plain", "x", mono=True, h=28)
        c.setToolTip("Remove")
        c.setCursor(Qt.CursorShape.PointingHandCursor)
        c.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        c.clicked.connect(lambda c=c: (self.hosts_flow.removeWidget(c), c.deleteLater(), self.changed()))
        self.hosts_flow.add(c)

    def _add_host(self, host: str = "") -> None:
        h = (host or self.host_edit.text()).strip().lower().rstrip(".")
        if h and h not in self._hosts():
            self._host_chip(h)
            self.changed()
        self.host_edit.clear()

    def _port_row(self, port: int, reach: str) -> None:
        row = frame("Row")
        row.setProperty("kind", "port")
        spin = QSpinBox()
        spin.setRange(1024, 65535)
        spin.setValue(port)
        spin.setFixedWidth(84)
        spin.setFont(th.mono_font(13))
        seg = Seg((("this-computer", "This computer", "monitor"), ("lan", "Local network", "tree")), reach)
        st, tone = port_status(port)
        ob = self.s["observed"]
        if ob["phase"] == "running" and port in [p["port"] for p in self.s["spec"]["network"]["ports"]]:
            st, tone = "this service", "ok"
        status = Chip(st, tone, "check" if tone == "ok" else "warn")
        rm = Btn(ico="trash", kind="ghost", tip="Close this port")
        row.setLayout(hbox(spin, 14, seg, 1, status, 30, rm, spacing=8, margins=(12, 10, 12, 10)))
        row.setProperty("spin", spin)
        row.setProperty("seg", seg)
        spin.valueChanged.connect(lambda v: (status.set(*port_status(v)), self.changed()))
        seg.changed.connect(lambda _k: self.changed())
        rm.clicked.connect(lambda _=False: (row.hide(), row.deleteLater(), self.changed()))
        self.port_rows.addWidget(row)

    def _add_port(self) -> None:
        used = {p for p in self._ports_list()}
        port = next(p for p in range(8000, 65535) if p not in used and port_status(p)[0] == "free")
        self._port_row(port, "this-computer")
        self.changed()

    def _ports_list(self) -> list[int]:
        return [r.property("spin").value() for r in self._rows(self.port_rows, "port")]

    @staticmethod
    def _rows(lay: QVBoxLayout, kind: str) -> list[QWidget]:
        """Rows of one kind still on the page (a removed row is hidden at once, deleted later)."""
        out = []
        for i in range(lay.count()):
            it = lay.itemAt(i)
            w = it.widget() if it else None
            if w is not None and w.property("kind") == kind and not w.isHidden():
                out.append(w)
        return out

    def _got_traffic(self, res: Any, err: Any) -> None:
        if err or res is None:
            return
        self.set_attempts(list(res))

    def set_attempts(self, rows: list[dict[str, Any]]) -> None:
        clear(self.attempts)
        rows = rows[-6:][::-1]
        blocked = sum(1 for r in rows if r["verdict"] == "blocked")
        self.net_badge.set(str(blocked))
        show_if(self.net_badge, blocked > 0)
        self.net_badge.move(150, 10)
        if not rows:
            msg = {"off": "Traffic is off, so nothing can try.", "any": "Anywhere: attempts aren't routed through the gate."}
            self.attempts.addWidget(label(msg.get(self.spec["network"]["traffic"]["mode"],
                                                  "No attempts yet."), "Muted", wrap=True))
            return
        for i, r in enumerate(rows):
            why = r.get("why") or ""
            meta = "metadata" in why or "this computer" in why
            tag = "metadata · always blocked" if "metadata" in why else "this computer · always blocked" if meta else r["verdict"]
            tone = "ok" if r["verdict"] == "allowed" else "bad"
            row = frame("Row")
            items: list[Any] = [label(time.strftime("%H:%M", time.localtime(r["ts"])), "MonoMuted"), 6,
                                Icon("check" if tone == "ok" else "x", tone, 15), label(f"{r['host']}:{r['port']}", "Mono"), 1,
                                Chip(tag, tone)]
            if r["verdict"] == "blocked" and not meta:
                items.append(Btn("Allow", "check", "primary", "sm", on=lambda h=r["host"]: self.allow(h)))
            row.setLayout(hbox(*items, spacing=10, margins=(14, 9, 12, 9)))
            if i:
                row.setStyleSheet(f"#Row {{ border-top: 1px solid {th.current().border}; }}")
            self.attempts.addWidget(row)

    def allow(self, host: str) -> None:
        def done(res: Any, err: Any) -> None:
            if err:
                self.main.after_err(None, err)
                return
            self._add_host(host)
            self.spec["network"]["traffic"]["hosts"] = self._hosts()
            self.dirty = False
            self._allowed = host
            self.toast_text.setText(f"<b>Allowed {esc(host)}</b><br><span style='color:{th.current().muted}'>"
                                    f"{'Live now, no restart needed' if res.get('approved') else 'Awaiting approval of the plan'}"
                                    "</span>")
            self.toast.show()
            self.main.snapshot()
            self._recompile()

        self.main.link.call("allow_host", done, name=self.name, host=host)

    def _undo_allow(self) -> None:
        host = getattr(self, "_allowed", "")
        for w in self.hosts_flow.chips():
            if w.text_ == host:
                self.hosts_flow.removeWidget(w)
                w.deleteLater()
        self.toast.hide()
        self.save_spec()

    def _limits(self, lay: QVBoxLayout) -> None:
        lim, r = self.spec["limits"], self.spec["restart"]
        lay.addWidget(section_head("Limits", "cpu", sub="Enforced by the kernel (cgroups, job objects) or, where the OS "
                                   "has none, by a watchdog that is labelled as such."))
        f = Field()
        self.mem = QSpinBox()
        self.mem.setRange(16, 262144)
        self.mem.setSuffix(" MB")
        self.mem.setValue(lim["memory_mb"])
        self.cpu = QDoubleSpinBox()
        self.cpu.setRange(0.1, 256)
        self.cpu.setSingleStep(0.5)
        self.cpu.setSuffix(" CPU")
        self.cpu.setValue(lim["cpu"])
        self.tasks = QSpinBox()
        self.tasks.setRange(4, 65536)
        self.tasks.setValue(lim["tasks"])
        for w in (self.mem, self.cpu, self.tasks):
            w.valueChanged.connect(lambda _v: self.changed())
            w.setFixedWidth(160)
        f.row("Memory", self.mem, "Over it, the kernel ends the biggest process of this service.")
        f.row("CPU", self.cpu, "1 CPU is one full core.")
        f.row("Tasks", self.tasks, "Processes and threads together: stops fork bombs.")
        self.policy = Seg((("always", "Always"), ("on-failure", "On failure"), ("never", "Never")), r["policy"])
        self.policy.changed.connect(lambda _k: self.changed())
        self.backoff = QDoubleSpinBox()
        self.backoff.setRange(0.5, 300)
        self.backoff.setSuffix(" s")
        self.backoff.setValue(r["backoff_s"])
        self.giveup = QSpinBox()
        self.giveup.setRange(1, 1000)
        self.giveup.setValue(r["give_up_after"])
        for w in (self.backoff, self.giveup):
            w.valueChanged.connect(lambda _v: self.changed())
            w.setFixedWidth(160)
        f.row("Restart", self.policy)
        f.row("First back-off", self.backoff, "Doubles after each crash, up to 5 minutes.")
        f.row("Give up after", self.giveup, "crashes in a row; then it waits in the Drift inbox for Heal.")
        lay.addLayout(f)

    def _schedule(self, lay: QVBoxLayout) -> None:
        sch = self.spec.get("schedule")
        lay.addWidget(section_head("Schedule", "calendar", sub="Keep it running, or start it at set times and let it finish."))
        self.when = Seg((("keep", "Keep running", "play"), ("cron", "On a schedule", "calendar")), "cron" if sch else "keep",
                        accent=True)
        self.when.changed.connect(lambda _k: (self._sched_fields(), self.changed()))
        lay.addWidget(self.when, 0, Qt.AlignmentFlag.AlignLeft)
        f = Field()
        self.cron = QLineEdit(sch["cron"] if sch else "")
        self.cron.setProperty("mono", True)
        self.cron.setPlaceholderText("0 3 * * *   (minute hour day month weekday)")
        self.cron.textEdited.connect(lambda _t: (self._next_runs(), self.changed()))
        self.next_lab = label("", "MonoMuted", wrap=True)
        self.catch = Toggle(sch.get("catch_up", True) if sch else True)
        self.catch.toggled.connect(lambda _on: self.changed())
        f.row("When", vbox(self.cron, self.next_lab, spacing=6))
        f.row("Catch up", hbox(self.catch, label("Run once at start if a time was missed while the computer was off"), 1,
                               spacing=10))
        self.sched_box = QWidget()
        self.sched_box.setLayout(f)
        lay.addWidget(self.sched_box)
        self._sched_fields()
        self._next_runs()

    def _sched_fields(self) -> None:
        show_if(self.sched_box, self.when.value() == "cron")

    def _next_runs(self) -> None:
        try:
            from datetime import datetime

            from cronsim import CronSim

            it = CronSim(self.cron.text().strip(), datetime.now().astimezone())
            self.next_lab.setText("Next: " + " · ".join(next(it).strftime("%a %d %b %H:%M") for _ in range(3)))
        except Exception:  # noqa: BLE001 - cronsim raises its own error types for each mistake
            self.next_lab.setText("Not a schedule yet." if self.cron.text().strip() else "")

    def _variables(self, lay: QVBoxLayout) -> None:
        lay.addWidget(section_head("Variables", "key", ("Variables and secrets", "Variables are saved in the service's spec "
                                   "file. Secrets are saved in your system keychain, given to the program at start, and "
                                   "masked in every log line, plugin view and AI request."),
                                   "Environment for the program. PATH, HOME, LD_* and friends are set by Pythonator."))
        self.env_rows = QVBoxLayout()
        self.env_rows.setSpacing(6)
        lay.addLayout(self.env_rows)
        for k, v in sorted(self.spec.get("env", {}).items()):
            self._env_row(k, v)
        lay.addWidget(Btn("Add variable", "plus", "ghost", on=lambda: (self._env_row("", ""), self.changed())), 0,
                      Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(section_head("Secrets", "lock", sub="Values never leave your keychain except into the running program."))
        self.sec_rows = QVBoxLayout()
        self.sec_rows.setSpacing(6)
        lay.addLayout(self.sec_rows)
        for k in self.spec.get("secrets", []):
            self._secret_row(k)
        lay.addWidget(Btn("Add secret", "plus", "ghost", on=self._add_secret), 0, Qt.AlignmentFlag.AlignLeft)

    def _env_row(self, k: str, v: str) -> None:
        row = QWidget()
        row.setProperty("kind", "env")
        ke, ve = QLineEdit(k), QLineEdit(v)
        ke.setProperty("mono", True)
        ve.setProperty("mono", True)
        ke.setPlaceholderText("NAME")
        ve.setPlaceholderText("value")
        ke.setFixedWidth(220)
        rm = Btn(ico="trash", kind="ghost", tip="Remove")
        row.setLayout(hbox(ke, label("=", "MonoMuted"), ve, rm, spacing=8))
        row.setProperty("k", ke)
        row.setProperty("v", ve)
        for e in (ke, ve):
            e.textEdited.connect(lambda _t: self.changed())
        rm.clicked.connect(lambda _=False: (row.hide(), row.deleteLater(), self.changed()))
        self.env_rows.addWidget(row)

    def _secret_row(self, k: str) -> None:
        from pythonator.reconcile import secrets

        row = QWidget()
        row.setProperty("kind", "secret")
        row.setProperty("name", k)
        is_set = secrets.get(self.name, k) is not None
        st = Chip("set" if is_set else "not set", "ok" if is_set else "warn")
        set_btn = Btn("Set value", "key", size="sm", on=lambda: self._set_secret(k, st))
        rm = Btn(ico="trash", kind="ghost", tip="Remove from this service")
        row.setLayout(hbox(Icon("lock", "muted"), label(k, "Mono"), 1, st, set_btn, rm, spacing=8))
        rm.clicked.connect(lambda _=False: (row.hide(), row.deleteLater(), self.changed()))
        self.sec_rows.addWidget(row)

    def _add_secret(self) -> None:
        k, ok = QInputDialog.getText(self, "Add secret", "Name (for example API_TOKEN):")
        if ok and k.strip():
            self._secret_row(k.strip())
            self.changed()

    def _set_secret(self, k: str, chip: Chip) -> None:
        from pythonator.reconcile import secrets

        v, ok = QInputDialog.getText(self, f"Secret {k}", f"Value for {k} (saved in your system keychain):",
                                     QLineEdit.EchoMode.Password)
        if ok and v:
            try:
                secrets.put(self.name, k, v)
                chip.set("set", "ok")
            except Exception as e:  # noqa: BLE001 - keyring backends raise their own types
                QMessageBox.warning(self, "Secret", f"Couldn't save it in the keychain: {e}")

    def _git(self, lay: QVBoxLayout) -> None:
        self.snap_btn = Btn(ico="git", size="sm", tip="Take a snapshot now", on=self._take_snapshot)
        lay.addWidget(section_head("Git snapshots", "git", ("Private snapshots", "Pythonator keeps its own git history of this "
                                   "folder, outside it (in Pythonator's data folder), so the service can't rewrite it and your "
                                   "folder gets no .git. Each approval and each Keep changes takes a snapshot."),
                                   "Every approved change and every Keep changes from Try in a copy.", self.snap_btn))
        self.changed_lab = label("", "Small")
        lay.addWidget(self.changed_lab)
        self.git_rows = QVBoxLayout()
        self.git_rows.setSpacing(0)
        lay.addWidget(frame("Card", self.git_rows))
        self.git_loaded = False  # read when the Git section is opened, not every time Settings opens

    def _load_git(self) -> None:
        def got(res: Any, err: Any) -> None:
            clear(self.git_rows)
            if err:
                self.git_rows.addWidget(label(err["message"], "Muted", wrap=True))
                return
            n = len(res["changed"])
            self.changed_lab.setText(f"{n} file{'s' if n != 1 else ''} changed since the last snapshot" if res["log"] else
                                     "No snapshot yet: the first one is taken when you approve or take one.")
            for i, x in enumerate(res["log"][:15]):
                when = time.strftime("%d %b %H:%M", time.localtime(int(x["ts"])))
                rb = Btn(ico="undo", size="sm", tip="Restore this snapshot", on=lambda rid=x["id"]: self._restore(rid))
                row = frame("Row", hbox(label(x["id"][:7], "MonoMuted"), label(x["message"]), 1, label(x["stat"], "Small"),
                                        label(when, "MonoMuted"), rb, spacing=10, margins=(14, 8, 12, 8)))
                if i:
                    row.setStyleSheet(f"#Row {{ border-top: 1px solid {th.current().border}; }}")
                self.git_rows.addWidget(row)

        self.main.link.call("git.log", got, name=self.name)

    def _take_snapshot(self) -> None:
        self.main.link.call("git.snapshot", lambda _r, e: (self.main.after_err(None, e), self._load_git()), name=self.name,
                            message="taken in Settings")

    def _restore(self, rid: str) -> None:
        if QMessageBox.question(self, "Restore", f"Put every file back as it was in {rid}? A snapshot of the folder as it "
                                "is now is taken first, so this can be undone.") != QMessageBox.StandardButton.Yes:
            return
        self.main.link.call("git.restore", lambda r, e: (self.main.after_err(None, e), self._load_git(),
                                                         None if e else self.main.toast("info", self.name, f"restored {r} files")),
                            name=self.name, id=rid)

    def _health(self, lay: QVBoxLayout) -> None:
        h = self.spec.get("health") or {"kind": "off", "port": 0, "path": "/", "every_s": 10, "fails": 3, "grace_s": 20}
        lay.addWidget(section_head("Health check", "pulse", ("Health check", "From this computer, Pythonator checks one of the "
                                   "service's own open ports. After the number of failures you set, it restarts the service "
                                   "and lists that under Healed today."),
                                   "Is it really working, not just running?"))
        self.hkind = Seg((("off", "Off", "x"), ("tcp", "Port answers", "plug"), ("http", "HTTP GET", "globe")), h["kind"],
                         accent=True)
        self.hkind.changed.connect(lambda _k: (self._health_fields(), self.changed()))
        lay.addWidget(self.hkind, 0, Qt.AlignmentFlag.AlignLeft)
        f = Field()
        self.hport = QComboBox()
        self.hpath = QLineEdit(h.get("path", "/"))
        self.hpath.setProperty("mono", True)
        self.hevery = QDoubleSpinBox()
        self.hevery.setRange(2, 3600)
        self.hevery.setSuffix(" s")
        self.hevery.setValue(h.get("every_s", 10))
        self.hfails = QSpinBox()
        self.hfails.setRange(1, 100)
        self.hfails.setValue(h.get("fails", 3))
        self.hgrace = QDoubleSpinBox()
        self.hgrace.setRange(0, 600)
        self.hgrace.setSuffix(" s")
        self.hgrace.setValue(h.get("grace_s", 20))
        for w in (self.hevery, self.hfails, self.hgrace):
            w.valueChanged.connect(lambda _v: self.changed())
            w.setFixedWidth(160)
        self.hpath.textEdited.connect(lambda _t: self.changed())
        self.hport.currentIndexChanged.connect(lambda _i: self.changed())
        f.row("Port", self.hport, "One of the ports in Network → Ports in.")
        f.row("Path", self.hpath)
        f.row("Every", self.hevery)
        f.row("Restart after", self.hfails, "failed checks in a row")
        f.row("Grace after start", self.hgrace)
        self.health_box = QWidget()
        self.health_box.setLayout(f)
        lay.addWidget(self.health_box)
        self._fill_health_ports(h.get("port", 0))
        self._health_fields()

    def _fill_health_ports(self, want: int) -> None:
        self.hport.blockSignals(True)
        self.hport.clear()
        for p in self._ports_list() if hasattr(self, "port_rows") else []:
            self.hport.addItem(str(p), p)
        i = self.hport.findData(want)
        self.hport.setCurrentIndex(max(0, i))
        self.hport.blockSignals(False)

    def _health_fields(self) -> None:
        k = self.hkind.value()
        show_if(self.health_box, k != "off")
        self.hpath.setEnabled(k == "http")

    # build, compile, save -----------------------------------------------------------------------------------------
    def build(self) -> dict[str, Any]:
        sp = copy.deepcopy(self.s["spec"])
        sp.pop("generation", None)
        sp["folder"] = self.folder.text().strip()
        try:
            sp["command"] = shlex.split(self.command.text())
        except ValueError:
            sp["command"] = [self.command.text()]
        sp["runtime"] = self.runtime.currentData()
        sp["runtime_version"] = self.pyver.text().strip() if sp["runtime"] == "python" else ""
        sp["group"] = self.group.text().strip()
        sp["gui"] = self.gui.isChecked()
        sp["mode"] = self.mode.value()
        sp["container"] = {"image": self.image.text().strip(), "engine": self.engine.value()} if sp["mode"] == "container" else None
        sp["folders"] = [{"path": r.property("path"), "write": r.property("tog").isChecked()}
                         for r in self._rows(self.folder_rows, "folder")]
        ports = [{"port": r.property("spin").value(), "reach": r.property("seg").value()}
                 for r in self._rows(self.port_rows, "port")]
        sp["network"] = {"traffic": {"mode": self.traffic.value(), "hosts": self._hosts() if self.traffic.value() == "hosts" else [],
                                     "allow_lan": self.lan.isChecked()}, "ports": ports}
        sp["limits"] = {"memory_mb": self.mem.value(), "cpu": round(self.cpu.value(), 2), "tasks": self.tasks.value()}
        sp["restart"] = {**sp["restart"], "policy": self.policy.value(), "backoff_s": self.backoff.value(),
                         "give_up_after": self.giveup.value()}
        sp["schedule"] = {"cron": self.cron.text().strip(), "catch_up": self.catch.isChecked()} if self.when.value() == "cron" else None
        env = {}
        for r in self._rows(self.env_rows, "env"):
            k = r.property("k").text().strip()
            if k:
                env[k] = r.property("v").text()
        sp["env"] = env
        sp["secrets"] = [r.property("name") for r in self._rows(self.sec_rows, "secret")]
        self._fill_health_ports(self.hport.currentData() or 0)
        hk = self.hkind.value()
        sp["health"] = {"kind": hk, "port": int(self.hport.currentData() or 0) if hk != "off" else 0,
                        "path": self.hpath.text().strip() or "/", "every_s": self.hevery.value(),
                        "timeout_s": 3.0, "fails": self.hfails.value(), "grace_s": self.hgrace.value()}
        return sp

    def _recompile(self) -> None:
        sp = self.build()
        target = self.os_seg.value()
        here = target == self.os or (target == "linux" and self.os == "linux")

        def got(res: Any, err: Any) -> None:
            clear(self.rows)
            if err:
                self.guard.set(f"<b>Not saved yet:</b> {esc(err['message'])}", "bad")
                self.save.setEnabled(False)
                return
            self.last = res if here else self.last
            self.save.setEnabled(True)
            plan, refused = res.get("plan"), res.get("refused")
            if refused:
                self.rows.addWidget(Note(f"<b>{esc(refused['code'])}</b> {esc(refused['message'])}"
                                         + (f"<br>Fix: {esc(refused['fix'])}" if refused.get("fix") else ""), "bad"))
            for x in (plan or {}).get("layers", []):
                if x["status"] == "off":
                    continue
                tone = {"enforced": "ok", "reduced": "warn"}.get(x["status"], "bad")
                row = frame("Row")
                d = x.get("detail") or ""
                both = f"{x['title']}: {d.rstrip('.')}"
                lab = label(both if d and len(both) <= 46 else x["title"], wrap=True)
                lab.setToolTip(f"{x['title']}: {d}" if d else x["title"])
                rl = hbox(Icon("check" if tone == "ok" else "warn", tone, 15), spacing=10, margins=(0, 10, 0, 10))
                rl.addWidget(lab, 1)
                by = x["by"] if len(x["by"]) <= 24 else x["by"][:23] + "…"
                chip = Chip(by, "acc" if tone == "ok" else tone, mono=True)
                chip.setToolTip(x["by"])
                rl.addWidget(chip, 0, Qt.AlignmentFlag.AlignTop)
                row.setLayout(rl)
                row.setStyleSheet(f"#Row {{ border-bottom: 1px solid {th.current().border}; }}")
                self.rows.addWidget(row)
            if res.get("reference"):
                self.rows.addWidget(label("A typical, fully set-up machine of that kind; System check there shows the real one.",
                                          "Small", wrap=True))
            if here:
                self._guard(res)

        if here:
            self.main.link.call("preview", got, spec=sp)
        else:
            self.main.link.call("plan_os", got, spec=sp, os=target)

    def _guard(self, res: dict[str, Any]) -> None:
        g = res.get("guard") or []
        bad = next((x for x in g if not x["allow"]), None)
        widens = res.get("widens") or []
        if bad:
            self.guard.set(f"<b>{esc(bad['reason'])}</b>" + (f" {esc(bad.get('fix', ''))}" if bad.get("fix") else ""), "bad")
        elif g:
            self.guard.set(f"<b>Allowed</b> by {esc(g[0]['plugin'])} ({esc(g[0].get('summary', ''))})."
                           + (f" {len(widens)} line{'s' if len(widens) != 1 else ''} will await approval." if widens else ""),
                           "ok")
        else:
            self.guard.set("No Guard installed: your approval is the only check."
                           + (f" {len(widens)} line{'s' if len(widens) != 1 else ''} will await approval." if widens else ""),
                           "info")

    def save_spec(self) -> None:
        sp = self.build()

        def saved(res: Any, err: Any) -> None:
            if err:
                self.main.after_err(None, err)
                return
            self.dirty = False
            if res.get("approved"):
                self.main.toast("ok", self.name, "saved; nothing new to approve")
                self._after_save()
                return
            base_widens = res.get("widens") or []
            if not base_widens:  # nothing wider than what you approved: your Save is the OK
                self.main.link.call("approve", lambda _r, e: (self.main.after_err(None, e), self._after_save()),
                                    name=self.name, hash=res["hash"])
                return
            self.main.close_overlay_page(self)
            self.main.snapshot()
            self.main.show_plan(self.name)

        self.main.link.call("apply", saved, spec=sp)

    def _after_save(self) -> None:
        self.main.snapshot()
        s = self.main._svc(self.name)
        if s and s["observed"]["phase"] == "running":
            self.toast_text.setText("<b>Saved</b><br>Plan recompiled · restart to apply")
            self.toast_undo.hide()
            self.toast.show()
        else:
            self.main.close_overlay_page(self)
