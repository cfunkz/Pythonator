"""Main Window - Bot configuration and log viewer."""
from __future__ import annotations
import sys
from pathlib import Path
from typing import Optional
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QTabWidget, QVBoxLayout, QWidget, QProxyStyle, QStyle)
from config import Bot, load_config, save_config, FLUSH_INTERVAL_MS, STATS_INTERVAL_MS, BTN, INPUT, __version__
from log_buffer import LogBuffer
from log_view import LogView
from process_mgr import ProcessManager
from stats import ProcessStats, StatsMonitor
from editor import EditorWindow

class NoSeamStyle(QProxyStyle):
    def drawPrimitive(self, el, opt, painter, widget=None):
        if el in (QStyle.PrimitiveElement.PE_FrameTabWidget, QStyle.PrimitiveElement.PE_FrameTabBarBase): return
        super().drawPrimitive(el, opt, painter, widget)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Pythonator v{__version__}")
        self.resize(1200, 750)
        self.bots = load_config()
        self.buffers: dict[str, LogBuffer] = {}
        self.views: dict[str, LogView] = {}
        self._editors: dict[str, EditorWindow] = {}
        self._scratch: Optional[EditorWindow] = None
        self.proc_mgr = ProcessManager(on_output=self._on_output, on_finished=self._on_finished)
        self.stats = StatsMonitor()
        self._syncing = False
        self._build_ui()
        self._load_bots()
        self._flush_timer = QTimer(self); self._flush_timer.timeout.connect(self._flush); self._flush_timer.start(FLUSH_INTERVAL_MS)
        self._stats_timer = QTimer(self); self._stats_timer.timeout.connect(self._update_stats); self._stats_timer.start(STATS_INTERVAL_MS)

    def _build_ui(self) -> None:
        root = QWidget(); self.setCentralWidget(root)
        layout = QVBoxLayout(root); layout.setContentsMargins(8, 8, 8, 8); layout.setSpacing(8)
        
        # Top bar
        top = QHBoxLayout(); top.setSpacing(8)
        self.status_label = QLabel("Ready"); self.status_label.setStyleSheet("color: #888; font-family: monospace;")
        top.addStretch(); top.addWidget(self.status_label)
        self.btn_start_all = QPushButton("▶ Start All"); self.btn_start_all.setStyleSheet(BTN); self.btn_start_all.clicked.connect(self._start_all)
        self.btn_stop_all = QPushButton("■ Stop All"); self.btn_stop_all.setStyleSheet(BTN); self.btn_stop_all.clicked.connect(self._stop_all)
        top.addWidget(self.btn_start_all); top.addWidget(self.btn_stop_all)
        layout.addLayout(top)

        # Splitter
        splitter = QSplitter(Qt.Orientation.Horizontal); splitter.setHandleWidth(9)
        splitter.setStyleSheet("QSplitter::handle:horizontal:hover { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0.45 transparent,stop:0.5 #3a3a3a,stop:0.55 transparent); }")
        splitter.addWidget(self._build_config()); splitter.addWidget(self._build_logs())
        splitter.setStretchFactor(0, 1); splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

        # Shortcuts
        for seq, fn in [("Ctrl+N", self._add_bot), ("Ctrl+S", self._start_current), ("Ctrl+R", self._restart_current), ("Ctrl+Q", self.close)]:
            QShortcut(QKeySequence(seq), self, fn)

    def _build_config(self) -> QWidget:
        panel = QWidget(); layout = QVBoxLayout(panel); layout.setContentsMargins(0, 0, 8, 0); layout.setSpacing(8)
        
        # Bot selector
        row = QHBoxLayout(); row.setSpacing(4)
        self.bot_combo = QComboBox()
        self.bot_combo.setStyleSheet("QComboBox { padding: 4px 8px; border: 1px solid #333; border-radius: 2px; background: #1a1a1a; } QComboBox:hover { border-color: #444; } QComboBox::drop-down { border: none; width: 20px; } QComboBox QAbstractItemView { background: #1a1a1a; border: 1px solid #333; selection-background-color: #4688d8; }")
        self.bot_combo.currentTextChanged.connect(self._on_combo_changed); row.addWidget(self.bot_combo, 1)
        btn_add = QPushButton("+"); btn_add.setFixedWidth(28); btn_add.setStyleSheet(BTN); btn_add.clicked.connect(self._add_bot); row.addWidget(btn_add)
        self.btn_del = QPushButton("×"); self.btn_del.setFixedWidth(28); self.btn_del.setStyleSheet(BTN); self.btn_del.clicked.connect(self._del_bot); row.addWidget(self.btn_del)
        layout.addLayout(row)

        # Entry
        layout.addWidget(QLabel("Entry:"))
        entry_row = QHBoxLayout(); entry_row.setSpacing(4)
        self.entry_input = QLineEdit(); self.entry_input.setPlaceholderText("Path to Python script"); self.entry_input.setStyleSheet(INPUT); self.entry_input.textChanged.connect(self._save_bot)
        entry_row.addWidget(self.entry_input, 1)
        self.btn_edit = QPushButton("✎"); self.btn_edit.setFixedWidth(28); self.btn_edit.setStyleSheet(BTN); self.btn_edit.clicked.connect(self._edit_entry); entry_row.addWidget(self.btn_edit)
        btn_browse = QPushButton("…"); btn_browse.setFixedWidth(28); btn_browse.setStyleSheet(BTN); btn_browse.clicked.connect(self._browse_entry); entry_row.addWidget(btn_browse)
        layout.addLayout(entry_row)

        # Python path
        layout.addWidget(QLabel("Python:"))
        py_row = QHBoxLayout(); py_row.setSpacing(4)
        self.python_input = QLineEdit(); self.python_input.setPlaceholderText("Auto-detect from venv"); self.python_input.setStyleSheet(INPUT); self.python_input.textChanged.connect(self._save_bot)
        py_row.addWidget(self.python_input, 1)
        btn_detect = QPushButton("⌕"); btn_detect.setFixedWidth(28); btn_detect.setStyleSheet(BTN); btn_detect.clicked.connect(self._detect_python); py_row.addWidget(btn_detect)
        btn_browse_py = QPushButton("…"); btn_browse_py.setFixedWidth(28); btn_browse_py.setStyleSheet(BTN); btn_browse_py.clicked.connect(self._browse_python); py_row.addWidget(btn_browse_py)
        layout.addLayout(py_row)

        # Custom command mode
        self.custom_check = QCheckBox("Custom command mode"); self.custom_check.stateChanged.connect(self._on_custom_toggle); layout.addWidget(self.custom_check)

        # Flags
        self.flags_label = QLabel("Flags:"); layout.addWidget(self.flags_label)
        self.flags_input = QLineEdit(); self.flags_input.setPlaceholderText("Arguments"); self.flags_input.setStyleSheet(INPUT); self.flags_input.textChanged.connect(self._save_bot); layout.addWidget(self.flags_input)

        # Requirements
        layout.addWidget(QLabel("Requirements:"))
        self.reqs_input = QPlainTextEdit(); self.reqs_input.setPlaceholderText("One package per line"); self.reqs_input.setStyleSheet(INPUT); self.reqs_input.textChanged.connect(self._save_bot); layout.addWidget(self.reqs_input, 1)

        # Venv buttons
        venv_row = QHBoxLayout(); venv_row.setSpacing(4)
        btn_venv = QPushButton("Setup venv"); btn_venv.setStyleSheet(BTN); btn_venv.clicked.connect(self._setup_venv); venv_row.addWidget(btn_venv)
        btn_deps = QPushButton("Install deps"); btn_deps.setStyleSheet(BTN); btn_deps.clicked.connect(self._install_deps); venv_row.addWidget(btn_deps)
        layout.addLayout(venv_row)

        # Editor button
        btn_editor = QPushButton("Open Editor"); btn_editor.setStyleSheet(BTN); btn_editor.clicked.connect(self._open_scratch); layout.addWidget(btn_editor)

        # Auto-restart
        self.auto_restart = QCheckBox("Auto-restart on crash"); layout.addWidget(self.auto_restart)
        return panel

    def _build_logs(self) -> QWidget:
        panel = QWidget(); layout = QVBoxLayout(panel); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(8)
        
        ctrl = QHBoxLayout(); ctrl.setSpacing(4)
        self.btn_start = QPushButton("▶ Start"); self.btn_start.setStyleSheet(BTN); self.btn_start.clicked.connect(self._start_current)
        self.btn_stop = QPushButton("■ Stop"); self.btn_stop.setStyleSheet(BTN); self.btn_stop.clicked.connect(self._stop_current)
        self.btn_restart = QPushButton("↻ Restart"); self.btn_restart.setStyleSheet(BTN); self.btn_restart.clicked.connect(self._restart_current)
        ctrl.addWidget(self.btn_start); ctrl.addWidget(self.btn_stop); ctrl.addWidget(self.btn_restart); ctrl.addStretch()
        layout.addLayout(ctrl)

        self.tabs = QTabWidget(); self.tabs.setStyle(NoSeamStyle(self.tabs.style())); self.tabs.setMovable(True); self.tabs.setDocumentMode(True)
        self.tabs.setStyleSheet("QTabWidget::pane { border: 1px solid #2a2a2a; border-top: none; border-radius: 0 0 2px 2px; } QTabBar::tab { background: #1a1a1a; border: 1px solid #2a2a2a; border-bottom: none; padding: 6px 16px; margin-right: 2px; border-radius: 2px 2px 0 0; } QTabBar::tab:selected { background: #252525; border-color: #252525; } QTabBar::tab:hover:!selected { background: #202020; }")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        layout.addWidget(self.tabs, 1)
        return panel

    def _load_bots(self) -> None:
        for name in self.bots: self._create_views(name)
        if self.bots:
            first = next(iter(self.bots)); self.bot_combo.setCurrentText(first); self._load_bot_ui(first)
        self._update_ui()

    def _create_views(self, name: str) -> None:
        if name not in self.buffers: self.buffers[name] = LogBuffer(name)
        if name not in self.views: self.views[name] = LogView(name, self.buffers[name]); self.tabs.addTab(self.views[name], name)
        if self.bot_combo.findText(name) < 0: self.bot_combo.addItem(name)

    def _load_bot_ui(self, name: str) -> None:
        bot = self.bots.get(name)
        if not bot: return
        for w in (self.entry_input, self.flags_input, self.reqs_input, self.python_input, self.custom_check): w.blockSignals(True)
        self.entry_input.setText(bot.entry); self.flags_input.setText(bot.flags); self.reqs_input.setPlainText(bot.reqs)
        self.python_input.setText(bot.python_path); self.custom_check.setChecked(bot.custom_cmd)
        is_custom = bot.custom_cmd
        self.flags_label.setText("Command:" if is_custom else "Flags:")
        self.flags_input.setPlaceholderText("e.g., uvicorn main:app" if is_custom else "Arguments")
        self.entry_input.setEnabled(not is_custom); self.btn_edit.setEnabled(not is_custom)
        for w in (self.entry_input, self.flags_input, self.reqs_input, self.python_input, self.custom_check): w.blockSignals(False)

    def _save_bot(self) -> None:
        name = self.bot_combo.currentText()
        if not name: return
        self.bots[name] = Bot(name=name, entry=self.entry_input.text().strip(), reqs=self.reqs_input.toPlainText().strip(),
                              flags=self.flags_input.text().strip(), custom_cmd=self.custom_check.isChecked(), python_path=self.python_input.text().strip())
        save_config(self.bots)

    def _add_bot(self) -> None:
        name, ok = QInputDialog.getText(self, "New Workspace", "Workspace name:")
        if not ok or not name.strip(): return
        name = name.strip()
        if name in self.bots: QMessageBox.warning(self, "Duplicate", f"Workspace '{name}' exists"); return
        self.bots[name] = Bot(name=name); save_config(self.bots)
        self._create_views(name); self.bot_combo.setCurrentText(name); self._load_bot_ui(name); self._update_ui()

    def _del_bot(self) -> None:
        name = self.bot_combo.currentText()
        if not name: return
        if self.proc_mgr.is_running(name): QMessageBox.warning(self, "Running", "Stop the script first"); return
        if QMessageBox.question(self, "Confirm", f"Delete '{name}'?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes: return
        del self.bots[name]; save_config(self.bots)
        if name in self.views: idx = self.tabs.indexOf(self.views[name]); (self.tabs.removeTab(idx) if idx >= 0 else None); del self.views[name]
        self.buffers.pop(name, None); self._editors.pop(name, None)
        idx = self.bot_combo.findText(name); (self.bot_combo.removeItem(idx) if idx >= 0 else None); self._update_ui()

    def _browse_entry(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select Script", "", "Python (*.py);;All (*)")
        if path: self.entry_input.setText(path)

    def _browse_python(self) -> None:
        filt = "Executable (*.exe);;All (*)" if sys.platform == "win32" else "All (*)"
        path, _ = QFileDialog.getOpenFileName(self, "Select Python", "", filt)
        if path: self.python_input.setText(path)

    def _detect_python(self) -> None:
        entry = self.entry_input.text().strip()
        if not entry: QMessageBox.warning(self, "No entry", "Set an entry script first."); return
        vpy = Path(entry).resolve().parent / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        if vpy.exists(): self.python_input.setText(str(vpy))
        else: QMessageBox.information(self, "Not found", "No .venv found. Create venv first or select Python manually.")

    def _edit_entry(self) -> None:
        name = self.bot_combo.currentText()
        if not name: return
        entry = self.entry_input.text().strip()
        if not entry: QMessageBox.warning(self, "No Entry", "Set an entry path first"); return
        if name in self._editors and self._editors[name].isVisible(): self._editors[name].raise_(); self._editors[name].activateWindow()
        else: self._editors[name] = EditorWindow(); self._editors[name].set_file(entry); self._editors[name].show()

    def _open_scratch(self) -> None:
        if self._scratch and self._scratch.isVisible(): self._scratch.raise_(); self._scratch.activateWindow()
        else: self._scratch = EditorWindow(); self._scratch.show()

    def _on_custom_toggle(self, _) -> None:
        is_custom = self.custom_check.isChecked()
        self.flags_label.setText("Command:" if is_custom else "Flags:")
        self.flags_input.setPlaceholderText("e.g., uvicorn main:app" if is_custom else "Arguments")
        self.entry_input.setEnabled(not is_custom); self.btn_edit.setEnabled(not is_custom); self._save_bot()

    def _on_tab_changed(self, index: int) -> None:
        if self._syncing or index < 0: return
        self._syncing = True
        widget = self.tabs.widget(index)
        for name, view in self.views.items():
            if view is widget: self.bot_combo.blockSignals(True); self.bot_combo.setCurrentText(name); self.bot_combo.blockSignals(False); self._load_bot_ui(name); break
        self._syncing = False; self._update_ui()

    def _on_combo_changed(self, name: str) -> None:
        if self._syncing or not name: return
        self._syncing = True; self._load_bot_ui(name)
        if name in self.views: self.tabs.blockSignals(True); self.tabs.setCurrentWidget(self.views[name]); self.tabs.blockSignals(False)
        self._syncing = False; self._update_ui()

    def _start_current(self) -> None:
        self._save_bot(); name = self.bot_combo.currentText()
        if name and name in self.bots: self.proc_mgr.start(self.bots[name])
        self._update_ui()

    def _stop_current(self) -> None:
        name = self.bot_combo.currentText()
        if name: self.proc_mgr.stop(name)
        self._update_ui()

    def _restart_current(self) -> None:
        self._save_bot(); name = self.bot_combo.currentText()
        if not name: return
        if self.proc_mgr.is_running(name): self.proc_mgr.stop(name); QTimer.singleShot(600, lambda: self._start_by_name(name))
        else: self._start_by_name(name)

    def _start_by_name(self, name: str) -> None:
        if name in self.bots: self.proc_mgr.start(self.bots[name])
        self._update_ui()

    def _start_all(self) -> None:
        self._save_bot()
        for name, bot in self.bots.items():
            if not self.proc_mgr.is_running(name): self.proc_mgr.start(bot)
        self._update_ui()

    def _stop_all(self) -> None: self.proc_mgr.stop_all(); self._update_ui()

    def _setup_venv(self) -> None: self._save_bot(); name = self.bot_combo.currentText(); (self.proc_mgr.setup_venv(self.bots[name]) if name and name in self.bots else None)
    def _install_deps(self) -> None: self._save_bot(); name = self.bot_combo.currentText(); (self.proc_mgr.install_deps(self.bots[name]) if name and name in self.bots else None)

    def _on_output(self, name: str, text: str) -> None:
        if name not in self.buffers: return
        disp, _ = self.buffers[name].append(text)
        if disp and name in self.views: self.views[name].append(disp)

    def _on_finished(self, name: str, code: int, should_restart: bool) -> None:
        pid = self.proc_mgr.get_pid(name); (self.stats.clear(pid) if pid else None)
        if name in self.views: self.views[name].update_stats(ProcessStats())
        if should_restart and self.auto_restart.isChecked() and code != 0:
            self._on_output(name, "\x1b[33m[runner] Restarting...\x1b[0m\n")
            QTimer.singleShot(500, lambda: self._start_by_name(name))
        self._update_ui()

    def _flush(self) -> None:
        for view in self.views.values(): view.flush()
        self._update_ui()

    def _update_stats(self) -> None:
        running = self.proc_mgr.running
        for name, view in self.views.items(): view.update_stats(self.stats.get_stats(self.proc_mgr.get_pid(name)) if name in running else ProcessStats())

    def _update_ui(self) -> None:
        name = self.bot_combo.currentText(); running = self.proc_mgr.is_running(name) if name else False
        n_running, n_total = len(self.proc_mgr.running), len(self.bots)
        self.status_label.setText(f"{n_running}/{n_total} running" if n_total else "Ready")
        self.btn_start.setEnabled(bool(name) and not running); self.btn_stop.setEnabled(running); self.btn_restart.setEnabled(bool(name))
        self.btn_del.setEnabled(bool(name) and not running); self.btn_start_all.setEnabled(n_running < n_total); self.btn_stop_all.setEnabled(n_running > 0)

    def closeEvent(self, event) -> None:
        self.proc_mgr.stop_all()
        for e in self._editors.values(): e.close()
        if self._scratch: self._scratch.close()
        event.accept()
