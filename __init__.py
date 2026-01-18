"""
Pythonator - PyQt6 GUI for running isolated Python scripts.

Features:
- Multiple bot management with tabbed interface
- Custom Python path support (venv auto-detect or explicit)
- Live log streaming with ANSI color support
- CPU/RAM monitoring per process tree
- Built-in Python code editor with syntax highlighting
- Strong process isolation for concurrent execution
- Auto-update from GitHub releases
"""
from app import main
from config import Bot, load_config, save_config

__version__ = "0.2.2"
__all__ = ["main", "Bot", "load_config", "save_config", "__version__"]
