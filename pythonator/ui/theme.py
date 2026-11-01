"""Dark theme: palette, stylesheet, fonts and status colours."""

from __future__ import annotations

import sys
from importlib import resources
from typing import TypeVar

from PyQt6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPalette
from PyQt6.QtWidgets import QApplication, QStyleFactory, QWidget

from ..supervisor import Status

BG = "#16181c"
BG_ALT = "#1c1f24"
SURFACE = "#22262c"
SURFACE_HI = "#2a2f36"
BORDER = "#2f343b"
TEXT = "#d7dae0"
TEXT_DIM = "#8b929c"
ACCENT = "#4f8ff7"
GREEN = "#4cc38a"
YELLOW = "#e5b84b"
RED = "#ef5d5d"
BLUE = "#6aa7ff"

STATUS_COLORS: dict[Status, str] = {
    Status.STOPPED: "#5c636d",
    Status.RUNNING: GREEN,
    Status.STOPPING: YELLOW,
    Status.WAITING: YELLOW,
    Status.EXITED: BLUE,
    Status.FAILED: RED,
}

STATUS_LABELS: dict[Status, str] = {
    Status.STOPPED: "Stopped",
    Status.RUNNING: "Running",
    Status.STOPPING: "Stopping",
    Status.WAITING: "Restarting",
    Status.EXITED: "Exited",
    Status.FAILED: "Failed",
}

_MONO_FAMILIES = (
    "Cascadia Mono", "Cascadia Code", "JetBrains Mono", "Fira Code", "Consolas",
    "SF Mono", "Menlo", "DejaVu Sans Mono", "Liberation Mono", "Ubuntu Mono",
)
# Sizes below are written for Windows / Linux (96 dpi points). macOS counts 72 points per inch,
# so the same number would draw text a quarter smaller there.
_POINT_SCALE = 4 / 3 if sys.platform == "darwin" else 1.0

STYLESHEET = f"""
QWidget {{ color: {TEXT}; }}
QMainWindow, QDialog {{ background: {BG}; }}
QToolTip {{ background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px 6px; }}

QToolBar {{ background: {BG_ALT}; border: none; border-bottom: 1px solid {BORDER}; padding: 4px; spacing: 4px; }}
QToolBar QToolButton {{ padding: 4px 10px; border-radius: 6px; }}
QToolBar QToolButton:hover {{ background: {SURFACE_HI}; }}
QToolButton {{ border: 1px solid transparent; border-radius: 6px; padding: 3px 6px; background: transparent; }}
QToolButton:hover {{ background: {SURFACE_HI}; border-color: {BORDER}; }}
QToolButton:pressed, QToolButton:checked {{ background: #2f3a4b; border-color: #3a4a62; }}
QToolButton:disabled {{ color: #59606a; }}
QToolButton::menu-indicator {{ image: none; }}

QPushButton {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 6px; padding: 5px 14px; }}
QPushButton:hover {{ background: {SURFACE_HI}; }}
QPushButton:pressed {{ background: #1e2227; }}
QPushButton:disabled {{ color: #59606a; background: {BG_ALT}; }}
QPushButton:default, QPushButton[accent="true"] {{ background: {ACCENT}; border-color: {ACCENT}; color: white; }}
QPushButton[accent="true"]:hover {{ background: #6aa0fa; }}
QPushButton[danger="true"] {{ background: #4a2326; border-color: #6b2c31; color: #ffd7d7; }}

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {BG_ALT}; border: 1px solid {BORDER}; border-radius: 6px; padding: 4px 8px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {ACCENT};
}}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{ background: {SURFACE}; border: 1px solid {BORDER}; selection-background-color: {ACCENT}; }}

QListView {{ background: {BG_ALT}; border: none; outline: none; }}
QListView::item {{ border: none; }}

QTabWidget::pane {{ border: none; }}
QTabBar {{ background: {BG_ALT}; }}
QTabBar::tab {{
    background: {BG_ALT}; color: {TEXT_DIM}; padding: 6px 14px; border: none;
    border-right: 1px solid {BORDER}; border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{ background: {BG}; color: {TEXT}; border-bottom: 2px solid {ACCENT}; }}
QTabBar::tab:hover:!selected {{ background: {SURFACE}; }}
QTabBar::close-button {{ subcontrol-position: right; }}

QSplitter::handle {{ background: {BORDER}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}

QScrollBar:vertical {{ background: transparent; width: 11px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #3a4048; min-height: 24px; border-radius: 4px; margin: 2px; }}
QScrollBar::handle:vertical:hover {{ background: #4a515b; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: #3a4048; min-width: 24px; border-radius: 4px; margin: 2px; }}
QScrollBar::handle:horizontal:hover {{ background: #4a515b; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

QMenu {{ background: {SURFACE}; border: 1px solid {BORDER}; padding: 4px; }}
QMenu::item {{ padding: 5px 22px 5px 14px; border-radius: 4px; }}
QMenu::item:selected {{ background: {ACCENT}; color: white; }}
QMenu::item:disabled {{ color: #59606a; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 6px; }}

QStatusBar {{ background: {BG_ALT}; border-top: 1px solid {BORDER}; color: {TEXT_DIM}; }}
QStatusBar::item {{ border: none; }}
QGroupBox {{ border: 1px solid {BORDER}; border-radius: 8px; margin-top: 14px; padding-top: 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {TEXT_DIM}; }}
QCheckBox::indicator {{ width: 15px; height: 15px; }}

#Sidebar {{ background: {BG_ALT}; border-right: 1px solid {BORDER}; }}
#SidebarPane {{ background: {BG_ALT}; }}
#SidebarTitle {{ color: {TEXT_DIM}; font-weight: 600; letter-spacing: 1px; }}
#Header {{ background: {BG_ALT}; border-bottom: 1px solid {BORDER}; }}
#HeaderTitle {{ font-size: 15px; font-weight: 600; }}
#Dim {{ color: {TEXT_DIM}; }}
#PanelBar {{ background: {BG_ALT}; border-bottom: 1px solid {BORDER}; }}
#InputBar {{ background: {BG_ALT}; border-top: 1px solid {BORDER}; }}
#FindBar {{ background: {BG_ALT}; border-top: 1px solid {BORDER}; }}
"""


_W = TypeVar("_W", bound=QWidget)


def named(widget: _W, name: str) -> _W:
    """Set the object name used by the stylesheet (``#Name`` selectors) and return the widget."""
    widget.setObjectName(name)
    return widget


def monospace_font(point_size: int = 10) -> QFont:
    point_size = round(point_size * _POINT_SCALE)
    available = set(QFontDatabase.families())
    for family in _MONO_FAMILIES:
        if family in available:
            font = QFont(family, point_size)
            break
    else:
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setPointSize(point_size)
    font.setStyleHint(QFont.StyleHint.TypeWriter)
    font.setFixedPitch(True)
    if hasattr(QFont, "Tag"):  # Qt >= 6.7: code should show "->" and "==", not ligatures
        for tag in ("calt", "liga"):
            font.setFeature(QFont.Tag(tag), 0)
    return font


def app_icon() -> QIcon:
    try:
        ref = resources.files("pythonator") / "resources" / "icon.ico"
        with resources.as_file(ref) as path:
            return QIcon(str(path))
    except (FileNotFoundError, ModuleNotFoundError):
        return QIcon()


def status_icon(status: Status, size: int = 12) -> QIcon:
    from PyQt6.QtCore import Qt  # noqa: PLC0415
    from PyQt6.QtGui import QPainter, QPixmap  # noqa: PLC0415

    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(STATUS_COLORS[status]))
    painter.drawEllipse(1, 1, size - 2, size - 2)
    painter.end()
    return QIcon(pix)


def apply_theme(app: QApplication) -> None:
    if style := QStyleFactory.create("Fusion"):
        app.setStyle(style)
    pal = QPalette()
    roles = {
        QPalette.ColorRole.Window: BG,
        QPalette.ColorRole.WindowText: TEXT,
        QPalette.ColorRole.Base: BG_ALT,
        QPalette.ColorRole.AlternateBase: SURFACE,
        QPalette.ColorRole.Text: TEXT,
        QPalette.ColorRole.Button: SURFACE,
        QPalette.ColorRole.ButtonText: TEXT,
        QPalette.ColorRole.Highlight: ACCENT,
        QPalette.ColorRole.HighlightedText: "#ffffff",
        QPalette.ColorRole.ToolTipBase: SURFACE,
        QPalette.ColorRole.ToolTipText: TEXT,
        QPalette.ColorRole.Link: ACCENT,
        QPalette.ColorRole.PlaceholderText: "#6b727c",
    }
    for role, color in roles.items():
        pal.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor("#59606a"))
    app.setPalette(pal)
    app.setStyleSheet(STYLESHEET)
