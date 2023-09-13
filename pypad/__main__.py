import os
import argparse

from . import PyPad

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('file', help="python file to watch")
    parser.add_argument('--debug', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()

    file_path = os.path.abspath(args.file)
    pypad = PyPad(file_path, args.debug)
    pypad.run()

if __name__=="__main__":
    main()
