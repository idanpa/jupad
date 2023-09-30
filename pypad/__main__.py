import sys
import argparse
import IPython
from traitlets.config.loader import Config

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('file', help="python file to watch")
    parser.add_argument('--debug', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()

    config = Config()
    config.PyPad.debug = args.debug
    config.TerminalInteractiveShell.term_title = False
    config.TerminalIPythonApp.display_banner = False
    config.InteractiveShellApp.extra_extensions = ['pypad']
    config.InteractiveShellApp.exec_lines = [f'%notepad {args.file}']
    sys.argv = [sys.argv[0]]
    IPython.start_ipython(config=config)

if __name__=="__main__":
    main()
