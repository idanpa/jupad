import os
import sys
import time
import signal
import builtins
import argparse
import logging
import threading
from IPython.terminal.interactiveshell import TerminalInteractiveShell
from IPython.terminal.ipapp import TerminalIPythonApp
from traitlets.config.loader import Config
from watchdog.events import PatternMatchingEventHandler
from watchdog.observers import Observer

class PyPad():
    def __init__(self, file_path, debug=False):
        self.file_path = file_path
        self.debug = debug
        self.prev_lines = []

        sys.argv = [sys.argv[0]]
        config = Config()
        config.TerminalInteractiveShell.simple_prompt = True
        config.TerminalInteractiveShell.term_title = False
        config.TerminalInteractiveShell.xmode = 'Minimal'
        config.TerminalIPythonApp.display_banner = False
        config.PlainTextFormatter.max_width = 120
        config.HistoryAccessor.enabled = False
        config.InteractiveShell.cache_size = 0
        ipapp = TerminalIPythonApp.instance(config=config)
        ipapp.initialize()
        self.ip:TerminalInteractiveShell = ipapp.shell
        self.ip.input_transformer_manager.cleanup_transforms = [] # don't ignore indentation
        builtins.display = self.display

        self.modified = threading.Condition()
        print(f'Watching: {file_path}')
        self.handler = PatternMatchingEventHandler(patterns=[self.file_path])
        self.handler.on_modified = self.on_modified
        self.observer = Observer(file_path)
        self.observer.schedule(self.handler, os.path.dirname(file_path))

    def on_modified(self, event):
        logging.debug('on_modified')
        with self.modified:
            self.modified.notify()

    def run(self):
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        self.observer.start()
        try:
            while True:
                self.run_file()
                time.sleep(.5) # for all modified events
                with self.modified:
                    self.modified.wait()
        except KeyboardInterrupt:
            pass
        except Exception as e:
            logging.error(f'Run error, {logging.traceback.format_exc()}')
        self.observer.stop()
        self.observer.join()

    def display(self, obj):
        lines = self.ip.display_formatter.formatters['text/plain'](obj).splitlines()
        self.display_lines += ['#: ' + l.rstrip() for l in lines]

    def check_complete(self, lines):
        return self.ip.check_complete('\n'.join(lines))

    def is_empty(self, line):
        return line.split('#')[0].isspace()

    def read_file(self):
        with open(self.file_path, 'r') as f:
            return f.read().splitlines()

    def write_file(self, lines):
        try:
            with open(self.file_path, 'w') as f:
                f.writelines('\n'.join(lines) + '\n')
        except Exception as e:
            logging.error(f'Write file failed, {logging.traceback.format_exc()}')

    def run_cell(self, lines):
        logging.debug('>>> ' + '\n... '.join(lines))
        need_write = False
        self.display_lines = []

        result = self.ip.run_cell('\n'.join(lines), store_history=False, silent=not self.debug)
        error = result.error_in_exec or result.error_before_exec
        if error:
            if isinstance(error, SyntaxError) and error.lineno < len(lines):
                lines[error.lineno - 1] += f' #: ❌ SyntaxError: {error}'.replace('\n',' ')
            else:
                lines[-1] += f' #: ❌ {type(error).__name__}: {error}'.replace('\n',' ')
            need_write = True
        elif result.result is not None:
            lines[-1] += f' #: {result.result}'.replace('\n',' ')
            need_write = True
        if self.display_lines:
            need_write = True
            lines.extend(self.display_lines)
        return need_write

    def run_file(self):
        lines = self.read_file()
        lines_done = []
        skip_unchanged = True
        while lines:
            cell = [lines.pop(0)]
            while lines:
                status, indent = self.check_complete(cell)
                if status == 'complete':
                    break
                if indent != '':
                    while lines and (lines[0].startswith(indent) or self.is_empty(lines[0])):
                        cell.append(lines.pop(0))
                if self.check_complete(cell + [''])[0] == 'complete':
                    break
                if lines:
                    cell.append(lines.pop(0))

            while lines and lines[0].startswith('#: '):
                cell.append(lines.pop(0))
            if skip_unchanged:
                for line in cell:
                    if len(self.prev_lines)==0 or line != self.prev_lines.pop(0):
                        skip_unchanged = False
                        break
                else:
                    lines_done += cell
                    continue
            while len(cell) > 1:
                if cell[-1].startswith('#: '):
                    cell.pop()
                elif self.is_empty(cell[-1]):
                    lines.insert(0, cell.pop())
                else:
                    break
            cell = [l.split(' #: ')[0] for l in cell]
            if self.run_cell(cell):
                self.write_file(lines_done + cell + lines)
            lines_done += cell
        self.prev_lines = lines_done
        if not skip_unchanged:
            self.write_file(lines_done)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('file', help="python file to watch")
    parser.add_argument('--debug', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()

    logging.basicConfig(format='%(relativeCreated)d: %(message)s', level=logging.DEBUG if args.debug else logging.INFO)

    file_path = os.path.abspath(args.file)
    pypad = PyPad(file_path, args.debug)
    pypad.run()

if __name__=="__main__":
    main()
