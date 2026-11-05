"""The code editor: QScintilla (Scintilla's lexers, folding, multi-cursor, fast with big files) and a file tree."""

from __future__ import annotations

import contextlib
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
from PyQt6.QtCore import QDir, QModelIndex, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QFileSystemModel, QKeySequence, QShortcut, QWheelEvent
from PyQt6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QMessageBox,
    QPushButton,
    QTabBar,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from ..core.store import write_atomic
from . import theme
from .icons import LANG, TINT, FileIcons, icon
from .widgets import label, line, tabs

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
        size = path.stat().st_size
        with path.open("rb") as f:
            data = f.read(MAX_EDIT)
        lexer = _lexer_for(path) if size < MAX_EDIT else None
        if lexer is not None:
            _style_lexer(lexer, BASE_SIZE)
            self.setLexer(lexer)
        else:
            self.setFont(theme.mono_font(BASE_SIZE))
        if b"\r\n" in data[:65536]:
            self.setEolMode(QsciScintilla.EolMode.EolWindows)
        self.setText(data.decode("utf-8", "replace"))
        if size > MAX_EDIT:
            self.setReadOnly(True)
            self.read_only = True
        self.zoom(font_size)
        self.setModified(False)

    def save(self) -> None:
        if self.read_only:
            return
        write_atomic(self.path, self.text())
        self.setModified(False)

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


class FindBar(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.find_edit = line("", "Find")
        self.replace_edit = line("", "Replace with")
        self.case = QCheckBox("Aa")
        self.case.setToolTip("Match case")
        self.regex = QCheckBox(".*")
        self.regex.setToolTip("Regular expression")
        self.next = QPushButton("Next")
        self.one = QPushButton("Replace")
        self.all = QPushButton("All")
        close = QPushButton("×")
        close.clicked.connect(self.hide)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        for w in (self.find_edit, self.replace_edit, self.case, self.regex, self.next, self.one, self.all, close):
            layout.addWidget(w)


def _bad_name(name: str) -> str:
    """Why ``name`` can't be a new file or folder name, or "" if it can."""
    if not name or name in (".", "..") or len(name) > 255:
        return "Give it a name."
    if any(ch in name for ch in '/\\:*?"<>|\0') or name != name.strip() or name.endswith("."):
        return 'A name can\'t contain / \\ : * ? " < > | or end with a dot or space.'
    return ""


class FileTree(QWidget):
    """The service's folder: under the service list, so the editor and console get the full width."""

    open_file = pyqtSignal(Path)
    status = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.root = ""
        self.model = QFileSystemModel(self)
        self.model.setIconProvider(FileIcons())
        self.model.setFilter(QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot | QDir.Filter.Hidden)
        self.view = QTreeView()
        self.view.setModel(self.model)
        for col in (1, 2, 3):
            self.view.hideColumn(col)
        self.view.setHeaderHidden(True)
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
            QMessageBox.warning(self, f"New {what}", problem)
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
            QMessageBox.warning(self, "New file", str(exc))
            return
        self.open_file.emit(path)

    def new_folder(self) -> None:
        path = self._ask_name("folder")
        if path is not None:
            try:
                path.mkdir()
            except OSError as exc:
                QMessageBox.warning(self, "New folder", str(exc))

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
        bar = QWidget()  # the same bar as the console's: a title and the file buttons
        bar.setObjectName("PanelBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 4, 6, 4)
        row.setSpacing(3)
        self.title = label("Editor", wrap=False, muted=True)
        row.addWidget(self.title, 1)
        self.save_button = QToolButton()
        for text, tip, slot in (("New", "New file in the service's folder", self.files.new_file),
                                ("Open…", "Open a file", self.open_dialog), ("Save", "Save (Ctrl+S)", self.save)):
            b = self.save_button if text == "Save" else QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _c=False, f=slot: f())
            row.addWidget(b)
        self.empty = label("Open a file from the tree under the services.\n"
                           "Ctrl+S saves · Ctrl+F finds · Ctrl+click adds a caret · Ctrl+ +/− zooms\n"
                           "Shift+wheel scrolls a page · Ctrl+End jumps to the end")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setProperty("muted", True)
        self.findbar = FindBar()
        self.findbar.hide()
        self.findbar.find_edit.returnPressed.connect(self._find_next)
        self.findbar.next.clicked.connect(self._find_next)
        self.findbar.one.clicked.connect(self._replace_one)
        self.findbar.all.clicked.connect(self._replace_all)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(bar)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(self.empty, 1)
        layout.addWidget(self.findbar)
        for keys, slot in ((QKeySequence.StandardKey.Save, self.save), (QKeySequence.StandardKey.Find, self.show_find),
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
            if b"\0" in path.read_bytes()[:8192]:
                self.status.emit(f"{path.name} looks like a binary file; not opened")
                return
            editor = CodeEditor(path, self.font_size)
        except OSError as exc:
            QMessageBox.warning(self, "Can't open", str(exc))
            return
        editor.modificationChanged.connect(lambda _m, e=editor: self._title(e))
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
        if editor.read_only:
            self.status.emit(f"{path.name} is over 20 MB: opened read only")

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
        if ed is None:
            return
        try:
            ed.save()
            self.status.emit(f"Saved {ed.path.name}")
        except OSError as exc:
            QMessageBox.warning(self, "Can't save", str(exc))

    def close_tab(self, index: int) -> None:
        ed = self.tabs.widget(index)
        if isinstance(ed, CodeEditor) and ed.isModified():
            answer = QMessageBox.question(self, "Unsaved changes", f"Save {ed.path.name}?",
                                          QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                                          | QMessageBox.StandardButton.Cancel)
            if answer == QMessageBox.StandardButton.Cancel:
                return
            if answer == QMessageBox.StandardButton.Save:
                with contextlib.suppress(OSError):
                    ed.save()
        self.tabs.removeTab(index)
        if ed is not None:
            ed.deleteLater()
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
        self.findbar.show()
        ed = self.current()
        if ed is not None and ed.hasSelectedText():
            self.findbar.find_edit.setText(ed.selectedText())
        self.findbar.find_edit.setFocus()
        self.findbar.find_edit.selectAll()

    def _find_next(self) -> bool:
        ed = self.current()
        if ed is None or not self.findbar.find_edit.text():
            return False
        bar = self.findbar
        found = ed.findFirst(bar.find_edit.text(), bar.regex.isChecked(), bar.case.isChecked(), False, True)
        if not found:
            self.status.emit("Not found")
        return bool(found)

    def _replace_one(self) -> None:
        ed = self.current()
        if ed is not None and ed.hasSelectedText():
            ed.replace(self.findbar.replace_edit.text())
        self._find_next()

    def _replace_all(self) -> None:
        ed = self.current()
        if ed is None or not self.findbar.find_edit.text():
            return
        count = 0
        ed.beginUndoAction()
        ed.setCursorPosition(0, 0)
        while ed.findFirst(self.findbar.find_edit.text(), self.findbar.regex.isChecked(), self.findbar.case.isChecked(),
                           False, False) and count < 100_000:
            ed.replace(self.findbar.replace_edit.text())
            count += 1
        ed.endUndoAction()
        self.status.emit(f"Replaced {count}")

    def _sync_empty(self) -> None:
        has = self.tabs.count() > 0
        self.tabs.setVisible(has)
        self.empty.setVisible(not has)
        self.save_button.setEnabled(has)
        self._cursor()
