"""Screen 10: System check. What protects a service here, how fast this machine is, and what would help.
The protection table is the jail compiler's own answer: this machine's column is compiled with its real facts, the
other two with the reference machines (the same compiler). A layer that can't be enforced is refused with the fix,
never "cooperative". Every speed number is measured here and kept for 30 days with a budget beside it, and every
hint comes from a probe, a measurement or one of your services."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPaintEvent
from PyQt6.QtWidgets import QApplication, QFrame, QGridLayout, QScrollArea, QSizePolicy, QWidget

from pythonator.ui import theme as th
from pythonator.ui.kit import (
    Btn,
    Chip,
    Icon,
    InfoBtn,
    Note,
    Panel,
    Seg,
    TwoColumns,
    clear,
    esc,
    frame,
    hbox,
    label,
    paint_spark,
    rebuild_when_narrow,
    vbox,
)

if TYPE_CHECKING:
    from pythonator.ui.app import Main

LABELS = {
    "sandbox_ms": ("Sandbox start", "ms"), "container_ms": ("Container start", "ms"),
    "plugin_ms": ("Plugin start", "ms"), "daemon_mb": ("Service, idle", "MB"), "first_paint_ms": ("First paint", "ms"),
    "ui_stall_ms": ("Worst UI stall", "ms"),
}
OS_NAME = {"linux": "Linux", "darwin": "macOS", "windows": "Windows"}
STATUS = {  # tone, icon, words
    "enforced": ("ok", "check", "enforced"),
    "reduced": ("warn", "warn", "reduced"),
    "refused": ("bad", "x", "refused"),
    "not-enforced": ("bad", "unlock", "not enforced"),
    "off": ("", None, "off"),
}
MODES = [("sandbox", "Sandbox", "shield"), ("container", "Container", "box"), ("wsl", "WSL", "console"),
         ("full", "Full access", "unlock")]
NEVER = ("<b>Strict enforcement:</b> if a protection cannot be enforced, the service is not started, and the "
         "remedy is shown (for example, on Windows a host allow-list requires WSL mode). The same table drives the "
         "(i) cards, the first console line and the documentation.")

class Spark(QWidget):
    def __init__(self, values: list[float], warn: bool) -> None:
        super().__init__()
        self.values, self.warn = values, warn
        self.setFixedSize(112, 24)

    def paintEvent(self, _e: QPaintEvent | None) -> None:
        t = th.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if len(self.values) < 2:
            p.setPen(QColor(t.faint))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignVCenter, "—")
            return
        paint_spark(p, QRectF(2, 3, 108, 18), self.values[-30:], t.warn if self.warn else t.accent_text, fill=False)

def status_cell(cell: dict[str, str] | None, here: bool) -> QWidget:
    f = QFrame()
    t = th.current()
    if here:
        f.setStyleSheet(f"QFrame#Cell {{ background: {th.rgba(t.accent, 0.05)}; }}")
    f.setObjectName("Cell")
    if cell is None:
        pill = frame("Pill", hbox(label("n/a", "Muted"), 1, margins=(10, 0, 10, 0)))
        pill.setStyleSheet(f"QFrame#Pill {{ border: 1px solid {t.border}; border-radius: {th.RC}px; }}")
        pill.setFixedHeight(28)
        pill.setToolTip("Not a layer in this mode on this system.")
        lay = vbox(pill, 1, spacing=4, margins=(12, 10, 12, 10))
    else:
        tone, ico, words = STATUS.get(cell["status"], ("", None, cell["status"]))
        col = th.tone(tone) if tone else t.muted
        text = words if cell["status"] != "reduced" else (cell.get("by", "").split(" ")[0] if cell.get("by") == "watchdog" else words)
        if cell["status"] == "refused" and cell.get("by", "").startswith("NET-"):
            text = "refused"
        word = label(text)
        word.setStyleSheet(f"color: {col}; background: transparent; font-weight: 500;")
        pill_lay = hbox(Icon(ico, tone, 13) if ico else None, word, 1, spacing=6, margins=(10, 0, 10, 0))
        pill = frame("Pill", pill_lay)
        pill.setStyleSheet(f"QFrame#Pill {{ background: {th.rgba(col, 0.16)}; border-radius: {th.RC}px; }}")
        pill.setFixedHeight(28)
        by = label(cell.get("detail", "") if text == cell.get("by") and cell.get("detail") else cell.get("by", ""), "MonoMuted")
        by.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        tip = cell.get("detail", "") + (f"\nFix: {cell['fix']}" if cell.get("fix") else "")
        f.setToolTip(tip.strip())
        lay = vbox(pill, by, spacing=5, margins=(12, 10, 12, 10))
    f.setLayout(lay)
    return f

class SystemCheck(QWidget):
    def __init__(self, main: Main) -> None:
        super().__init__()
        self.setObjectName("Page")
        self.main = main
        self.link = main.link
        self.mode = "sandbox"
        self.data: dict[str, Any] = {}
        self.measuring = False

        self.left_box = vbox(spacing=16, margins=(26, 20, 22, 24))
        lb = QWidget()
        lb.setObjectName("Page")
        lb.setLayout(self.left_box)
        self.side_box = vbox(spacing=16, margins=(20, 20, 20, 20))
        side = frame("SidePanel", self.side_box)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(TwoColumns(lb, side, 450))
        self.setLayout(hbox(scroll, spacing=0))
        self._build()

    def resizeEvent(self, e: Any) -> None:
        super().resizeEvent(e)
        rebuild_when_narrow(self)

    # data -------------------------------------------------------------------------------------------------------
    def refresh(self) -> None:
        self.link.call("check", self._got, mode=self.mode)

    def _got(self, res: Any, err: Any) -> None:
        if err:
            self.data = {"error": err.get("message", str(err)) if isinstance(err, dict) else str(err)}
        elif isinstance(res, dict):
            self.data = res
        self._build()

    def _mode(self, key: str) -> None:
        self.mode = key
        self.refresh()

    def measure(self) -> None:
        self.measuring = True
        self._build()

        def done(_r: Any, _e: Any) -> None:
            self.measuring = False
            self.refresh()

        self.link.call("measure", done)

    def _act(self, action: str) -> None:
        kind, _, arg = action.partition(":")
        if kind == "copy":
            QApplication.clipboard().setText(arg)
            self.main.toast("ok", "System check", f"Copied: {arg}")
        elif kind == "measure":
            self.measure()
        elif kind == "pull":
            self.main.toast("ok", "Engines", f"Downloading {arg}'s image…")
            self.link.call("engines.pull", lambda _r, e: (self.main.after_err(None, e) if e else None, self.refresh()), name=arg)
        elif kind == "open":
            self.main.go("services")
            self.main.pick(arg)
            self.main.open_settings(arg, "general")
        elif kind == "plugin-timings":
            self.main.go("plugins")

    def _copy(self) -> None:
        d = self.data
        lines = [(d.get("host") or {}).get("describe", "")]
        lines += [f"{p['title']}: {p['detail']}" for p in d.get("probes", [])]
        lines += [f"{s['key']}: {s['now']} (budget {s['budget']})" for s in d.get("speed", [])]
        for col in (d.get("matrix") or {}).get("columns", []):
            if col.get("here"):
                lines += [f"{k}: {v['status']} · {v['by']}" for k, v in col["cells"].items()]
        home = str(Path.home())
        QApplication.clipboard().setText("\n".join(lines).replace(home, "~"))  # no account name in a bug report
        self.main.toast("ok", "System check", "Copied for a bug report (no secrets, no paths beyond the OS).")

    # layout -----------------------------------------------------------------------------------------------------

    def _build(self) -> None:
        clear(self.left_box)
        clear(self.side_box)
        narrow = 50 < self.width() < 700
        self.left_box.setContentsMargins(*((12, 14, 12, 16) if narrow else (26, 20, 22, 24)))
        self.side_box.setContentsMargins(*((12, 8, 12, 16) if narrow else (20, 20, 20, 20)))
        d = self.data
        host = d.get("host") or {}
        info = InfoBtn("System check", "What protects a service on this machine, measured here. The table is the isolation "
                       "compiler's own answer; macOS and Windows are the same compiler run for a typical machine.")
        sub = label(f"{host.get('describe', '…')} · this machine", "Muted")
        sub.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        head = hbox(label("System check", "H1"), info, sub, Btn(ico="restart", tip="Run the probes again", on=self.refresh), spacing=10)
        head.setStretch(2, 1)
        self.left_box.addLayout(head)
        if d.get("error"):
            self.left_box.addWidget(Note(esc(d["error"]), "bad"))
        narrow = self.width() < 700
        seg = Seg([(k, "Full" if narrow and k == "full" else t, None if narrow else i) for k, t, i in MODES], self.mode)
        seg.changed.connect(self._mode)
        self.left_box.addLayout(hbox(seg, 1, None if narrow else Chip("this column is compiled here", "line", "check"),
                                     spacing=10))
        self.left_box.addWidget(self._table(d.get("matrix") or {}))
        self.left_box.addWidget(self._hints(d.get("hints", [])))
        self.left_box.addStretch(1)
        self._side(d)

    def _table(self, m: dict[str, Any]) -> QWidget:
        t = th.current()
        p = frame("Panel")
        g = QGridLayout()
        g.setSpacing(0)
        g.setContentsMargins(0, 0, 0, 0)
        cols = m.get("columns", [])
        if self.width() < 700:  # one pane: this machine only (the reference columns are on wider windows)
            cols = [c for c in cols if c.get("here")]
        hdr = label("PROTECTION", "Faint")
        hdr.setStyleSheet("font-size: 11px; font-weight: 600; letter-spacing: 0.6px;")
        g.addWidget(frame("", hbox(hdr, margins=(14, 12, 12, 12))), 0, 0)
        for c, col in enumerate(cols, start=1):
            name = OS_NAME.get(col["os"], col["os"]).upper()
            text = (f"<span style='color:{t.accent_text};font-weight:600'>{name} · THIS MACHINE</span>" if col["here"]
                    else f"<span style='font-weight:600'>{name}</span> <span style='color:{t.faint}'>· reference</span>")
            hl = label(text, rich=True)
            hl.setStyleSheet(f"font-size: 11px; letter-spacing: 0.6px; color: {t.muted};")
            g.addWidget(frame("", hbox(hl, margins=(12, 12, 12, 12))), 0, c)
        for r, row in enumerate(m.get("rows", []), start=1):
            line = f"border-top: 1px solid {t.border};"
            title = frame("", hbox(label(row["title"], wrap=True), margins=(14, 0, 12, 0)))
            title.setStyleSheet(line)
            title.setMinimumHeight(66)
            g.addWidget(title, r, 0)
            for c, col in enumerate(cols, start=1):
                if col.get("refused"):
                    if r == 1:
                        ref = col["refused"]
                        box = vbox(Chip("not here", "line"), label(esc(ref["message"]), "Small", wrap=True), spacing=6,
                                   margins=(12, 12, 12, 12))
                        box.addStretch(1)
                        w = frame("", box)
                        w.setToolTip(ref.get("fix", ""))
                        w.setStyleSheet(line)
                        g.addWidget(w, r, c, len(m["rows"]), 1)
                    continue
                cell = status_cell(col["cells"].get(row["key"]), col["here"])
                cell.setStyleSheet(cell.styleSheet() + f" QFrame#Cell {{ {line} }}")
                g.addWidget(cell, r, c)
        g.setColumnStretch(0, 10)
        for c in range(1, len(cols) + 1):
            g.setColumnStretch(c, 9)
        if not cols:
            g.addWidget(label("Running the probes…", "Muted"), 1, 0, 1, 2)
            g.setContentsMargins(16, 0, 16, 16)
        p.setLayout(g)
        return p

    def _hints(self, hints: list[dict[str, str]]) -> QWidget:
        p = Panel("What would help on this machine", "sparkle",
                  info=InfoBtn("Hints", "From the probes, the last 30 days of measurements and your own services. "
                               "Each has the one-click fix, or the command to run."),
                  right=[] if 50 < self.width() < 700 else [label("from the probes and the last 30 days", "Small")])
        if not hints:
            p.box.addWidget(label("Nothing right now. Every layer this machine can enforce is enforced.", "Muted"))
            return p
        g = QGridLayout()
        g.setSpacing(12)
        t = th.current()
        for i, h in enumerate(hints[:6]):
            tone = "warn" if h.get("level") == "warn" else "info"
            card = frame("Card")
            card.setStyleSheet(f"#Card {{ background: {th.rgba(th.tone(tone), 0.07)}; border: 1px solid "
                               f"{th.rgba(th.tone(tone), 0.35)}; border-radius: {th.RCARD}px; }}")
            body = label(f"<b>{esc(h['title'])}</b> <span style='color:{t.muted}'>{esc(h.get('detail', ''))}</span>",
                         wrap=True, rich=True)
            body.setMinimumWidth(10)
            lay = vbox(Icon(h.get("ico") or ("warn" if tone == "warn" else "info"), tone, 16), body, spacing=8,
                       margins=(14, 14, 14, 14))
            act = h.get("action", "")
            if act:
                text = h.get("label") or ("Copy: " + act[5:] if act.startswith("copy:") else
                                          "Measure again" if act == "measure" else "Plugin timings"
                                          if act == "plugin-timings" else "Open")
                ico = {"pull": "download", "copy": "copy", "open": "shield", "measure": "restart"}.get(act.split(":")[0])
                lay.addLayout(hbox(Btn(text, ico, size="sm", on=lambda a=act: self._act(a)), 1))
            lay.addStretch(1)
            card.setLayout(lay)
            per = 1 if self.width() < 700 else 3
            g.addWidget(card, i // per, i % per)
        for c in range(3):
            g.setColumnStretch(c, 1)
        p.box.addLayout(g)
        return p

    def _side(self, d: dict[str, Any]) -> None:
        t = th.current()
        speed = d.get("speed", [])
        last = max((s.get("measured_at") or 0 for s in speed), default=0)
        ago = ""
        if last:
            mins = int((datetime.now().timestamp() - last) // 60)
            ago = "measured just now" if mins < 1 else f"measured {mins} min ago" if mins < 120 else \
                f"measured {datetime.fromtimestamp(last):%d %b %H:%M}"
        sp = Panel("Speed on this machine", "gauge",
                   info=InfoBtn("Speed", "Measured on this machine, kept for 30 days, with the budget each must stay "
                                "under. Sandbox and container starts are real starts; the window reports its own "
                                "first paint and worst stall."),
                   right=[label(ago or "not measured yet", "Small")])
        g = QGridLayout()
        g.setHorizontalSpacing(14)
        g.setVerticalSpacing(10)
        for c, title in enumerate(("MEASURE", "NOW", "LAST 30 DAYS" if self.width() >= 700 else "", "BUDGET")):
            h = label(title, "Faint")
            h.setStyleSheet("font-size: 11px; font-weight: 600; letter-spacing: 0.6px;")
            g.addWidget(h, 0, c, alignment=Qt.AlignmentFlag.AlignRight if c in (1, 3) else Qt.AlignmentFlag.AlignLeft)
        for r, s in enumerate(speed, start=1):
            name, unit = LABELS.get(s["key"], (s["key"], ""))
            over = s["now"] is not None and s["now"] > s["budget"]
            g.addWidget(label(name), r, 0)
            now = label("—" if s["now"] is None else f"{s['now']:g} {unit}", "Mono")
            now.setStyleSheet(f"font-size: 12.5px; color: {t.warn if over else t.accent_text};")
            g.addWidget(now, r, 1, alignment=Qt.AlignmentFlag.AlignRight)
            if self.width() >= 700:
                g.addWidget(Spark(s.get("series", []), over), r, 2)
            b = label(f"≤ {s['budget']:g}" + (" MB" if unit == "MB" else ""), "MonoMuted")
            g.addWidget(b, r, 3, alignment=Qt.AlignmentFlag.AlignRight)
        g.setColumnStretch(0, 1)
        sp.box.addLayout(g)
        mb = Btn("Measuring…" if self.measuring else "Measure again · ~8 s", "restart", size="sm", on=self.measure)
        mb.setEnabled(not self.measuring)
        sp.box.addLayout(hbox(mb, Btn("Copy for a bug report", "copy", size="sm", on=self._copy), 1, spacing=8))
        self.side_box.addWidget(sp)

        probes = d.get("probes", [])
        bad = [x for x in probes if x["status"] != "ok"]
        pr = Panel("Probes", "check" if not bad else "warn",
                   info=InfoBtn("Probes", "Read-only checks of what this machine offers: the isolation tools, the kernel "
                                "features they need, and the engines."),
                   right=[label(f"{d.get('probe_ms', 0)} ms", "Small")] if probes else [])
        for x in probes:
            tone = {"ok": "ok", "warn": "warn", "bad": "bad"}.get(x["status"], "muted")
            txt = label(f"{esc(x['title'])} <span style='color:{t.muted}'>· {esc(x['detail'])}</span>", wrap=True, rich=True)
            if x.get("fix"):
                txt.setToolTip(f"Fix: {x['fix']}")
            row = hbox(Icon("check" if x["status"] == "ok" else "warn", tone, 14), txt, spacing=10)
            row.setStretch(1, 1)
            pr.box.addLayout(row)
        if not probes:
            pr.box.addWidget(label("Running…", "Muted"))
        self.side_box.addWidget(pr)
        self.side_box.addWidget(Note(NEVER, "warn"))
        self.side_box.addStretch(1)
