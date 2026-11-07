"""Colours, fonts, the stylesheet and the small status and isolation glyphs.

A developer-tool look: a cool blue-grey palette with one indigo accent, 2-3 px corners and 1 px hairlines.
Monospace is kept for what reads like terminal output (paths, pids, CPU and memory, the status bar). Fusion
everywhere, so the app looks the same on every system; popups appear at once instead of fading in.
"""

from __future__ import annotations

import functools
import sys
from dataclasses import dataclass

from PyQt6.QtCore import QPointF, QRectF, Qt, QTemporaryDir
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QPainter, QPainterPath, QPalette, QPen, QPixmap, QPolygonF
from PyQt6.QtWidgets import QApplication, QProxyStyle, QStyle, QStyleHintReturn, QStyleOption, QWidget


@dataclass(frozen=True)
class Colors:
    bg: str  # the editor, the console and dialogs
    panel: str  # the sidebar, bars, lists
    raised: str  # buttons, popups, hover
    border: str  # hairlines between areas
    text: str
    muted: str
    accent: str  # indigo: focus, selection, the active tab
    ok: str
    warn: str
    error: str
    selection: str
    code_bg: str
    faint: str = "#555c6c"  # section titles, disabled text
    edge: str = "#2e3340"  # control outlines
    accent_hi: str = "#9497ff"
    teal: str = "#3fc8c0"  # the sandbox's colour
    pressed: str = "#21252f"
    blue: str = "#5aa9ff"  # containers


DARK = Colors(bg="#0d0f14", panel="#12141b", raised="#191c25", border="#22262f", text="#e3e6ed", muted="#8b92a3",
              accent="#7c7fff", ok="#3ecf8e", warn="#f5b544", error="#f25f66", selection="#1f2140",
              code_bg="#0d0f14", faint="#555c6c", edge="#2e3340", accent_hi="#9497ff", teal="#3fc8c0",
              pressed="#21252f", blue="#5aa9ff")
LIGHT = Colors(bg="#ffffff", panel="#f4f5f8", raised="#eceef3", border="#dcdfe6", text="#1b1e26", muted="#5c6373",
               accent="#5b5ef0", ok="#1f9d62", warn="#b7791f", error="#d93843", selection="#e4e5ff",
               code_bg="#ffffff", faint="#8a91a0", edge="#c9cdd6", accent_hi="#7275f5", teal="#16928b",
               pressed="#e2e4ea", blue="#2f7fd6")
current: Colors = DARK

STATE_COLORS = {"running": "ok", "starting": "accent", "stopping": "warn", "waiting": "warn", "failed": "error",
                "task": "accent", "stopped": "faint"}
STATE_LABELS = {"running": "Running", "starting": "Starting", "stopping": "Stopping", "waiting": "Waiting",
                "failed": "Failed", "task": "Busy", "stopped": "Stopped"}
LIVE = ("running", "starting", "stopping", "task")  # a process is up
MODE_COLORS = {"sandbox": "teal", "container": "blue", "full": "warn", "wsl": "warn"}

R_SM, R_MD, R_LG = 2, 3, 4  # corners: squarish, never quite square
TOOLTIP_DELAY_MS = 250  # Qt's 700 ms feels like lag in a dense window
# Sizes are written for 96 dpi points; macOS counts 72 points per inch and would draw the same number smaller.
POINT_SCALE = 4 / 3 if sys.platform == "darwin" else 1.0
_MONO = ("JetBrains Mono", "Cascadia Mono", "Cascadia Code", "Fira Code", "Consolas", "SF Mono", "Menlo",
         "DejaVu Sans Mono", "Liberation Mono", "Ubuntu Mono")


def color(name: str) -> str:
    return str(getattr(current, name, current.text))


def rgba(name_or_hex: str, alpha: float) -> str:
    """A colour (a palette name or ``#rrggbb``) with ``alpha`` (0-1), for the stylesheet."""
    c = QColor(name_or_hex if name_or_hex.startswith("#") else color(name_or_hex))
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {round(alpha * 255)})"


def state_color(state: str) -> str:
    return color(STATE_COLORS.get(state, "faint"))


def mode_color(mode: str) -> str:
    return color(MODE_COLORS.get(mode, "muted"))


@functools.cache
def mono_family() -> str:
    families = set(QFontDatabase.families())
    for family in _MONO:
        if family in families:
            return family
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()


def mono_font(size: float = 10) -> QFont:
    font = QFont(mono_family())
    font.setPointSizeF(size * POINT_SCALE)
    font.setStyleHint(QFont.StyleHint.TypeWriter)
    font.setFixedPitch(True)
    for tag in ("calt", "liga"):  # code shows "->" and "==", not ligatures
        font.setFeature(QFont.Tag(tag), 0)
    return font


def set_style(widget: QWidget, sheet: str) -> None:
    """Change a style sheet only when it changed: each change re-polishes the widget."""
    if widget.styleSheet() != sheet:
        widget.setStyleSheet(sheet)


def set_tooltip(widget: QWidget, text: str) -> None:
    """Change a tooltip only when it changed: re-setting it every second makes an open tooltip flicker."""
    if widget.toolTip() != text:
        widget.setToolTip(text)


# -- glyphs: drawn with QPainter (no image files) and cached, so repainting the list every second is free ----


def _shield(x: float, y: float, w: float, h: float) -> QPainterPath:
    path = QPainterPath()
    path.moveTo(x + w / 2, y)
    path.lineTo(x + w, y + h * 0.18)
    path.lineTo(x + w, y + h * 0.5)
    path.cubicTo(x + w, y + h * 0.78, x + w * 0.72, y + h * 0.92, x + w / 2, y + h)
    path.cubicTo(x + w * 0.28, y + h * 0.92, x, y + h * 0.78, x, y + h * 0.5)
    path.lineTo(x, y + h * 0.18)
    path.closeSubpath()
    return path


def draw_mode(p: QPainter, mode: str, box: QRectF, *, offline: bool = False) -> None:
    """How a service is isolated: a shield (sandbox; a bar across when offline), a box (container) or a warning
    triangle (Full access, WSL: neither is isolated)."""
    c = QColor(mode_color(mode))
    hole = QColor(current.panel)
    side = min(box.width(), box.height())
    x, y = box.center().x() - side / 2, box.center().y() - side / 2
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    if mode == "container":
        pen = QPen(c, max(1.0, side * 0.1))
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        tint = QColor(c)
        tint.setAlpha(70)
        p.setBrush(tint)
        left, cx, right, bottom = x + side * 0.1, x + side / 2, x + side * 0.9, y + side * 0.94
        p.drawPolygon(QPolygonF([QPointF(cx, y + side * 0.06), QPointF(right, y + side * 0.2),
                                 QPointF(right, y + side * 0.74), QPointF(cx, bottom),
                                 QPointF(left, y + side * 0.74), QPointF(left, y + side * 0.2)]))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPolyline(QPolygonF([QPointF(left, y + side * 0.2), QPointF(cx, y + side * 0.36),
                                  QPointF(right, y + side * 0.2)]))
        p.drawLine(QPointF(cx, y + side * 0.36), QPointF(cx, bottom))
    elif mode in ("full", "wsl"):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        tri = QPainterPath()
        tri.moveTo(x + side / 2, y + side * 0.06)
        tri.lineTo(x + side * 0.98, y + side * 0.92)
        tri.lineTo(x + side * 0.02, y + side * 0.92)
        tri.closeSubpath()
        p.drawPath(tri)
        pen = QPen(hole, max(1.0, side * 0.13))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawLine(QPointF(x + side / 2, y + side * 0.38), QPointF(x + side / 2, y + side * 0.62))
        p.drawPoint(QPointF(x + side / 2, y + side * 0.78))
    else:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        p.drawPath(_shield(x + side * 0.12, y, side * 0.76, side))
        pen = QPen(hole, max(1.0, side * 0.12))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        if offline:
            p.drawLine(QPointF(x + side * 0.34, y + side * 0.48), QPointF(x + side * 0.66, y + side * 0.48))
        else:
            p.drawPolyline(QPolygonF([QPointF(x + side * 0.34, y + side * 0.5),
                                      QPointF(x + side * 0.46, y + side * 0.62),
                                      QPointF(x + side * 0.67, y + side * 0.36)]))
    p.restore()


def draw_state(p: QPainter, state: str, center: QPointF, radius: float = 3.5) -> None:
    """A status dot, with a soft halo while a process is up."""
    c = QColor(state_color(state))
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    if state in LIVE:
        halo = QColor(c)
        halo.setAlpha(55)
        p.setBrush(halo)
        p.drawEllipse(center, radius * 1.85, radius * 1.85)
    p.setBrush(c)
    p.drawEllipse(center, radius, radius)
    p.restore()


@functools.lru_cache(maxsize=64)
def mode_pixmap(mode: str, size: int, ratio: float = 1.0, offline: bool = False) -> QPixmap:
    pix = QPixmap(round(size * ratio), round(size * ratio))
    pix.setDevicePixelRatio(ratio)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    draw_mode(p, mode, QRectF(0, 0, size, size), offline=offline)
    p.end()
    return pix


@functools.lru_cache(maxsize=64)
def state_pixmap(state: str, size: int, ratio: float = 1.0) -> QPixmap:
    pix = QPixmap(round(size * ratio), round(size * ratio))
    pix.setDevicePixelRatio(ratio)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    draw_state(p, state, QPointF(size / 2, size / 2), size * 0.2)
    p.end()
    return pix


# -- applying -------------------------------------------------------------------------------------------------


class _Style(QProxyStyle):
    """Fusion with quicker tooltips."""

    def styleHint(self, hint: QStyle.StyleHint, option: QStyleOption | None = None, widget: QWidget | None = None,
                  returned: QStyleHintReturn | None = None) -> int:
        if hint == QStyle.StyleHint.SH_ToolTip_WakeUpDelay:
            return TOOLTIP_DELAY_MS
        if hint == QStyle.StyleHint.SH_ToolTip_FallAsleepDelay:
            return 1500
        return super().styleHint(hint, option, widget, returned)


_keep: list[object] = []  # the style and the image folder live as long as the app
POPUP_EFFECTS = (Qt.UIEffect.UI_AnimateTooltip, Qt.UIEffect.UI_FadeTooltip, Qt.UIEffect.UI_AnimateMenu,
                 Qt.UIEffect.UI_FadeMenu, Qt.UIEffect.UI_AnimateCombo, Qt.UIEffect.UI_AnimateToolBox)


def apply(app: QApplication, name: str = "dark") -> None:
    global current
    if name == "system":
        hints = app.styleHints()
        name = "light" if hints is not None and hints.colorScheme() == Qt.ColorScheme.Light else "dark"
    current = DARK if name == "dark" else LIGHT
    mode_pixmap.cache_clear()
    state_pixmap.cache_clear()
    c = current
    style = _Style("Fusion")
    _keep.append(style)
    app.setStyle(style)
    # Qt fades tooltips and menus in itself over 150-250 ms (Windows turns it on from the system settings),
    # which reads as lag. Native popups appear at once.
    for effect in POPUP_EFFECTS:
        QApplication.setEffectEnabled(effect, False)
    pal = QPalette()
    for role, value in ((QPalette.ColorRole.Window, c.bg), (QPalette.ColorRole.WindowText, c.text),
                        (QPalette.ColorRole.Base, c.bg), (QPalette.ColorRole.AlternateBase, c.raised),
                        (QPalette.ColorRole.Text, c.text), (QPalette.ColorRole.Button, c.raised),
                        (QPalette.ColorRole.ButtonText, c.text), (QPalette.ColorRole.Highlight, c.accent),
                        (QPalette.ColorRole.HighlightedText, "#ffffff"), (QPalette.ColorRole.ToolTipBase, c.raised),
                        (QPalette.ColorRole.ToolTipText, c.text), (QPalette.ColorRole.Link, c.accent),
                        (QPalette.ColorRole.PlaceholderText, c.faint)):
        pal.setColor(role, QColor(value))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(c.faint))
    app.setPalette(pal)
    app.setStyleSheet(stylesheet(c, mono_family(), _images(c)))


def _images(c: Colors) -> str:
    """Arrows for spin and combo boxes and a check mark (styled controls lose the style's own), drawn at 2x."""
    folder = QTemporaryDir()
    if not folder.isValid():
        return ""
    _keep.append(folder)
    path = folder.path()

    def canvas(w: int, h: int) -> tuple[QPixmap, QPainter]:
        pix = QPixmap(w * 2, h * 2)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.scale(2, 2)
        return pix, p

    for name, points in (("up", ((0, 4), (3.5, 0), (7, 4))), ("down", ((0, 0), (3.5, 4), (7, 0)))):
        pix, p = canvas(7, 4)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(c.muted))
        p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in points]))
        p.end()
        pix.save(f"{path}/{name}.png")
    pix, p = canvas(14, 14)
    pen = QPen(QColor("white"), 1.8)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.drawPolyline(QPolygonF([QPointF(3.5, 7.2), QPointF(6, 9.6), QPointF(10.5, 4.6)]))
    p.end()
    pix.save(f"{path}/check.png")
    return path


def stylesheet(c: Colors, mono: str = "monospace", images: str = "") -> str:
    small = f"{8.5 * POINT_SCALE:g}pt"
    img = (f'QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url("{images}/up.png"); width: 7px;'
           f' height: 4px; }}\n'
           f'QSpinBox::down-arrow, QDoubleSpinBox::down-arrow, QComboBox::down-arrow {{'
           f' image: url("{images}/down.png"); width: 7px; height: 4px; }}\n'
           f'QCheckBox::indicator:checked {{ image: url("{images}/check.png"); }}') if images else ""
    green, red, indigo = rgba(c.ok, 0.14), rgba(c.error, 0.15), rgba(c.accent, 0.16)
    return f"""
QWidget {{ color: {c.text}; }}
QMainWindow, QDialog {{ background: {c.bg}; }}
QToolTip {{ background: {c.pressed}; color: {c.text}; border: 1px solid {c.edge}; border-radius: {R_SM}px;
    padding: 4px 8px; }}
#HeaderInfo, QStatusBar, QStatusBar QLabel {{ font-family: "{mono}"; font-size: {small}; }}

QToolBar {{ background: {c.panel}; border: none; border-bottom: 1px solid {c.border}; padding: 4px 6px; spacing: 4px; }}
QToolBar::separator {{ background: {c.edge}; width: 1px; margin: 6px 4px; }}
QToolBar QToolButton {{ padding: 4px 10px; }}
QToolButton {{ border: 1px solid transparent; border-radius: {R_MD}px; padding: 3px 8px; background: transparent; }}
QToolButton:hover {{ background: {c.pressed}; border-color: {c.edge}; }}
QToolButton:pressed {{ background: {c.raised}; }}
QToolButton:checked {{ background: {indigo}; border-color: {rgba(c.accent, 0.40)}; color: {c.accent_hi}; }}
QToolButton:disabled {{ color: {c.faint}; }}
QToolButton::menu-indicator {{ image: none; width: 0; }}
QToolButton[flat="true"] {{ padding: 2px; }}
QToolButton[accent="true"] {{ background: {green}; border-color: {rgba(c.ok, 0.45)}; color: {c.ok}; font-weight: 600; }}
QToolButton[accent="true"]:hover {{ background: {rgba(c.ok, 0.24)}; border-color: {c.ok}; }}
QToolButton[accent="true"]:disabled {{ background: {c.raised}; border-color: {c.border}; color: {c.faint}; }}
QToolButton[stop="true"]:enabled:hover {{ background: {red}; border-color: {rgba(c.error, 0.5)}; }}

QPushButton {{ background: {c.raised}; border: 1px solid {c.edge}; border-radius: {R_MD}px; padding: 5px 16px; }}
QPushButton:hover {{ background: {c.pressed}; border-color: {c.muted}; }}
QPushButton:pressed {{ background: {c.panel}; }}
QPushButton:focus {{ border-color: {c.accent}; }}
QPushButton:checked {{ background: {indigo}; border-color: {c.accent}; }}
QPushButton:disabled {{ color: {c.faint}; background: {c.panel}; border-color: {c.border}; }}
QPushButton:default, QPushButton[primary="true"] {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {c.accent_hi}, stop:1 {c.accent});
    border: 1px solid {c.accent}; color: white; font-weight: 600; }}
QPushButton:default:hover, QPushButton[primary="true"]:hover {{ background: {c.accent_hi}; }}
QPushButton[danger="true"] {{ background: {red}; border-color: {rgba(c.error, 0.5)}; color: {c.error}; }}
QPushButton[danger="true"]:hover {{ background: {rgba(c.error, 0.25)}; }}

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {c.bg}; border: 1px solid {c.edge}; border-radius: {R_MD}px; padding: 4px 8px;
    selection-background-color: {rgba(c.accent, 0.45)}; selection-color: white; }}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover {{ border-color: {c.muted}; }}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {c.accent}; }}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{ color: {c.faint}; border-color: {c.border}; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{ background: {c.raised}; border: 1px solid {c.edge}; outline: none; padding: 4px;
    selection-background-color: {c.selection}; selection-color: {c.text}; }}
QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    width: 16px; border: none; background: transparent; }}
{img}

QCheckBox, QRadioButton {{ spacing: 7px; }}
QCheckBox::indicator {{ width: 14px; height: 14px; border: 1px solid {c.muted}; border-radius: {R_SM}px;
    background: {c.bg}; }}
QRadioButton::indicator {{ width: 12px; height: 12px; border: 1px solid {c.muted}; border-radius: 6px;
    background: {c.bg}; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {c.accent}; }}
QCheckBox::indicator:checked {{ background: {c.accent}; border-color: {c.accent}; }}
QRadioButton::indicator:checked {{ background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
    stop:0 {c.accent}, stop:0.45 {c.accent}, stop:0.55 {c.bg}, stop:1 {c.bg}); border-color: {c.accent}; }}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{ border-color: {c.border}; }}

QListView, QTreeView, QTableView {{ background: {c.panel}; border: none; outline: none; }}
QListView::item {{ border: none; }}
QTreeView::item {{ padding: 2px; }}
QTreeView::item:hover {{ background: {c.raised}; }}
QTreeView::item:selected, QTableView::item:selected {{ background: {c.selection}; color: {c.text}; }}
QListWidget::item {{ padding: 5px 4px; }}
QListWidget::item:selected {{ background: {c.selection}; color: {c.text}; }}
QHeaderView::section {{ background: {c.panel}; border: none; border-bottom: 1px solid {c.border}; padding: 4px 6px;
    color: {c.faint}; }}

QTabWidget::pane {{ border: none; border-top: 1px solid {c.border}; background: {c.bg}; }}
QTabBar {{ background: {c.panel}; }}
QTabBar::tab {{ background: transparent; color: {c.muted}; padding: 7px 14px; border: none;
    border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ background: {c.bg}; color: {c.text}; border-bottom: 2px solid {c.accent}; }}
QTabBar::tab:hover:!selected {{ color: {c.text}; background: {c.raised}; }}

QSplitter::handle {{ background: {c.border}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {c.edge}; min-height: 28px; border-radius: {R_SM}px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {c.edge}; min-width: 28px; border-radius: {R_SM}px; margin: 2px; }}
QScrollBar::handle:hover {{ background: {c.muted}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

QMenu {{ background: {c.raised}; border: 1px solid {c.edge}; border-radius: {R_SM}px; padding: 4px; }}
QMenu::item {{ padding: 5px 24px 5px 12px; border-radius: {R_SM}px; }}
QMenu::item:selected {{ background: {rgba(c.accent, 0.22)}; }}
QMenu::item:disabled {{ color: {c.faint}; }}
QMenu::separator {{ height: 1px; background: {c.edge}; margin: 4px 6px; }}
QStatusBar {{ background: {c.panel}; border-top: 1px solid {c.border}; color: {c.muted}; }}
QStatusBar QLabel {{ color: {c.muted}; padding: 0 8px; }}
QStatusBar::item {{ border: none; }}
QGroupBox {{ border: 1px solid {c.edge}; border-radius: {R_LG}px; margin-top: 14px; padding: 10px 8px 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {c.muted}; }}
QDockWidget::title {{ background: {c.panel}; padding: 6px; color: {c.faint}; }}

#Sidebar, #SidebarPane {{ background: {c.panel}; }}
#Header {{ background: {c.panel}; border-bottom: 1px solid {c.border}; }}
#HeaderTitle {{ font-size: {11.5 * POINT_SCALE:g}pt; font-weight: 700; }}
#HeaderInfo {{ color: {c.muted}; }}
#PanelBar, #InputBar {{ background: {c.panel}; border-top: 1px solid {c.border}; }}
#PanelBar {{ border-bottom: 1px solid {c.border}; }}
#PanelBar QToolButton {{ padding: 2px 8px; }}
QLabel[section="true"] {{ color: {c.faint}; font-weight: 700; letter-spacing: 1px; font-size: {small}; }}
QLabel[muted="true"] {{ color: {c.muted}; }}
QLabel[heading="true"] {{ font-size: {13 * POINT_SCALE:g}pt; font-weight: 700; }}
QLabel[badge="true"] {{ border-radius: {R_SM}px; padding: 1px 7px; font-weight: 600; }}
QFrame[card="true"] {{ background: {c.panel}; border: 1px solid {c.border}; border-radius: {R_MD}px; }}
#EditorTabs QTabBar::tab {{ padding: 3px 8px 3px 10px; min-height: 18px; }}
#EditorTabs QTabBar QToolButton {{ padding: 0; }}
QToolButton[iconButton="true"] {{ padding: 2px; }}
QPushButton[segment="true"] {{ padding: 4px 12px; border-radius: {R_SM}px; background: transparent;
    border-color: {c.border}; color: {c.muted}; }}
QPushButton[segment="true"]:checked {{ background: {indigo}; border-color: {rgba(c.accent, 0.55)};
    color: {c.accent_hi}; font-weight: 600; }}
"""
