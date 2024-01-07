import os
import time
import logging
import asyncio
import traitlets
import IPython
from watchdog.events import PatternMatchingEventHandler
from .utils import logger, PausableObserver
from IPython.core.async_helpers import get_asyncio_loop

@IPython.core.magic.magics_class
class PyPad(IPython.core.magic.Magics):
    debug = traitlets.Bool(False, config=True)

    def __init__(self, ip:IPython.terminal.interactiveshell.TerminalInteractiveShell):
        super(PyPad, self).__init__(ip)
        logger.setLevel(logging.DEBUG if self.debug else logging.INFO)

        self.ip = ip
        self.ip.enable_gui('asyncio')
        self.ip.input_transformer_manager.cleanup_transforms = [] # don't ignore indentation
        self.hijack_display = False
        write_output_prompt = self.ip.displayhook.write_output_prompt
        def write_output_prompt_if_not_hijacked():
            if not self.hijack_display:
                write_output_prompt()
        self.ip.displayhook.write_output_prompt = write_output_prompt_if_not_hijacked
        self.prev_lines = []
        self.display_lines = []
        self.register_mime_renderer('text/plain', self.text_mime_renderer)

    @IPython.core.magic.line_magic
    def notepad(self, file_path):
        print(f'Watching: {file_path}')
        self.file_path = os.path.abspath(file_path)
        self.handler = PatternMatchingEventHandler(patterns=[self.file_path])
        self.handler.on_modified = self.on_modified
        self.observer = PausableObserver(self.file_path)
        self.observer.schedule(self.handler, os.path.dirname(self.file_path))
        self.observer.start()

    def on_modified(self, event):
        with self.observer.pause():
            logger.debug('on_modified')
            self.run_file()
            time.sleep(.2) # let last write propegate

    def text_mime_renderer(self, data, metadata):
        if self.hijack_display:
            self.display_lines += data.splitlines()
        else:
            print(data)

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
            self.file_content = f.read()
            return self.file_content.splitlines()

    def write_file(self, lines):
        try:
            with open(self.file_path, 'r+') as f:
                if self.file_content != f.read():
                    raise ValueError('File has been changed')
                self.file_content = '\n'.join(lines) + '\n'
                f.truncate(0)
                f.seek(0)
                f.write(self.file_content)
        except Exception as e:
            logger.error(f'Write file failed, {logging.traceback.format_exc()}')

    def run_cell(self, lines):
        logger.debug('>>> ' + '\n... '.join(lines))
        need_write = False

        self.display_lines = []
        self.hijack_display = True
        coro = self.ip.run_cell_async('\n'.join(lines), store_history=False)
        result_future = asyncio.run_coroutine_threadsafe(coro, get_asyncio_loop())
        result = result_future.result() # TODO: timeout
        self.hijack_display = False

        error = result.error_in_exec or result.error_before_exec
        if error:
            need_write = True
            if isinstance(error, SyntaxError) and error.lineno < len(lines):
                lines[error.lineno - 1] += f' #: ❌ SyntaxError: {error}'.replace('\n',' ')
            else:
                lines[-1] += f' #: ❌ {type(error).__name__}: {error}'.replace('\n',' ')
        if self.display_lines:
            need_write = True
            if len(self.display_lines) == 1 and len(lines) == 1:
                lines[-1] += f' #: {self.display_lines[0]}'
            else:
                lines.extend(['#: ' + l.rstrip() for l in self.display_lines])
        return need_write

    def pop_cell(self, lines):
        cell = [lines.pop(0)]
        # if marked by cell prefix, take the entire cell:
        if cell[0].startswith('# %%'):
            while lines and not lines[0].startswith('# %%'):
                cell.append(lines.pop(0))
            return cell
        # otherwise, take the minimal amount of lines that make sense:
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
        # add previous output to cell:
        while lines and lines[0].startswith('#:'):
            cell.append(lines.pop(0))
        return cell

    def run_file(self):
        lines = self.read_file()
        lines_done = []
        skip_unchanged = True
        while lines:
            cell = self.pop_cell(lines)
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
            cell = [l.split('#:')[0].rstrip() for l in cell]
            if self.run_cell(cell):
                self.write_file(lines_done + cell + lines)
            lines_done += cell
        self.prev_lines = lines_done
        if not skip_unchanged:
            self.write_file(lines_done)

def load_ipython_extension(ip:IPython.InteractiveShell):
    ip.pypad = PyPad(ip)
    ip.register_magics(ip.pypad)
