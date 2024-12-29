import sys
import argparse

from PyQt6.QtWidgets import QApplication
from pypad import MainWindow

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--debug', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()

    app = QApplication([])
    main_window = MainWindow(debug=args.debug)
    sys.exit(app.exec())

if __name__ == '__main__':
    main()

