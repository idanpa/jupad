import os
import time
import logging
import asyncio
import yaml
import shutil
import traitlets
import IPython
from prompt_toolkit.styles import Style, merge_styles
from watchdog.events import PatternMatchingEventHandler, FileModifiedEvent
from IPython.core.async_helpers import get_asyncio_loop
from contextlib import contextmanager

from .utils import *

@IPython.core.magic.magics_class
class PyPad(IPython.core.magic.Magics):
    debug = traitlets.Bool(False, config=True)
    timeout = traitlets.Int(30, allow_none=True, config=True)

    def __init__(self, ip:IPython.terminal.interactiveshell.TerminalInteractiveShell):
        super(PyPad, self).__init__(ip)
        logger.setLevel(logging.DEBUG if self.debug else logging.INFO)

        self.ip = ip
        self.ip.enable_gui('asyncio') # to run cells on the event loop
        self.loop = get_asyncio_loop() # avoid race in the creation
        self.ip.input_transformer_manager.cleanup_transforms = [] # don't ignore indentation
        self.prev_lines = []
        self.display_lines = []
        self.ip.display_formatter.active_types.append('text/plain')
        self.ip.display_formatter.formatters['text/plain'].enabled = True
        self.ip.mime_renderers['text/plain'] = self.text_mime_renderer_print
        self.ip.pt_app.style = merge_styles([self.ip.pt_app.style,
            Style([('bottom-toolbar', 'noreverse')])])

    def bottom_toolbar(self, status):
        n_col, n_row = shutil.get_terminal_size()
        file_path = self.file_path
        if len(file_path) > n_col-len(status)-5:
            file_path = '...' + file_path[-(n_col-len(status)-8):]
        self.ip.pt_app.bottom_toolbar = status.ljust(n_col-len(file_path)-1) + file_path
        self.ip.pt_app.app.invalidate()

    @IPython.core.magic.line_magic
    def notepad(self, file_path):
        self.file_path = os.path.abspath(file_path)
        self.handler = PatternMatchingEventHandler(patterns=[self.file_path])
        self.handler.on_modified = self.on_modified
        self.observer = PausableObserver(self.file_path)
        self.observer.schedule(self.handler, os.path.dirname(self.file_path))
        list(self.observer.emitters)[0].queue_event(FileModifiedEvent(self.file_path))
        self.observer.start()
        self.bottom_toolbar('watching')

    def on_modified(self, event):
        with self.observer.pause():
            while True:
                try:
                    with self.run_context():
                        self.run_file()
                    break
                except FileRemodifiedError:
                    pass
                except Exception:
                    logger.error(f'Run file failed, {logging.traceback.format_exc()}')
            time.sleep(.2) # let last write propegate

    def text_mime_renderer_to_display_lines(self, data, metadata):
        self.display_lines += data.splitlines()

    def text_mime_renderer_print(self, data, metadata):
        print(data)

    @contextmanager
    def run_context(self):
        def nop(*args, **kwargs):
            pass
        write_output_prompt = self.ip.displayhook.write_output_prompt
        showtraceback = self.ip.showtraceback
        showsyntaxerror = self.ip.showsyntaxerror
        showindentationerror = self.ip.showindentationerror
        showtraceback = self.ip.showtraceback
        try:
            self.bottom_toolbar('running')
            self.ip.mime_renderers['text/plain'] = self.text_mime_renderer_to_display_lines
            self.ip.displayhook.write_output_prompt = nop
            self.ip.showsyntaxerror = nop
            self.ip.showindentationerror = nop
            self.ip.showtraceback = nop
            yield
        finally:
            self.ip.mime_renderers['text/plain'] = self.text_mime_renderer_print
            self.ip.displayhook.write_output_prompt = write_output_prompt
            self.ip.showsyntaxerror = showsyntaxerror
            self.ip.showindentationerror = showindentationerror
            self.ip.showtraceback = showtraceback
            self.bottom_toolbar('watching')

    def check_complete(self, lines):
        return self.ip.check_complete('\n'.join(lines))

    def is_empty(self, line):
        return line.split('#')[0].isspace()

    def read_file(self):
        with open(self.file_path, 'r') as f:
            self.file_content = f.read()
            return self.file_content.splitlines()

    def write_file(self, lines):
        with open(self.file_path, 'r+') as f:
            curr_file_content = f.read()
            if self.file_content != curr_file_content:
                logger.debug('File remodified')
                raise FileRemodifiedError()
            self.file_content = '\n'.join(lines) + '\n'
            if self.file_content != curr_file_content:
                f.truncate(0)
                f.seek(0)
                f.write(self.file_content)

    def parse_meta(self, meta, line):
        try:
            d = yaml.safe_load('{'+line+'}')
        except yaml.YAMLError:
            return
        if not isinstance(d, dict):
            return
        # ignore unknown keys (yaml interpert any string as key)
        for k in ['timeout', 'cache', 'figure']:
            if k in d:
                meta[k] = d[k]

    def dump_meta(self, meta):
        return yaml.safe_dump(meta).replace('\n',', ').strip('{}, ')

    def run_cell(self, lines, meta):
        logger.debug('>>> ' + '\n... '.join(lines))
        self.display_lines = []
        self.assignments = AssignmentsGetter()
        # self.ip.ast_transformers.append(self.assignments)

        if 'cache' in meta and meta['cache'] and True: # TODO and file exists
            cache_file_name = None
            if cache_file_name is None:
                cache_file_name = 'TODO.pkl'

        coro = self.ip.run_cell_async('\n'.join(lines), store_history=False)
        result_future = asyncio.run_coroutine_threadsafe(coro, get_asyncio_loop())
        try:
            result = result_future.result() # TODO: timeout
        finally:
            pass
            # self.ip.ast_transformers.remove(self.assignments)

        error = result.error_in_exec or result.error_before_exec
        if error:
            if isinstance(error, SyntaxError) and error.lineno < len(lines):
                lines[error.lineno - 1] += f' #: ❌ SyntaxError: {error}'.replace('\n',' ')
            else:
                lines[-1] += f' #: ❌ {type(error).__name__}: {error}'.replace('\n',' ')
        if meta:
            if lines[0].startswith('# %%'):
                lines[0] = f'# %% {self.dump_meta(meta)}'
            else:
                lines[0] += f' #: {self.dump_meta(meta)}'
        if self.display_lines:
            if lines[0].startswith('# %%') or meta or len(self.display_lines) > 1 or len(lines) > 1:
                lines.extend(['#: ' + l.rstrip() for l in self.display_lines])
            else:
                lines[-1] += f' #: {self.display_lines[0]}'

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
        logger.debug('run_file')
        lines_tbd = self.read_file()
        lines_done = []
        skip_unchanged = True
        while lines_tbd:
            cell = self.pop_cell(lines_tbd)
            if skip_unchanged:
                for line in cell:
                    if len(self.prev_lines)==0 or line != self.prev_lines.pop(0):
                        skip_unchanged = False
                        break
                else:
                    lines_done += cell
                    continue
            # remove results from cells we are about to run:
            while len(cell) > 1:
                if cell[-1].startswith('#:'):
                    cell.pop()
                elif self.is_empty(cell[-1]):
                    lines_tbd.insert(0, cell.pop())
                else:
                    break
            meta = {}
            for i in range(len(cell)):
                sp = cell[i].split('#:')
                self.parse_meta(meta, ''.join(sp[1:]))
                cell[i] = sp[0].rstrip()
            if cell[0].startswith('# %%'):
                self.parse_meta(meta, cell[0].removeprefix('# %%'))
            self.run_cell(cell, meta)
            self.write_file(lines_done + cell + lines_tbd)
            lines_done += cell
        self.prev_lines = lines_done
        if not skip_unchanged:
            self.write_file(lines_done)

def load_ipython_extension(ip:IPython.InteractiveShell):
    ip.pypad = PyPad(ip)
    ip.register_magics(ip.pypad)
