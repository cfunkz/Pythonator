"""Dark theme: palette, stylesheet, fonts and status colours.

A modern developer tool look: a cool blue-grey palette with one indigo accent, soft 4-8px corners,
1px hairline borders and tight spacing. Monospace is kept for what reads like terminal output (paths,
pids, CPU / memory, the status bar); everything else uses the system UI font.
"""

from __future__ import annotations

import sys
from importlib import resources
from typing import TypeVar

from PyQt6.QtCore import QTemporaryDir
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPalette
from PyQt6.QtWidgets import QApplication, QStyleFactory, QWidget

from ..supervisor import Status

BG = "#0d0f14"  # editor and console
BG_ALT = "#12141b"  # sidebar, toolbars, bars
SURFACE = "#191c25"  # hover, popups, buttons
SURFACE_HI = "#21252f"  # pressed, raised
BORDER = "#22262f"  # hairlines between areas
BORDER_HI = "#2e3340"  # control outlines
TEXT = "#e3e6ed"
TEXT_DIM = "#8b92a3"
TEXT_FAINT = "#555c6c"
ACCENT = "#7c7fff"  # indigo
ACCENT_HI = "#9497ff"
SELECTION = "#1f2140"  # selected rows, checked toggles: the accent mixed into the background
GREEN = "#3ecf8e"
YELLOW = "#f5b544"
RED = "#f25f66"
BLUE = "#5aa9ff"

# Corner radii: small controls, buttons and inputs, cards and popups.
R_SM, R_MD, R_LG = 4, 6, 8

STATUS_COLORS: dict[Status, str] = {
    Status.STOPPED: "#5b6274",
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


def rgba(color: str, alpha: float) -> str:
    """``color`` with ``alpha`` (0-1) as a stylesheet ``rgba()``."""
    c = QColor(color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {round(alpha * 255)})"


def badge_style(color: str) -> str:
    """Stylesheet of a small status pill: tinted background, coloured text."""
    return (f"QLabel {{ background: {rgba(color, 0.14)}; color: {color}; border: 1px solid {rgba(color, 0.35)};"
            f" border-radius: 9px; padding: 0 8px; font-weight: 600; }}")


def stylesheet(mono: str = "monospace", images: str = "") -> str:
    """The application stylesheet.

    ``mono`` is the monospace family for terminal-like text, ``images`` the folder holding the arrow
    and check mark images written by :func:`_write_images` (without it the style's own are used).
    """
    image_rules = (
        f'QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url("{images}/up.png"); width: 7px; height: 4px; }}\n'
        f'QSpinBox::down-arrow, QDoubleSpinBox::down-arrow, QComboBox::down-arrow '
        f'{{ image: url("{images}/down.png"); width: 7px; height: 4px; }}\n'
        f'QCheckBox::indicator:checked {{ image: url("{images}/check.png"); }}'
    ) if images else ""
    small = f"{8.5 * _POINT_SCALE:g}pt"
    return f"""
QWidget {{ color: {TEXT}; }}
QMainWindow, QDialog {{ background: {BG}; }}
QToolTip {{ background: {SURFACE_HI}; color: {TEXT}; border: 1px solid {BORDER_HI}; padding: 4px 8px; }}

/* Terminal-like text: paths, pids, usage figures, the status bar. */
#HeaderInfo, QStatusBar, QStatusBar QLabel, #InputBar QComboBox {{ font-family: "{mono}"; font-size: {small}; }}

QToolBar {{ background: {BG_ALT}; border: none; border-bottom: 1px solid {BORDER}; padding: 4px 6px; spacing: 4px; }}
QToolBar::separator {{ background: {BORDER_HI}; width: 1px; margin: 6px 4px; }}
QToolBar QToolButton {{ padding: 4px 10px; }}

QToolButton {{
    border: 1px solid transparent; border-radius: {R_MD}px; padding: 3px 8px; background: transparent;
}}
QToolButton:hover {{ background: {SURFACE_HI}; border-color: {BORDER_HI}; }}
QToolButton:pressed {{ background: {SURFACE}; }}
QToolButton:checked {{ background: {rgba(ACCENT, 0.16)}; border-color: {rgba(ACCENT, 0.40)}; color: #c4c6ff; }}
QToolButton:disabled {{ color: {TEXT_FAINT}; }}
QToolButton::menu-indicator {{ image: none; width: 0; }}

/* The run button: green, the one coloured button in the header. */
QToolButton[accent="true"] {{
    background: {rgba(GREEN, 0.14)}; border-color: {rgba(GREEN, 0.45)}; color: #6ee7b0; font-weight: 600;
}}
QToolButton[accent="true"]:hover {{ background: {rgba(GREEN, 0.24)}; border-color: {GREEN}; }}
QToolButton[accent="true"]:pressed {{ background: {rgba(GREEN, 0.32)}; }}
QToolButton[accent="true"]:disabled {{ background: {SURFACE}; border-color: {BORDER}; color: {TEXT_FAINT}; }}

QPushButton {{
    background: {SURFACE}; border: 1px solid {BORDER_HI}; border-radius: {R_MD}px; padding: 5px 16px;
}}
QPushButton:hover {{ background: {SURFACE_HI}; border-color: #3c4252; }}
QPushButton:pressed {{ background: {BG_ALT}; }}
QPushButton:focus {{ border-color: {ACCENT}; }}
QPushButton:disabled {{ color: {TEXT_FAINT}; background: {BG_ALT}; border-color: {BORDER}; }}
QPushButton:default, QPushButton[accent="true"] {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {ACCENT_HI}, stop:1 {ACCENT});
    border: 1px solid {ACCENT}; color: white; font-weight: 600;
}}
QPushButton:default:hover, QPushButton[accent="true"]:hover {{ background: {ACCENT_HI}; }}
QPushButton[danger="true"] {{ background: {rgba(RED, 0.15)}; border-color: {rgba(RED, 0.5)}; color: #ffc2c4; }}
QPushButton[danger="true"]:hover {{ background: {rgba(RED, 0.25)}; }}

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {BG}; border: 1px solid {BORDER_HI}; border-radius: {R_MD}px; padding: 4px 8px;
    selection-background-color: {rgba(ACCENT, 0.45)}; selection-color: white;
}}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover {{ border-color: #3c4252; }}
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {ACCENT};
}}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{ color: {TEXT_FAINT}; border-color: {BORDER}; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background: {SURFACE}; border: 1px solid {BORDER_HI}; outline: none; padding: 4px;
    selection-background-color: {SELECTION}; selection-color: {TEXT};
}}
QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    width: 16px; border: none; background: transparent;
}}
QSpinBox::up-button:hover, QSpinBox::down-button:hover,
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {{ background: {SURFACE_HI}; border-radius: {R_SM}px; }}
{image_rules}

QCheckBox {{ spacing: 7px; }}
QCheckBox::indicator {{
    width: 14px; height: 14px; border: 1px solid #3c4252; border-radius: {R_SM}px; background: {BG};
}}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QCheckBox::indicator:disabled {{ border-color: {BORDER}; }}
QCheckBox::indicator:checked:disabled {{ background: {TEXT_FAINT}; border-color: {TEXT_FAINT}; }}

QListView, QTreeView {{ background: {BG_ALT}; border: none; outline: none; }}
QListView::item {{ border: none; }}

QTabWidget::pane {{ border: none; border-top: 1px solid {BORDER}; }}
QTabBar {{ background: {BG_ALT}; }}
QTabBar::tab {{
    background: transparent; color: {TEXT_DIM}; padding: 7px 14px; border: none;
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{ background: {BG}; color: {TEXT}; border-bottom: 2px solid {ACCENT}; }}
QTabBar::tab:hover:!selected {{ color: {TEXT}; background: {SURFACE}; }}
QTabBar::close-button {{ subcontrol-position: right; }}

QSplitter::handle {{ background: {BORDER}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #2a2f3b; min-height: 28px; border-radius: {R_SM}px; margin: 2px; }}
QScrollBar::handle:vertical:hover {{ background: #3a4050; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: #2a2f3b; min-width: 28px; border-radius: {R_SM}px; margin: 2px; }}
QScrollBar::handle:horizontal:hover {{ background: #3a4050; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

QMenu {{ background: {SURFACE}; border: 1px solid {BORDER_HI}; padding: 4px; }}
QMenu::item {{ padding: 5px 24px 5px 12px; border-radius: {R_SM}px; }}
QMenu::item:selected {{ background: {rgba(ACCENT, 0.22)}; color: white; }}
QMenu::item:disabled {{ color: {TEXT_FAINT}; }}
QMenu::separator {{ height: 1px; background: {BORDER_HI}; margin: 4px 6px; }}

QStatusBar {{ background: {BG_ALT}; border-top: 1px solid {BORDER}; color: {TEXT_DIM}; }}
QStatusBar QLabel {{ color: {TEXT_DIM}; padding: 0 8px; }}
QStatusBar::item {{ border: none; }}
QGroupBox {{ border: 1px solid {BORDER_HI}; border-radius: {R_LG}px; margin-top: 14px; padding-top: 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {TEXT_DIM}; }}

#Sidebar {{ background: {BG_ALT}; border-right: 1px solid {BORDER}; }}
#SidebarPane {{ background: {BG_ALT}; }}
#SidebarTitle {{ color: {TEXT_FAINT}; font-weight: 700; letter-spacing: 1px; font-size: {small}; }}
#SidebarTool {{ color: {TEXT_DIM}; padding: 0; border-radius: {R_SM}px; }}
#SidebarTool:hover {{ color: {TEXT}; }}
#Header {{ background: {BG_ALT}; border-bottom: 1px solid {BORDER}; }}
#Header #HeaderTitle {{ font-size: {11.5 * _POINT_SCALE:g}pt; font-weight: 700; }}
#HeaderInfo {{ color: {TEXT_DIM}; }}
#Dim {{ color: {TEXT_DIM}; }}
#PanelBar {{ background: {BG_ALT}; border-bottom: 1px solid {BORDER}; border-top: 1px solid {BORDER}; }}
#PanelBar QToolButton {{ padding: 2px 8px; }}
#InputBar {{ background: {BG_ALT}; border-top: 1px solid {BORDER}; }}
#FindBar {{ background: {BG_ALT}; border-top: 1px solid {BORDER}; }}
#TabClose {{ border: none; padding: 0 4px; color: {TEXT_FAINT}; border-radius: {R_SM}px; }}
#TabClose:hover {{ color: {TEXT}; background: {SURFACE_HI}; }}
"""


_W = TypeVar("_W", bound=QWidget)


def named(widget: _W, name: str) -> _W:
    """Set the object name used by the stylesheet (``#Name`` selectors) and return the widget."""
    widget.setObjectName(name)
    return widget


def mono_family() -> str:
    """The first installed font of ``_MONO_FAMILIES``, else the system's fixed-width font."""
    available = set(QFontDatabase.families())
    for family in _MONO_FAMILIES:
        if family in available:
            return family
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()


def monospace_font(point_size: int = 10) -> QFont:
    font = QFont(mono_family())
    font.setPointSize(round(point_size * _POINT_SCALE))
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


def _write_images(folder: str) -> None:
    """Arrows for spin and combo boxes and a check mark: styled controls lose the style's own."""
    from PyQt6.QtCore import QPointF, Qt  # noqa: PLC0415
    from PyQt6.QtGui import QPainter, QPen, QPixmap, QPolygonF  # noqa: PLC0415

    def canvas(w: int, h: int) -> tuple[QPixmap, QPainter]:
        pix = QPixmap(w * 2, h * 2)  # drawn at 2x so it stays sharp on high-dpi screens
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(2, 2)
        return pix, painter

    for name, points in (("up", ((0, 4), (3.5, 0), (7, 4))), ("down", ((0, 0), (3.5, 4), (7, 0)))):
        pix, painter = canvas(7, 4)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(TEXT_DIM))
        painter.drawPolygon(QPolygonF([QPointF(x, y) for x, y in points]))
        painter.end()
        pix.save(f"{folder}/{name}.png")

    pix, painter = canvas(14, 14)
    pen = QPen(QColor("white"), 1.8)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.drawPolyline(QPolygonF([QPointF(3.5, 7.2), QPointF(6, 9.6), QPointF(10.5, 4.6)]))
    painter.end()
    pix.save(f"{folder}/check.png")


def apply_theme(app: QApplication) -> None:
    if style := QStyleFactory.create("Fusion"):
        app.setStyle(style)
    pal = QPalette()
    roles = {
        QPalette.ColorRole.Window: BG,
        QPalette.ColorRole.WindowText: TEXT,
        QPalette.ColorRole.Base: BG,
        QPalette.ColorRole.AlternateBase: SURFACE,
        QPalette.ColorRole.Text: TEXT,
        QPalette.ColorRole.Button: SURFACE,
        QPalette.ColorRole.ButtonText: TEXT,
        QPalette.ColorRole.Highlight: ACCENT,
        QPalette.ColorRole.HighlightedText: "#ffffff",
        QPalette.ColorRole.ToolTipBase: SURFACE,
        QPalette.ColorRole.ToolTipText: TEXT,
        QPalette.ColorRole.Link: ACCENT,
        QPalette.ColorRole.PlaceholderText: TEXT_FAINT,
    }
    for role, color in roles.items():
        pal.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(TEXT_FAINT))
    app.setPalette(pal)
    app.setStyleSheet(stylesheet(mono_family(), _image_folder()))


_assets: list[QTemporaryDir] = []  # one folder per run, removed with its files when the app exits


def _image_folder() -> str:
    if not _assets:
        folder = QTemporaryDir()
        if not folder.isValid():
            return ""
        _write_images(folder.path())
        _assets.append(folder)
    return _assets[0].path()
