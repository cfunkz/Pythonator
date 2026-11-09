"""The look, as one set of tokens: navy and chartreuse (dark) or paper and navy (light), from the final design.
Fonts are bundled (Space Grotesk for text, IBM Plex Mono for code and numbers, both SIL OFL), so the window looks the
same on every OS. Corners are 6 px on controls and 8 px on panels; only status dots, layer rings and step numbers are
round. One solid colour per control, no gradients. Widgets never hard-code a colour: they read `current()`."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QEvent, QObject, Qt
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PyQt6.QtWidgets import QAbstractSpinBox, QApplication, QMainWindow, QWidget

FONTS = Path(__file__).resolve().parent.parent / "resources" / "fonts"

@dataclass(frozen=True)
class Tokens:
    name: str
    bg: str
    surface: str
    raised: str
    raised2: str
    border: str
    border_strong: str
    text: str
    muted: str
    faint: str
    chip: str
    accent: str  # primary buttons, selection: the one colour that means "act"
    accent_ink: str
    accent_text: str
    ok: str
    warn: str
    bad: str
    info: str
    m_sandbox: str
    m_container: str
    m_wsl: str
    m_full: str
    console: str
    console_text: str
    pop: str

DARK = Tokens(
    "dark", bg="#06172a", surface="#081c31", raised="#0b2540", raised2="#0f2d4b", border="#173550", border_strong="#245072",
    text="#e3e9df", muted="#93a7b1", faint="#557287", chip="#0d2a45", accent="#c0d070", accent_ink="#06172a",
    accent_text="#d4e28c", ok="#a9cf6a", warn="#e5b54e", bad="#e2705f", info="#8dbfd3", m_sandbox="#c0d070",
    m_container="#6fb0cf", m_wsl="#b9a3e0", m_full="#e5b54e", console="#041120", console_text="#dfe6dc", pop="#0d2945",
)
LIGHT = Tokens(
    "light", bg="#f4f1e8", surface="#fbf9f3", raised="#fffdf8", raised2="#ebe6d8", border="#ddd6c4", border_strong="#c9bfa6",
    text="#142230", muted="#46535e", faint="#8a939a", chip="#ece7d9", accent="#003050", accent_ink="#f4f1e8",
    accent_text="#0d5876", ok="#4f7a28", warn="#946300", bad="#b03a2e", info="#0d5876", m_sandbox="#6f7d3c",
    m_container="#106080", m_wsl="#7a5aa8", m_full="#946300", console="#0a2238", console_text="#e3e9df", pop="#fffdf8",
)

RC, RCARD = 6, 8
UI_FAMILY, MONO_FAMILY = "Space Grotesk", "IBM Plex Mono"
_current = DARK
_loaded = False

def current() -> Tokens:
    return _current

def tone(name: str, t: Tokens | None = None) -> str:
    """A token by name: 'ok', 'warn', 'm_sandbox', 'muted', … Unknown names fall back to muted."""
    t = t or _current
    return str(getattr(t, name.replace("-", "_"), t.muted))

def rgba(hex_: str, alpha: float) -> str:
    c = QColor(hex_)
    return f"rgba({c.red()},{c.green()},{c.blue()},{alpha:.2f})"

def mix(fg: str, bg: str, f: float) -> QColor:
    """`color-mix(fg f, bg)`, as the design writes it; solid, so it paints the same on every style."""
    a, b = QColor(fg), QColor(bg)
    return QColor(
        round(a.red() * f + b.red() * (1 - f)), round(a.green() * f + b.green() * (1 - f)), round(a.blue() * f + b.blue() * (1 - f))
    )

def load_fonts() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    for f in sorted(FONTS.glob("*.ttf")):
        QFontDatabase.addApplicationFont(str(f))

def ui_font(px: float = 13, weight: int = 400) -> QFont:
    f = QFont(UI_FAMILY)
    f.setPixelSize(round(px))
    f.setWeight(QFont.Weight(weight))
    f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return f

def mono_font(px: float = 12, weight: int = 400) -> QFont:
    f = QFont(MONO_FAMILY)
    f.setPixelSize(round(px))
    f.setWeight(QFont.Weight(weight))
    f.setStyleHint(QFont.StyleHint.Monospace)
    return f

def label_font(px: float = 10.5) -> QFont:
    """The small uppercase labels (SERVICES, WEB, JOBS…): 650 weight, wide letter spacing."""
    f = ui_font(px, 600)
    f.setCapitalization(QFont.Capitalization.AllUppercase)
    f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 110)
    return f

def qss(t: Tokens) -> str:
    sel_bg = rgba(t.accent, 0.10)
    return f"""
* {{ outline: 0; }}
QWidget {{ background: transparent; color: {t.text};
           selection-background-color: {t.accent}; selection-color: {t.accent_ink}; }}
QMainWindow, QDialog, #Page, #Work {{ background: {t.bg}; }}
QToolTip {{ background: {t.pop}; color: {t.text}; border: 1px solid {t.border_strong}; padding: 6px 9px; border-radius: 5px; font-size: 12px; }}

#TopBar {{ background: {t.surface}; border-bottom: 1px solid {t.border}; }}
#StatusBar {{ background: {t.surface}; border-top: 1px solid {t.border}; }}
#StatusBar QLabel {{ color: {t.muted}; font-size: 11.5px; }}
#Rail {{ background: {t.surface}; border-right: 1px solid {t.border}; }}
#RailBtn {{ border: 0; border-radius: {RC}px; background: transparent; }}
#RailBtn:hover {{ background: {t.raised}; }}
#RailBtn:checked {{ background: {t.raised2}; border-left: 2px solid {t.accent}; border-top-left-radius: 0; border-bottom-left-radius: 0; }}
#BottomNav {{ background: {t.surface}; border-top: 1px solid {t.border}; }}
#BottomNav QToolButton {{ border: 0; color: {t.muted}; font-size: 10.5px; font-weight: 500; padding-top: 6px; }}
#BottomNav QToolButton:checked {{ color: {t.accent_text}; }}
#ListPane {{ background: {t.bg}; border-right: 1px solid {t.border}; }}
#Inspector, #SideNav, #SidePanel, #Steps, #WizardSide, #WizardFoot, #Tree, #AiPane, #PluginList {{ background: {t.surface}; }}
#Inspector, #SidePanel, #WizardSide, #AiPane {{ border-left: 1px solid {t.border}; }}
#SideNav, #Steps, #Tree, #PluginList {{ border-right: 1px solid {t.border}; }}
#WizardFoot {{ border-top: 1px solid {t.border}; }}

#Brand {{ font-weight: 700; font-size: 14px; }}
#H1 {{ font-size: 19px; font-weight: 700; }}
#H2 {{ font-size: 15px; font-weight: 700; }}
#H3 {{ font-size: 13.5px; font-weight: 600; }}
#Muted, QLabel[muted="true"] {{ color: {t.muted}; }}
#Faint {{ color: {t.faint}; }}
#Small {{ color: {t.muted}; font-size: 12px; }}
#Mono {{ font-family: "{MONO_FAMILY}"; font-size: 12px; }}
#MonoMuted {{ font-family: "{MONO_FAMILY}"; font-size: 11.5px; color: {t.muted}; }}
#Kbd {{ font-family: "{MONO_FAMILY}"; font-size: 10.5px; padding: 2px 5px; border-radius: 5px; border: 1px solid {t.border};
        color: {t.muted}; background: {t.surface}; }}

#Card, #Panel {{ background: {t.raised}; border: 1px solid {t.border}; border-radius: {RC}px; }}
#Panel #PanelHead {{ border-bottom: 1px solid {t.border}; }}
#CardHead {{ font-weight: 600; }}
#Sheet {{ background: {t.raised}; border: 1px solid {t.border_strong}; border-radius: {RCARD}px; }}
#Surface {{ background: {t.surface}; border: 1px solid {t.border}; border-radius: {RC}px; }}
#Console {{ background: {t.console}; border: 1px solid {t.border}; border-radius: {RC}px; }}
#Pop {{ background: {t.pop}; border: 1px solid {t.border_strong}; border-radius: {RC}px; }}
#Scrim {{ background: {rgba("#000000", 0.45)}; }}
#Row {{ border-bottom: 1px solid {t.border}; }}
#Hr {{ background: {t.border}; max-height: 1px; min-height: 1px; }}

QPushButton {{ background: {t.raised}; color: {t.text}; border: 1px solid {t.border}; border-radius: {RC}px;
               padding: 0 12px; min-height: 30px; max-height: 30px; font-weight: 500; }}
QPushButton:hover {{ border-color: {t.border_strong}; }}
QPushButton:pressed {{ background: {t.raised2}; }}
QPushButton:disabled {{ color: {t.faint}; }}
QPushButton:focus {{ border-color: {t.accent}; }}
QPushButton[kind="primary"] {{ background: {t.accent}; color: {t.accent_ink}; border-color: {t.accent}; }}
QPushButton[kind="primary"]:hover {{ background: {mix(t.accent, t.text, 0.88).name()}; }}
QPushButton[kind="primary"]:disabled {{ background: {t.raised2}; border-color: {t.border}; color: {t.faint}; }}
QPushButton[kind="danger"] {{ color: {t.bad}; border-color: {rgba(t.bad, 0.40)}; background: {rgba(t.bad, 0.10)}; }}
QPushButton[kind="ghost"] {{ background: transparent; border-color: transparent; color: {t.muted}; }}
QPushButton[kind="ghost"]:hover {{ background: {t.raised}; }}
QPushButton[size="sm"] {{ min-height: 24px; max-height: 24px; padding: 0 9px; font-size: 12px; }}
QPushButton[icon_only="true"] {{ padding: 0; min-width: 30px; max-width: 30px; }}
QPushButton[icon_only="true"][size="sm"] {{ min-width: 24px; max-width: 24px; }}
QPushButton::menu-indicator {{ width: 0; image: none; }}

#Seg {{ background: {t.surface}; border: 1px solid {t.border}; border-radius: {RC + 2}px; }}
#Seg QPushButton {{ background: transparent; border: 0; min-height: 28px; max-height: 28px; padding: 0 12px; color: {t.muted}; }}
#Seg QPushButton:checked {{ background: {t.raised2}; color: {t.text}; border: 1px solid {t.border}; }}
#Seg[accent="true"] QPushButton:checked {{ background: {t.accent}; color: {t.accent_ink}; border: 0; }}

#Tabs {{ border-bottom: 1px solid {t.border}; }}
#Tabs QToolButton {{ border: 0; border-bottom: 2px solid transparent; color: {t.muted}; font-weight: 500; padding: 0 12px;
                     min-height: 38px; max-height: 38px; }}
#Tabs QToolButton:hover {{ color: {t.text}; }}
#Tabs QToolButton:checked {{ color: {t.text}; border-bottom-color: {t.accent}; }}

#SideNav QToolButton, #Steps QToolButton {{ border: 0; border-radius: {RC}px; color: {t.muted}; font-weight: 500;
                       text-align: left; padding: 0 10px; min-height: 36px; }}
#SideNav QToolButton:hover {{ background: {t.raised}; }}
#SideNav QToolButton:checked {{ background: {t.raised2}; color: {t.text}; border-left: 2px solid {t.accent};
                                border-top-left-radius: 0; border-bottom-left-radius: 0; }}

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{ background: {t.surface}; border: 1px solid {t.border};
             border-radius: {RC}px; padding: 0 10px; min-height: 32px; color: {t.text}; }}
QPlainTextEdit, QTextEdit {{ padding: 6px 8px; }}
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {t.accent}; }}
QLineEdit[mono="true"] {{ font-family: "{MONO_FAMILY}"; font-size: 12px; }}
#Search {{ background: {t.bg}; border: 1px solid {t.border}; border-radius: {RC}px; }}
#Search QLineEdit, #Bare {{ background: transparent; border: 0; padding: 0; min-height: 28px; }}
QComboBox::drop-down {{ border: 0; width: 22px; }}
QComboBox QAbstractItemView {{ background: {t.pop}; border: 1px solid {t.border_strong}; selection-background-color: {t.raised2};
                               selection-color: {t.text}; outline: 0; padding: 4px; }}
QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width: 0; border: 0; }}

QListView, QTreeView, QTableView, QListWidget, QTreeWidget, QTableWidget {{ background: transparent; border: 0; }}
QTreeView::item, QListView::item {{ min-height: 26px; border-radius: 5px; padding: 0 4px; color: {t.muted}; }}
QTreeView::item:hover, QListView::item:hover {{ background: {t.raised}; }}
QTreeView::item:selected, QListView::item:selected {{ background: {t.raised2}; color: {t.text}; }}
#ServiceList::item, #ServiceList::item:selected, #ServiceList::item:hover {{ background: transparent; }}
QHeaderView::section {{ background: transparent; color: {t.muted}; border: 0; border-bottom: 1px solid {t.border};
                        padding: 9px 10px; font-size: 10.5px; font-weight: 600; }}
QTableView {{ gridline-color: transparent; }}
QTableView::item {{ border-bottom: 1px solid {t.border}; padding: 0 10px; }}
QTableView::item:selected {{ background: {sel_bg}; color: {t.text}; }}

QSplitter::handle {{ background: transparent; }}
QSplitter::handle:hover, QSplitter::handle:pressed {{ background: {rgba(t.accent, 0.5)}; }}
QScrollArea {{ border: 0; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {t.border_strong}; border-radius: 3px; min-height: 30px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {t.border_strong}; border-radius: 3px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ background: none; border: 0; width: 0; height: 0; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid {t.border_strong}; background: {t.surface}; }}
QCheckBox::indicator:checked {{ background: {t.accent}; border-color: {t.accent}; }}
QMenu {{ background: {t.pop}; border: 1px solid {t.border_strong}; border-radius: {RC}px; padding: 4px; }}
QMenu::item {{ padding: 7px 14px; border-radius: 4px; }}
QMenu::item:selected {{ background: {t.raised2}; color: {t.text}; }}
QMenu::separator {{ height: 1px; background: {t.border}; margin: 4px 6px; }}
"""

def apply(app: QApplication, t: Tokens) -> None:
    global _current
    _current = t
    load_fonts()
    app.setFont(ui_font(13))
    pal = app.palette()
    for role, col in (
        (QPalette.ColorRole.Window, t.bg), (QPalette.ColorRole.Base, t.surface),
        (QPalette.ColorRole.AlternateBase, t.raised), (QPalette.ColorRole.Text, t.text),
        (QPalette.ColorRole.WindowText, t.text), (QPalette.ColorRole.Button, t.raised),
        (QPalette.ColorRole.ButtonText, t.text), (QPalette.ColorRole.Highlight, t.accent),
        (QPalette.ColorRole.HighlightedText, t.accent_ink), (QPalette.ColorRole.PlaceholderText, t.faint),
        (QPalette.ColorRole.ToolTipBase, t.pop), (QPalette.ColorRole.ToolTipText, t.text),
    ):
        pal.setColor(role, QColor(col))
    app.setPalette(pal)
    app.setStyleSheet(qss(t))
    global _pad
    if _pad is None:
        _pad = _SpinPadding(app)
        app.installEventFilter(_pad)

class _SpinPadding(QObject):
    """A spin box ignores the stylesheet's left padding, so its number sat on the border while every text field had
    10 px. Give each one the same inset when it is first shown."""

    def eventFilter(self, obj: QObject | None, e: QEvent | None) -> bool:
        if e is None:
            return False
        if e.type() == QEvent.Type.Polish and isinstance(obj, QAbstractSpinBox):
            le = obj.lineEdit()
            if le is not None and le.textMargins().left() == 0:
                le.setTextMargins(9, 0, 0, 0)
        elif e.type() == QEvent.Type.Show and isinstance(obj, QWidget) and _stray(obj):
            # A part of a page shown before it was put in the page would open as a bare window for a moment (on
            # Windows a dark flash, like a console). It never reaches the screen; it shows inside its page later.
            obj.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            obj.setProperty("stray", True)
        elif e.type() == QEvent.Type.ParentChange and isinstance(obj, QWidget) and obj.property("stray"):
            obj.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, False)
            obj.setProperty("stray", False)
        return False

def _stray(w: QWidget) -> bool:
    return (w.isWindow() and w.parentWidget() is None and w.windowType() == Qt.WindowType.Widget
            and not isinstance(w, QMainWindow) and not w.property("window"))

_pad: _SpinPadding | None = None

def remembered() -> Tokens:
    """The theme you picked last time (Ctrl Shift L or the moon button), else dark."""
    from PyQt6.QtCore import QSettings

    return LIGHT if QSettings("Pythonator", "Pythonator").value("theme", "dark") == "light" else DARK

def remember(t: Tokens) -> None:
    from PyQt6.QtCore import QSettings

    QSettings("Pythonator", "Pythonator").setValue("theme", t.name)
