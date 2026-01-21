"""Pythonator - Python Bot Runner."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtGui import QIcon, QColor, QPalette
from PyQt6.QtWidgets import QApplication, QStyleFactory
from config import STYLE
from main_window import MainWindow

def dark_palette() -> QPalette:
    pal = QPalette()
    dark, darker, light, mid, accent = QColor(24,24,24), QColor(18,18,18), QColor(220,220,220), QColor(35,35,35), QColor(70,130,220)
    for role, color in [(QPalette.ColorRole.Window, dark), (QPalette.ColorRole.WindowText, light),
        (QPalette.ColorRole.Base, darker), (QPalette.ColorRole.AlternateBase, dark), (QPalette.ColorRole.Text, light),
        (QPalette.ColorRole.Button, mid), (QPalette.ColorRole.ButtonText, light),
        (QPalette.ColorRole.Highlight, accent), (QPalette.ColorRole.HighlightedText, QColor(255,255,255)),
        (QPalette.ColorRole.ToolTipBase, dark), (QPalette.ColorRole.ToolTipText, light),
        (QPalette.ColorRole.Link, accent), (QPalette.ColorRole.LinkVisited, QColor(180,130,220)),
        (QPalette.ColorRole.PlaceholderText, QColor(128,128,128))]:
        pal.setColor(role, color)
    return pal

def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle(QStyleFactory.create("Fusion"))
    app.setPalette(dark_palette())
    font = app.font(); font.setPointSize(10); app.setFont(font)
    app.setStyleSheet(STYLE)
    try: app.setWindowIcon(QIcon("icon.ico"))
    except: pass
    window = MainWindow()
    try: window.setWindowIcon(QIcon("icon.ico"))
    except: pass
    window.show()
    return app.exec()

if __name__ == "__main__":
    sys.exit(main())
