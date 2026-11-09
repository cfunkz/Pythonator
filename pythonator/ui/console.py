"""Range-read console: the window holds only the lines on screen plus a small cache.
The daemon owns the log. This view knows the total line count and asks for pages of lines as you scroll, so a
10-million-line log costs the same as a 100-line one, and a service that floods output can't freeze the window.
The first lines are Pythonator's own: what protects this run, read from the EnforcementPlan."""

from __future__ import annotations

import re
from collections import OrderedDict
from datetime import datetime
from typing import Any

from PyQt6.QtCore import QPoint, QRect, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QKeySequence, QPainter, QShortcut
from PyQt6.QtWidgets import QAbstractScrollArea, QApplication

from pythonator.ui import ansi
from pythonator.ui import theme as th
from pythonator.ui.icons import pixmap
from pythonator.ui.link import Link

PAGE = 256
CACHE_PAGES = 24
LEVEL = re.compile(r"\b(DEBUG|INFO|WARN(?:ING)?|ERR(?:OR)?|CRITICAL|FATAL)\b")
LEVEL_TONE = {"DEBUG": "faint", "INFO": "info", "WARN": "warn", "WARNING": "warn", "ERR": "bad", "ERROR": "bad",
              "CRITICAL": "bad", "FATAL": "bad"}

class Console(QAbstractScrollArea):
    def __init__(self, link: Link) -> None:
        super().__init__()
        self.setObjectName("Console")
        self.link = link
        self.name = ""
        self.first = 0
        self.total = 0
        self.follow = True
        self.header: list[str] = []
        self.find = ""
        self.pages: OrderedDict[int, list[list[Any]]] = OrderedDict()
        self.loading: set[int] = set()
        self.font_ = th.mono_font(12.5)
        self.fm = QFontMetrics(self.font_)
        self.lh = round(12.5 * 1.75)
        self.setViewportMargins(14, 12, 10, 12)
        self.viewport().setAutoFillBackground(False)
        self.verticalScrollBar().valueChanged.connect(self._scrolled)
        self._refresh = QTimer(self, singleShot=True, interval=60)  # coalesce bursts of "log" events
        self._refresh.timeout.connect(self._pull_tail)
        QShortcut(QKeySequence.StandardKey.Copy, self, activated=self.copy_visible)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def show_service(self, name: str, total: int, header: list[str]) -> None:
        self.name, self.pages, self.loading, self.header = name, OrderedDict(), set(), header
        self.total, self.first, self.follow = total, 0, True
        self._pull_tail()
        self.viewport().update()

    def set_header(self, header: list[str]) -> None:
        self.header = header
        self._update_range()

    def on_log(self, name: str, total: int) -> None:
        if name != self.name:
            return
        self.total = max(self.total, total)
        self._update_range()
        if self.follow and not self._refresh.isActive():
            self._refresh.start()

    def refresh(self) -> None:
        """Fetch the tail again (after being hidden behind another page)."""
        self._update_range()
        self._pull_tail()

    def showEvent(self, e: Any) -> None:
        super().showEvent(e)
        self.refresh()

    def _rows(self) -> int:
        return max(1, (self.viewport().height() - self.lh * (len(self.header) + 1)) // self.lh)

    def _pull_tail(self) -> None:
        if not self.name:
            return
        start = max(self.first, self.total - self._rows() - 1)
        self._fetch(start // PAGE, force=True)
        if start // PAGE != (self.total - 1) // PAGE:
            self._fetch((self.total - 1) // PAGE, force=True)

    def _fetch(self, page: int, force: bool = False) -> None:
        if page < 0 or page in self.loading or (page in self.pages and not force):
            return
        self.loading.add(page)
        name = self.name

        def done(res: Any, err: Any) -> None:
            self.loading.discard(page)
            if err or name != self.name or not res:
                return
            self.first, self.total = res["first"], max(self.total, res["total"])
            self.pages[page] = res["lines"]
            self.pages.move_to_end(page)
            while len(self.pages) > CACHE_PAGES:
                self.pages.popitem(last=False)
            self._update_range()

        self.link.call("logs", done, name=name, start=page * PAGE, count=PAGE)

    def _update_range(self) -> None:
        rows = self._rows()
        sb = self.verticalScrollBar()
        sb.blockSignals(True)
        sb.setRange(self.first, max(self.first, self.total - rows))
        sb.setPageStep(rows)
        if self.follow:
            sb.setValue(sb.maximum())
        sb.blockSignals(False)
        self.viewport().update()

    def _scrolled(self, value: int) -> None:
        self.follow = value >= self.verticalScrollBar().maximum()
        self.viewport().update()

    def resizeEvent(self, e: Any) -> None:
        super().resizeEvent(e)
        self._update_range()

    def _line(self, n: int) -> list[Any] | None:
        page = self.pages.get(n // PAGE)
        if page is None:
            self._fetch(n // PAGE)
            return None
        row = page[n % PAGE] if n % PAGE < len(page) else None
        if row and row[0] == n:
            return row
        return next((r for r in page if r[0] == n), None)

    def paintEvent(self, _e: Any) -> None:
        t = th.current()
        p = QPainter(self.viewport())
        p.setFont(self.font_)
        w = self.viewport().width()
        y = 0
        for line in self.header:  # Pythonator · Sandbox · bubblewrap · Landlock · …
            p.drawPixmap(QPoint(0, y + (self.lh - 13) // 2), pixmap("shield", t.accent_text, 13))
            head, _, rest = line.partition(" · ")
            p.setFont(th.mono_font(12.5, 600))
            p.setPen(QColor(t.accent_text))
            hw = QFontMetrics(p.font()).horizontalAdvance(head)
            p.drawText(QRect(20, y, hw + 2, self.lh), Qt.AlignmentFlag.AlignVCenter, head)
            p.setFont(self.font_)
            p.setPen(QColor(th.mix(t.accent_text, t.console, 0.75)))
            p.drawText(QRect(20 + hw, y, w - 20 - hw, self.lh), Qt.AlignmentFlag.AlignVCenter,
                       self.fm.elidedText(" · " + rest, Qt.TextElideMode.ElideRight, w - 20 - hw))
            y += self.lh
        if not self.name or self.total == 0:
            p.setPen(QColor(t.faint))
            p.drawText(QRect(0, y, w, self.lh), Qt.AlignmentFlag.AlignVCenter,
                       "Pick a service to see its output." if not self.name else "No output yet.")
            return
        top = self.verticalScrollBar().value()
        ts_w = self.fm.horizontalAdvance("00:00:00 ")
        shown = 0
        for i in range(self._rows()):
            n = top + i
            if n >= self.total:
                break
            row = self._line(n)
            shown += 1
            if row is not None:
                self._paint_row(p, row, y, w, ts_w, t)
            y += self.lh
        p.setPen(QColor(t.faint))
        p.drawText(QRect(0, y + 6, w, self.lh), Qt.AlignmentFlag.AlignVCenter, self.fm.elidedText(
            f"… {self.total - self.first:,} lines in log · showing {'the last ' if self.follow else ''}{shown} · read by range"
            + ("" if self.follow else " · scroll to the end to follow"), Qt.TextElideMode.ElideRight, w))

    def _paint_row(self, p: QPainter, row: list[Any], y: int, w: int, ts_w: int, t: th.Tokens) -> None:
        _n, ts, stream, text = row
        p.setPen(QColor(t.faint))
        p.drawText(QRect(0, y, ts_w, self.lh), Qt.AlignmentFlag.AlignVCenter, datetime.fromtimestamp(ts / 1000).strftime("%H:%M:%S"))
        x = ts_w
        base = t.accent_text if stream == "s" else t.console_text
        runs = ansi.segments(("› " if stream == "s" else "") + text)
        flat = "".join(s for s, _c, _b in runs)
        if self.find and self.find.lower() in flat.lower():
            p.fillRect(QRect(x - 4, y + 2, w - x + 4, self.lh - 4), th.mix(t.warn, t.console, 0.16))
        if len(runs) == 1 and runs[0][1] is None and (m := LEVEL.search(flat[:48])):  # no colours of its own
            runs = [(flat[: m.start()], None, False), (m.group(1), "tone:" + LEVEL_TONE[m.group(1)], False),
                    (flat[m.end():], None, False)]
        bold = QFont(self.font_)
        bold.setBold(True)
        for part, col, strong in runs:
            if not part or x >= w:
                continue
            p.setFont(bold if strong else self.font_)
            fm = QFontMetrics(p.font())
            p.setPen(QColor(base if col is None else th.tone(col[5:]) if col.startswith("tone:") else col))
            pw = fm.horizontalAdvance(part)
            p.drawText(QRect(x, y, w - x, self.lh), Qt.AlignmentFlag.AlignVCenter,
                       part if x + pw <= w else fm.elidedText(part, Qt.TextElideMode.ElideRight, w - x))
            x += pw
        p.setFont(self.font_)

    def search(self, text: str) -> None:
        """Jump to the last line containing `text` (the daemon searches the whole log)."""
        self.find = text
        if not text or not self.name:
            self.viewport().update()
            return

        def got(res: Any, err: Any) -> None:
            if err or not res:
                return
            n = res[-1][0]
            self.follow = False
            self.verticalScrollBar().setValue(max(self.first, n - self._rows() // 2))

        self.link.call("search_logs", got, name=self.name, text=text)

    def copy_visible(self) -> None:
        top = self.verticalScrollBar().value()
        out = [ansi.plain(r[3]) for n in range(top, min(top + self._rows(), self.total)) if (r := self._line(n))]
        QApplication.clipboard().setText("\n".join(out))
