import sys
from PyQt6.QtWidgets import QApplication
from pypad import MainWindow

def main():
    app = QApplication([])
    main_window = MainWindow()
    sys.exit(app.exec())

if __name__ == '__main__':
    main()

