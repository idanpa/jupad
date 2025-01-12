import os
import sys
import argparse

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QIcon
from pypad import MainWindow

if os.name == 'nt':
    # for taskbar icon
    try:
        from ctypes import windll
        windll.shell32.SetCurrentProcessExplicitAppUserModelID(u'pypad.pypad')
    except AttributeError:
        pass

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--debug', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()

    app = QApplication([])

    base_path = os.path.abspath(os.path.dirname(__file__))
    icon_path = os.path.join(base_path, 'resources', 'icon.svg')
    app.icon = QIcon(icon_path)
    app.setWindowIcon(app.icon)

    main_window = MainWindow(debug=args.debug)
    sys.exit(app.exec())

if __name__ == '__main__':
    main()

