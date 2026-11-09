"""The design's building blocks as Qt widgets: buttons, chips, dots, toggles, segmented controls, tabs, (i) cards,
panels, notes, the search box, sparklines, layer rings and meters.
Each one reads its colours from the current theme when it paints (or in `retheme()`), so switching light/dark is one
walk over the window. Painted widgets cache nothing that depends on the theme."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Any

from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFontMetrics, QMouseEvent, QPainter, QPainterPath, QPaintEvent, QPen
from PyQt6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetItem,
)

from pythonator.ui import theme as th
from pythonator.ui.icons import icon, pixmap

KIND_ICON = {"primary": "accent_ink", "danger": "bad", "ghost": "muted"}

def retheme(root: QWidget) -> None:
    """After the palette changes: let every widget that draws its own icons pick the new colours."""
    for w in [root, *root.findChildren(QWidget)]:
        try:  # a widget waiting for deleteLater is still listed
            fn = getattr(w, "retheme", None)
            if callable(fn):
                fn()
            else:
                w.update()
        except RuntimeError:
            continue

def rebuild_when_narrow(page: Any) -> None:
    """Pages lay out again (page._build) when they cross 700 px wide."""
    if (narrow := page.width() < 700) != getattr(page, "_bucket", None):
        page._bucket = narrow
        page._build()

# --- layout helpers ----------------------------------------------------------------------------------------------
def hbox(*items: QWidget | QLayout | int | None, spacing: int = 8, margins: Sequence[int] = (0, 0, 0, 0)) -> QHBoxLayout:
    """Items in a row. 0 or 1 is a stretch, a larger int is fixed spacing in px; None is skipped."""
    lay = QHBoxLayout()
    _fill(lay, items, spacing, margins)
    return lay

def vbox(*items: QWidget | QLayout | int | None, spacing: int = 8, margins: Sequence[int] = (0, 0, 0, 0)) -> QVBoxLayout:
    lay = QVBoxLayout()
    _fill(lay, items, spacing, margins)
    return lay

def _fill(lay: QHBoxLayout | QVBoxLayout, items: Iterable[QWidget | QLayout | int | None], spacing: int, margins: Sequence[int]) -> None:
    lay.setSpacing(spacing)
    lay.setContentsMargins(*margins)
    for it in items:
        if it is None:
            continue
        if isinstance(it, int):
            if it <= 1:
                lay.addStretch(1)
            else:
                lay.addSpacing(it)
        elif isinstance(it, QLayout):
            lay.addLayout(it)
        else:
            lay.addWidget(it)

def frame(obj: str = "", layout: QLayout | None = None) -> QFrame:
    f = QFrame()
    if obj:
        f.setObjectName(obj)
    if layout is not None:
        f.setLayout(layout)
    return f

def splitter(key: str, *panes: tuple[QWidget, int, int, int]) -> QSplitter:
    """Side panels you drag to resize, remembered per screen: each pane is (widget, start width, min, max)."""
    from PyQt6.QtCore import QSettings

    sp = QSplitter(Qt.Orientation.Horizontal)
    sp.setChildrenCollapsible(False)
    sp.setHandleWidth(4)
    for i, (w, _start, lo, hi) in enumerate(panes):
        w.setMinimumWidth(lo)
        w.setMaximumWidth(hi)
        sp.addWidget(w)
        sp.setStretchFactor(i, 1 if hi >= 16777215 else 0)
    saved = QSettings("Pythonator", "Pythonator").value(f"split/{key}")
    try:
        sizes = [int(x) for x in saved] if isinstance(saved, list) and len(saved) == len(panes) else []
    except (TypeError, ValueError):
        sizes = []
    sp.setSizes(sizes or [start for _w, start, _lo, _hi in panes])
    sp.splitterMoved.connect(lambda *_: QSettings("Pythonator", "Pythonator").setValue(f"split/{key}", sp.sizes()))
    return sp

def clear(lay: QLayout | None) -> None:
    """Empty a layout: its widgets are deleted, nested layouts emptied the same way."""
    while lay is not None and lay.count():
        it = lay.takeAt(0)
        if it is None:
            continue
        if it.widget() is not None:
            it.widget().hide()
            it.widget().deleteLater()
        elif it.layout() is not None:
            clear(it.layout())

def show_if(w: QWidget, on: bool) -> None:
    """setVisible for a widget that may not be in a window yet: showing one without a parent opens it as a window
    of its own (a flash on Windows), so it is left to show with its parent instead."""
    if on and w.parentWidget() is None:
        return
    w.setVisible(on)

def label(text: str = "", obj: str = "", wrap: bool = False, rich: bool = False) -> QLabel:
    lab = QLabel(text)
    if obj:
        lab.setObjectName(obj)
    if wrap:
        lab.setWordWrap(True)
    lab.setTextFormat(Qt.TextFormat.RichText if rich else Qt.TextFormat.PlainText)
    return lab

def lbl(text: str) -> QLabel:
    """Small uppercase section label."""
    lab = QLabel(text)
    lab.setFont(th.label_font())
    lab.setObjectName("Muted")
    return lab

def hr() -> QFrame:
    return frame("Hr")

def esc(s: object) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

# --- icons and buttons -------------------------------------------------------------------------------------------
class Icon(QLabel):
    def __init__(self, name: str, tone: str = "muted", size: int = 16) -> None:
        super().__init__()
        self.name_, self.tone, self.size_ = name, tone, size
        self.setFixedSize(size, size)
        self.retheme()

    def set(self, name: str, tone: str | None = None) -> None:
        self.name_ = name
        self.tone = tone or self.tone
        self.retheme()

    def retheme(self) -> None:
        self.setPixmap(pixmap(self.name_, th.tone(self.tone), self.size_))

class Btn(QPushButton):
    """`.btn`: 32 px, 6 px corners, one solid fill. kind: '' | primary | danger | ghost; size: '' | sm."""

    def __init__(
        self,
        text: str = "",
        ico: str | None = None,
        kind: str = "",
        size: str = "",
        tip: str = "",
        on: Callable[[], object] | None = None,
        tone: str | None = None,
        trailing: bool = False,
    ) -> None:
        super().__init__(text)
        self.ico, self.tone = ico, tone
        if kind:
            self.setProperty("kind", kind)
        if size:
            self.setProperty("size", size)
        if ico and not text:
            self.setProperty("icon_only", True)
        if trailing:
            self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.setToolTip(tip or ("" if text else ""))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setIconSize(QSize(13, 13) if size == "sm" else QSize(16, 16))
        if on:
            self.clicked.connect(lambda _=False: on())
        self.retheme()

    def set_icon(self, ico: str, tone: str | None = None) -> None:
        self.ico, self.tone = ico, tone or self.tone
        self.retheme()

    def retheme(self) -> None:
        if self.ico:
            kind = str(self.property("kind") or "")
            self.setIcon(icon(self.ico, th.tone(self.tone or KIND_ICON.get(kind, "text")), 16))

def primary(text: str, ico: str | None = None, on: Callable[[], object] | None = None) -> Btn:
    return Btn(text, ico, "primary", on=on)

class Kbd(QLabel):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.setObjectName("Kbd")
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

# --- chips, dots, badges -----------------------------------------------------------------------------------------
class Chip(QWidget):
    """`.chip`: 22 px high, 6 px corners, tinted fill. tone: '' (neutral), ok, warn, bad, info, acc, line, m_sandbox…"""

    clicked = pyqtSignal()

    def __init__(self, text: str = "", tone: str = "", ico: str | None = None, mono: bool = False, h: int = 22) -> None:
        super().__init__()
        self.text_, self.tone, self.ico, self.mono, self.h = text, tone, ico, mono, h
        self.font_ = th.mono_font(11 if h >= 22 else 10) if mono else th.ui_font(11.5 if h >= 22 else 10.5, 500)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def set(self, text: str, tone: str | None = None, ico: str | None = None) -> None:
        self.text_ = text
        if tone is not None:
            self.tone = tone
        if ico is not None:
            self.ico = ico or None
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:
        pad = 8 if self.h >= 22 else 6
        w = QFontMetrics(self.font_).horizontalAdvance(self.text_) + 2 * pad
        if self.ico:
            w += 12 + (5 if self.text_ else 0)
        return QSize(w, self.h)

    def colors(self) -> tuple[QColor, QColor | None, QColor | None]:
        t = th.current()
        if not self.tone:
            return QColor(t.muted), QColor(t.chip), None
        if self.tone == "line":
            return QColor(t.muted), None, QColor(t.border)
        if self.tone == "plain":
            return QColor(t.text), QColor(t.raised2), None
        fg = t.accent_text if self.tone == "acc" else th.tone(self.tone)
        base = t.accent if self.tone == "acc" else fg
        return QColor(fg), th.mix(base, t.bg, 0.14), None

    def paintEvent(self, _e: QPaintEvent | None) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        fg, fill, line = self.colors()
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(line, 1) if line else Qt.PenStyle.NoPen)
        p.setBrush(fill if fill else Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r, th.RC, th.RC)
        x = 8 if self.h >= 22 else 6
        if self.ico:
            p.drawPixmap(QPoint(x, (self.h - 12) // 2), pixmap(self.ico, fg.name(), 12))
            x += 17
        p.setPen(fg)
        p.setFont(self.font_)
        p.drawText(QRectF(x, 0, self.width() - x, self.h), Qt.AlignmentFlag.AlignVCenter, self.text_)

    def mousePressEvent(self, e: QMouseEvent | None) -> None:
        self.clicked.emit()

class Dot(QWidget):
    """`.dot`: 8 px status dot with a soft 3 px halo. state: run | warn | bad | off | idle."""

    def __init__(self, state: str = "idle") -> None:
        super().__init__()
        self.state = state
        self.setFixedSize(14, 14)

    def set(self, state: str) -> None:
        self.state = state
        self.update()

    def paintEvent(self, _e: QPaintEvent | None) -> None:
        paint_dot(QPainter(self), QPointF(7, 7), self.state)

def paint_dot(p: QPainter, c: QPointF, state: str) -> None:
    t = th.current()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    col = {"run": t.ok, "warn": t.warn, "bad": t.bad}.get(state)
    if col:
        halo = QColor(col)
        halo.setAlphaF(0.22)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(halo)
        p.drawEllipse(c, 7, 7)
        p.setBrush(QColor(col))
        p.drawEllipse(c, 4, 4)
    elif state == "off":
        p.setPen(QPen(QColor(t.faint), 1.6))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(c, 3.4, 3.4)
    else:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(t.faint))
        p.drawEllipse(c, 4, 4)

class Badge(QWidget):
    """Red count badge, 15 px, 6 px corners. On a button, it sits inside its top-right corner (never clipped)."""

    def __init__(self, n: int = 0, on: QWidget | None = None) -> None:
        super().__init__(on)
        self.n = n
        self.setFixedHeight(15)
        if on is not None:
            on.installEventFilter(self)
        self.set(n)

    def set(self, n: int) -> None:
        self.n = n
        self.setFixedWidth(max(15, QFontMetrics(th.ui_font(9, 700)).horizontalAdvance(str(n)) + 8))
        show_if(self, n > 0)
        self._place()
        self.update()

    def _place(self) -> None:
        p = self.parentWidget()
        if p is not None:
            self.move(max(0, p.width() - self.width() - 2), 2)
            self.raise_()

    def eventFilter(self, obj: Any, e: Any) -> bool:
        if e.type() == e.Type.Resize:
            self._place()
        return False

    def paintEvent(self, _e: QPaintEvent | None) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(th.current().bad))
        p.drawRoundedRect(QRectF(self.rect()), 6, 6)
        p.setPen(QColor("#ffffff"))
        p.setFont(th.ui_font(9, 700))
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, str(self.n))

# --- inputs ------------------------------------------------------------------------------------------------------
class Toggle(QAbstractButton):
    """`.tog`: 34 × 20, 6 px corners, round knob."""

    def __init__(self, on: bool = False, tip: str = "") -> None:
        super().__init__()
        self.setCheckable(True)
        self.setChecked(on)
        self.setFixedSize(34, 20)
        self.setToolTip(tip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, _e: QPaintEvent | None) -> None:
        t = th.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        if self.isChecked():
            p.setBrush(QColor(t.accent))
        else:
            c = QColor(t.faint)
            c.setAlphaF(0.8)
            p.setBrush(c)
        if not self.isEnabled():
            p.setOpacity(0.45)
        p.drawRoundedRect(QRectF(0, 0, 34, 20), 6, 6)
        p.setBrush(QColor(t.accent_ink if self.isChecked() else "#ffffff"))
        p.drawEllipse(QRectF(17 if self.isChecked() else 3, 3, 14, 14))

class Seg(QFrame):
    """`.seg`: a segmented control. Emits the chosen key."""

    changed = pyqtSignal(str)

    def __init__(self, options: Sequence[tuple[str, str] | tuple[str, str, str]], value: str = "", accent: bool = False) -> None:
        super().__init__()
        self.setObjectName("Seg")
        if accent:
            self.setProperty("accent", True)
        lay = hbox(spacing=2, margins=(3, 3, 3, 3))
        self.setLayout(lay)
        self.group = QButtonGroup(self)
        self.buttons: dict[str, Btn] = {}
        for opt in options:
            key, text = opt[0], opt[1]
            b = Btn(text, opt[2] if len(opt) > 2 else None, tone="muted")  # type: ignore[misc]
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, k=key: self.changed.emit(k))
            self.group.addButton(b)
            self.buttons[key] = b
            lay.addWidget(b)
        self.set(value or options[0][0])
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def value(self) -> str:
        return next((k for k, b in self.buttons.items() if b.isChecked()), "")

    def set(self, key: str) -> None:
        if key in self.buttons:
            self.buttons[key].setChecked(True)

class Tabs(QFrame):
    """`.tabs`: icon + label + optional count chip, 2 px accent underline on the open one."""

    changed = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("Tabs")
        self.lay = hbox(spacing=2, margins=(14, 0, 14, 0))
        self.lay.addStretch(1)
        self.setLayout(self.lay)
        self.group = QButtonGroup(self)
        self.buttons: dict[str, QToolButton] = {}
        self.counts: dict[str, Chip] = {}
        self.icons: dict[str, str] = {}
        self.compact = False
        self.texts: dict[str, str] = {}

    def add(self, key: str, text: str, ico: str) -> None:
        b = QToolButton()
        b.setCheckable(True)
        b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        b.setText(text)
        b.setIconSize(QSize(16, 16))
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(lambda _=False, k=key: self.changed.emit(k))
        self.group.addButton(b)
        self.buttons[key], self.icons[key], self.texts[key] = b, ico, text
        c = Chip("", "bad", h=18)
        c.hide()
        self.counts[key] = c
        self.lay.insertWidget(self.lay.count() - 1, b)
        self.lay.insertWidget(self.lay.count() - 1, c)
        if len(self.buttons) == 1:
            b.setChecked(True)
        self.retheme()

    def set_count(self, key: str, n: int, tone: str = "bad") -> None:
        c = self.counts[key]
        c.set(str(n), tone)
        show_if(c, n > 0)

    def set_compact(self, on: bool) -> None:
        """Narrow width: icons only."""
        self.compact = on
        for k, b in self.buttons.items():
            b.setText("" if on else self.texts[k])
            b.setToolTip(self.texts[k] if on else "")

    def select(self, key: str) -> None:
        self.buttons[key].setChecked(True)
        self.retheme()

    def current(self) -> str:
        return next((k for k, b in self.buttons.items() if b.isChecked()), "")

    def retheme(self) -> None:
        for k, b in self.buttons.items():
            b.setIcon(icon(self.icons[k], th.tone("text" if b.isChecked() else "muted"), 16))

class Search(QFrame):
    """The search box: magnifier, the field and a key hint (Ctrl K)."""

    def __init__(self, placeholder: str, kbd: str = "") -> None:
        super().__init__()
        self.setObjectName("Search")
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        self.edit.setClearButtonEnabled(False)
        self.setFixedHeight(34)
        self.setLayout(hbox(Icon("search", "faint"), self.edit, Kbd(kbd) if kbd else None, spacing=8, margins=(11, 0, 6, 0)))

class InfoBtn(QAbstractButton):
    """The square (i): 16 px, 4 px corners, a centred "i". Opens a card that explains the thing next to it."""

    def __init__(self, title: str, text: str, rows: Sequence[tuple[str, str]] = (), foot: str = "", ico: str = "info") -> None:
        super().__init__()
        self.title, self.text_, self.rows, self.foot, self.ico = title, text, list(rows), foot, ico
        self.setFixedSize(16, 16)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("What is this?")
        self.pop: Popover | None = None
        self.clicked.connect(self.open)

    def set_card(self, text: str, rows: Sequence[tuple[str, str]] = (), foot: str = "") -> None:
        self.text_, self.rows, self.foot = text, list(rows), foot or self.foot

    def open(self) -> None:
        self.pop = Popover(self.title, self.text_, self.rows, self.foot, self.ico)
        self.pop.show_near(self)
        self.update()

    def paintEvent(self, _e: QPaintEvent | None) -> None:
        t = th.current()
        on = bool(self.pop and self.pop.isVisible())
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(0.7, 0.7, 14.6, 14.6)
        if on:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(t.accent))
        else:
            p.setPen(QPen(QColor(t.faint), 1.4))
            p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r, 4, 4)
        pen = QPen(QColor(t.accent_ink if on else t.muted), 1.7)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawLine(QPointF(8, 7.0), QPointF(8, 11.4))  # the stem, centred
        p.drawPoint(QPointF(8, 4.7))  # the dot

class Popover(QFrame):
    """`.pop`: a card with a title, plain words, optional key/value rows and a foot line."""

    def __init__(self, title: str, text: str, rows: Sequence[tuple[str, str]] = (), foot: str = "", ico: str = "info") -> None:
        super().__init__(None, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("Pop")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setFixedWidth(300)
        head = hbox(Icon(ico, "accent_text"), label(title, "H3"), 0, spacing=8)
        body = label(text, "Small", wrap=True, rich=True)
        lay = vbox(head, body, spacing=8, margins=(15, 14, 15, 14))
        if rows:
            for k, v in rows:
                kl, vl = label(k, "Muted"), label(v, "Mono", wrap=True)
                kl.setFixedWidth(84)
                lay.addLayout(hbox(kl, vl, spacing=10))
        if foot:
            lay.addWidget(hr())
            lay.addWidget(label(foot, "Small", wrap=True, rich=True))
        self.setLayout(lay)

    def show_near(self, anchor: QWidget) -> None:
        self.adjustSize()
        g = anchor.mapToGlobal(QPoint(0, anchor.height() + 8))
        scr = anchor.screen().availableGeometry() if anchor.screen() else QRect(0, 0, 4000, 4000)
        x = min(max(scr.left() + 8, g.x() - 24), scr.right() - self.width() - 8)
        y = g.y() if g.y() + self.height() < scr.bottom() else anchor.mapToGlobal(QPoint(0, 0)).y() - self.height() - 8
        self.move(x, y)
        self.show()

# --- containers --------------------------------------------------------------------------------------------------
class Panel(QFrame):
    """`.panel`: raised, 1 px border, 6 px corners, a head row with an icon, title, (i) and right-side items."""

    def __init__(
        self,
        title: str = "",
        ico: str | None = None,
        info: InfoBtn | None = None,
        right: Sequence[QWidget] = (),
        card: bool = False,
        pad: int = 14,
    ) -> None:
        super().__init__()
        self.setObjectName("Panel")
        outer = vbox(spacing=0)
        self.setLayout(outer)
        self.head_icon: Icon | None = None
        self.right = hbox(spacing=6)
        if title:
            self.head_icon = Icon(ico, "text") if ico else None
            self.title = label(title, "CardHead")
            head = hbox(self.head_icon, self.title, info, 0, self.right, spacing=7,
                        margins=(12, 12, 12, 0) if card else (pad, 12, pad, 12))
            for w in right:
                self.right.addWidget(w)
            hw = frame("" if card else "PanelHead", head)
            outer.addWidget(hw)
        self.box = vbox(spacing=8, margins=(12, 10, 12, 12) if card else (pad, 12, pad, 12))
        outer.addLayout(self.box)

class Note(QFrame):
    """`.note`: an info / warn / bad callout with an icon and wrapped text."""

    def __init__(self, html: str, tone: str = "info", ico: str | None = None) -> None:
        super().__init__()
        self.tone = tone
        self.icon_ = Icon(ico or {"info": "info", "warn": "warn", "bad": "warn", "ok": "check"}.get(tone, "info"), tone)
        self.text = label(html, wrap=True, rich=True)
        self.text.setStyleSheet("font-size: 12.5px;")
        self.text.setOpenExternalLinks(False)
        lay = hbox(spacing=9, margins=(12, 10, 12, 10))
        lay.addWidget(self.icon_, 0, Qt.AlignmentFlag.AlignTop)
        lay.addWidget(self.text, 1)
        self.setLayout(lay)
        self.retheme()

    def set(self, html: str, tone: str | None = None) -> None:
        self.text.setText(html)
        if tone:
            self.tone = tone
            self.icon_.tone = tone
            self.retheme()

    def retheme(self) -> None:
        c = th.tone(self.tone)
        t = th.current()
        self.setStyleSheet(
            f"Note {{ background: {th.mix(c, t.bg, 0.10).name()}; border: 1px solid {th.mix(c, t.bg, 0.27).name()}; border-radius: 6px; }}"
        )
        self.icon_.retheme()

# --- painted data ------------------------------------------------------------------------------------------------
LAYER_TONE = {"enforced": "ok", "reduced": "warn", "not-enforced": "bad", "off": "faint", "info": "info"}

def paint_spark(p: QPainter, r: QRectF, values: Sequence[float], color: str, fill: bool = True, width: float = 1.6) -> None:
    if len(values) < 2:
        c = QColor(color)
        c.setAlphaF(0.35)
        p.setPen(QPen(c, 1.2))
        p.drawLine(QPointF(r.left(), r.bottom() - 2), QPointF(r.right(), r.bottom() - 2))
        return
    hi, lo = max(values), min(values)
    span = (hi - lo) or 1.0
    path = QPainterPath()
    n = len(values) - 1
    for i, v in enumerate(values):
        pt = QPointF(r.left() + i * r.width() / n, r.bottom() - 1 - (v - lo) / span * (r.height() - 2))
        if i:
            path.lineTo(pt)
        else:
            path.moveTo(pt)
    if fill:
        area = QPainterPath(path)
        area.lineTo(r.right(), r.bottom())
        area.lineTo(r.left(), r.bottom())
        c = QColor(color)
        c.setAlphaF(0.14)
        p.fillPath(area, c)
    pen = QPen(QColor(color), width)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPath(path)

def paint_rings(p: QPainter, c: QPointF, layers: Sequence[str], radius: float = 16) -> None:
    """`.lring`: concentric rings, outermost layer first; ok / warn / off by what's enforced here."""
    t = th.current()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    rings = list(layers)[:4] or ["off"]
    step = radius / (len(rings) + 0.6)
    for i, st in enumerate(rings):
        col = QColor(th.tone(LAYER_TONE.get(st, "faint")))
        if st == "off":
            col.setAlphaF(0.6)
        p.setPen(QPen(col, 2.6))
        p.setBrush(Qt.BrushStyle.NoBrush)
        rr = radius - i * step
        p.drawEllipse(c, rr, rr)
    core = QColor(t.text)
    core.setAlphaF(0.85)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(core)
    p.drawEllipse(c, step * 0.55, step * 0.55)

class Spark(QWidget):
    def __init__(self, tone: str = "accent", h: int = 16, fill: bool = True) -> None:
        super().__init__()
        self.values: list[float] = []
        self.tone, self.fill = tone, fill
        self.setFixedHeight(h)

    def set(self, values: Sequence[float], tone: str | None = None) -> None:
        self.values = list(values)
        if tone:
            self.tone = tone
        self.update()

    def paintEvent(self, _e: QPaintEvent | None) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        paint_spark(p, QRectF(self.rect()).adjusted(1, 1, -1, -1), self.values, th.tone(self.tone), self.fill)

class Meter(QWidget):
    """`.meter`: label, 6 px track filled in the accent, value in mono."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text_, self.frac, self.value = text, 0.0, ""
        self.setFixedHeight(22)

    def set(self, frac: float, value: str) -> None:
        self.frac, self.value = max(0.0, min(frac, 1.0)), value
        self.update()

    def paintEvent(self, _e: QPaintEvent | None) -> None:
        t = th.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setFont(th.ui_font(12))
        p.setPen(QColor(t.text))
        p.drawText(QRect(0, 0, 62, 22), Qt.AlignmentFlag.AlignVCenter, self.text_)
        track = QRectF(70, 8, max(10, self.width() - 70 - 78), 6)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(t.chip))
        p.drawRoundedRect(track, 3, 3)
        p.setBrush(QColor(t.warn if self.frac > 0.85 else t.accent))
        p.drawRoundedRect(QRectF(track.left(), track.top(), max(6.0, track.width() * self.frac), 6), 3, 3)
        p.setFont(th.mono_font(11.5))
        p.setPen(QColor(t.muted))
        p.drawText(QRect(self.width() - 76, 0, 76, 22), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self.value)

# --- flow layout (chips that wrap instead of scrolling sideways) -----------------------------------------------
class Flow(QLayout):
    def __init__(self, spacing: int = 6) -> None:
        super().__init__()
        self.items: list[QLayoutItem] = []
        self.gap = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item: QLayoutItem | None) -> None:  # noqa: N802 - Qt API
        if item is not None:
            self.items.append(item)

    def add(self, w: QWidget) -> None:
        self.addChildWidget(w)
        self.addItem(QWidgetItem(w))

    def chips(self) -> list[Chip]:
        return [w for it in self.items if isinstance(w := it.widget(), Chip)]

    def clear(self) -> None:
        while self.items:
            it = self.items.pop()
            w = it.widget()
            if w:
                # hide, never unparent: a widget just added has a queued show, and without a parent that show
                # opens it as a window of its own for a moment (the flash on Windows when a service was picked)
                w.hide()
                w.deleteLater()

    def count(self) -> int:
        return len(self.items)

    def itemAt(self, i: int) -> QLayoutItem | None:  # noqa: N802
        return self.items[i] if 0 <= i < len(self.items) else None

    def takeAt(self, i: int) -> QLayoutItem | None:  # noqa: N802
        return self.items.pop(i) if 0 <= i < len(self.items) else None

    def expandingDirections(self) -> Qt.Orientation:  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, w: int) -> int:  # noqa: N802
        return self._do(QRect(0, 0, w, 0), True)

    def setGeometry(self, r: QRect) -> None:  # noqa: N802
        super().setGeometry(r)
        self._do(r, False)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        s = QSize()
        for it in self.items:
            s = s.expandedTo(it.minimumSize())
        return s

    def _do(self, r: QRect, test: bool) -> int:
        x, y, line = r.x(), r.y(), 0
        for it in self.items:
            if it.widget() and not it.widget().isVisibleTo(it.widget().parentWidget()):
                continue
            hint = it.sizeHint()
            if x + hint.width() > r.right() + 1 and line > 0:
                x, y, line = r.x(), y + line + self.gap, 0
            if not test:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self.gap
            line = max(line, hint.height())
        return y + line - r.y()

def flow(*widgets: QWidget, spacing: int = 6) -> QWidget:
    w = QWidget()
    lay = Flow(spacing)
    w.setLayout(lay)
    for x in widgets:
        lay.add(x)
    return w


class TwoColumns(QWidget):
    """A main column and a side column that moves under it below 1000 px (nothing scrolls sideways)."""

    def __init__(self, main_col: QWidget, side: QWidget, side_width: int = 430) -> None:
        super().__init__()
        from PyQt6.QtWidgets import QBoxLayout

        self.side, self.side_width = side, side_width
        self.lay = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(0)
        self.lay.addWidget(main_col, 1)
        self.lay.addWidget(side, 0)
        self.setLayout(self.lay)
        self.layout_for(2000)

    def layout_for(self, w: int) -> None:
        from PyQt6.QtWidgets import QBoxLayout

        narrow = w < 1000
        self.lay.setDirection(QBoxLayout.Direction.TopToBottom if narrow else QBoxLayout.Direction.LeftToRight)
        if narrow:
            self.side.setMinimumWidth(0)
            self.side.setMaximumWidth(16777215)
        else:
            self.side.setFixedWidth(self.side_width)

    def resizeEvent(self, e: object) -> None:
        super().resizeEvent(e)  # type: ignore[arg-type]
        self.layout_for(self.width())
