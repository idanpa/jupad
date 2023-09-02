import os
import sys
import time
import builtins
import argparse
import IPython
from traitlets.config.loader import Config
from watchdog.events import PatternMatchingEventHandler
from watchdog.observers import Observer

class NotepadFileHandler(PatternMatchingEventHandler):
    def __init__(self, observer, file_path):
        super().__init__(patterns=[file_path])
        self.observer = observer
        self.file_path = file_path
        self.prev_lines = []

        sys.argv = [sys.argv[0]]
        config = Config()
        config.TerminalInteractiveShell.simple_prompt = True
        config.TerminalInteractiveShell.term_title = False
        config.TerminalInteractiveShell.xmode = 'Minimal'
        config.HistoryAccessor.enabled = False
        ipapp = IPython.terminal.ipapp.TerminalIPythonApp.instance(config=config)
        ipapp.initialize()
        self.ip = ipapp.shell
        builtins.display = self.display

    def display(self, obj):
        lines = self.ip.display_formatter.formatters['text/plain'](obj).splitlines()
        self.display_lines += ['#: ' + l for l in lines]

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

    def on_modified(self, event):
        print('modified event')
        lines = self.read_file()
        lines_done = []
        # skip unchanged lines:
        while lines and self.prev_lines and lines[0] == self.prev_lines.pop(0):
            lines_done.append(lines.pop(0))
        while lines:
            cell = [lines.pop(0)]
            if cell[0].startswith('#: '):
                continue
            while self.ip.check_complete('\n'.join(cell))[0] == 'incomplete' and lines:
                cell.append(lines.pop(0))
            cell[-1] = cell[-1].split(' #: ')[0]
            print(f'exec: {cell}')
            self.display_lines = []
            result = self.ip.run_cell('\n'.join(cell), store_history=False)
            res = '❌' if result.error_in_exec else result.result
            cell[-1] += f' #: {res}' if res else ''
            cell += self.display_lines
            if res or self.display_lines:
                self.write_file(lines_done + cell + lines)
            lines_done += cell
        self.write_file(lines_done)
        self.prev_lines = lines_done

class NotepadObserver(Observer):
    def __init__(self, file_path, timeout=1):
        super().__init__(timeout=timeout)
        self.handler = NotepadFileHandler(self, file_path)
        self.schedule(self.handler, os.path.dirname(file_path))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('file', help="?")
    parser.add_argument('--debug', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()

    file_path = os.path.abspath(args.file)
    print(f'Watching: {file_path}')
    observer = NotepadObserver(file_path)
    observer.start()
    try:
        while True:
            time.sleep(1)
    except:
        observer.stop()
    observer.join()

if __name__=="__main__":
    main()
