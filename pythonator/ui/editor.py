"""The code editor: QScintilla (Scintilla's lexers, folding, multi-cursor, fast with big files) and a file tree."""

from __future__ import annotations

import contextlib
from functools import cache
from pathlib import Path

from PyQt6.Qsci import (
    QsciLexer,
    QsciLexerBash,
    QsciLexerBatch,
    QsciLexerCPP,
    QsciLexerCSharp,
    QsciLexerCSS,
    QsciLexerHTML,
    QsciLexerJava,
    QsciLexerJavaScript,
    QsciLexerJSON,
    QsciLexerLua,
    QsciLexerMakefile,
    QsciLexerMarkdown,
    QsciLexerPerl,
    QsciLexerProperties,
    QsciLexerPython,
    QsciLexerRuby,
    QsciLexerSQL,
    QsciLexerXML,
    QsciLexerYAML,
    QsciScintilla,
)
from PyQt6.QtCore import QDir, QModelIndex, QSize, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QFileSystemModel, QKeySequence, QShortcut, QWheelEvent
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QMessageBox,
    QTabBar,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from ..core.store import write_atomic
from . import theme
from .findbar import FindBar
from .icons import LANG, TINT, FileIcons, icon
from .widgets import ask, label, tabs, warn

MAX_EDIT = 20 * 1024 * 1024  # bigger files open read only, without highlighting

GO_KEYWORDS = ("break case chan const continue default defer else fallthrough for func go goto if import interface "
               "map package range return select struct switch type var true false nil iota bool byte error float32 "
               "float64 int int8 int16 int32 int64 rune string uint uint8 uint16 uint32 uint64 uintptr any")
RUST_KEYWORDS = ("as async await break const continue crate dyn else enum extern false fn for if impl in let loop "
                 "match mod move mut pub ref return self Self static struct super trait true type unsafe use where "
                 "while i8 i16 i32 i64 i128 isize u8 u16 u32 u64 u128 usize f32 f64 bool char str String Vec Option "
                 "Result Some None Ok Err")


class _Keywords(QsciLexerCPP):
    """The C++ lexer with another language's keywords (Go, Rust)."""

    def __init__(self, words: str, name: str) -> None:
        super().__init__()
        self._words, self._name = words, name

    def keywords(self, kind: int) -> str:
        return self._words if kind == 1 else ""

    def language(self) -> str:
        return self._name


def _lexer_for(path: Path) -> QsciLexer | None:
    ext = path.suffix.lower()
    name = path.name.lower()
    table: dict[str, type[QsciLexer]] = {
        ".py": QsciLexerPython, ".pyw": QsciLexerPython, ".js": QsciLexerJavaScript, ".mjs": QsciLexerJavaScript,
        ".cjs": QsciLexerJavaScript, ".ts": QsciLexerJavaScript, ".tsx": QsciLexerJavaScript,
        ".jsx": QsciLexerJavaScript, ".java": QsciLexerJava, ".kt": QsciLexerJava, ".cs": QsciLexerCSharp,
        ".c": QsciLexerCPP, ".h": QsciLexerCPP, ".cpp": QsciLexerCPP, ".hpp": QsciLexerCPP, ".rb": QsciLexerRuby,
        ".php": QsciLexerHTML, ".html": QsciLexerHTML, ".htm": QsciLexerHTML, ".css": QsciLexerCSS,
        ".sh": QsciLexerBash, ".bash": QsciLexerBash, ".zsh": QsciLexerBash, ".ps1": QsciLexerBash,
        ".bat": QsciLexerBatch, ".cmd": QsciLexerBatch, ".json": QsciLexerJSON, ".yml": QsciLexerYAML,
        ".yaml": QsciLexerYAML, ".toml": QsciLexerProperties, ".ini": QsciLexerProperties,
        ".cfg": QsciLexerProperties, ".env": QsciLexerProperties, ".md": QsciLexerMarkdown,
        ".sql": QsciLexerSQL, ".xml": QsciLexerXML, ".csproj": QsciLexerXML, ".lua": QsciLexerLua,
        ".pl": QsciLexerPerl,
    }
    if ext == ".go":
        return _Keywords(GO_KEYWORDS, "Go")
    if ext == ".rs":
        return _Keywords(RUST_KEYWORDS, "Rust")
    if name in ("dockerfile", "containerfile"):
        return QsciLexerBash()
    if name in ("makefile", "gnumakefile"):
        return QsciLexerMakefile()
    if name.startswith(".env"):
        return QsciLexerProperties()
    cls = table.get(ext)
    return cls() if cls else None


def _style_lexer(lexer: QsciLexer, size: int) -> None:
    font = theme.mono_font(size)
    lexer.setDefaultFont(font)
    lexer.setFont(font)
    lexer.setDefaultPaper(QColor(theme.color("code_bg")))
    lexer.setPaper(QColor(theme.color("code_bg")))
    if theme.current is theme.DARK:  # Scintilla's default colours are for light backgrounds
        lexer.setDefaultColor(QColor(theme.color("text")))
        for style in range(128):
            c = lexer.color(style)
            if c.lightness() < 140:
                lexer.setColor(QColor.fromHsl(c.hslHue(), min(255, c.hslSaturation() + 40),
                                              255 - c.lightness() // 2), style)


def _decode(data: bytes) -> tuple[str, str]:
    """The file's text, and a note when it isn't UTF-8: such a file opens read only, so saving can't replace
    its bytes with the � of a lossy decode."""
    try:
        return data.decode("utf-8"), ""
    except UnicodeDecodeError:
        return data.decode("utf-8", "replace"), "isn't UTF-8 text: opened read only so saving can't damage it"


def _eol_mode(text: str) -> QsciScintilla.EolMode:
    """The line ending most lines of the file use; new lines and pasted text get the same one."""
    sample = text[:262_144]
    crlf = sample.count("\r\n")
    lf = sample.count("\n") - crlf
    return QsciScintilla.EolMode.EolWindows if crlf > lf else QsciScintilla.EolMode.EolUnix


BASE_SIZE = 11  # the editor's fonts are set at this size; Ctrl+ and Ctrl- zoom from it (Scintilla's own zoom)
MIN_SIZE, MAX_SIZE = 6, 30  # Scintilla zooms from -10 to +20 points


class CodeEditor(QsciScintilla):
    def __init__(self, path: Path, font_size: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.path = path
        self.font_size = font_size
        self.setUtf8(True)
        self.setAutoIndent(True)
        self.setIndentationsUseTabs(False)
        self.setTabWidth(4)
        self.setIndentationGuides(True)
        self.setBraceMatching(QsciScintilla.BraceMatch.SloppyBraceMatch)
        self.setFolding(QsciScintilla.FoldStyle.BoxedTreeFoldStyle)
        self.setCaretLineVisible(True)
        self.setCaretLineBackgroundColor(QColor(theme.color("raised")))
        self.setCaretForegroundColor(QColor(theme.color("text")))
        self.setMarginsBackgroundColor(QColor(theme.color("bg")))
        self.setMarginsForegroundColor(QColor(theme.color("muted")))
        self.setFoldMarginColors(QColor(theme.color("bg")), QColor(theme.color("bg")))
        self.setSelectionBackgroundColor(QColor(theme.color("selection")))
        self.setPaper(QColor(theme.color("code_bg")))
        self.setColor(QColor(theme.color("text")))
        self.setMarginType(0, QsciScintilla.MarginType.NumberMargin)
        self.setMarginLineNumbers(0, True)
        self.setAutoCompletionSource(QsciScintilla.AutoCompletionSource.AcsAll)
        self.setAutoCompletionThreshold(3)
        self.SendScintilla(QsciScintilla.SCI_SETMULTIPLESELECTION, 1)  # Ctrl+click adds a caret
        self.SendScintilla(QsciScintilla.SCI_SETADDITIONALSELECTIONTYPING, 1)
        self.setEolMode(QsciScintilla.EolMode.EolUnix)
        self.SendScintilla(QsciScintilla.SCI_SETSCROLLWIDTHTRACKING, 1)
        # Fast scrolling never waits on the lexer: it styles the whole file in idle time, not when Ctrl+End or a
        # scrollbar drag first shows a far part. The visible page's line layouts are kept between frames.
        self.SendScintilla(QsciScintilla.SCI_SETIDLESTYLING, QsciScintilla.SC_IDLESTYLING_ALL)
        self.SendScintilla(QsciScintilla.SCI_SETLAYOUTCACHE, QsciScintilla.SC_CACHE_PAGE)
        self.read_only = False
        stat = path.stat()
        size, self.mtime = stat.st_size, stat.st_mtime_ns  # to notice a change made outside (a pull, a plugin)
        with path.open("rb") as f:
            data = f.read(MAX_EDIT)
        lexer = _lexer_for(path) if size < MAX_EDIT else None
        if lexer is not None:
            _style_lexer(lexer, BASE_SIZE)
            self.setLexer(lexer)
        else:
            self.setFont(theme.mono_font(BASE_SIZE))
        text, self.encoding_note = _decode(data)
        self.setEolMode(_eol_mode(text))
        # Pasted text takes this file's line endings, so a paste from a Windows program never mixes \r\n into an
        # \n file (or the other way round), which shows as extra blank lines once saved and opened again.
        self.SendScintilla(QsciScintilla.SCI_SETPASTECONVERTENDINGS, 1)
        self.setText(text)
        # Opening a file isn't an edit: Ctrl+Z must not take the whole text away (or redo put it back twice).
        self.SendScintilla(QsciScintilla.SCI_EMPTYUNDOBUFFER)
        if size > MAX_EDIT or self.encoding_note:
            self.setReadOnly(True)
            self.read_only = True
        self.zoom(font_size)
        self.setModified(False)

    def changed_on_disk(self) -> bool:
        try:
            return self.path.stat().st_mtime_ns != self.mtime
        except OSError:
            return False  # deleted: saving puts it back

    def save(self) -> bool:
        """Write the file; False when it is read only (nothing was written)."""
        if self.read_only:
            return False
        write_atomic(self.path, self.text())  # exactly what is shown: the file's own line endings, no translation
        with contextlib.suppress(OSError):
            self.mtime = self.path.stat().st_mtime_ns
        self.setModified(False)
        self.SendScintilla(QsciScintilla.SCI_SETSAVEPOINT)
        return True

    def zoom(self, size: int) -> None:
        """Show the text at ``size`` points: Scintilla's zoom, which scales every style and the margins alike."""
        self.font_size = size
        if self.SendScintilla(QsciScintilla.SCI_GETZOOM) != size - BASE_SIZE:
            self.zoomTo(size - BASE_SIZE)
        self.setMarginWidth(0, "00000")  # measured at the zoomed size, so line numbers are never cut off

    def wheelEvent(self, e: QWheelEvent | None) -> None:
        if e is not None and e.modifiers() & Qt.KeyboardModifier.ControlModifier:  # one notch, one point
            step = 1 if e.angleDelta().y() > 0 else -1 if e.angleDelta().y() < 0 else 0
            self.zoom(max(MIN_SIZE, min(MAX_SIZE, self.font_size + step)))
            e.accept()
            return
        if e is not None:
            super().wheelEvent(e)


def _bad_name(name: str) -> str:
    """Why ``name`` can't be a new file or folder name, or "" if it can."""
    if not name or name in (".", "..") or len(name) > 255:
        return "Give it a name."
    if any(ch in name for ch in '/\\:*?"<>|\0') or name != name.strip() or name.endswith("."):
        return 'A name can\'t contain / \\ : * ? " < > | or end with a dot or space.'
    return ""


# Folders the tree shows but doesn't list: tools fill them with thousands of files (a node_modules can hold
# 100,000), and listing, sorting and watching them all would hold up the window. The file manager still opens them.
NOT_LISTED = frozenset({"node_modules", ".venv", ".venv-container", ".container-cache", ".git", "__pycache__",
                        ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".nox", ".next", ".gradle", "target"})


@cache
def _file_icons() -> FileIcons:
    """One icon provider for the program's whole life. A model only points at its provider, and Python may free a
    tree's attributes before Qt deletes the tree's model (a cycle collected by the garbage collector): a provider
    freed first left the model pointing at nothing, and macOS crashed on the next event."""
    return FileIcons()


class _Files(QFileSystemModel):
    def _unlisted(self, parent: QModelIndex) -> bool:
        return parent.isValid() and self.fileName(parent) in NOT_LISTED and self.isDir(parent)

    def hasChildren(self, parent: QModelIndex = QModelIndex()) -> bool:  # noqa: B008 - Qt's own default
        return False if self._unlisted(parent) else super().hasChildren(parent)

    def canFetchMore(self, parent: QModelIndex) -> bool:
        return False if self._unlisted(parent) else super().canFetchMore(parent)

    def fetchMore(self, parent: QModelIndex) -> None:
        if not self._unlisted(parent):
            super().fetchMore(parent)


class FileTree(QWidget):
    """The service's folder: under the service list, so the editor and console get the full width."""

    open_file = pyqtSignal(Path)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.root = ""
        self.view = QTreeView()
        self.icons = _file_icons()
        self.model = self._new_model()
        self.view.setHeaderHidden(True)
        self.view.setUniformRowHeights(True)  # a folder of 60,000 files: no row is measured on its own
        self.view.setAnimated(True)
        self.view.setIndentation(14)
        self.view.doubleClicked.connect(self._open_index)
        self.view.activated.connect(self._open_index)
        self.title = label("FILES")
        self.title.setProperty("section", True)
        head = QHBoxLayout()
        head.setContentsMargins(10, 6, 4, 2)
        head.setSpacing(2)
        head.addWidget(self.title, 1)
        self.buttons: list[QToolButton] = []
        for name, tip, slot in (("new", "New file", self.new_file), ("folder", "New folder", self.new_folder),
                                ("collapse", "Collapse all", self.view.collapseAll),
                                ("open", "Open the folder in the file manager", self.reveal)):
            b = QToolButton()
            b.setIcon(icon(name))
            b.setToolTip(tip)
            b.setProperty("flat", True)
            b.clicked.connect(lambda _c=False, f=slot: f())
            head.addWidget(b)
            self.buttons.append(b)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(head)
        layout.addWidget(self.view, 1)
        self.set_root("")

    def _new_model(self) -> QFileSystemModel:
        model = _Files(self)
        model.setIconProvider(self.icons)
        model.setFilter(QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot | QDir.Filter.Hidden)
        self.view.setModel(model)
        for col in (1, 2, 3):
            self.view.hideColumn(col)
        return model

    def release(self) -> None:
        """Show nothing, and drop the model with every folder it watches: Windows can't delete a watched one."""
        old, selection = self.model, self.view.selectionModel()
        self.model = self._new_model()
        if selection is not None:
            selection.deleteLater()  # it points at the old model
        old.deleteLater()
        self.set_root("")

    def set_root(self, folder: str, name: str = "") -> None:
        ok = bool(folder) and Path(folder).is_dir()
        self.root = folder if ok else ""
        self.title.setText((name or Path(folder).name).upper() if ok else "FILES")
        for b in self.buttons:
            b.setEnabled(ok)
        if not ok:
            self.view.setRootIndex(self.model.setRootPath(""))
            self.view.setVisible(False)
            return
        self.view.setVisible(True)
        self.view.setRootIndex(self.model.setRootPath(folder))

    def _open_index(self, index: QModelIndex) -> None:
        path = Path(self.model.filePath(index))
        if path.is_file():
            self.open_file.emit(path)

    def _target_folder(self) -> Path | None:
        if not self.root:
            return None
        root = Path(self.root)
        index = self.view.currentIndex()
        if index.isValid():
            picked = Path(self.model.filePath(index))
            folder = picked if picked.is_dir() else picked.parent
            if folder.resolve().is_relative_to(root.resolve()):
                return folder
        return root

    def _ask_name(self, what: str) -> Path | None:
        folder = self._target_folder()
        if folder is None:
            return None
        name, ok = QInputDialog.getText(self, f"New {what}", f"Name of the new {what} in {folder.name}:")
        if not ok:
            return None
        problem = _bad_name(name)
        if problem:
            warn(self, f"New {what}", problem)
            return None
        return folder / name

    def new_file(self) -> None:
        path = self._ask_name("file")
        if path is None:
            return
        try:
            with path.open("x", encoding="utf-8"):
                pass
        except OSError as exc:
            warn(self, "New file", str(exc))
            return
        self.open_file.emit(path)

    def new_folder(self) -> None:
        path = self._ask_name("folder")
        if path is not None:
            try:
                path.mkdir()
            except OSError as exc:
                warn(self, "New folder", str(exc))

    def reveal(self) -> None:
        if self.root:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.root))


class EditorPanel(QWidget):
    """The open files of the service's folder. The tree (``files``) is placed by the window."""

    status = pyqtSignal(str)
    cursor_info = pyqtSignal(str)  # "Ln 3, Col 14" for the status bar

    def __init__(self, font_size: int = 11, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.font_size = max(MIN_SIZE, min(MAX_SIZE, font_size))
        self.files = FileTree()
        self.files.open_file.connect(self.open)
        self.tabs = tabs(movable=True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(lambda _i: self._cursor())
        # No bar of its own: New, Open and Save sit beside the tabs, so the code gets the height. With no file
        # open, the same buttons sit in a slim bar over the hint.
        self.tabs.setObjectName("EditorTabs")
        self.tabs.setCornerWidget(self._file_buttons(), Qt.Corner.TopRightCorner)
        bar = QWidget()
        bar.setObjectName("PanelBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 2, 6, 2)
        row.addWidget(label("Editor", wrap=False, muted=True), 1)
        row.addWidget(self._file_buttons(save=False))
        self.bar = bar
        self.empty = label("Open a file from the tree under the services.\n"
                           "Ctrl+S saves · Ctrl+F finds · Ctrl+H replaces · Ctrl+click adds a caret · Ctrl+ +/− zooms\n"
                           "Shift+wheel scrolls a page · Ctrl+End jumps to the end")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setProperty("muted", True)
        self.findbar = FindBar(self.current, self.status.emit)
        self.findbar.hide()
        self.tabs.currentChanged.connect(lambda _i: self.findbar.refresh() if self.findbar.isVisible() else None)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(bar)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(self.empty, 1)
        layout.addWidget(self.findbar)
        for keys, slot in ((QKeySequence.StandardKey.Save, self.save), (QKeySequence.StandardKey.Find, self.show_find),
                           ("Ctrl+H", lambda: self.findbar.open(replace=True)),
                           ("F3", lambda: self._step(True)), ("Shift+F3", lambda: self._step(False)),
                           ("Ctrl+=", lambda: self.zoom(1)),  # Ctrl++ and Ctrl+- are the editor's own keys
                           ("Ctrl+0", lambda: self.set_font_size(BASE_SIZE)),
                           ("Ctrl+W", lambda: self.close_tab(self.tabs.currentIndex()))):
            QShortcut(QKeySequence(keys), self, slot, context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._sync_empty()

    def set_root(self, folder: str, name: str = "") -> None:
        self.files.set_root(folder, name)

    def open(self, path: Path) -> None:
        for i in range(self.tabs.count()):
            ed = self.tabs.widget(i)
            if isinstance(ed, CodeEditor) and ed.path == path:
                self.tabs.setCurrentIndex(i)
                return
        try:
            with path.open("rb") as f:  # only the start: a log of gigabytes must not be read to check it
                head = f.read(8192)
            if b"\0" in head:
                self.status.emit(f"{path.name} looks like a binary file; not opened")
                return
            editor = CodeEditor(path, self.font_size)
        except OSError as exc:
            warn(self, "Can't open", str(exc))
            return
        editor.modificationChanged.connect(lambda _m, e=editor: self._title(e))
        editor.textChanged.connect(lambda e=editor: self.findbar.text_changed() if e is self.current() else None)
        editor.cursorPositionChanged.connect(lambda _l, _c: self._cursor())
        editor.SCN_ZOOM.connect(lambda e=editor: self._zoomed(e))  # Ctrl+, Ctrl-, Ctrl+wheel in that editor
        index = self.tabs.addTab(editor, icon("file", LANG.get(path.suffix.lower(), TINT["file"])), path.name)
        self.tabs.setCurrentIndex(index)
        close = QToolButton()
        close.setIcon(icon("close"))
        close.setProperty("flat", True)
        close.setToolTip("Close (Ctrl+W)")
        close.clicked.connect(lambda _c=False, e=editor: self.close_tab(self.tabs.indexOf(e)))
        bar = self.tabs.tabBar()
        if bar is not None:
            bar.setTabButton(index, QTabBar.ButtonPosition.RightSide, close)
        self.tabs.setTabToolTip(self.tabs.currentIndex(), str(path))
        self._sync_empty()
        if editor.encoding_note:
            self.status.emit(f"{path.name} {editor.encoding_note}")
        elif editor.read_only:
            self.status.emit(f"{path.name} is over 20 MB: showing its first 20 MB, read only")

    def open_dialog(self) -> None:
        name, _ = QFileDialog.getOpenFileName(self, "Open file", self.files.root or str(Path.home()))
        if name:
            self.open(Path(name))

    def _cursor(self) -> None:
        ed = self.current()
        if ed is None:
            self.cursor_info.emit("")
            return
        line_, col = ed.getCursorPosition()
        self.cursor_info.emit(f"Ln {line_ + 1}, Col {col + 1}")

    def _title(self, editor: CodeEditor) -> None:
        i = self.tabs.indexOf(editor)
        if i >= 0:
            self.tabs.setTabText(i, ("● " if editor.isModified() else "") + editor.path.name)

    def current(self) -> CodeEditor | None:
        w = self.tabs.currentWidget()
        return w if isinstance(w, CodeEditor) else None

    def save(self) -> None:
        ed = self.current()
        if ed is not None:
            self._save(ed)

    def _save(self, ed: CodeEditor) -> bool:
        """Save ``ed``, asking first if the file changed on disk since it was opened. False if it wasn't saved."""
        if ed.read_only:
            self.status.emit(f"{ed.path.name} is read only here: not saved")
            return False
        if ed.changed_on_disk():
            answer = ask(
                self, "Changed on disk", f"{ed.path.name} changed on disk since you opened it (a pull, a plugin, "
                "the service). Save over those changes?",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
            if answer != QMessageBox.StandardButton.Save:
                return False
        try:
            ed.save()
        except OSError as exc:
            warn(self, "Can't save", str(exc))
            return False
        self.status.emit(f"Saved {ed.path.name}")
        return True

    def close_tab(self, index: int) -> None:
        ed = self.tabs.widget(index)
        if isinstance(ed, CodeEditor) and ed.isModified():
            answer = ask(self, "Unsaved changes", f"Save {ed.path.name}?",
                                          QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                                          | QMessageBox.StandardButton.Cancel)
            if answer == QMessageBox.StandardButton.Cancel:
                return
            if answer == QMessageBox.StandardButton.Save and not self._save(ed):
                return  # the edits stay open rather than lost
        self.tabs.removeTab(index)
        if ed is not None:
            ed.deleteLater()
        self._sync_empty()

    def release(self, folder: Path) -> None:
        """Let go of ``folder`` before it is deleted: close its files without asking and stop watching it (on
        Windows a watched or open folder can't be deleted)."""
        try:
            inside = folder.resolve()
        except OSError:
            return
        for i in reversed(range(self.tabs.count())):
            ed = self.tabs.widget(i)
            if isinstance(ed, CodeEditor) and ed.path.resolve().is_relative_to(inside):
                self.tabs.removeTab(i)
                ed.deleteLater()
        if self.files.root and Path(self.files.root).resolve().is_relative_to(inside):
            self.files.release()
        self._sync_empty()

    def unsaved(self) -> list[CodeEditor]:
        return [e for i in range(self.tabs.count()) if isinstance(e := self.tabs.widget(i), CodeEditor)
                and e.isModified()]

    def zoom(self, step: int) -> None:
        self.set_font_size(self.font_size + step)

    def set_font_size(self, size: int) -> None:
        """One size for every open file, kept when the window closes."""
        self.font_size = max(MIN_SIZE, min(MAX_SIZE, size))
        for i in range(self.tabs.count()):
            ed = self.tabs.widget(i)
            if isinstance(ed, CodeEditor) and ed.font_size != self.font_size:
                ed.zoom(self.font_size)

    def _zoomed(self, ed: CodeEditor) -> None:
        size = BASE_SIZE + int(ed.SendScintilla(QsciScintilla.SCI_GETZOOM))
        if size != ed.font_size:  # Scintilla zoomed by itself: follow it
            ed.zoom(max(MIN_SIZE, min(MAX_SIZE, size)))
        if ed.font_size != self.font_size:
            self.set_font_size(ed.font_size)

    def show_find(self) -> None:
        self.findbar.open()

    def _step(self, forward: bool) -> None:
        if not self.findbar.isVisible():
            self.findbar.open()
        self.findbar.step(forward=forward)

    def _file_buttons(self, *, save: bool = True) -> QWidget:
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 4, 0)
        row.setSpacing(2)
        actions = [("add", "New file in the service's folder", self.files.new_file),
                   ("open", "Open a file…", self.open_dialog)]
        if save:
            actions.append(("save", "Save (Ctrl+S)", self.save))
        for name, tip, slot in actions:
            b = QToolButton()
            b.setIcon(icon(name))
            b.setToolTip(tip)
            b.setAutoRaise(True)
            b.setIconSize(QSize(15, 15))
            b.setFixedSize(28, 24)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _c=False, f=slot: f())
            row.addWidget(b)
        return box

    def _sync_empty(self) -> None:
        has = self.tabs.count() > 0
        self.tabs.setVisible(has)
        self.bar.setVisible(not has)
        self.empty.setVisible(not has)
        self._cursor()
