"""Screen 11: the command palette (Ctrl K). Services, actions, settings, files and plugins in one list.
Every button in the window has an entry here, with its shortcut. Typing filters by letters in order (prefixes of
words rank first); ↑ ↓ move, ↵ runs, Tab shows more actions for the selected entry (a service's start, stop, logs,
settings), Esc closes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from PyQt6.QtCore import QEvent, QObject, Qt
from PyQt6.QtGui import QColor, QKeyEvent
from PyQt6.QtWidgets import QFrame, QGraphicsDropShadowEffect, QLineEdit, QScrollArea, QSizePolicy, QWidget

from pythonator.ui import theme as th
from pythonator.ui.kit import Chip, Icon, Kbd, clear, esc, frame, hbox, label, show_if, vbox
from pythonator.ui.widgets import meta, mode_label, needs_you

if TYPE_CHECKING:
    from pythonator.ui.app import Main

SECTIONS = ("Services", "Actions", "Settings", "Files", "Plugins", "Go to")
MAX_ROWS = 60
PER_SECTION = 5

@dataclass
class Entry:
    section: str
    ico: str
    title: str
    hint: str = ""
    keys: str = ""
    run: Callable[[], object] | None = None
    more: list[Entry] = field(default_factory=list)
    words: str = ""  # extra text to match (group, mode)

def score(query: str, text: str) -> tuple[int, list[int]] | None:
    """Higher is better, plus which characters matched. None when the letters aren't all there, in order."""
    q = query.lower().strip()
    if not q:
        return 0, []
    t = text.lower()
    i = t.find(q)
    if i >= 0:
        word_start = i == 0 or not t[i - 1].isalnum()
        return (300 if i == 0 else 200 if word_start else 100) - min(i, 50), list(range(i, i + len(q)))
    pos, hits, gaps = 0, [], 0
    for ch in q:
        j = t.find(ch, pos)
        if j < 0:
            return None
        gaps += j - pos
        hits.append(j)
        pos = j + 1
    return max(1, 60 - gaps), hits

def bold(text: str, hits: list[int]) -> str:
    out, hs = [], set(hits)
    for i, ch in enumerate(text):
        out.append(f"<b>{esc(ch)}</b>" if i in hs else esc(ch))
    return "".join(out)

class Row(QFrame):
    def __init__(self, e: Entry, html: str, on: Callable[[], None]) -> None:
        super().__init__()
        self.entry, self.on = e, on
        self.setObjectName("PalRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        title = label(html, rich=True)
        title.setStyleSheet("font-size: 14px;")
        hint = label(f"· {e.hint}" if e.hint else "", "Muted")
        hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.enter = Kbd("↵")
        lay = hbox(Icon(e.ico, "text", 16), title, hint, Kbd(e.keys) if e.keys else None, self.enter, spacing=10,
                   margins=(12, 0, 12, 0))
        lay.setStretch(2, 1)
        self.setLayout(lay)
        self.setFixedHeight(44)
        self.set_selected(False)

    def set_selected(self, on: bool) -> None:
        t = th.current()
        bg = t.raised2 if on else "transparent"
        show_if(self.enter, on)
        self.setStyleSheet(f"#PalRow {{ background: {bg}; border-radius: {th.RC}px; }}")

    def mousePressEvent(self, _e: Any) -> None:
        self.on()

class Palette(QFrame):
    def __init__(self, main: Main, text: str = "") -> None:
        super().__init__(main)
        self.main = main
        self.setObjectName("Palette")
        t = th.current()
        self.setStyleSheet(f"#Palette {{ background: {t.pop}; border: 1px solid {t.border_strong}; "
                           f"border-radius: {th.RCARD}px; }}")
        sh = QGraphicsDropShadowEffect(self)
        sh.setBlurRadius(40)
        sh.setOffset(0, 12)
        sh.setColor(QColor(0, 0, 0, 110))
        self.setGraphicsEffect(sh)
        self.edit = QLineEdit(text)
        self.edit.setPlaceholderText("Type a service, a file or a command…")
        self.edit.setFrame(False)
        self.edit.setStyleSheet("font-size: 16px; background: transparent; border: 0;")
        self.edit.textChanged.connect(self._filter)
        self.edit.installEventFilter(self)
        self.count = Chip("", "line")
        head = frame("", hbox(Icon("search", "text", 18), self.edit, self.count, spacing=12, margins=(18, 12, 14, 12)))
        head.setStyleSheet(f"QFrame {{ border-bottom: 1px solid {t.border}; }} QLineEdit, QLabel {{ border: 0; }}")
        self.rows_box = vbox(spacing=2, margins=(10, 6, 10, 8))
        body = QWidget()
        body.setLayout(self.rows_box)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(body)
        self.scroll.setStyleSheet("background: transparent;")
        foot = frame("", hbox(Kbd("↑"), Kbd("↓"), label("move", "Muted"), 6, Kbd("↵"), label("run", "Muted"), 6,
                              Kbd("Tab"), label("more actions", "Muted"), 6, Kbd("Esc"), label("close", "Muted"), 1,
                              label("every button has a palette entry", "Muted"), spacing=5, margins=(18, 10, 18, 10)))
        foot.setStyleSheet(f"QFrame {{ border-top: 1px solid {t.border}; }} QLabel {{ border: 0; }}")
        self.setLayout(vbox(head, self.scroll, foot, spacing=0))
        self.entries = self._entries()
        self.stack: list[list[Entry]] = []
        self.files: list[str] = []
        self.rows: list[Row] = []
        self.sel = 0
        self._load_files()
        self._place()
        self._filter(text)
        main.installEventFilter(self)

    # entries ----------------------------------------------------------------------------------------------------
    def _entries(self) -> list[Entry]:
        m = self.main
        out: list[Entry] = []
        cur = m.current
        groups: dict[str, list[str]] = {}
        for s in m.services:
            spec, name = s["spec"], s["spec"]["name"]
            phase = s["observed"].get("phase", "stopped")
            mode = mode_label(spec)[0]
            group = spec.get("group") or ""
            if group:
                groups.setdefault(group, []).append(name)
            where = " · ".join(x for x in (group.capitalize() if group else "", phase) if x)
            running = phase in ("running", "starting", "backoff")
            more = [
                Entry("Services", "play", f"Start {name}", where, run=lambda n=name: m.act("start", n)),
                Entry("Services", "stop", f"Stop {name}", where, run=lambda n=name: m.act("stop", n)),
                Entry("Services", "restart", f"Restart {name}", where, run=lambda n=name: m.act("restart", n)),
                Entry("Services", "console", f"Logs of {name}", meta(s), run=lambda n=name: m.pick(n)),
                Entry("Settings", "sliders", f"Settings · {name}", mode, run=lambda n=name: m.open_settings(n)),
                Entry("Settings", "globe", f"Network · {name}", mode, run=lambda n=name: m.open_settings(n, "network")),
                Entry("Actions", "code", f"Open files of {name}", run=lambda n=name: self._files_of(n)),
            ]
            keys = "Ctrl R" if name == cur else ""
            if running:
                out.append(Entry("Services", "restart", f"Restart {name}", where, keys, lambda n=name: m.act("restart", n), more,
                                 f"{group} {mode}"))
            else:
                out.append(Entry("Services", "play", f"Start {name}", where, "", lambda n=name: m.act("start", n), more,
                                 f"{group} {mode}"))
            out.append(Entry("Services", "services", f"Open {name}", f"{mode} · {meta(s)}", "",
                             lambda n=name: m.pick(n), more, f"{group} {mode}"))
            if needs_you(s) and not s.get("approved"):
                out.append(Entry("Actions", "list", f"Review plan for {name}", "awaiting approval", "",
                                 lambda n=name: m.show_plan(n)))
            if spec.get("network", {}).get("traffic", {}).get("mode") == "hosts":
                n_hosts = len(spec["network"]["traffic"].get("hosts", []))
                out.append(Entry("Settings", "globe", f"Traffic allow-list · {name}", f"{n_hosts} host{'s' * (n_hosts != 1)}",
                                 "", lambda n=name: m.open_settings(n, "network")))
        for g, names in sorted(groups.items()):
            if len(names) > 1:
                out.append(Entry("Services", "restart", f"Restart all in group {g.capitalize()}", f"{len(names)} services",
                                 run=lambda ns=tuple(names): [m.act("restart", n) for n in ns]))
        out += [
            Entry("Actions", "plus", "New service", "a step flow", "Ctrl N", m.new_service),
            Entry("Actions", "play", f"Try in a copy: {cur}" if cur else "Try in a copy", "throwaway folder · traffic off",
                  "Ctrl T", m.try_copy),
            Entry("Actions", "sliders", f"Settings · {cur}" if cur else "Settings", "", "Ctrl ,", lambda: m.open_settings()),
            Entry("Actions", "sun" if th.current() is th.DARK else "moon",
                  "Light theme" if th.current() is th.DARK else "Dark theme", "remembered", "Ctrl Shift L", m.toggle_theme),
            Entry("Actions", "bell", "Notifications", "", "", m.show_notes),
            Entry("Actions", "gauge", "Measure speed on this machine", "about 8 s", "",
                  lambda: (m.go("check"), m.lazy["check"].measure())),  # type: ignore[attr-defined]
            Entry("Actions", "download", "Save the log", cur or "", "", m.save_log),
        ]
        for i, (key, ico, text) in enumerate((("services", "services", "Services"), ("files", "code", "Files"),
                                              ("engines", "box", "Engines"), ("plugins", "plug", "Plugins"),
                                              ("security", "shield-ok", "Security"), ("check", "gauge", "System check"))):
            out.append(Entry("Go to", ico, text, "", f"Ctrl {i + 1}", lambda k=key: m.go(k)))
        for p in (getattr(m, "plugins_snap", None) or {}).get("installed", []):
            pid, name = p["id"], p["name"]
            tag = "official" if p.get("official") else "third-party"
            out.append(Entry("Plugins", "plug", f"{name}: permissions and calls", tag, run=lambda i=pid: self._plugin(i)))
            if p.get("enabled"):
                out.append(Entry("Plugins", "power", f"{name}: turn off", tag,
                                 run=lambda i=pid: m.link.call("plugins.enable", lambda *_: m.plugins_snapshot(), id=i, on=False)))
            if p.get("status"):
                out.append(Entry("Plugins", "pulse", f"{name}: {p['status']}", tag, run=lambda i=pid: self._plugin(i)))
        out.append(Entry("Plugins", "power", "Safe mode: run no plugins", "until you turn it off",
                         run=lambda: m.link.call("plugins.safe_mode", lambda *_: m.plugins_snapshot(), on=True)))
        return out

    def _plugin(self, pid: str) -> None:
        self.main.go("plugins")
        pg = self.main.lazy.get("plugins")
        if pg is not None:
            pg.current = pid  # type: ignore[attr-defined]
            pg.refresh()  # type: ignore[attr-defined]

    def _files_of(self, name: str) -> None:
        self.main.go("files")
        self.main.lazy["files"].open_service(name)  # type: ignore[attr-defined]

    def _open_file(self, name: str, path: str) -> None:
        self.main.go("files")
        self.main.lazy["files"].open_service(name, path)  # type: ignore[attr-defined]

    def _load_files(self) -> None:
        name = self.main.current
        if not name:
            return

        def got(res: Any, err: Any) -> None:
            if err or not isinstance(res, dict):
                return
            self.files = [x["path"] for x in res.get("tree", []) if x.get("kind") == "file"][:2000]
            if self.edit.text().strip():
                self._filter(self.edit.text())

        self.main.link.call("files.list", got, name=name)

    # filtering --------------------------------------------------------------------------------------------------
    def _filter(self, text: str) -> None:
        base = self.stack[-1] if self.stack else self.entries
        q = text.strip()
        scored: list[tuple[int, int, Entry, list[int]]] = []
        for i, e in enumerate(base):
            sc = score(q, e.title)
            if sc is None and e.words and score(q, e.words) is not None:
                sc = (1, [])
            if sc is not None:
                scored.append((sc[0], -i, e, sc[1]))
        if q and not self.stack and self.main.current:
            name = self.main.current
            for path in self.files:
                sc = score(q, path)
                if sc is not None and sc[0] >= 60:
                    e = Entry("Files", "file", path, name, run=lambda p=path: self._open_file(name, p))
                    scored.append((sc[0] - 20, -10_000, e, sc[1]))
        if q:  # sections keep their order; the best matches first, five per section (Tab on one shows the rest)
            scored.sort(key=lambda x: (SECTIONS.index(x[2].section), -x[0], -x[1]))
            per: dict[str, int] = {}
            kept = []
            for row in scored:
                per[row[2].section] = per.get(row[2].section, 0) + 1
                if per[row[2].section] <= PER_SECTION:
                    kept.append(row)
            scored = kept
        self._show([(e, hits) for _s, _i, e, hits in scored[:MAX_ROWS]], q)

    def _show(self, items: list[tuple[Entry, list[int]]], q: str) -> None:
        clear(self.rows_box)
        self.rows = []
        section = ""
        for e, hits in items:
            if e.section != section:
                section = e.section
                h = label(section.upper(), "Faint")
                h.setStyleSheet("font-size: 11px; font-weight: 600; letter-spacing: 0.8px; padding: 8px 12px 2px 12px;")
                self.rows_box.addWidget(h)
            row = Row(e, bold(e.title, hits) if q else esc(e.title), lambda e=e: self._run(e))
            self.rows.append(row)
            self.rows_box.addWidget(row)
        if not items:
            self.rows_box.addWidget(label("Nothing matches. Try a service name, a page or an action.", "Muted"))
        self.rows_box.addStretch(1)
        n = len(items)
        self.count.set(f"{n} result{'s' * (n != 1)}" if q or self.stack else f"{n} entries")
        self.sel = 0
        self._select(0)

    def _select(self, i: int) -> None:
        if not self.rows:
            return
        self.sel = max(0, min(i, len(self.rows) - 1))
        for k, r in enumerate(self.rows):
            r.set_selected(k == self.sel)
        self.scroll.ensureWidgetVisible(self.rows[self.sel], 0, 30)

    def _run(self, e: Entry) -> None:
        self.close()
        if e.run:
            e.run()

    def _more(self) -> None:
        if self.rows and self.rows[self.sel].entry.more:
            self.stack.append(self.rows[self.sel].entry.more)
            self.edit.clear()
            self._filter("")

    # window -----------------------------------------------------------------------------------------------------
    def _place(self) -> None:
        m = self.main
        w = min(840, m.width() - 32)
        h = min(560, m.height() - 120)
        x = (m.width() - w) // 2
        self.setGeometry(x, 64, w, h)

    def show(self) -> None:
        super().show()
        self.raise_()
        self.edit.setFocus()
        self.edit.end(False)

    def close(self) -> bool:
        self.main.removeEventFilter(self)
        return super().close()

    def eventFilter(self, obj: QObject | None, e: QEvent | None) -> bool:
        if obj is self.main and e is not None and e.type() == QEvent.Type.Resize:
            self._place()
        if obj is self.edit and isinstance(e, QKeyEvent) and e.type() == QEvent.Type.KeyPress:
            k = e.key()
            if k == Qt.Key.Key_Down:
                self._select(self.sel + 1)
                return True
            if k == Qt.Key.Key_Up:
                self._select(self.sel - 1)
                return True
            if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if self.rows:
                    self._run(self.rows[self.sel].entry)
                return True
            if k == Qt.Key.Key_Tab:
                self._more()
                return True
            if k == Qt.Key.Key_Escape:
                if self.stack:
                    self.stack.pop()
                    self._filter(self.edit.text())
                else:
                    self.close()
                return True
            if k == Qt.Key.Key_Backspace and not self.edit.text() and self.stack:
                self.stack.pop()
                self._filter("")
                return True
        return False
