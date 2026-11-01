"""Project folder tree: browse, open, create, rename and delete files in a service's folder."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from PyQt6.QtCore import QDir, QFile, QModelIndex, QPoint, QSortFilterProxyModel, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QFileSystemModel, QGuiApplication
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from ..core.models import SCRIPT_EXTENSIONS
from . import theme
from .prompts import ask

# Tool caches nobody wants to browse. Environments (.venv, node_modules) stay visible: they
# are collapsed until opened and it's useful to peek inside them.
HIDDEN_NAMES = frozenset({
    "__pycache__", ".git", ".hg", ".svn", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    ".DS_Store", "Thumbs.db",
})


class _HideNoise(QSortFilterProxyModel):
    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model = self.sourceModel()
        if model is None:
            return True
        name = model.index(source_row, 0, source_parent).data()
        return not (isinstance(name, str) and name in HIDDEN_NAMES)

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        # Folders first, then case-insensitive names, like file managers do.
        model = self.sourceModel()
        if isinstance(model, QFileSystemModel):
            ldir, rdir = model.isDir(left), model.isDir(right)
            if ldir != rdir:
                return ldir
            return str(model.fileName(left)).lower() < str(model.fileName(right)).lower()
        return super().lessThan(left, right)


class FileTree(QWidget):
    """A tree of one folder; emits ``open_file`` for files and ``run_file`` for "Run as service"."""

    open_file = pyqtSignal(str)
    run_file = pyqtSignal(str)
    renamed = pyqtSignal(str, str)  # (old path, new path)
    status_message = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SidebarPane")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._root: Path | None = None
        self.fs = QFileSystemModel(self)
        self.fs.setReadOnly(False)
        self.fs.fileRenamed.connect(lambda folder, old, new: self.renamed.emit(str(Path(folder) / old),
                                                                                str(Path(folder) / new)))
        self.fs.setFilter(QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot | QDir.Filter.Hidden)
        self.proxy = _HideNoise(self)
        self.proxy.setSourceModel(self.fs)
        self.proxy.setDynamicSortFilter(True)

        self.view = theme.named(QTreeView(), "FileTree")
        self.view.setModel(self.proxy)
        self.view.setHeaderHidden(True)
        for col in (1, 2, 3):
            self.view.hideColumn(col)
        self.view.setSortingEnabled(True)
        self.view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.view.setUniformRowHeights(True)
        self.view.setAnimated(False)
        self.view.setIndentation(14)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.EditKeyPressed)  # F2 renames in place
        self.view.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._menu)
        self.view.activated.connect(self._activated)
        self.view.setStyleSheet(
            f"QTreeView {{ background: {theme.BG_ALT}; border: none; outline: none; }}"
            f"QTreeView::item {{ padding: 2px 0; border: none; }}"
            f"QTreeView::item:hover {{ background: {theme.SURFACE}; }}"
            f"QTreeView::item:selected {{ background: {theme.SELECTION}; color: {theme.TEXT}; }}"
        )

        self.title = theme.named(QLabel("FILES"), "SidebarTitle")
        self.title.setToolTip("")
        self.btn_new = self._tool("+", "New file in the selected folder")
        self.btn_new.clicked.connect(lambda: self.new_file())
        self.btn_folder = self._tool("▭", "New folder")
        self.btn_folder.clicked.connect(lambda: self.new_folder())
        self.btn_collapse = self._tool("⊟", "Collapse all")
        self.btn_collapse.clicked.connect(self.view.collapseAll)
        self.btn_reveal = self._tool("↗", "Open the folder in the file manager")
        self.btn_reveal.clicked.connect(lambda: self._root and self.reveal(self._root))
        self.empty = theme.named(QLabel("Select a service to see its folder."), "Dim")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)

        bar = QHBoxLayout()
        bar.setContentsMargins(14, 0, 6, 0)
        bar.setSpacing(2)
        bar.addWidget(self.title, 1)
        for b in (self.btn_new, self.btn_folder, self.btn_collapse, self.btn_reveal):
            bar.addWidget(b)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(4)
        layout.addLayout(bar)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.empty, 1)
        self.set_root(None)

    @staticmethod
    def _tool(text: str, tip: str) -> QToolButton:
        b = theme.named(QToolButton(), "SidebarTool")
        b.setText(text)
        b.setToolTip(tip)
        b.setAutoRaise(True)
        b.setFixedSize(22, 20)
        return b

    # -- root -----------------------------------------------------------------------------

    @property
    def root(self) -> Path | None:
        return self._root

    def set_root(self, folder: Path | None) -> None:
        if folder is not None and not folder.is_dir():
            folder = None
        if folder == self._root and folder is not None:
            return
        self._root = folder
        has = folder is not None
        self.view.setVisible(has)
        self.empty.setVisible(not has)
        for b in (self.btn_new, self.btn_folder, self.btn_collapse, self.btn_reveal):
            b.setEnabled(has)
        if folder is None:
            self.title.setText("FILES")
            self.title.setToolTip("")
            self.fs.setRootPath("")
            return
        self.title.setText(folder.name.upper() or str(folder))
        self.title.setToolTip(str(folder))
        source_root = self.fs.setRootPath(str(folder))
        self.view.setRootIndex(self.proxy.mapFromSource(source_root))

    def show_message(self, text: str) -> None:
        self.empty.setText(text)

    # -- helpers --------------------------------------------------------------------------

    def path_at(self, index: QModelIndex) -> Path | None:
        if not index.isValid():
            return None
        return Path(self.fs.filePath(self.proxy.mapToSource(index)))

    def selected_path(self) -> Path | None:
        return self.path_at(self.view.currentIndex())

    def _target_folder(self, path: Path | None = None) -> Path | None:
        path = path or self.selected_path()
        if path is None:
            return self._root
        return path if path.is_dir() else path.parent

    def select_path(self, path: Path) -> None:
        index = self.proxy.mapFromSource(self.fs.index(str(path)))
        if index.isValid():
            self.view.setCurrentIndex(index)
            self.view.scrollTo(index)

    def _activated(self, index: QModelIndex) -> None:
        path = self.path_at(index)
        if path is not None and path.is_file():
            self.open_file.emit(str(path))

    # -- actions --------------------------------------------------------------------------

    def new_file(self, folder: Path | None = None, name: str | None = None) -> Path | None:
        folder = self._target_folder(folder)
        if folder is None:
            return None
        if name is None:
            name, ok = QInputDialog.getText(self, "New file", f"File name in {folder}:", QLineEdit.EchoMode.Normal,
                                            "main.py")
            if not ok:
                return None
        name = name.strip()
        if not _valid_name(name, nested=True):
            self.status_message.emit(f"“{name}” is not a valid file name.")
            return None
        path = folder / name
        if path.exists():
            self.status_message.emit(f"{path.name} already exists.")
            if path.is_file():
                self.open_file.emit(str(path))
            return None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        except OSError as exc:
            self.status_message.emit(f"Could not create {path}: {exc.strerror or exc}")
            return None
        self.open_file.emit(str(path))
        return path

    def new_folder(self, parent: Path | None = None, name: str | None = None) -> Path | None:
        parent = self._target_folder(parent)
        if parent is None:
            return None
        if name is None:
            name, ok = QInputDialog.getText(self, "New folder", f"Folder name in {parent}:")
            if not ok:
                return None
        name = name.strip()
        if not _valid_name(name, nested=True):
            self.status_message.emit(f"“{name}” is not a valid folder name.")
            return None
        path = parent / name
        try:
            path.mkdir(parents=True)
        except OSError as exc:
            self.status_message.emit(f"Could not create {path}: {exc.strerror or exc}")
            return None
        return path

    def rename(self, path: Path, new_name: str | None = None) -> Path | None:
        if new_name is None:
            new_name, ok = QInputDialog.getText(self, "Rename", f"New name for {path.name}:",
                                                QLineEdit.EchoMode.Normal, path.name)
            if not ok:
                return None
        new_name = new_name.strip()
        if new_name == path.name:
            return path
        if not _valid_name(new_name):
            self.status_message.emit(f"“{new_name}” is not a valid name.")
            return None
        target = path.with_name(new_name)
        if target.exists():
            self.status_message.emit(f"{new_name} already exists.")
            return None
        try:
            path.rename(target)
        except OSError as exc:
            self.status_message.emit(f"Could not rename {path.name}: {exc.strerror or exc}")
            return None
        self.renamed.emit(str(path), str(target))
        return target

    def delete(self, path: Path, *, confirm: bool = True) -> bool:
        kind = "folder" if path.is_dir() and not path.is_symlink() else "file"
        if confirm:
            inside = "\n\nEverything inside it goes too." if kind == "folder" else ""
            if not ask(self, f"Delete {kind}", f"Move “{path.name}” to the trash?{inside}", str(path),
                       accept="Move to trash")[0]:
                return False
        if QFile.moveToTrash(str(path))[0]:
            return True
        # No trash (some Linux setups, network drives): ask before deleting for good.
        if confirm and not ask(self, f"Delete {kind}", f"“{path.name}” can't be moved to the trash.",
                               "Delete it permanently? This can't be undone.", accept="Delete permanently")[0]:
            return False
        try:
            if kind == "folder":
                shutil.rmtree(path)  # removes links inside, never what they point to
            else:
                path.unlink()
        except OSError as exc:
            self.status_message.emit(f"Could not delete {path.name}: {exc.strerror or exc}")
            return False
        return True

    @staticmethod
    def reveal(path: Path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path if path.is_dir() else path.parent)))

    # -- context menu ---------------------------------------------------------------------

    def _menu(self, pos: QPoint) -> None:
        if self._root is None:
            return
        index = self.view.indexAt(pos)
        path = self.path_at(index)
        menu = QMenu(self)

        def add(text: str, slot: object) -> None:
            action = menu.addAction(text)
            assert action is not None
            if callable(slot):
                action.triggered.connect(lambda _c=False: slot())

        if path is not None and path.is_file():
            add("Open", lambda: self.open_file.emit(str(path)))
            if path.suffix.lower() in SCRIPT_EXTENSIONS:
                add("Run as new service…", lambda: self.run_file.emit(str(path)))
            menu.addSeparator()
        folder = self._target_folder(path)
        add("New file…", lambda: self.new_file(folder))
        add("New folder…", lambda: self.new_folder(folder))
        if path is not None:
            menu.addSeparator()
            add("Rename…", lambda: self.rename(path))
            add("Delete…", lambda: self.delete(path))
            menu.addSeparator()
            add("Copy path", lambda: _copy(str(path)))
            add("Copy relative path", lambda: _copy(_relative(path, self._root)))
        add("Show in file manager", lambda: self.reveal(path or self._root or Path.home()))
        if viewport := self.view.viewport():
            menu.exec(viewport.mapToGlobal(pos))


_WINDOWS_RESERVED = frozenset({"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                               *(f"lpt{i}" for i in range(1, 10))})


def _valid_name(name: str, *, nested: bool = False) -> bool:
    """A name for something inside the current folder: ``nested`` allows ``sub/dir/file.py``.

    ``..``, absolute paths and drive letters are refused so nothing lands outside the folder.
    """
    if not name or "\0" in name:
        return False
    parts = name.replace("\\", "/").split("/")
    if not nested and len(parts) > 1:
        return False
    if Path(name).is_absolute() or Path(name).drive:
        return False
    for part in parts:
        if part in ("", ".", ".."):
            return False
        if sys.platform == "win32" and (
            any(c in '<>:"|?*' for c in part)
            or part.rstrip(" .") != part
            or part.split(".")[0].lower() in _WINDOWS_RESERVED
        ):
            return False
    return True


def _relative(path: Path, root: Path | None) -> str:
    if root is None:
        return str(path)
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _copy(text: str) -> None:
    if clipboard := QGuiApplication.clipboard():
        clipboard.setText(text)
