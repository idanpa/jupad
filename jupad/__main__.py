import os
import sys
import argparse

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QIcon
from jupad import MainWindow

if os.name == 'nt':
    # for taskbar icon
    try:
        from ctypes import windll
        windll.shell32.SetCurrentProcessExplicitAppUserModelID(u'jupad.jupad')
    except AttributeError:
        pass

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--debug', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('file', nargs='?', default=os.path.expanduser(os.path.join('~','.jupad','jupad.py')))
    args = parser.parse_args()

    app = QApplication([])
    if os.name == 'nt':
        app.setStyle('windows11')

    base_path = os.path.abspath(os.path.dirname(__file__))
    icon_path = os.path.join(base_path, 'resources', 'icon.svg')
    app.icon = QIcon(icon_path)
    app.setWindowIcon(app.icon)

    main_window = MainWindow(file_path=args.file, debug=args.debug)
    sys.exit(app.exec())

if __name__ == '__main__':
    main()

