"""Small coloured icons, drawn with QPainter: the same on every system, no image files, sharp on HiDPI."""

from __future__ import annotations

from collections.abc import Callable
from functools import cache
from pathlib import Path

from PyQt6.QtCore import QFileInfo, QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QAbstractFileIconProvider,
    QColor,
    QFont,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)
from PyQt6.QtWidgets import QFileIconProvider

# The file tree's buttons and icons.
TINT = {"new": "#4f9cf9", "file": "#8b93a3", "folder": "#d29922", "collapse": "#8b93a3", "open": "#39c5cf",
        "close": "#8b93a3"}  # the rest (the find bar's) are grey: "#8b93a3"

# Language colours for the file tree, as most editors use them.
LANG = {
    ".py": "#3776ab", ".pyw": "#3776ab", ".ipynb": "#f37626", ".js": "#f1e05a", ".mjs": "#f1e05a",
    ".cjs": "#f1e05a", ".jsx": "#61dafb", ".ts": "#3178c6", ".tsx": "#3178c6", ".go": "#00add8",
    ".rs": "#dea584", ".rb": "#cc342d", ".php": "#777bb4", ".java": "#b07219", ".jar": "#b07219",
    ".kt": "#a97bff", ".cs": "#178600", ".csproj": "#178600", ".c": "#555555", ".h": "#555555",
    ".cpp": "#f34b7d", ".sh": "#89e051", ".bash": "#89e051", ".ps1": "#5391fe", ".bat": "#c1f12e",
    ".html": "#e34c26", ".css": "#563d7c", ".scss": "#c6538c", ".json": "#cbcb41", ".toml": "#9c4221",
    ".yml": "#cb171e", ".yaml": "#cb171e", ".md": "#8b93a3", ".txt": "#8b93a3", ".lock": "#6e7681",
    ".env": "#ecd53f", ".sql": "#e38c00", ".lua": "#000080", ".xml": "#0060ac", ".svg": "#ffb13b",
    ".png": "#a371f7", ".jpg": "#a371f7", ".gif": "#a371f7", ".ico": "#a371f7",
}

Draw = Callable[[QPainter, QColor], None]


def _pen(color: QColor, width: float = 2.0) -> QPen:
    pen = QPen(color, width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _plus(p: QPainter, c: QColor) -> None:
    p.setPen(_pen(c, 2.4))
    p.drawLine(QPointF(8, 3), QPointF(8, 13))
    p.drawLine(QPointF(3, 8), QPointF(13, 8))


def _page(p: QPainter, c: QColor) -> None:
    path = QPainterPath()
    path.moveTo(3.5, 1.5)
    path.lineTo(9.5, 1.5)
    path.lineTo(12.5, 4.5)
    path.lineTo(12.5, 14.5)
    path.lineTo(3.5, 14.5)
    path.closeSubpath()
    p.setPen(_pen(c, 1.3))
    p.setBrush(QColor(c.red(), c.green(), c.blue(), 60))
    p.drawPath(path)
    p.setPen(_pen(c, 1.1))
    for y in (7.5, 10, 12.3):
        p.drawLine(QPointF(5.5, y), QPointF(10.5, y))


def _folder(p: QPainter, c: QColor) -> None:
    path = QPainterPath()
    path.moveTo(1.5, 3.5)
    path.lineTo(6, 3.5)
    path.lineTo(7.5, 5)
    path.lineTo(14.5, 5)
    path.lineTo(14.5, 13.5)
    path.lineTo(1.5, 13.5)
    path.closeSubpath()
    p.setPen(_pen(c, 1.2))
    p.setBrush(QColor(c.red(), c.green(), c.blue(), 150))
    p.drawPath(path)


def _collapse(p: QPainter, c: QColor) -> None:
    p.setPen(_pen(c, 1.8))
    p.drawLine(QPointF(4.5, 4), QPointF(8, 7))
    p.drawLine(QPointF(11.5, 4), QPointF(8, 7))
    p.drawLine(QPointF(4.5, 9), QPointF(8, 12))
    p.drawLine(QPointF(11.5, 9), QPointF(8, 12))


def _open(p: QPainter, c: QColor) -> None:
    p.setPen(_pen(c, 1.8))
    p.drawLine(QPointF(5, 11), QPointF(12, 4))
    p.drawLine(QPointF(7, 4), QPointF(12, 4))
    p.drawLine(QPointF(12, 4), QPointF(12, 9))


def _close(p: QPainter, c: QColor) -> None:
    p.setPen(_pen(c, 1.6))
    p.drawLine(QPointF(4.5, 4.5), QPointF(11.5, 11.5))
    p.drawLine(QPointF(11.5, 4.5), QPointF(4.5, 11.5))


def _glyph(text: str, *, underline: bool = False, size: float = 8.5) -> Draw:
    """Letters as an icon: Aa (match case), ab (whole word), .* (regular expression)."""

    def draw(p: QPainter, c: QColor) -> None:
        font = QFont()
        font.setPointSizeF(size)
        font.setBold(True)
        p.setFont(font)
        p.setPen(c)
        p.drawText(QRectF(0, -1 if underline else 0, 16, 16), Qt.AlignmentFlag.AlignCenter, text)
        if underline:  # a bracket under the word: only whole words
            p.setPen(_pen(c, 1.2))
            p.drawPolyline(QPolygonF([QPointF(3, 11.5), QPointF(3, 13.5), QPointF(13, 13.5), QPointF(13, 11.5)]))

    return draw


def _arrow(up: bool) -> Draw:
    def draw(p: QPainter, c: QColor) -> None:
        p.setPen(_pen(c, 1.7))
        tip, tail = (3.5, 12.5) if up else (12.5, 3.5)
        p.drawLine(QPointF(8, tip), QPointF(8, tail))
        wing = tip + (4 if up else -4)
        p.drawLine(QPointF(8, tip), QPointF(4, wing))
        p.drawLine(QPointF(8, tip), QPointF(12, wing))

    return draw


def _chevron(down: bool) -> Draw:
    def draw(p: QPainter, c: QColor) -> None:
        p.setPen(_pen(c, 1.7))
        if down:
            p.drawPolyline(QPolygonF([QPointF(4, 6), QPointF(8, 10), QPointF(12, 6)]))
        else:
            p.drawPolyline(QPolygonF([QPointF(6, 4), QPointF(10, 8), QPointF(6, 12)]))

    return draw


def _replace(every: bool) -> Draw:
    """A line that turns into the new text: ↵ (all: with a second line above)."""

    def draw(p: QPainter, c: QColor) -> None:
        p.setPen(_pen(c, 1.6))
        if every:
            p.drawLine(QPointF(3, 3.5), QPointF(11, 3.5))
        p.drawPolyline(QPolygonF([QPointF(13, 6), QPointF(13, 11), QPointF(4, 11)]))
        p.drawPolyline(QPolygonF([QPointF(6.5, 8.5), QPointF(4, 11), QPointF(6.5, 13.5)]))

    return draw


SHAPES: dict[str, Draw] = {
    "new": _plus, "file": _page, "folder": _folder, "collapse": _collapse, "open": _open, "close": _close,
    "case": _glyph("Aa"), "word": _glyph("ab", underline=True), "regex": _glyph(".*", size=10),
    "up": _arrow(True), "down": _arrow(False), "more": _chevron(False), "less": _chevron(True),
    "replace": _replace(False), "replace_all": _replace(True),
}


def _render(shape: Draw, color: str, size: int = 16, alpha: int = 255) -> QPixmap:
    scale = 3  # drawn large, so it stays sharp on any screen
    pm = QPixmap(size * scale, size * scale)
    pm.fill(Qt.GlobalColor.transparent)
    pm.setDevicePixelRatio(scale)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size / 16, size / 16)
    c = QColor(color)
    c.setAlpha(alpha)
    shape(p, c)
    p.end()
    return pm


@cache
def icon(name: str, color: str = "") -> QIcon:
    """The ``name`` icon in its own colour (or ``color``), with a faded version for disabled buttons."""
    shape = SHAPES[name]
    tint = color or TINT.get(name, "#8b93a3")
    out = QIcon()
    out.addPixmap(_render(shape, tint), QIcon.Mode.Normal)
    out.addPixmap(_render(shape, "#6e7681", alpha=150), QIcon.Mode.Disabled)
    out.addPixmap(_render(shape, "#9aa5ff"), QIcon.Mode.Normal, QIcon.State.On)  # a toggle that is on
    return out


class FileIcons(QFileIconProvider):
    """Folders in amber, files in their language's colour."""

    def icon(self, what: QAbstractFileIconProvider.IconType | QFileInfo) -> QIcon:
        if isinstance(what, QFileInfo):
            if what.isDir():
                return icon("folder")
            name = what.fileName().lower()
            ext = ".env" if name.startswith(".env") else Path(name).suffix
            return icon("file", LANG.get(ext, TINT["file"]))
        if what == QAbstractFileIconProvider.IconType.Folder:
            return icon("folder")
        return icon("file")
