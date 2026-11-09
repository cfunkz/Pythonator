"""Screen 9: the security timeline, Guard rules and exposure.
Every block, refusal, permission change and self-heal, from the background service's own log (security.jsonl),
with the fix one click away: Allow a blocked host, open the rule that refused a change, review a plugin whose
grants changed. Guard rules are data (plugin settings), switched here. Each service has an exposure score (0
sealed, 10 runs as you) that Guard can cap."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from PyQt6.QtCore import QPointF, QRect, QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPaintEvent, QPen
from PyQt6.QtWidgets import QFrame, QScrollArea, QSizePolicy, QToolTip, QWidget

from pythonator.ui import kit
from pythonator.ui import theme as th
from pythonator.ui.icons import icon
from pythonator.ui.kit import Btn, Chip, Flow, Icon, InfoBtn, Panel, Seg, Toggle, TwoColumns, clear, esc, frame, hbox, label, vbox

if TYPE_CHECKING:
    from pythonator.ui.app import Main

KIND = {  # icon, tone, chip text
    "blocked": ("x", "bad", "Blocked"),
    "refused": ("shield", "bad", "Refused"),
    "permission": ("key", "warn", "Permissions"),
    "healed": ("check", "ok", "Healed"),
}
SEVERITY = {"blocked": 3, "refused": 2, "permission": 1, "healed": 0}
EXPOSURE_INFO = (
    "Exposure score",
    "0 is fully isolated; 10 runs with your full permissions. Computed from the compiled isolation: mode is most of it, then traffic, the local "
    "network, ports, extra folders and secrets. Guard can refuse any change that pushes a service above your cap.",
    [("Full access", "starts at 6"), ("Any host", "+2"), ("Port to network", "+1 each")],
    "Recomputed on every save",
)
GUARD_INFO = (
    "Guard rules",
    "Rules are plugin settings, not code: Guard reads them and answers allow or refuse on every change before it "
    "is saved. Turning one off here takes effect on the next change.",
    [("Where", "Plugins → Guard → Settings"), ("Refusals", "listed on this timeline")],
    "",
)

def _icon_for(row: dict[str, Any]) -> str:
    if row["kind"] == "healed" and row.get("who") == "ledger":
        return "trash"
    if row["kind"] == "blocked" and "always blocked" in row.get("detail", ""):
        return "x"
    return KIND[row["kind"]][0]

class Strip(QWidget):
    """The dot timeline: one marker per moment (nearby events share one, the most severe shows)."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict[str, Any]] = []
        self.since = 0.0
        self.until = 1.0
        self.days = 1
        self.marks: list[tuple[float, dict[str, Any], int]] = []
        self.setFixedHeight(96)
        self.setMouseTracking(True)

    def set(self, rows: list[dict[str, Any]], since: float, until: float, days: int) -> None:
        self.rows, self.since, self.until, self.days = rows, since, until, days
        self._layout()
        self.update()

    def _x(self, ts: float) -> float:
        left, right = 32.0, self.width() - 32.0
        span = max(1.0, self.until - self.since)
        return left + (right - left) * min(1.0, max(0.0, (ts - self.since) / span))

    def _layout(self) -> None:
        self.marks = []
        for r in sorted(self.rows, key=lambda r: r["ts"]):
            x = self._x(r["ts"])
            if self.marks and x - self.marks[-1][0] < 30:
                px, pr, n = self.marks[-1]
                best = r if SEVERITY[r["kind"]] > SEVERITY[pr["kind"]] else pr
                self.marks[-1] = (px, best, n + 1)
            else:
                self.marks.append((x, r, 1))

    def resizeEvent(self, e: Any) -> None:
        super().resizeEvent(e)
        self._layout()

    def mouseMoveEvent(self, e: Any) -> None:
        pos = e.position()
        for x, r, n in self.marks:
            if abs(pos.x() - x) < 13 and abs(pos.y() - 38) < 13:
                more = f" (+{n - 1} nearby)" if n > 1 else ""
                QToolTip.showText(e.globalPosition().toPoint(), f"{r['who']}: {r['title']}{more}", self)
                return
        QToolTip.hideText()

    def paintEvent(self, _e: QPaintEvent | None) -> None:
        t = th.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        y = 38.0
        p.setPen(QPen(QColor(t.border_strong), 1.5))
        p.drawLine(QPointF(20, y), QPointF(self.width() - 20, y))
        p.setFont(th.mono_font(11))
        p.setPen(QColor(t.faint))
        ticks = 4 if self.days == 1 else min(self.days, 7)
        now = datetime.now().timestamp()
        if self.since < now < self.until - 60:
            nx = self._x(now)
            p.setPen(QPen(QColor(t.accent_text), 1.2, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(nx, y - 18), QPointF(nx, y + 18))
            p.drawText(QRect(int(nx) - 30, 4, 60, 14), Qt.AlignmentFlag.AlignCenter, "now")
            p.setPen(QColor(t.faint))
        for i in range(ticks + 1):
            ts = self.since + (self.until - self.since) * i / ticks
            if i == ticks and self.until - 60 <= now:
                text = "now"
            elif i == ticks and self.days == 1:
                text = "24:00"
            elif self.days == 1:
                text = datetime.fromtimestamp(ts).strftime("%H:%M")
            else:
                text = datetime.fromtimestamp(ts).strftime("%d %b")
            x = self._x(ts)
            r = QRect(int(x) - 40, 62, 80, 18)
            align = Qt.AlignmentFlag.AlignHCenter
            if i == 0:
                r = QRect(int(x) - 14, 62, 80, 18)
                align = Qt.AlignmentFlag.AlignLeft
            elif i == ticks:
                r = QRect(int(x) - 66, 62, 80, 18)
                align = Qt.AlignmentFlag.AlignRight
            p.drawText(r, align | Qt.AlignmentFlag.AlignVCenter, text)
        if not self.marks:
            p.setFont(th.ui_font(12))
            p.drawText(QRect(0, 4, self.width(), 24), Qt.AlignmentFlag.AlignCenter, "Quiet: nothing in this period.")
        for x, r, n in self.marks:
            tone = th.tone(KIND[r["kind"]][1])
            p.setPen(QPen(QColor(tone), 1.8))
            p.setBrush(QColor(th.rgba(tone, 0.16)))
            p.drawEllipse(QPointF(x, y), 12, 12)
            p.drawPixmap(int(x) - 7, int(y) - 7, icon(_icon_for(r), tone, 14).pixmap(14, 14))
            if n > 1:
                p.setFont(th.mono_font(9.5))
                p.setPen(QColor(t.muted))
                p.drawText(QRectF(x + 8, y - 24, 30, 12), Qt.AlignmentFlag.AlignLeft, f"{n}")

class Bar(QWidget):
    def __init__(self, name: str, score: float) -> None:
        super().__init__()
        self.name, self.score = name, score
        self.setFixedHeight(28)

    def paintEvent(self, _e: QPaintEvent | None) -> None:
        t = th.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setFont(th.ui_font(13))
        p.setPen(QColor(t.text))
        p.drawText(QRect(0, 0, 150, 28), Qt.AlignmentFlag.AlignVCenter, self.name)
        track = QRectF(156, 11, max(20, self.width() - 156 - 56), 7)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(t.chip))
        p.drawRoundedRect(track, 3.5, 3.5)
        col = t.ok if self.score < 4 else t.warn if self.score < 7 else t.bad
        p.setBrush(QColor(col))
        p.drawRoundedRect(QRectF(track.left(), track.top(), max(7.0, track.width() * self.score / 10), 7), 3.5, 3.5)
        p.setFont(th.mono_font(12.5))
        p.setPen(QColor(t.text))
        p.drawText(QRect(self.width() - 50, 0, 50, 28), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   f"{self.score:.1f}")

class SecurityPage(QWidget):
    def __init__(self, main: Main) -> None:
        super().__init__()
        self.setObjectName("Page")
        self.main = main
        self.link = main.link
        self.days = 1
        self.only = ""
        self.data: dict[str, Any] = {}

        # left: head, chips, strip, rows
        self.seg = Seg([("1", "Today"), ("7", "7 days"), ("30", "30 days")], "1")
        self.seg.changed.connect(self._days)
        info = InfoBtn("Security timeline", "Every refusal, block, permission change and self-heal, written by the "
                       "background service as it happens. Services and plugins can't add to it or remove from it.",
                       [("Kept", "30 days"), ("File", "security.jsonl, private")])
        self.sub = label("Every refusal, block and permission change, from the background service's own log.", "Muted")
        self.sub.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        head = hbox(label("Security", "H1"), info, self.sub, self.seg, spacing=10)
        head.setStretch(2, 1)
        self.chips = Flow(8)
        chips_w = QWidget()
        chips_w.setLayout(self.chips)
        self.strip = Strip()
        strip_box = frame("Panel", vbox(self.strip, margins=(4, 4, 4, 4)))
        self.list_box = vbox(spacing=0)
        self.list_frame = frame("Panel", self.list_box)
        left_body = QWidget()
        left_body.setObjectName("Page")
        lv = vbox(head, chips_w, strip_box, self.list_frame, 1, spacing=16, margins=(26, 20, 22, 24))
        left_body.setLayout(lv)

        # right: guard rules and exposure (under the timeline below 1000 px)
        self.side = frame("SidePanel")
        self.side_box = vbox(spacing=16, margins=(20, 20, 20, 20))
        self.side.setLayout(self.side_box)
        cols = TwoColumns(left_body, self.side, 430)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(cols)
        self.setLayout(hbox(scroll, spacing=0))
        self._build()

    def resizeEvent(self, e: Any) -> None:
        super().resizeEvent(e)
        kit.rebuild_when_narrow(self)

    # data -------------------------------------------------------------------------------------------------------
    def refresh(self) -> None:
        self.link.call("security.list", self._got, days=self.days)

    def _got(self, res: Any, err: Any) -> None:
        if not err and isinstance(res, dict):
            self.data = res
            self._build()

    def _days(self, key: str) -> None:
        self.days = int(key)
        self.refresh()

    def _filter(self, kind: str) -> None:
        self.only = "" if self.only == kind else kind
        self._build()

    def allow(self, row: dict[str, Any]) -> None:
        def done(_r: Any, err: Any) -> None:
            if err:
                self.main.after_err(None, err)
            else:
                self.main.toast("ok", row["who"], f"{row['action'].get('host')} added to allowed hosts")
            self.refresh()

        self.link.call("security.allow", done, id=row["id"])

    def open_plugin(self, pid: str) -> None:
        self.main.go("plugins")
        pg = self.main.lazy.get("plugins")
        if pg is not None:
            pg.current = pid
            pg.refresh()

    def open_service(self, name: str) -> None:
        self.main.go("services")
        self.main.pick(name)

    def set_rule(self, key: str, on: bool) -> None:
        def done(_r: Any, err: Any) -> None:
            if err:
                self.main.after_err(None, err)
            self.refresh()

        self.link.call("security.rule", done, key=key, on=on)

    # layout -----------------------------------------------------------------------------------------------------

    def _build(self) -> None:
        d = self.data
        rows = d.get("rows", [])
        counts = d.get("counts", {})
        self.chips.clear()
        allc = Chip(f"All {len(rows)}", "acc" if not self.only else "line")
        allc.setCursor(Qt.CursorShape.PointingHandCursor)
        allc.clicked.connect(lambda: self._filter(""))
        self.chips.add(allc)
        for k, (ico, tone, text) in KIND.items():
            c = Chip(f"{text} {counts.get(k, 0)}", tone if self.only in ("", k) else "line", ico)
            c.setCursor(Qt.CursorShape.PointingHandCursor)
            c.clicked.connect(lambda k=k: self._filter(k))
            self.chips.add(c)
        self.chips.invalidate()
        shown = [r for r in rows if not self.only or r["kind"] == self.only]
        now = datetime.now().timestamp()
        since = float(d.get("since", now - 86400))
        self.strip.set(shown, since, since + 86400 if int(d.get("days", 1)) == 1 else now, int(d.get("days", 1)))

        clear(self.list_box)
        if not shown:
            self.list_box.addWidget(frame("", hbox(label(
                "Nothing here yet. Blocks, refusals, permission changes and self-heals show up as they happen."
                if not rows else "Nothing of this kind in this period.", "Muted"), margins=(18, 18, 18, 18))))
        for i, r in enumerate(reversed(shown[-200:])):
            self.list_box.addWidget(self._row(r, last=i == min(len(shown), 200) - 1))
        self._side()

    def _row(self, r: dict[str, Any], last: bool) -> QWidget:
        _ico, tone, _ = KIND[r["kind"]]
        f = QFrame()
        f.setObjectName("Row" if not last else "")
        ic = Icon(_icon_for(r), tone, 16)
        ic.setFixedWidth(26)
        when = datetime.fromtimestamp(r["ts"])
        tm = label(when.strftime("%H:%M") if self.days == 1 else when.strftime("%d %b %H:%M"), "MonoMuted")
        tm.setFixedWidth(110 if self.days > 1 else 64)
        narrow = self.width() < 700
        who = label(r["who"], "H3")
        who.setFixedWidth(110 if narrow else 200)
        who.setToolTip(r["who"])
        title = r["title"] + (f"  ×{r['count']}" if r.get("count", 1) > 1 else "")
        t1 = label(esc(title), rich=True)
        t1.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        t2 = label(r.get("detail", ""), "MonoMuted")
        t2.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        text = vbox(t1, t2, spacing=2)
        act = r.get("action") or {}
        if act.get("done"):
            btn: QWidget = label(act["done"], "Faint")
        elif act.get("kind") == "allow":
            btn = Btn("Allow", "check", "primary", "sm", on=lambda r=r: self.allow(r),
                      tip=f"Add {act.get('host')} to {act.get('service')}'s allowed hosts")
        elif act.get("kind") == "rule":
            btn = Btn("Open rule", size="sm", on=lambda: self.open_plugin("guard"))
        elif act.get("kind") == "review":
            pid = str(act.get("plugin", ""))
            btn = Btn("Review", size="sm", on=lambda: self.open_plugin(pid))
        elif act.get("kind") == "open":
            svc = str(act.get("service", ""))
            btn = Btn("Open", size="sm", on=lambda: self.open_service(svc))
        else:
            btn = label("no action", "Faint")
        if narrow:  # two lines: who and the button, then what happened
            top = hbox(ic, tm, who, 1, btn, spacing=8)
            lay = vbox(top, text, spacing=6, margins=(14, 10, 12, 10))
        else:
            lay = hbox(ic, tm, who, text, btn, spacing=12, margins=(18, 12, 14, 12))
            lay.setStretch(3, 1)
        f.setLayout(lay)
        f.setMinimumHeight(64)
        return f

    def _side(self) -> None:
        clear(self.side_box)
        g = self.data.get("guard", {})
        s = g.get("settings", {})
        home = g.get("home", "")
        rules = Panel("Guard rules", "shield", info=InfoBtn(*GUARD_INFO), right=[Chip("data, not code", "line")])
        rules.box.setContentsMargins(0, 0, 0, 0)
        rules.box.setSpacing(0)
        if not g.get("installed"):
            rules.box.addWidget(frame("", vbox(label("Guard isn't installed. It checks every change against your rules "
                                                     "before it is saved.", "Muted", wrap=True),
                                               Btn("Open Plugins", "plug", size="sm", on=lambda: self.open_plugin("guard")),
                                               spacing=10, margins=(18, 14, 18, 16))))
        else:
            roots = [x.replace(home, "~") if home else x for x in s.get("full_access_only_inside", [])]
            lo, hi = s.get("port_range", [1024, 65535])
            items = [
                ("full_access_rule", f"Full access only inside {', '.join(roots) or 'nowhere'}", s.get("full_access_rule", True), True),
                ("port_rule", f"Ports {lo}–{hi}" + ("" if s.get("lan_ports") else ", this computer only"),
                 s.get("port_rule", True), True),
                ("exposure_rule", f"Exposure score must stay ≤ {float(s.get('max_exposure', 6)):g}", s.get("exposure_rule", False), True),
                ("", "New plugins start with no permissions", True, False),
                ("block_lan", "Block LAN for all services", s.get("block_lan", False), True),
            ]
            live = g.get("enabled") and g.get("granted") and g.get("state") == "running"
            for i, (key, text, on, editable) in enumerate(items):
                tog = Toggle(bool(on), "Built into Pythonator: always on" if not editable else "")
                tog.setEnabled(editable and bool(g.get("enabled")))
                if editable:
                    tog.toggled.connect(lambda v, k=key: self.set_rule(k, v))
                t = label(text, wrap=True)
                row = frame("Row" if i < len(items) - 1 else "", hbox(Icon("shield", "muted", 15), t, tog, spacing=12,
                                                                     margins=(18, 13, 18, 13)))
                row.layout().setStretch(1, 1)  # type: ignore[union-attr]
                rules.box.addWidget(row)
            if not live:
                why = ("Guard is off, so these rules aren't checked." if not g.get("enabled") else
                       "Guard isn't allowed to review changes (Review changes permission)." if not g.get("granted") else
                       f"Guard isn't running ({g.get('state')}), so every change is refused until it is.")
                rules.box.addWidget(frame("", hbox(Icon("warn", "warn", 14), label(why, "Small", wrap=True), 1,
                                                   spacing=8, margins=(18, 4, 18, 14))))
        self.side_box.addWidget(rules)

        exp = Panel("Exposure", "gauge", info=InfoBtn(*EXPOSURE_INFO[:2], EXPOSURE_INFO[2], EXPOSURE_INFO[3]),
                    right=[label("lower is safer", "Small")])
        exp.box.setSpacing(4)
        exp.box.setContentsMargins(18, 14, 18, 16)
        rows = self.data.get("exposure", [])
        if not rows:
            exp.box.addWidget(label("No services yet.", "Muted"))
        for e in rows:
            b = Bar(e["name"], float(e["score"]))
            b.setToolTip(f"{e['name']}: {e['mode']} mode")
            exp.box.addWidget(b)
        cap = s.get("max_exposure") if g.get("installed") and s.get("exposure_rule") else None
        if cap is not None:
            over = [e["name"] for e in rows if e["score"] > float(cap)]
            if over:
                exp.box.addWidget(label(f"Over the cap of {float(cap):g}: {', '.join(over)}. Guard refuses changes "
                                        "that keep them there; narrowing is always allowed.", "Small", wrap=True))
        self.side_box.addWidget(exp)
        self.side_box.addStretch(1)
