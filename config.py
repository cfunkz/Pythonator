"""Configuration, data models, and shared styles."""
from __future__ import annotations
import json, re, sys
from dataclasses import dataclass, asdict
from pathlib import Path

__version__ = "1.0.0"

# Paths
def _app_dir() -> Path:
    return Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent

APP_DIR = _app_dir()
CONFIG_FILE = APP_DIR / "bots.json"
LOGS_DIR = APP_DIR / "logs"

# Tuning
MAX_LOG_LINES = 50_000
FLUSH_INTERVAL_MS = 100
STATS_INTERVAL_MS = 1000
HISTORY_CHUNK = 5000
KILL_TIMEOUT_MS = 500
MAX_FLUSH_CHARS = 50_000

# ANSI
ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
strip_ansi = lambda t: ANSI_RE.sub('', t)
normalize = lambda t: t.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")

@dataclass
class Bot:
    name: str
    entry: str = ""
    reqs: str = ""
    flags: str = ""
    custom_cmd: bool = False
    python_path: str = ""

def load_config() -> dict[str, Bot]:
    if not CONFIG_FILE.exists(): return {}
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return {n: Bot(**{**{"custom_cmd": False, "python_path": ""}, **c}) for n, c in data.items()}
    except: return {}

def save_config(bots: dict[str, Bot]) -> None:
    try: CONFIG_FILE.write_text(json.dumps({n: asdict(b) for n, b in bots.items()}, indent=2), encoding="utf-8")
    except: pass

# Shared styles
STYLE = """
QToolTip { background: #252525; color: #ddd; border: 1px solid #444; padding: 4px; border-radius: 2px; }
QScrollBar:vertical { background: #1a1a1a; width: 12px; }
QScrollBar::handle:vertical { background: #404040; min-height: 20px; border-radius: 4px; margin: 2px; }
QScrollBar::handle:vertical:hover { background: #505050; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: #1a1a1a; height: 12px; }
QScrollBar::handle:horizontal { background: #404040; min-width: 20px; border-radius: 4px; margin: 2px; }
QScrollBar::handle:horizontal:hover { background: #505050; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
"""
BTN = """QPushButton { padding: 4px 10px; border: 1px solid #333; border-radius: 2px; background: #252525; }
QPushButton:hover { background: #303030; border-color: #444; }
QPushButton:pressed { background: #202020; }
QPushButton:disabled { color: #555; background: #1a1a1a; }"""
INPUT = """QLineEdit, QPlainTextEdit { padding: 4px 8px; border: 1px solid #333; border-radius: 2px; background: #1a1a1a; }
QLineEdit:focus, QPlainTextEdit:focus { border-color: #4688d8; }"""
