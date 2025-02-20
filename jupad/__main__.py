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
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--debug', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--kernel', type=str, default='python3', help='kernel name to use (`jupyter kernelspec list` to see available kernels)')
    parser.add_argument('file', nargs='?', default=os.path.expanduser(os.path.join('~','.jupad','jupad.py')), help='script file to open')
    args = parser.parse_args()

    app = QApplication([])
    if os.name == 'nt':
        app.setStyle('windows11')

    base_path = os.path.abspath(os.path.dirname(__file__))
    icon_path = os.path.join(base_path, 'resources', 'icon.svg')
    app.icon = QIcon(icon_path)
    app.setWindowIcon(app.icon)

    if args.kernel != 'python3':
        from jupyter_client.kernelspec import KernelSpecManager
        kernels = KernelSpecManager().find_kernel_specs()
        if args.kernel not in kernels:
            print(f'No such kernel: {args.kernel}, available kernels: {", ".join(kernels)}')
            sys.exit(1)

    main_window = MainWindow(file_path=args.file, kernel_name=args.kernel, debug=args.debug)
    sys.exit(app.exec())

if __name__ == '__main__':
    main()

