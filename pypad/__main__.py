import os
import sys
import time
import builtins
import argparse
import logging
import IPython
from traitlets.config.loader import Config
from watchdog.events import PatternMatchingEventHandler
from watchdog.observers import Observer

class NotepadFileHandler(PatternMatchingEventHandler):
    def __init__(self, observer, file_path, debug=False):
        super().__init__(patterns=[file_path])
        self.observer = observer
        self.file_path = file_path
        self.debug = debug
        self.prev_lines = []

        sys.argv = [sys.argv[0]]
        config = Config()
        config.TerminalInteractiveShell.simple_prompt = True
        config.TerminalInteractiveShell.term_title = False
        config.TerminalInteractiveShell.xmode = 'Minimal'
        config.PlainTextFormatter.max_width = 120
        config.HistoryAccessor.enabled = False
        config.InteractiveShell.cache_size = 0
        ipapp = IPython.terminal.ipapp.TerminalIPythonApp.instance(config=config)
        ipapp.initialize()
        self.ip = ipapp.shell
        builtins.display = self.display

    def display(self, obj):
        lines = self.ip.display_formatter.formatters['text/plain'](obj).splitlines()
        self.display_lines += ['#: ' + l.rstrip() for l in lines]

    def check_incomplete(self, lines):
        return self.ip.check_complete('\n'.join(lines))[0] == 'incomplete'

    def read_file(self):
        with open(self.file_path, 'r') as f:
            return f.read().splitlines()

    def write_file(self, lines):
        try:
            with open(self.file_path, 'w') as f:
                f.writelines('\n'.join(lines) + '\n')
            self.observer.event_queue.get(timeout=1)
        except Exception as e:
            print(repr(e))

    def run_cell(self, lines):
        logging.debug('>>> ' + '\n... '.join(lines))
        result = self.ip.run_cell('\n'.join(lines), store_history=False, silent=not self.debug)
        if result.error_in_exec:
            return f'❌ {type(result.error_in_exec).__name__}: {result.error_in_exec}'.replace('\n',' ')
        if result.result is not None:
            return str(result.result).replace('\n',' ')
        return None

    def on_modified(self, event):
        logging.debug(f'modified event')
        lines = self.read_file()
        lines_done = []
        skip_unchanged = True
        while lines:
            cell = [lines.pop(0)]
            while lines and (self.check_incomplete(cell) or lines[0].startswith('#: ')):
                cell.append(lines.pop(0))
            if skip_unchanged:
                for line in cell:
                    if len(self.prev_lines)==0 or line != self.prev_lines.pop(0):
                        skip_unchanged = False
                        break
                else:
                    lines_done += cell
                    continue
            cell = [l.split(' #: ')[0] for l in cell if not l.startswith('#: ')]
            self.display_lines = []
            result = self.run_cell(cell)
            if result:
                cell[-1] += f' #: {result}'
            cell += self.display_lines
            if result or self.display_lines:
                self.write_file(lines_done + cell + lines)
            lines_done += cell
        self.write_file(lines_done)
        self.prev_lines = lines_done

class NotepadObserver(Observer):
    def __init__(self, file_path, debug=False, timeout=1):
        super().__init__(timeout=timeout)
        self.handler = NotepadFileHandler(self, file_path, debug)
        self.schedule(self.handler, os.path.dirname(file_path))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('file', help="python file to watch")
    parser.add_argument('--debug', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()

    logging.basicConfig(format='%(message)s', level=logging.DEBUG if args.debug else logging.INFO)

    file_path = os.path.abspath(args.file)
    print(f'Watching: {file_path}')
    observer = NotepadObserver(file_path, args.debug)
    observer.start()
    try:
        while True:
            time.sleep(1)
    except:
        observer.stop()
    observer.join()

if __name__=="__main__":
    main()
