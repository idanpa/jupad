import os
import time
import asyncio
import logging
import threading
import traitlets
import IPython
from IPython.terminal.interactiveshell import TerminalInteractiveShell
from watchdog.events import PatternMatchingEventHandler
from watchdog.observers import Observer

@IPython.core.magic.magics_class
class PyPad(IPython.core.magic.Magics):
    debug = traitlets.Bool(False, config=True)

    def __init__(self, ip):
        super(PyPad, self).__init__(ip)
        self.prev_lines = []

        self.logger = logging.getLogger(__name__)
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter('%(relativeCreated)d: %(message)s'))
        self.logger.addHandler(h)
        self.logger.setLevel(logging.DEBUG if self.debug else logging.INFO)

        self.ip:TerminalInteractiveShell = ip
        self.ip.input_transformer_manager.cleanup_transforms = [] # don't ignore indentation
        self.ip.displayhook.write_output_prompt = lambda: None
        self.display_lines = []
        self.register_mime_renderer('text/plain', self.text_mime_renderer)

    @IPython.core.magic.line_magic
    def notepad(self, file_path):
        t = threading.Thread(target=self.run, args=[file_path], daemon=True)
        t.start()

    def on_modified(self, event):
        self.logger.debug('on_modified')
        with self.modified:
            self.modified.notify()

    def run(self, file_path):
        self.file_path = file_path
        self.modified = threading.Condition()
        print(f'Watching: {file_path}')
        self.handler = PatternMatchingEventHandler(patterns=[self.file_path])
        self.handler.on_modified = self.on_modified
        self.observer = Observer(file_path)
        self.observer.schedule(self.handler, os.path.dirname(file_path))
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
            self.logger.error(f'Run error, {logging.traceback.format_exc()}')
        self.observer.stop()
        self.observer.join()

    def text_mime_renderer(self, data, metadata):
        self.display_lines += data.splitlines()

    def register_mime_renderer(self, mime, handler):
        self.ip.display_formatter.active_types.append(mime)
        self.ip.display_formatter.formatters[mime].enabled = True
        self.ip.mime_renderers[mime] = handler

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
            self.logger.error(f'Write file failed, {logging.traceback.format_exc()}')

    def run_cell(self, lines):
        self.logger.debug('>>> ' + '\n... '.join(lines))
        need_write = False
        self.display_lines = []

        coro = self.ip.run_cell_async('\n'.join(lines), store_history=False)
        result_future = asyncio.run_coroutine_threadsafe(coro, self.ip.pt_loop) # what if pt_loop doesn't exist
        result = result_future.result() # TODO: timeout
        error = result.error_in_exec or result.error_before_exec
        if error:
            if isinstance(error, SyntaxError) and error.lineno < len(lines):
                lines[error.lineno - 1] += f' #: ❌ SyntaxError: {error}'.replace('\n',' ')
            else:
                lines[-1] += f' #: ❌ {type(error).__name__}: {error}'.replace('\n',' ')
            need_write = True
        if self.display_lines:
            need_write = True
            if len(self.display_lines) == 1:
                lines[-1] += f' #: {self.display_lines[0]}'
            else:
                lines.extend(['#: ' + l.rstrip() for l in self.display_lines])
        return need_write

    def run_file(self):
        self.logger.debug('run_file')
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

            while lines and lines[0].startswith('#:'):
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
                if cell[-1].startswith('#:'):
                    cell.pop()
                elif self.is_empty(cell[-1]):
                    lines.insert(0, cell.pop())
                else:
                    break
            cell = [l.split('#:')[0] for l in cell]
            if self.run_cell(cell):
                self.write_file(lines_done + cell + lines)
            lines_done += cell
        self.prev_lines = lines_done
        if not skip_unchanged:
            self.write_file(lines_done)

def load_ipython_extension(ip:IPython.InteractiveShell):
    ip.pypad = PyPad(ip)
    ip.register_magics(ip.pypad)
