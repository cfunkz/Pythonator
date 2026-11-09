"""Screens 6 and 7: plugins, their permissions, and installing one.
Each plugin is its own jailed process. Permissions are granted one by one and stored against a hash of every file in
the plugin, so new code starts with nothing. Here you see what each plugin may do, what it actually did (every call,
refused ones too), and the jail its permissions compile into. Safe mode and "Find the plugin causing this" (a
bisect over the enabled plugins) are one click."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPaintEvent, QPen
from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHeaderView,
    QInputDialog,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pythonator.ui import theme as th
from pythonator.ui.kit import (
    Btn,
    Chip,
    Icon,
    InfoBtn,
    Note,
    Panel,
    Search,
    Toggle,
    clear,
    esc,
    frame,
    hbox,
    label,
    primary,
    show_if,
    splitter,
    vbox,
)

if TYPE_CHECKING:
    from pythonator.ui.app import Main

PLUGIN_ICON = {"notify": "bell", "guard": "shield", "builder": "wrench", "ai": "sparkle"}
PERM_ICON = {
    "services.read": "eye", "logs.read": "list", "ui.notify": "bell", "ui.views": "layers", "ui.statusbar": "drawer",
    "net.service": "in", "net.connect": "out", "policy.check": "shield", "editor.assist": "sparkle",
    "projects.create": "wrench",
}
ADDS = {
    "ui.views": ("layers", "A section in a service's inspector"),
    "ui.statusbar": ("drawer", "A status item at the bottom of the window"),
    "ui.notify": ("bell", "Notifications in the bell menu"),
    "editor.assist": ("sparkle", "The AI assistant pane in the editor"),
    "projects.create": ("wrench", "Starter projects in New service"),
    "policy.check": ("shield", "A verdict on every change, on the plan sheet"),
}
GRANTS_RESET = ("Why grants reset", "Grants are stored against a hash of every file in the plugin. New code starts "
                "with nothing, so an update can't quietly inherit trust you gave an older version.",
                "Same rule for official plugins")
WALLS = ("Permissions are walls", "Each permission is compiled into the plugin's own jail by the same compiler as "
         "services. A plugin that tries more than it was granted hits a wall, not a check it could skip.",
         "Same compiler as services")

def tile(ico: str, size: int = 40, accent: bool = False) -> QFrame:
    t = th.current()
    f = QFrame()
    f.setFixedSize(size, size)
    f.setObjectName("Tile")
    f.setStyleSheet(f"#Tile {{ background: {t.accent if accent else t.raised2}; border-radius: {th.RCARD}px; }}")
    i = Icon(ico, "accent_ink" if accent else "text", size // 2 if accent else 18)
    lay = vbox(i, margins=(0, 0, 0, 0))
    lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
    f.setLayout(lay)
    return f

def _brief(text: str, n: int = 44) -> str:
    first = text.split(". ")[0].split(":")[0].rstrip(".")
    return first if len(first) <= n else first[: n - 1].rsplit(" ", 1)[0] + "…"

def short(h: str) -> str:
    return f"{h[:4]}…{h[-4:]}" if len(h) > 10 else h

def scope_text(perm: dict[str, Any], value: Any) -> str:
    if not perm["scoped"]:
        return "—"
    if value is True or value is None:
        return "all" if perm["scope_kind"] == "services" else ", ".join(perm.get("hosts") or []) or "all"
    return ", ".join(value) if value else "none"

class PermRow(QFrame):
    """One permission: what it is, its scope (pick services or hosts), and a switch that is staged until you grant."""

    def __init__(self, perm: dict[str, Any], choices: list[str], on_change: Any, mono: bool = False) -> None:
        super().__init__()
        self.setObjectName("Row")
        self.perm = perm
        self.choices = choices
        self.scope: Any = perm["granted"] if perm["granted"] is not None else (choices if perm["scope_kind"] == "hosts" else True)
        self.on_change = on_change
        t = th.current()
        if perm.get("new"):
            self.setStyleSheet(f"#Row {{ background: {th.rgba(t.warn, 0.06)}; }}")
        key = label(perm["key"], "Mono")
        key.setStyleSheet("font-size: 13px;" + (" font-weight: 600;" if mono else ""))
        head = hbox(key, spacing=8)
        if perm.get("new"):
            head.addWidget(Chip("new", "warn", h=20))
        head.addStretch(1)
        det = label(perm["detail"], "Small", wrap=True)
        txt = vbox(head, det, spacing=2)
        self.scope_chip = Chip(scope_text(perm, self.scope), "line", mono=False)
        if perm["scoped"]:
            self.scope_chip.setCursor(Qt.CursorShape.PointingHandCursor)
            self.scope_chip.setToolTip("Pick which " + ("hosts" if perm["scope_kind"] == "hosts" else "services"))
            self.scope_chip.clicked.connect(self.pick_scope)
        self.tog = Toggle(perm["granted"] is not None, "Granted" if perm["granted"] is not None else "Not granted")
        self.tog.setEnabled(perm["available"])
        self.tog.toggled.connect(lambda _on: self.on_change())
        lay = hbox(tile(PERM_ICON.get(perm["key"], "key"), 34), txt, self.scope_chip, self.tog, spacing=12,
                   margins=(16, 11, 16, 11))
        lay.setStretch(1, 1)
        self.setLayout(lay)

    def pick_scope(self) -> None:
        m = QMenu(self)
        allowed = set(self.choices if self.scope is True else self.scope)
        if self.perm["scope_kind"] == "services":
            a = m.addAction("All services, now and later")
            a.setCheckable(True)
            a.setChecked(self.scope is True)
            a.triggered.connect(lambda: self._set(True))
            m.addSeparator()
        for c in self.choices:
            a = m.addAction(c)
            a.setCheckable(True)
            a.setChecked(self.scope is not True and c in allowed)
            a.triggered.connect(lambda on, c=c: self._toggle(c, on))
        m.exec(self.scope_chip.mapToGlobal(self.scope_chip.rect().bottomLeft()))

    def _toggle(self, c: str, on: bool) -> None:
        cur = set(self.choices if self.scope is True else self.scope)
        cur = cur | {c} if on else cur - {c}
        self._set(sorted(cur) if cur else [])

    def _set(self, v: Any) -> None:
        self.scope = v
        self.scope_chip.set(scope_text(self.perm, v))
        if self.perm["granted"] is not None and v != self.perm["granted"]:
            self.on_change()

    def wanted(self) -> tuple[bool, Any]:
        return self.tog.isChecked(), self.scope

    def dirty(self) -> bool:
        on, scope = self.wanted()
        was = self.perm["granted"]
        return on != (was is not None) or (on and self.perm["scoped"] and scope != (was if was is not None else scope))

class JailBoxes(QWidget):
    """The jail as nested walls: limits, network gate, files, syscalls, and the plugin in the middle."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[tuple[str, str]] = []
        self.core = ""
        self.setMinimumHeight(250)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set(self, rows: list[tuple[str, str]], core: str) -> None:
        self.rows, self.core = rows, core
        self.setFixedHeight(44 * len(rows) + 52)
        self.update()

    def paintEvent(self, _e: QPaintEvent | None) -> None:
        t = th.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        acc = QColor(t.m_sandbox)
        for i, (name, detail) in enumerate(self.rows):
            fill = th.mix(t.m_sandbox, t.bg, 0.06 + 0.03 * i)
            pen = QPen(acc, 1.2, Qt.PenStyle.DashLine)
            pen.setDashPattern([4, 3])
            p.setPen(pen)
            p.setBrush(fill)
            p.drawRoundedRect(r, th.RC, th.RC)
            p.setFont(th.mono_font(12))
            p.setPen(QColor(t.accent_text))
            p.drawText(QRectF(r.left() + 12, r.top() + 8, r.width() - 24, 20), Qt.AlignmentFlag.AlignLeft, name)
            p.setPen(QColor(t.muted))
            p.drawText(QRectF(r.left() + 12, r.top() + 8, r.width() - 24, 20), Qt.AlignmentFlag.AlignRight, detail)
            r = r.adjusted(11, 36, -11, -8)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(t.console))
        core = QRectF(r.left() + 2, r.top() + 4, r.width() - 4, 36)
        p.drawRoundedRect(core, th.RC, th.RC)
        p.setPen(QColor(t.console_text))
        f = th.mono_font(13, 600)
        p.setFont(f)
        p.drawText(core, Qt.AlignmentFlag.AlignCenter, self.core)

def jail_rows(info: dict[str, Any], on: dict[str, tuple[bool, Any]]) -> list[tuple[str, str]]:
    layers = {x["title"].lower(): x for x in info.get("jail", [])}
    lim = layers.get("limits", {})
    hosts = on.get("net.connect", (False, None))
    gate = ("only " + ", ".join(hosts[1] if isinstance(hosts[1], list) else info.get("hosts", [])) + ", via Pythonator"
            if hosts[0] else "no network at all" + (" (net.connect off)" if "net.connect" in on else ""))
    files = layers.get("files", {})
    return [
        ("limits", (lim.get("detail") or lim.get("by") or "256 MB").replace(" checked every second", "")),
        ("network", gate),
        ("files", "own code read-only · no home" if files.get("status") != "not-enforced" else files.get("detail", "")),
        ("syscalls", {"linux": "seccomp · Landlock", "darwin": "Seatbelt", "windows": "AppContainer"}.get(
            info.get("_os", "linux"), "jailed")),
    ]

class InstallDialog(QDialog):
    """Screen 7: install a plugin, granting permissions one by one, and see the jail they compile into."""

    def __init__(self, page: PluginsPage, info: dict[str, Any]) -> None:
        super().__init__(page)
        self.page, self.info = page, info
        self.setModal(True)
        self.setWindowTitle(f"Install {info['name']}")
        self.setMinimumWidth(1080)
        t = th.current()
        self.setStyleSheet(f"QDialog {{ background: {t.raised}; }}")
        info["_os"] = page.main.snap.get("host", {}).get("os", "linux")
        off = not info["official"]
        head_chips = hbox(Chip("official", "ok", "shield-ok") if info["official"] else Chip("third-party", "warn", "user"),
                          Chip(f"hash {short(info['hash'])}", "line", "fingerprint"), spacing=8)
        title = hbox(label(f"Install {esc(info['name'])} {esc(info['version'])}", "H1", rich=True), head_chips, 1, spacing=12)
        src = "bundled with Pythonator" if info["path"].startswith(str(Path(__file__).resolve().parent.parent)) else \
            "from a folder you picked"
        sub = label(f"by {esc(info['publisher'] or 'unknown')} · {src} · {info['files']} files, {info['lines']} lines of "
                    "Python" + (f" · replaces {info['replaces']}" if info["replaces"] else ""), "Muted", rich=True)
        x = Btn("", "x", "ghost", tip="Close", on=self.reject)
        head = hbox(tile(PLUGIN_ICON.get(info["id"], "plug"), 56, accent=True), vbox(title, sub, spacing=4), 1, x,
                    spacing=16, margins=(24, 20, 20, 18))
        services = [s["spec"]["name"] for s in page.main.services]
        self.rows: list[PermRow] = []
        left = vbox(spacing=0)
        left.addLayout(hbox(label("It asks for", "H3"), 1, InfoBtn("Granted one by one", "Each switch is one "
                       "permission. What you grant is stored against this exact code's hash."),
                            label("granted one by one · tied to this exact code", "Small"), spacing=8,
                            margins=(0, 0, 0, 10)))
        for perm in info["permissions"]:
            perm = {**perm, "hosts": info.get("hosts", [])}
            default = (info["official"] and perm["key"] not in ("net.connect",)) and perm["available"]
            perm["granted"] = (True if not perm["scoped"] else (info["hosts"] if perm["scope_kind"] == "hosts"
                                                                 else True)) if default else None
            row = PermRow(perm, info["hosts"] if perm["scope_kind"] == "hosts" else services, self._changed, mono=True)
            row.perm = {**row.perm, "granted": None}  # staged from nothing: install starts with no grants
            self.rows.append(row)
            left.addWidget(row)
        left.addSpacing(14)
        left.addWidget(Note("Turning a permission off is always safe: the plugin keeps working with less. An update "
                            "with new code starts with nothing again.", "info"))
        if off and info["permissions"]:
            left.addWidget(Note("Third-party: nothing is on until you switch it on.", "warn", "user"))
        left.addStretch(1)
        self.boxes = JailBoxes()
        right = vbox(hbox(label("Becomes this jail", "H3"), 1, InfoBtn(*WALLS[:2], foot=WALLS[2], ico="layers"),
                          spacing=8), self.boxes, 8, frame("Card", vbox(
                              hbox(Icon("layers", "accent_text", 15), label(WALLS[0], "H3"), 1, spacing=8),
                              label(WALLS[1], "Small", wrap=True), spacing=6, margins=(14, 12, 14, 12))), 1, spacing=10)
        body = hbox(spacing=26, margins=(24, 18, 24, 18))
        body.addLayout(left, 3)
        body.addLayout(right, 2)
        act = " · ".join({"startup": "starts with Pythonator", "first-use": "starts on first use"}.get(a, a)
                         for a in info.get("activation", ["startup"]))
        self.go = primary("Install", "download", self.install)
        foot = hbox(Icon("clock", "muted", 14), label(f"{act} · restarts up to 3 times if it crashes", "Small"), 1,
                    Btn("Cancel", on=self.reject), self.go, spacing=10, margins=(24, 14, 24, 16))
        top = frame("Row", head)
        bottom = frame("", foot)
        bottom.setStyleSheet(f"QFrame {{ border-top: 1px solid {t.border}; }} QLabel, QPushButton {{ border-top: 0; }}")
        self.setLayout(vbox(top, body, bottom, spacing=0))
        self._changed()

    def _changed(self) -> None:
        on = {r.perm["key"]: r.wanted() for r in self.rows}
        self.boxes.set(jail_rows(self.info, on), f"{self.info['id']} plugin")
        n = sum(1 for v in on.values() if v[0])
        self.go.setText(f"Install with {n} permission{'s' if n != 1 else ''}" if n else "Install with no permissions")

    def install(self) -> None:
        link = self.page.main.link
        wanted = [(r.perm["key"], r.wanted()[1]) for r in self.rows if r.wanted()[0]]

        def installed(res: Any, err: Any) -> None:
            if err:
                QMessageBox.warning(self, "Install", err["message"])
                return
            pid = res["id"]
            for key, scope in wanted:
                link.call("plugins.grant", None, id=pid, permission=key, scope=scope)
            link.call("plugins.enable", lambda r, e: self.page._got(r, e), id=pid, on=True)
            self.page.current = pid
            self.accept()

        link.call("plugins.install", installed, path=self.info["path"])

class PluginsPage(QWidget):
    def __init__(self, main: Main) -> None:
        super().__init__()
        self.setObjectName("Page")
        self.main = main
        self.link = main.link
        self.snap: dict[str, Any] = {"installed": [], "available": [], "safe_mode": False}
        self.current = ""
        self.filter = ""
        self.only = ""
        self.bisect: dict[str, Any] | None = None
        self.narrow = False
        self.detail_open = False

        side = frame("PluginList")
        self.side = side
        self.search = Search("Filter")
        self.search.edit.textChanged.connect(self._filter)
        self.c_inst = Chip("", "acc")
        self.c_upd = Chip("", "warn")
        self.c_run = Chip("", "line")
        for c, k in ((self.c_inst, ""), (self.c_upd, "update"), (self.c_run, "running")):
            c.setCursor(Qt.CursorShape.PointingHandCursor)
            c.clicked.connect(lambda k=k: self._only(k))
        self.items = QVBoxLayout()
        self.items.setSpacing(4)
        inner = QWidget()
        iv = vbox(spacing=0)
        iv.addLayout(self.items)
        iv.addStretch(1)
        inner.setLayout(iv)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sc.setWidget(inner)
        self.bisect_btn = Btn("Find the plugin causing this", "bug", size="sm", on=self.start_bisect,
                              tip="Turns plugins off half at a time and asks you whether the problem is still there")
        self.safe_btn = Btn("Safe mode", "power", "ghost", "sm", on=self.toggle_safe,
                            tip="Run no plugins at all until you turn it off")
        foot = frame("", hbox(self.bisect_btn, 1, self.safe_btn, spacing=8, margins=(18, 12, 18, 14)))
        foot.setStyleSheet(f"QFrame {{ border-top: 1px solid {th.current().border}; }} QPushButton {{ border-top: 0; }}")
        sl = vbox(hbox(label("Plugins", "H2"), 1, Btn("Install…", "download", size="sm", on=self.install_folder),
                       spacing=8, margins=(18, 18, 18, 0)),
                  frame("", vbox(self.search, margins=(18, 0, 18, 0))),
                  frame("", hbox(self.c_inst, self.c_upd, self.c_run, 1, spacing=6, margins=(18, 0, 18, 4))),
                  sc, foot, spacing=12)
        side.setLayout(sl)

        self.detail = QScrollArea()
        self.detail.setWidgetResizable(True)
        self.detail.setFrameShape(QFrame.Shape.NoFrame)
        self.detail.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setLayout(hbox(splitter("plugins", (side, 370, 260, 600), (self.detail, 800, 360, 16777215)), spacing=0))
        self.layout().setStretch(1, 1)  # type: ignore[union-attr]
        self._render()

    # data --------------------------------------------------------------------------------------------------------
    def refresh(self) -> None:
        self.link.call("plugins.list", self._got)

    def retheme(self) -> None:
        self._render()

    def layout_for(self, w: int) -> None:
        """Wide: list and detail side by side. Narrow (phone width): one pane, the list or one plugin."""
        side = self.side
        was = getattr(self, "narrow", False)
        self.narrow = w < 700
        if self.narrow:
            side.setMinimumWidth(0)
            side.setMaximumWidth(16777215)
            show_if(side, not getattr(self, "detail_open", False))
            show_if(self.detail, getattr(self, "detail_open", False))
        else:
            side.setMinimumWidth(260)
            side.setMaximumWidth(600)
            show_if(side, True)
            show_if(self.detail, True)
        if was != self.narrow and self.snap.get("installed") is not None:
            self._show()

    def back_to_list(self) -> None:
        self.detail_open = False
        self.layout_for(self.width())

    def _got(self, res: Any, err: Any) -> None:
        if err:
            QMessageBox.warning(self, "Plugins", err["message"] + (f"\n\nFix: {err['fix']}" if err.get("fix") else ""))
            self.refresh()
            return
        if isinstance(res, dict) and "installed" in res:
            self.snap = res
            self._render()
        else:
            self.refresh()

    def _filter(self, text: str) -> None:
        self.filter = text.lower().strip()
        self._render_list()

    def _only(self, k: str) -> None:
        self.only = "" if self.only == k else k
        self._render_list()

    # list -------------------------------------------------------------------------------------------------------
    def _render(self) -> None:
        self._render_list()
        self._show()

    def _render_list(self) -> None:
        clear(self.items)
        inst = self.snap["installed"]
        self.c_inst.set(f"Installed {len(inst)}")
        self.c_upd.set(f"Updates {sum(1 for p in inst if p.get('update') or p['code_changed'])}")
        self.c_run.set(f"Running {sum(1 for p in inst if p['state'] == 'running')}")
        show_if(self.c_upd, any(p.get("update") or p["code_changed"] for p in inst))
        for c, k in ((self.c_inst, ""), (self.c_upd, "update"), (self.c_run, "running")):
            c.tone = ("acc" if k == "" else "warn" if k == "update" else "plain") if self.only == k else "line"
            c.update()
        if not self.current:
            self.current = inst[0]["id"] if inst else (self.snap["available"][0]["id"] if self.snap["available"] else "")
        for p in inst:
            if self.filter and self.filter not in (p["name"] + p["description"]).lower():
                continue
            if self.only == "running" and p["state"] != "running":
                continue
            if self.only == "update" and not (p.get("update") or p["code_changed"]):
                continue
            state = "safe mode" if self.snap.get("safe_mode") else ("on" if p["state"] == "running" else
                                                                     "off" if not p["enabled"] else p["state"])
            if p["code_changed"]:
                state = "review grants"
            elif p.get("update"):
                state = "update"
            sub = f"{'official' if p['official'] else 'third-party'} · {state} · {_brief(p['description'])}"
            self.items.addWidget(self._row(p["id"], p["name"], sub, p["official"], p["enabled"]))
        for a in self.snap["available"]:
            if self.only or (self.filter and self.filter not in (a["name"] + a["description"]).lower()):
                continue
            self.items.addWidget(self._row(a["id"], a["name"], f"official · not installed · {_brief(a['description'])}", True,
                                           None))

    def _row(self, pid: str, name: str, sub: str, official: bool, enabled: bool | None) -> QFrame:
        t = th.current()
        f = QFrame()
        f.setObjectName("PRow")
        sel = pid == self.current
        f.setStyleSheet(f"#PRow {{ border-radius: {th.RCARD}px; border: 1px solid "
                        f"{t.border_strong if sel else 'transparent'}; background: {t.raised if sel else 'transparent'}; }}"
                        f"#PRow:hover {{ background: {t.raised}; }}")
        f.setCursor(Qt.CursorShape.PointingHandCursor)
        nm = hbox(label(esc(name), "H3", rich=True), spacing=6)
        if official:
            nm.addWidget(Icon("shield-ok", "ok", 13))
        nm.addStretch(1)
        s = label(esc(sub), "Small", wrap=True, rich=True)
        right: QWidget
        if enabled is None:
            right = Btn("Install", "download", size="sm", on=lambda: self.inspect(self._bundled(pid)))
        else:
            right = Toggle(enabled, "On" if enabled else "Off")
            right.toggled.connect(lambda on: self.link.call("plugins.enable", self._got, id=pid, on=on))
        lay = hbox(tile(PLUGIN_ICON.get(pid, "plug"), 40), vbox(nm, s, spacing=2), right, spacing=12, margins=(10, 10, 12, 10))
        lay.setStretch(1, 1)
        lay.setAlignment(right, Qt.AlignmentFlag.AlignTop)
        f.setLayout(lay)
        f.mousePressEvent = lambda _e, pid=pid: self._pick(pid)  # type: ignore[method-assign,assignment]
        return f

    def _bundled(self, pid: str) -> str:
        return next((a["path"] for a in self.snap["available"] if a["id"] == pid), "")

    def _pick(self, pid: str) -> None:
        self.current = pid
        self.detail_open = True
        self._render()
        if getattr(self, "narrow", False):
            self.layout_for(self.width())

    # detail -------------------------------------------------------------------------------------------------------
    def _show(self) -> None:
        t = th.current()
        body = QWidget()
        lay = vbox(spacing=16, margins=(28, 22, 28, 28))
        body.setLayout(lay)
        p = next((x for x in self.snap["installed"] if x["id"] == self.current), None)
        a = next((x for x in self.snap["available"] if x["id"] == self.current), None)
        if p is None and a is None:
            lay.addWidget(Note("No plugins yet. Notify, Guard, Builder and the AI assistant ship with Pythonator: "
                               "install them from the list. Or install a folder with Install…", "info", "plug"))
            lay.addStretch(1)
            self.detail.setWidget(body)
            return
        if p is None:
            assert a is not None  # noqa: S101 - narrowed above
            lay.addLayout(hbox(tile(PLUGIN_ICON.get(a["id"], "plug"), 72, accent=True),
                               vbox(label(esc(a["name"]), "H1", rich=True), label(esc(a["description"]), "Muted",
                                                                                     wrap=True), spacing=6), 1,
                               primary("Install…", "download", lambda: self.inspect(a["path"])), spacing=18))
            lay.addWidget(Note("It starts with nothing granted unless you switch it on in the next step.", "info"))
            lay.addStretch(1)
            self.detail.setWidget(body)
            return

        # head
        chips = hbox(label(esc(p["name"]), "H1", rich=True), Chip(p["version"], "line", mono=True),
                     Chip("official", "ok", "shield-ok") if p["official"] else Chip("third-party", "warn", "user"),
                     Chip("jailed · no files · no network", "line", "shield"), 1, spacing=8)
        src = "bundled with Pythonator" if p["official"] else "installed from a folder"
        desc = label(f"{esc(p['description'])} By {esc(p['publisher'] or 'unknown')} · {src}", "Muted", wrap=True, rich=True)
        onoff = Btn("Disable" if p["enabled"] else "Enable", "power", "" if p["enabled"] else "primary",
                    on=lambda: self.link.call("plugins.enable", self._got, id=p["id"], on=not p["enabled"]))
        rm = Btn("Remove", "trash", "danger", on=lambda: self.remove(p))
        if getattr(self, "narrow", False):
            lay.addLayout(hbox(Btn("Plugins", "back", "ghost", "sm", on=self.back_to_list), 1))
            chips.itemAt(3).widget().hide()  # type: ignore[union-attr]  # the jail chip: the panel below says it
            lay.addLayout(chips)
            lay.addWidget(desc)
            lay.addLayout(hbox(onoff, rm, 1, spacing=8))
        else:
            lay.addLayout(hbox(tile(PLUGIN_ICON.get(p["id"], "plug"), 72, accent=True), vbox(chips, desc, spacing=6), 1,
                               onoff, rm, spacing=18))
        if p["code_changed"]:
            n = len(p.get("new_perms", []))
            lay.addWidget(Note(f"<b>Version {esc(p['version'])} changed the plugin's code.</b> Its grants were reset "
                               "because they are tied to the content hash. Review the permissions below"
                               + (f"; {n} {'is' if n == 1 else 'are'} new." if n else "."), "warn", "warn"))
        elif p.get("update"):
            up = Btn("Update", "download", size="sm", on=lambda: self.inspect(self._bundled_path(p["id"])))
            lay.addWidget(frame("", hbox(Note("A newer official version ships with this Pythonator. Updating resets "
                                              "its grants; you grant them again.", "info", "download"), up, spacing=10)))
        if p["message"] and p["state"] in ("crashed", "refused"):
            lay.addWidget(Note(f"<b>{esc(p['state'])}</b>: {esc(p['message'])}", "bad", "warn"))
        if self.snap.get("safe_mode"):
            lay.addWidget(Note("Safe mode is on: no plugin runs. Turn it off at the bottom left.", "warn", "power"))

        # permissions + process
        services = [s["spec"]["name"] for s in self.main.services]
        perms = Panel("Permissions", info=InfoBtn("Granted one by one", "Each permission is one switch. Switch, then "
                                                  "Grant selected. Turning one off is always safe: the plugin keeps "
                                                  "working with less."),
                      right=[label("granted one by one", "Small")])
        perms.box.setContentsMargins(0, 0, 0, 0)
        perms.box.setSpacing(0)
        self.rows = []
        for perm in p["permissions"]:
            row = PermRow({**perm, "hosts": p.get("hosts", [])},
                          p.get("hosts", []) if perm["scope_kind"] == "hosts" else services, self._staged)
            self.rows.append(row)
            perms.box.addWidget(row)
        for sec in p.get("secrets", []):
            setb = Btn("Change…" if sec["set"] else "Set…", "key", size="sm", on=lambda k=sec["key"]: self.set_secret(p, k))
            perms.box.addWidget(frame("Row", hbox(tile("key", 34), vbox(label(sec["key"], "Mono"), label(
                "Kept in the OS keychain. The plugin never reads it; Pythonator fills it in on the way out.", "Small",
                wrap=True), spacing=2), 1, Chip("set" if sec["set"] else "not set", "ok" if sec["set"] else "warn"),
                setb, spacing=12, margins=(16, 11, 16, 11))))
        self.keep = Btn("Keep it off", on=lambda: self.keep_off(p))
        self.grant = primary("Grant selected", "check", lambda: self.apply_grants(p))
        self.perm_foot = frame("", hbox(1, self.keep, self.grant, spacing=10, margins=(16, 14, 16, 16)))
        perms.box.addWidget(self.perm_foot)
        self._staged(p)

        proc = Panel("Process", right=[Chip(f"{p['state']}" + (f" · {p['crashes']} crashes" if p.get("crashes") else ""),
                                            "ok" if p["state"] == "running" else "line", "clock")])
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(10)
        net = p["permissions"] and next((x for x in p["permissions"] if x["key"] == "net.connect"), None)
        host_txt = "own jail, no files, " + ("network only via Pythonator" if net and net["granted"] else "no network")
        rows = [
            ("Host", host_txt),
            ("Memory", f"{p.get('memory_mb', 0)} MB own · cap 256 MB" if p["state"] == "running" else "not running"),
            ("Messages", f"{p.get('rate', 0)}/s · cap 200/s"), ("Content hash", short(p["hash"])),
            ("Starts", " · ".join({"startup": "with Pythonator"}.get(x, x) for x in p.get("activation", ["startup"]))),
        ]
        for i, (k, v) in enumerate(rows):
            grid.addWidget(label(k, "Muted"), i, 0)
            val = hbox(label(v, "Mono"), spacing=6)
            if k == "Content hash":
                val.addWidget(Icon("fingerprint", "muted", 13))
                val.addWidget(InfoBtn(GRANTS_RESET[0], GRANTS_RESET[1], foot=GRANTS_RESET[2], ico="key"))
            val.addStretch(1)
            grid.addLayout(val, i, 1)
        grid.setColumnStretch(1, 1)
        proc.box.addLayout(grid)
        adds = Panel("Adds to the window", right=[Btn(ico="sliders", size="sm", tip="Settings", on=lambda: self.edit_settings(p)),
                                                   Btn(ico="list", size="sm", tip="Log", on=lambda: self.show_log(p))])
        added = [ADDS[x["key"]] for x in p["permissions"] if x["key"] in ADDS and x["granted"] is not None]
        for ico, text in added:
            adds.box.addLayout(hbox(Icon(ico, "muted", 15), label(text), 1, spacing=10))
        if p.get("status"):
            adds.box.addLayout(hbox(Icon("drawer", "muted", 15), label(f"Status now: “{esc(p['status'])}”", rich=True), 1,
                                    spacing=10))
        if not added:
            adds.box.addWidget(label("Nothing yet: it adds to the window only through permissions you grant. Views are "
                                     "drawn by Pythonator from data; a plugin can't draw in the window.", "Small",
                                     wrap=True))
        right = vbox(proc, adds, 1, spacing=16)
        from PyQt6.QtWidgets import QBoxLayout

        row = hbox(spacing=16)
        if getattr(self, "narrow", False):
            row.setDirection(QBoxLayout.Direction.TopToBottom)
        row.addWidget(perms, 3)
        row.addLayout(right, 2)
        lay.addLayout(row)

        # calls
        calls = Panel("Calls since Pythonator started", info=InfoBtn("Every call", "Each host call a plugin makes "
                      "needs one permission. Pythonator counts every call, and every refusal, here."))
        tbl = QTableWidget(0, 5)
        tbl.setHorizontalHeaderLabels(["CAPABILITY", "SCOPE", "CALLS", "REFUSED", "LAST"])
        tbl.verticalHeader().setVisible(False)  # type: ignore[union-attr]
        tbl.setShowGrid(False)
        tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        tbl.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        hh = tbl.horizontalHeader()
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft)  # type: ignore[union-attr]
        for c in range(5):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.Stretch)  # type: ignore[union-attr]
        permrow = {x["key"]: x for x in p["permissions"]}
        for k, c in sorted(p["calls"].items()):
            r = tbl.rowCount()
            tbl.insertRow(r)
            tbl.setRowHeight(r, 44)
            perm = permrow.get(k)
            vals = (k, scope_text({**perm, "hosts": p.get("hosts", [])}, perm["granted"]) if perm else "—",
                    f"{c['calls']:,}", "", datetime.fromtimestamp(c["last"]).strftime("%H:%M:%S") if c["last"] else "—")
            for col, v in enumerate(vals):
                it = QTableWidgetItem(v)
                it.setFont(th.mono_font(12.5))
                if col == 4:
                    it.setForeground(QColor(t.faint))
                tbl.setItem(r, col, it)
            ref = Chip(f"{c['refused']} · not granted", "bad") if c["refused"] else Chip("0", "line")
            tbl.setCellWidget(r, 3, frame("", hbox(ref, 1, margins=(8, 0, 0, 0))))
        tbl.setFixedHeight(40 + 44 * tbl.rowCount())
        if tbl.rowCount():
            calls.box.addWidget(tbl)
        else:
            calls.box.addWidget(label("No calls yet. Every call it makes is counted here, refused ones too.", "Muted"))
        if p.get("sent"):
            calls.box.addWidget(label("Network requests (through Pythonator)", "H3"))
            for s in p["sent"][-5:][::-1]:
                calls.box.addWidget(label(
                    f"<span style='font-family:\"{th.MONO_FAMILY}\";font-size:12px;color:{t.muted}'>"
                    f"{datetime.fromtimestamp(s['ts']):%H:%M:%S}</span> &nbsp;{esc(s.get('host') or s['url'])} · "
                    f"{esc(str(s.get('status') or s.get('error', '')))} · {s.get('sent', 0)} bytes out", rich=True))
        lay.addWidget(calls)
        lay.addStretch(1)
        self.detail.setWidget(body)

    def _bundled_path(self, pid: str) -> str:
        return str(Path(__file__).resolve().parent.parent / "resources" / "plugins" / pid)

    # actions ------------------------------------------------------------------------------------------------------
    def _staged(self, p: dict[str, Any] | None = None) -> None:
        p = p or next((x for x in self.snap["installed"] if x["id"] == self.current), None)
        if p is None or not hasattr(self, "perm_foot"):
            return
        dirty = any(r.dirty() for r in self.rows)
        show_if(self.perm_foot, dirty or p["code_changed"])
        self.grant.setEnabled(dirty)

    def apply_grants(self, p: dict[str, Any]) -> None:
        for r in self.rows:
            if not r.dirty():
                continue
            on, scope = r.wanted()
            if on:
                self.link.call("plugins.grant", None, id=p["id"], permission=r.perm["key"],
                               scope=scope if r.perm["scoped"] else True)
            else:
                self.link.call("plugins.revoke", None, id=p["id"], permission=r.perm["key"])
        self.link.call("plugins.reviewed", self._got, id=p["id"])

    def keep_off(self, p: dict[str, Any]) -> None:
        self.link.call("plugins.reviewed", self._got, id=p["id"])

    def remove(self, p: dict[str, Any]) -> None:
        if QMessageBox.question(self, "Remove", f"Remove {p['name']}? Its grants go with it.") == \
                QMessageBox.StandardButton.Yes:
            self.current = ""
            self.link.call("plugins.remove", self._got, id=p["id"])

    def set_secret(self, p: dict[str, Any], key: str) -> None:
        val, ok = QInputDialog.getText(self, f"{p['name']}: {key}", f"{key} (kept in the OS keychain; empty deletes it)",
                                       QLineEdit.EchoMode.Password)
        if ok:
            self.link.call("plugins.secret", self._got, id=p["id"], key=key, value=val)

    def edit_settings(self, p: dict[str, Any]) -> None:
        d = QDialog(self)
        d.setWindowTitle(f"{p['name']} settings")
        d.resize(560, 420)
        edit = QPlainTextEdit(json.dumps(p["settings"], indent=2))
        edit.setFont(th.mono_font(12.5))
        err = label("", "Small")
        save = primary("Save and restart plugin", "check")

        def go() -> None:
            try:
                data = json.loads(edit.toPlainText())
            except ValueError as e:
                err.setText(f"Not valid JSON: {e}")
                return
            self.link.call("plugins.settings", self._got, id=p["id"], settings=data)
            d.accept()

        save.clicked.connect(go)
        d.setLayout(vbox(label("Settings live beside the code, so saving them keeps its grants.", "Muted"), edit, err,
                         hbox(1, Btn("Cancel", on=d.reject), save, spacing=8), spacing=10, margins=(18, 18, 18, 18)))
        d.exec()

    def show_log(self, p: dict[str, Any]) -> None:
        d = QDialog(self)
        d.setWindowTitle(f"{p['name']} log")
        d.resize(820, 480)
        box = QPlainTextEdit("\n".join(f"{datetime.fromtimestamp(ts / 1000):%H:%M:%S}  {text}" for _n, ts, _s, text in
                                       p["log"]) or "Nothing yet.")
        box.setReadOnly(True)
        box.setFont(th.mono_font(12))
        d.setLayout(vbox(box, margins=(12, 12, 12, 12)))
        d.exec()

    def install_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Plugin folder", str(Path.home()))
        if d:
            self.inspect(d)

    def inspect(self, path: str) -> None:
        def got(res: Any, err: Any) -> None:
            if err:
                QMessageBox.warning(self, "Install", err["message"])
                return
            dlg = InstallDialog(self, res)
            dlg.open()
            self._dialog = dlg

        self.link.call("plugins.inspect", got, path=path)

    def toggle_safe(self) -> None:
        self.link.call("plugins.safe_mode", self._got, on=not self.snap.get("safe_mode"))

    # bisect ------------------------------------------------------------------------------------------------------
    def start_bisect(self) -> None:
        on = [p["id"] for p in self.snap["installed"] if p["enabled"]]
        if len(on) < 1:
            QMessageBox.information(self, "Find the plugin", "No plugin is on, so a plugin can't be the cause.")
            return
        if QMessageBox.question(self, "Find the plugin causing this",
                                f"{len(on)} plugins are on. Pythonator will turn half of them off at a time and ask you "
                                "whether the problem is still there. At the end every plugin is put back as it was."
                                ) != QMessageBox.StandardButton.Yes:
            return
        self.bisect = {"orig": on, "suspects": on}
        self._bisect_step()

    def _set_enabled(self, keep_on: list[str]) -> None:
        for pid in self.bisect["orig"]:  # type: ignore[index]
            self.link.call("plugins.enable", None, id=pid, on=pid in keep_on)

    def _bisect_step(self) -> None:
        b = self.bisect
        assert b is not None  # noqa: S101 - set by start_bisect
        sus = b["suspects"]
        if len(sus) == 1:
            name = next((p["name"] for p in self.snap["installed"] if p["id"] == sus[0]), sus[0])
            self._set_enabled(b["orig"])
            self.bisect = None
            self.current = sus[0]
            QMessageBox.information(self, "Found it", f"{name} is the one. Every plugin is back as it was; turn it off "
                                    "here, or remove it.")
            self.refresh()
            return
        half = sus[: len(sus) // 2]
        b["half"] = half
        self._set_enabled([x for x in b["orig"] if x not in sus or x in half])
        names = ", ".join(next((p["name"] for p in self.snap["installed"] if p["id"] == x), x) for x in half)
        r = QMessageBox.question(self, "Is it still happening?", f"On now: {names}. The other suspects are off.\n\n"
                                 "Try what went wrong. Is the problem still there?",
                                 QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                                 | QMessageBox.StandardButton.Cancel)
        if r == QMessageBox.StandardButton.Cancel:
            self._set_enabled(b["orig"])
            self.bisect = None
            self.refresh()
            return
        b["suspects"] = half if r == QMessageBox.StandardButton.Yes else [x for x in sus if x not in half]
        if not b["suspects"]:
            self._set_enabled(b["orig"])
            self.bisect = None
            QMessageBox.information(self, "Not a plugin", "It isn't one of the plugins. Every plugin is back as it was.")
            self.refresh()
            return
        self._bisect_step()
