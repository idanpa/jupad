import os
import sys
import io
import logging
from base64 import b64decode
from contextlib import contextmanager

from PyQt6.QtWidgets import QApplication, QMainWindow, QTextEdit, QFrame
from PyQt6.QtCore import (Qt, QObject, QRect, QMimeData, QEvent, QUrl,
                          QTimer, QRunnable, QThreadPool, pyqtSlot, pyqtSignal)
from PyQt6.QtGui import (QFont, QFontMetrics, QFontDatabase, QImage,
    QPainter, QColor, QKeyEvent, QResizeEvent, QCloseEvent,
    QTextCursor, QTextLength, QTextCharFormat, QTextFrameFormat, QTextBlockFormat,
    QTextDocument, QTextImageFormat, QTextTableCell, QTextTableFormat, QTextTableCellFormat)

from qtconsole.pygments_highlighter import PygmentsHighlighter
from qtconsole.base_frontend_mixin import BaseFrontendMixin
from qtconsole.manager import QtKernelManager
from qtconsole.completion_widget import CompletionWidget

from IPython.core.inputtransformer2 import TransformerManager
from IPython.lib.latextools import latex_to_png

from ansi2html import Ansi2HTMLConverter

light_theme = {
    'code_background': QColor('#ffffff'),
    'out_background': QColor('#fcfcfc'),
    'separater_color': QColor('#f8f8f8'),
    'done_color': QColor('#d4f4d4'),
    'pending_color': QColor('#fcfcfc'),
    'executing_color': QColor('#f5ca6e'),
    'error_color': QColor('#f4bdbd'),
    'inactive_color': QColor('#ffffff'),
    'active_color': QColor('#f4f4f4'),
    'splash_color': QColor('#a0a0a0'),
}
theme = light_theme

class LatexWorkerSignals(QObject):
    # separate class as you must be QObject to have signals
    result = pyqtSignal(int, str, bytes)

class LatexWorker(QRunnable):
    def __init__(self, cell_idx, latex):
        super().__init__()
        self.cell_idx = cell_idx
        self.latex = latex
        self.signals = LatexWorkerSignals()

    @pyqtSlot()
    def run(self):
        latex = self.latex.replace('$\\displaystyle', '$')
        try:
            image_data = latex_to_png(latex, wrap=False, backend='matplotlib')
            if image_data is None:
                image_data = latex_to_png(latex, wrap=False, backend='dvipng')
            if image_data:
                self.signals.result.emit(self.cell_idx, self.latex, image_data)
        except Exception as e:
            print(f'latex error: {e}')

class Highlighter(PygmentsHighlighter):
    def highlightBlock(self, string):
        # don't highlight output cells
        cursor = QTextCursor(self.currentBlock())
        table = cursor.currentTable()
        if table and table.cellAt(cursor).column() != 0:
                return
        return super().highlightBlock(string)

class CompletionWidget_(CompletionWidget):
    def _complete_current(self):
        super()._complete_current()
        self._text_edit.execute(self._text_edit.complete_cell_idx)

class PyPadTextEdit(QTextEdit, BaseFrontendMixin):
    def __init__(self, parent):
        super().__init__(parent)

        self.recalculate_columns_timer = QTimer()
        self.recalculate_columns_timer.setSingleShot(True)
        self.recalculate_columns_timer.setInterval(500)
        self.recalculate_columns_timer.timeout.connect(self.recalculate_columns)

        self.save_timer = QTimer()
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(5000)
        self.save_timer.timeout.connect(self.save_file)

        # so ctrl+z won't undo initialization:
        self.setUndoRedoEnabled(False)

        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setPixelSize(16)
        self.setFont(font)
        font_metrics = QFontMetrics(font)
        self.char_width = font_metrics.width(' ')
        self.char_height = font_metrics.height()
        self.setTabStopDistance(4 * self.char_width)

        self.highlighter = Highlighter(self)
        self.highlighter.set_style('vs')

        self.setFrameStyle(QFrame.Shape.NoFrame)

        cursor = self.textCursor()
        table_format = QTextTableFormat()
        table_format.setBorder(0)
        table_format.setPadding(-3)
        table_format.setMargin(0)
        table_format.setWidth(QTextLength(QTextLength.PercentageLength, 100))
        table_format.setColumnWidthConstraints([
            QTextLength(QTextLength.PercentageLength, 40),
            QTextLength(QTextLength.PercentageLength, 60)])
        self.table = cursor.insertTable(1, 2, table_format)

        self.execute_msg_id = ''
        self.in_undo_redo = False
        # last execution count of each cell
        self.execution_count = [None]
        # if cell has an image
        self.has_image = [False]
        # latex code of cell
        self.latex = ['']
        self.setTextCursor(self.code_cell(0).firstCursorPosition())

        self.document().begin().setVisible(False) # https://stackoverflow.com/questions/76061158

        self.cursorPositionChanged.connect(self.position_changed)

        kernel_manager = QtKernelManager(kernel_name='python3')
        kernel_manager.start_kernel()

        kernel_client = kernel_manager.client()
        kernel_client.start_channels()

        self.kernel_manager = kernel_manager
        self.kernel_client = kernel_client

        self.splash_visible = False
        self.kernel_info = ''
        kernel_client.kernel_info()

        self.execute_running = False
        self.execute_cell_idx = -1

        self.html_converter = Ansi2HTMLConverter()

        self._control = self # for CompletionWidget
        self.completion_widget = CompletionWidget_(self, 0)

        self.log = logging.getLogger('pypad')
        self.log.setLevel(logging.DEBUG)
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(logging.DEBUG)
        self.log.addHandler(handler)

        self.divider_drag = False
        self.setMouseTracking(True)

        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)

        self.transformer_manager = TransformerManager()

        self.edit_block_cursor = self.textCursor()

        self.thread_pool = QThreadPool()

    def set_splash(self):
        if self.splash_visible:
            return
        self.splash_visible = True

        cell_format = QTextTableCellFormat()
        cell_format.setBorder(0)
        self.code_cell(0).setFormat(cell_format)
        self.out_cell(0).setFormat(cell_format)

        cursor = self.table.lastCursorPosition()
        cursor.movePosition(QTextCursor.Right)
        block_format = QTextBlockFormat()
        block_format.setAlignment(Qt.AlignCenter)
        cursor.setBlockFormat(block_format)
        format = QTextCharFormat()
        format.setForeground(theme['splash_color'])
        cursor.insertText('\n\npypad - Python Notepad\n\n'
                          + self.kernel_info + '\n\n'
                          'Restart Kernel [Ctrl]+[R]\n', format)

    def hide_splash(self):
        cursor = self.table.lastCursorPosition()
        cursor.movePosition(QTextCursor.Right)
        cursor.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)
        cursor.removeSelectedText()
        self.splash_visible = False

    def is_complete(self, code):
        return self.transformer_manager.check_complete(code)

    def code_cell(self, cell_idx):
        cell = self.table.cellAt(cell_idx, 0)
        assert cell.isValid()
        return cell

    def out_cell(self, cell_idx):
        cell = self.table.cellAt(cell_idx, 1)
        assert cell.isValid()
        return cell

    def insert_cell(self, cell_idx):
        '''insert cell before the given index'''
        self.table.insertRows(cell_idx, 1)
        self.execution_count.insert(cell_idx, None)
        self.has_image.insert(cell_idx, False)
        self.latex.insert(cell_idx, '')
        self.set_cell_pending(cell_idx)
        self.setTextCursor(self.code_cell(cell_idx).firstCursorPosition())

    def remove_cells(self, cell_idx, count):
        if self.execute_running: # stop execution
            self.execute_msg_id = ''
            self.kernel_manager.interrupt_kernel()

        self.table.removeRows(cell_idx, count)
        self.execution_count[cell_idx:cell_idx+count] = []
        self.has_image[cell_idx:cell_idx+count] = []
        self.latex[cell_idx:cell_idx+count] = []

    def get_cell_code(self, cell_idx):
        cell = self.code_cell(cell_idx)
        cursor = cell.firstCursorPosition()
        cursor.setPosition(cell.lastCursorPosition().position(), QTextCursor.KeepAnchor)
        return cursor.selection().toPlainText()

    def get_cell_out(self, cell_idx):
        cell = self.out_cell(cell_idx)
        cursor = cell.firstCursorPosition()
        cursor.setPosition(cell.lastCursorPosition().position(), QTextCursor.KeepAnchor)
        return cursor.selection().toPlainText()

    @pyqtSlot()
    def position_changed(self):
        if self.in_undo_redo:
            return # don't corrupt undo stack
        # fix selection to be within one column in table
        cursor = self.textCursor()
        if cursor.hasSelection():
            if cursor.position() < self.table.firstCursorPosition().position():
                cell = self.table.cellAt(cursor.anchor())
                if cell.isValid():
                    cursor.setPosition(self.table.cellAt(0, cell.column()).firstCursorPosition().position(), QTextCursor.KeepAnchor)
            if cursor.position() > self.table.lastCursorPosition().position():
                cell = self.table.cellAt(cursor.anchor())
                if cell.isValid():
                    cursor.setPosition(self.table.cellAt(self.table.rows()-1, cell.column()).lastCursorPosition().position(), QTextCursor.KeepAnchor)
        elif cursor.position() < self.table.firstCursorPosition().position():
            cursor.setPosition(self.table.firstCursorPosition().position())
        elif cursor.position() > self.table.lastCursorPosition().position():
            cursor.setPosition(self.code_cell(self.table.rows()-1).lastCursorPosition().position())
        self.setTextCursor(cursor)

        if self.table.rows() == 1:
            if self.get_cell_code(0) == '':
                self.set_splash()
                return
        if self.splash_visible:
            self.hide_splash()

        mrow, mrow_num, mcol, mcol_num = cursor.selectedTableCells()
        cell = self.table.cellAt(cursor)
        assert cell.isValid()
        if mcol_num > 1 or mrow_num > 1 or cell.column() == 1:
            cell_idx = -1
        else:
            cell_idx = cell.row()

        for i in range(self.table.rows()):
            self.set_cell_active(i, i == cell_idx)

    def pos_in_cell(self, cell_idx, cursor):
        cell = self.table.cellAt(cursor)
        if cell.isValid() and cell.row() == cell_idx:
            return cursor.position() - cell.firstCursorPosition().position()
        return -1

    @contextmanager
    def edit_block(self):
        self.edit_block_cursor.setPosition(self.textCursor().position())
        self.edit_block_cursor.beginEditBlock()
        try:
            yield
        finally:
            self.edit_block_cursor.endEditBlock()

    @contextmanager
    def join_edit_block(self):
        self.edit_block_cursor.setPosition(self.textCursor().position())
        self.edit_block_cursor.joinPreviousEditBlock()
        try:
            yield
        finally:
            self.edit_block_cursor.endEditBlock()

    def set_cell_text(self, cell_idx, txt):
        with self.join_edit_block():
            cell = self.out_cell(cell_idx)
            cursor = cell.firstCursorPosition()
            cursor.setPosition(cell.lastCursorPosition().position(), QTextCursor.KeepAnchor)
            cursor.insertText(txt)
            self.has_image[self.execute_cell_idx] = False

    def set_cell_img(self, cell_idx, img, format, name):
        with self.join_edit_block():
            try:
                # name should be unique to allow undo/redo
                cell = self.out_cell(cell_idx)
                cursor = cell.firstCursorPosition()
                cursor.setPosition(cell.lastCursorPosition().position(), QTextCursor.KeepAnchor)

                image = QImage()
                image.loadFromData(img, format.upper())
                self.document().addResource(QTextDocument.ImageResource, QUrl(name), image)
                image_format = QTextImageFormat()
                image_format.setName(name)
                image_format.setMaximumWidth(self.table.format().columnWidthConstraints()[1])
                cursor.insertImage(image_format)
                self.has_image[self.execute_cell_idx] = True
            except Exception as e:
                self.log.error(f'set image error: {e}')

    @staticmethod
    def _out_cell_format(color):
        cell_format = QTextTableCellFormat()
        cell_format.setLeftBorder(3)
        cell_format.setLeftBorderStyle(QTextTableFormat.BorderStyle_Solid)
        cell_format.setLeftBorderBrush(color)
        cell_format.setLeftPadding(4)

        cell_format.setBottomBorder(1)
        cell_format.setBottomBorderStyle(QTextTableFormat.BorderStyle_Solid)
        cell_format.setBottomBorderBrush(theme['separater_color'])

        return cell_format

    @staticmethod
    def _code_cell_format(active):
        cell_format = QTextTableCellFormat()
        cell_format.setLeftBorder(3)
        cell_format.setLeftBorderStyle(QTextTableFormat.BorderStyle_Solid)
        cell_format.setLeftBorderBrush(theme['active_color'] if active else theme['inactive_color'])

        cell_format.setBottomBorder(1)
        cell_format.setBottomBorderStyle(QTextTableFormat.BorderStyle_Solid)
        cell_format.setBottomBorderBrush(theme['separater_color'])

        return cell_format

    def set_cell_active(self, cell_idx, active):
        with self.join_edit_block():
            self.code_cell(cell_idx).setFormat(self._code_cell_format(active))

    def set_cell_done(self, cell_idx):
        with self.join_edit_block():
            self.out_cell(cell_idx).setFormat(self._out_cell_format(theme['done_color']))

    def set_cell_pending(self, cell_idx):
        with self.join_edit_block():
            self.out_cell(cell_idx).setFormat(self._out_cell_format(theme['pending_color']))

    def set_cell_executing(self, cell_idx):
        with self.join_edit_block():
            self.out_cell(cell_idx).setFormat(self._out_cell_format(theme['executing_color']))
            self.set_cell_text(cell_idx, '')

    def set_cell_error(self, cell_idx, txt, tooltip=None):
        with self.join_edit_block():
            cell = self.out_cell(cell_idx)
            cell_format = self._out_cell_format(theme['error_color'])
            cell_format.setToolTip(tooltip)
            cell.setFormat(cell_format)
            self.set_cell_text(cell_idx, txt) # after setting tooltip

    def restart_kernel(self):
        self.kernel_manager.restart_kernel()
        self.execute(0)

    def _execute(self, cell_idx, code=None):
        if code is None:
            code = self.get_cell_code(cell_idx)
        if cell_idx == 0 and code == '' and self.table.rows() == 1:
            self.set_cell_text(cell_idx, '')
            return # keep splash clean
        self.set_cell_executing(cell_idx)
        # set '_', '__', '___' to hold the previous cells output:
        prep_code = ''
        for i, var_name in ((cell_idx-1, '_'), (cell_idx-2, '__'), (cell_idx-3, '___')):
            if i >= 0 and self.execution_count[i] is not None:
                prep_code += f'{var_name} = Out.get({self.execution_count[i]}, None)\n'
        self.kernel_client.execute(prep_code, silent=True, stop_on_error=False)
        # don't stop on error, we interrupt kernel and execute a new cell immediately after, otherwise might get aborted
        self.execute_msg_id = self.kernel_client.execute(code, stop_on_error=False)
        self.log.debug(f'execute [{cell_idx}] ({self.execute_msg_id.split("_")[-1]}): {code}')

    def execute(self, cell_idx, code=None):
        if self.execute_running:
            if self.execute_cell_idx < cell_idx:
                return # eventually we will execute this cell
            else:
                self.log.debug('interrupt kernel: new code')
                self.kernel_manager.interrupt_kernel()
        self.execute_running = True
        self.execute_cell_idx = cell_idx
        self._execute(cell_idx, code)
        for i in range(cell_idx+1, self.table.rows()):
            self.set_cell_pending(i)

    @pyqtSlot(int, str, bytes)
    def set_cell_latex_img(self, cell_idx, latex, img):
        if self.latex[cell_idx] == latex:
            self.set_cell_img(cell_idx, img, 'PNG', latex)

    def _handle_execute_result(self, msg):
        msg_id = msg['parent_header']['msg_id']
        self.log.debug(f'execute_result ({msg_id.split("_")[-1]})')
        if msg_id != self.execute_msg_id:
            return
        self._handle_execute_result_or_display_data(msg['content'], msg_id)

    def _handle_display_data(self, msg):
        msg_id = msg['parent_header']['msg_id']
        self.log.debug(f'display_data ({msg_id.split("_")[-1]})')
        if msg_id != self.execute_msg_id:
            return
        self._handle_execute_result_or_display_data(msg['content'], msg_id)

    def _handle_execute_result_or_display_data(self, content, msg_id):
        data = content['data']
        if 'image/png' in data:
            image_data = b64decode(data['image/png'].encode('ascii'))
            self.set_cell_img(self.execute_cell_idx, image_data, 'PNG', msg_id)
        elif 'image/jpeg' in data:
            image_data = b64decode(data['image/jpeg'].encode('ascii'))
            self.set_cell_img(self.execute_cell_idx, image_data, 'JPG', msg_id)
        elif 'text/plain' in data:
            if not self.has_image[self.execute_cell_idx]:
                self.set_cell_text(self.execute_cell_idx, data['text/plain'])
        else:
            self.log.error(f'unsupported type {data}')

        if 'text/latex' in data:
            self.latex[self.execute_cell_idx] = data['text/latex']
            latex_worker = LatexWorker(self.execute_cell_idx, data['text/latex'])
            latex_worker.signals.result.connect(self.set_cell_latex_img)
            self.thread_pool.start(latex_worker)
        else:
            self.latex[self.execute_cell_idx] = ''

    def _handle_error(self, msg):
        msg_id = msg['parent_header']['msg_id']
        content = msg['content']
        ename = content['ename']
        self.log.debug(f'error ({msg_id.split("_")[-1]}): {ename}')
        if msg_id != self.execute_msg_id:
            return
        self.set_cell_error(self.execute_cell_idx, ename, self.html_converter.convert(''.join(content['traceback'])))

    def _handle_execute_reply(self, msg):
        msg_id = msg['parent_header']['msg_id']
        content = msg['content']
        status = content['status']
        self.log.debug(f'execute_reply ({msg_id.split("_")[-1]}): {status}')
        if msg_id != self.execute_msg_id:
            return
        self.execution_count[self.execute_cell_idx] = content['execution_count']
        if status == 'ok':
            self.set_cell_done(self.execute_cell_idx)
        else:
            self.latex[self.execute_cell_idx] = ''
        if self.execute_cell_idx+1 < self.table.rows():
            self.execute_cell_idx = self.execute_cell_idx+1
            self._execute(self.execute_cell_idx)
        else:
            self.execute_running = False

    def _handle_complete_reply(self, msg):
        # code from qtconsole:
        msg_id = msg['parent_header']['msg_id']
        self.log.debug(f'complete_reply ({msg_id.split("_")[-1]})')
        cursor = self.textCursor()
        if  (msg_id == self.complete_msg_id and
             self.pos_in_cell(self.complete_cell_idx, cursor) == self.complete_pos_in_cell and
             self.get_cell_code(self.complete_cell_idx) == self.complete_code):

            content = msg['content']
            matches = content['matches']
            start = content['cursor_start']
            end = content['cursor_end']

            start = max(start, 0)
            end = max(end, start)

            # Move the control's cursor to the desired end point
            cursor_pos_in_cell = self.complete_pos_in_cell
            if end < cursor_pos_in_cell:
                cursor.movePosition(QTextCursor.Left, n=(cursor_pos_in_cell - end))
            elif end > cursor_pos_in_cell:
                cursor.movePosition(QTextCursor.Right, n=(end - cursor_pos_in_cell))
            self.setTextCursor(cursor)
            offset = end - start
            # Move the local cursor object to the start of the match and complete
            cursor.movePosition(QTextCursor.Left, n=offset)
            self.completion_widget.cancel_completion()

            if len(matches) == 1:
                cursor.setPosition(self.textCursor().position(), QTextCursor.KeepAnchor)
                cursor.insertText(matches[0])

            elif len(matches) > 1:
                current_pos = self.textCursor().position()
                prefix = os.path.commonprefix(matches)
                if prefix:
                    cursor.setPosition(current_pos, QTextCursor.KeepAnchor)
                    cursor.insertText(prefix)
                    current_pos = cursor.position()
                self.completion_widget.show_items(cursor, matches, prefix_length=len(prefix))

    def _handle_kernel_info_reply(self, msg):
        self.log.debug(f'kernel_info_reply')
        language_info = msg['content']['language_info']
        self.kernel_info = language_info['name'] + ' ' + language_info['version']
        self.set_splash()

        self.open_file()
        self.load_file()

        self.setUndoRedoEnabled(True)
        self.parent().show()

    def _handle_clear_output(self, msg):
        self.log.debug(f'clear_output')

    def _handle_exec_callback(self, msg):
        self.log.debug(f'exec_callback')

    def _handle_input_request(self, msg):
        self.log.debug(f'input_request')

    def _handle_inspect_reply(self, rep):
        self.log.debug(f'inspect_reply')

    def _handle_shutdown_reply(self, msg):
        self.log.debug(f'shutdown_reply')

    def _handle_status(self, msg):
        return

    def _handle_stream(self, msg):
        print(msg['content']['text'], end='')

    def _handle_kernel_restarted(self, died=True):
        self.log.debug(f'kernel_restarted')

    def _handle_kernel_died(self, since_last_heartbeat):
        self.log.debug(f'kernel_died {since_last_heartbeat}')

    def keyPressEvent(self, e):
        # operations that always propegate:
        if e.key() in [Qt.Key_Z, Qt.Key_Y] and (e.modifiers() & Qt.ControlModifier):
            self.in_undo_redo = True
            super().keyPressEvent(e)
            self.in_undo_redo = False
            return
        elif e.key() == Qt.Key_V and (e.modifiers() & Qt.ControlModifier):
            return super().keyPressEvent(e) # paste handled by insertFromMimeData
        elif e.key() == Qt.Key_R and (e.modifiers() & Qt.ControlModifier):
            self.restart_kernel()
            return

        cursor = self.textCursor()
        if e.key() == Qt.Key_C and (e.modifiers() & Qt.ControlModifier):
            if cursor.hasSelection():
                return super().keyPressEvent(e) # see createMimeDataFromSelection
            else:
                self.log.debug('interrupt kernel: ctrl+c')
                self.kernel_manager.interrupt_kernel()
            return
        elif e.key() == Qt.Key_A and (e.modifiers() & Qt.ControlModifier):
            cursor = self.code_cell(0).firstCursorPosition()
            cursor.setPosition(self.code_cell(self.table.rows()-1).lastCursorPosition().position(), QTextCursor.KeepAnchor)
            self.setTextCursor(cursor)
            return
        if cursor.currentTable() != self.table:
            return
        mrow, mrow_num, mcol, mcol_num = cursor.selectedTableCells()
        cell = self.table.cellAt(cursor)
        assert cell.isValid()
        col = cell.column()
        cell_idx = cell.row()
        # allow navigation keys to propegate, restrict navigation to one column
        # don't start edit block for navigation keys - causing navigation to be inconsistent for multline code
        if e.key() in [Qt.Key_Up, Qt.Key_Down, Qt.Key_End, Qt.Key_Home]:
            return super().keyPressEvent(e)
        elif e.key() == Qt.Key_Left:
            if cell.firstCursorPosition().position() == cursor.position() or mrow_num > 1:
                if cell_idx > 0:
                    cursor.setPosition(self.table.cellAt(cell_idx-1, col).lastCursorPosition().position(),
                                       QTextCursor.KeepAnchor if (e.modifiers() & Qt.ShiftModifier) else QTextCursor.MoveAnchor)
                    self.setTextCursor(cursor)
                return
            return super().keyPressEvent(e)
        elif e.key() == Qt.Key_Right:
            if cell.lastCursorPosition().position() == cursor.position() or mrow_num > 1:
                if cell_idx + 1 < self.table.rows():
                    cursor.setPosition(self.table.cellAt(cell_idx+1, col).firstCursorPosition().position(),
                                       QTextCursor.KeepAnchor if (e.modifiers() & Qt.ShiftModifier) else QTextCursor.MoveAnchor)
                    self.setTextCursor(cursor)
                return
            return super().keyPressEvent(e)
        if col == 1 or mcol_num > 1:
            return
        with self.edit_block():
            if e.key() == Qt.Key_Return and not (e.modifiers() & Qt.ShiftModifier):
                # shift+enter always adds a new line
                cursor.setPosition(self.code_cell(cell_idx).firstCursorPosition().position(), QTextCursor.KeepAnchor)
                is_complete, indent = self.is_complete(cursor.selection().toPlainText())
                if is_complete == 'incomplete':
                    cursor.setPosition(cursor.anchor())
                    self.textCursor().insertText('\n' + indent*' ')
                    self.execute(cell_idx)
                else: # 'complete' or 'invalid', add a new cell below
                    self.insert_cell(cell_idx+1)
                    cursor.setPosition(self.code_cell(cell_idx).lastCursorPosition().position(), QTextCursor.KeepAnchor)
                    code = cursor.selection().toPlainText()
                    cursor.removeSelectedText()
                    cursor = self.code_cell(cell_idx+1).firstCursorPosition()
                    cursor.insertText(code)
                    self.setTextCursor(self.code_cell(cell_idx+1).firstCursorPosition())
                    if code == '': # no need to re-execute current
                        self.execute(cell_idx + 1)
                    else:
                        self.execute(cell_idx)
                return
            elif e.key() == Qt.Key_Backspace:
                if mrow_num > 1:
                    if mrow == 0 and mrow_num == self.table.rows():
                        self.insert_cell(0)
                        mrow += 1
                    self.remove_cells(mrow, mrow_num)
                    return
                if cursor.position() == self.code_cell(cell_idx).firstCursorPosition().position():
                    if cell_idx > 0:
                        code = self.get_cell_code(cell_idx)
                        self.remove_cells(cell_idx, 1)
                        cursor = self.code_cell(cell_idx-1).lastCursorPosition()
                        pos = cursor.position()
                        cursor.insertText(code)
                        cursor.setPosition(pos)
                        self.setTextCursor(cursor)
                        self.execute(cell_idx-1)
                    return
            elif e.key() == Qt.Key_Delete:
                if mrow_num > 1:
                    if mrow == 0 and mrow_num == self.table.rows():
                        self.insert_cell(0)
                        mrow += 1
                    self.remove_cells(mrow, mrow_num)
                    return
                if (not cursor.hasSelection() and
                        cursor.position() == self.code_cell(cell_idx).lastCursorPosition().position() and
                        cell_idx+1 < self.table.rows()):
                    pos = cursor.position()
                    cursor.insertText(self.get_cell_code(cell_idx+1))
                    cursor.setPosition(pos)
                    self.setTextCursor(cursor)
                    self.remove_cells(cell_idx+1, 1)
                    self.execute(cell_idx)
                    return
            elif e.key() == Qt.Key_Tab:
                if not cursor.hasSelection():
                    check_cursor = QTextCursor(cursor)
                    check_cursor.movePosition(QTextCursor.Left, QTextCursor.KeepAnchor)
                    if check_cursor.hasSelection() and not check_cursor.selectedText().isspace():
                        self.complete_cell_idx = cell_idx
                        self.complete_pos_in_cell = self.pos_in_cell(cell_idx, cursor)
                        self.complete_code = self.get_cell_code(cell_idx)
                        self.complete_msg_id = self.kernel_client.complete(code=self.complete_code, cursor_pos=self.complete_pos_in_cell)
                        return

            old_code = self.get_cell_code(cell_idx)
            super().keyPressEvent(e)
            code = self.get_cell_code(cell_idx)
            if code != old_code:
                self.execute(cell_idx, code)
                self.save_timer.start()

    def createMimeDataFromSelection(self) -> QMimeData:
        mime_data = QMimeData()

        cursor = self.textCursor()
        mrow, mrow_num, mcol, mcol_num = cursor.selectedTableCells()
        if mcol_num > 1:
            # doesn't make much sense to copy multiple columns
            mime_data.setText(cursor.selection().toPlainText())
        elif mrow_num > 1 and mcol == 0:
            # copy code
            mime_data.setText(cursor.selection().toPlainText())
        elif mrow_num > 1 and mcol == 1:
            text = ''
            for cell_idx in range(mrow, mrow+mrow_num):
                if self.latex[cell_idx] != '':
                    text += self.latex[cell_idx] + '\n'
                else:
                    text += self.get_cell_out(cell_idx) + '\n'
            mime_data.setText(text)
        else:
            cell = self.table.cellAt(cursor)
            if cell.isValid():
                cell_idx = cell.row()
                if cell.column() == 1 and self.latex[cell_idx] != '':
                    mime_data.setText(self.latex[cell_idx])
                else:
                    mime_data.setText(cursor.selection().toPlainText())

        return mime_data

    def insertFromMimeData(self, source: QMimeData):
        lines = source.text().splitlines()
        lines.reverse()
        cursor = self.textCursor()
        cursor.insertText(lines.pop())
        cell = self.table.cellAt(cursor)
        if cell.isValid():
            self.execute(cell.row())
        while lines:
            self.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key_Return,
                    Qt.KeyboardModifier.NoModifier, '\r', False, 0))
            self.textCursor().insertText(lines.pop())

    @pyqtSlot()
    def recalculate_columns(self):
        table_format = self.table.format()
        width = self.viewport().width()*table_format.columnWidthConstraints()[1].rawValue()/100
        padding = 10
        columns = int((width-padding) // self.char_width)
        lines = int((self.viewport().height()-padding) // self.char_height)
        self.kernel_client.execute(f'import os\nos.environ["COLUMNS"] = "{columns}"\nos.environ["LINES"] = "{lines}"', silent=True, stop_on_error=False)

        # new pictures would use the new width
        self.execute(0)

    def paintEvent(self, event):
        painter = QPainter(self.viewport())
        rect = self.viewport().rect()

        table_format = self.table.format()
        divider_precentage = table_format.columnWidthConstraints()[0].rawValue()

        editor_rect = QRect(rect.x(), rect.y(), int(rect.width() * divider_precentage/100), rect.height())
        painter.fillRect(editor_rect, theme['code_background'])
        out_rect = QRect(rect.x()+editor_rect.width(), rect.y(), rect.width() - editor_rect.width(), rect.height())
        painter.fillRect(out_rect, theme['out_background'])

        super().paintEvent(event)

    def move_divider(self, delta_x):
        table_format = self.table.format()
        divider_precentage = table_format.columnWidthConstraints()[0].rawValue()
        divider_precentage += 100*delta_x/self.viewport().width()
        table_format.setColumnWidthConstraints([
            QTextLength(QTextLength.PercentageLength, divider_precentage),
            QTextLength(QTextLength.PercentageLength, 100 - divider_precentage)])
        self.table.setFormat(table_format)
        self.recalculate_columns_timer.start()

    def get_divider_x(self):
        table_format = self.table.format()
        return self.viewport().width() * table_format.columnWidthConstraints()[0].rawValue()/100

    def near_divider(self, x):
        margin = 5
        return abs(x - self.get_divider_x()) < margin

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.near_divider(event.pos().x()):
            self.divider_drag_start_pos = event.pos()
            self.divider_drag = True
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.divider_drag:
            delta_x = event.pos().x() - self.divider_drag_start_pos.x()
            self.move_divider(delta_x)
            self.divider_drag_start_pos = event.pos()
        else:
            if self.near_divider(event.pos().x()):
                self.viewport().setCursor(Qt.SplitHCursor)
            else:
                self.viewport().unsetCursor()
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.divider_drag = False
        else:
            super().mouseReleaseEvent(event)

    def open_file(self):
        try:
            file_path = os.environ['PYPAD_SCRIPT']
            if file_path == '':
                self.file = io.StringIO()
            else:
                self.file = open(os.environ['PYPAD_SCRIPT'], 'a+')
            return
        except KeyError:
            pass
        try:
            if os.name == 'nt':
                profile_folder = os.environ.get('USERPROFILE')
            else:
                profile_folder = os.environ.get('HOME')
            if not profile_folder:
                profile_folder = os.getcwd()
            profile_folder = os.path.join(profile_folder, '.pypad')
            os.makedirs(profile_folder, exist_ok=True)

            file_name = 'pypad'
            self.file = open(os.path.join(profile_folder, file_name +'.py'), 'a+')
            self.log.debug(f'open_file: {self.file.name}')
        except Exception as e:
            self.log.error(f'file open error: {e}')
            self.file = io.StringIO()

    def close_file(self):
        self.file.close()

    @pyqtSlot()
    def save_file(self):
        self.log.debug('save_file')
        try:
            self.file.seek(0)
            self.file.truncate()
            for i in range(self.table.rows()):
                self.file.write(self.get_cell_code(i))
                self.file.write('\n')
            self.file.flush()
            os.fsync(self.file.fileno())
        except Exception as e:
            self.log.error(f'file save error: {e}')

    def load_file(self):
        self.log.debug('load_file')
        try:
            self.file.seek(0)
            lines = self.file.readlines()
            if lines[-1] == '': # ignore last new line
                lines.pop()
            if lines:
                for line in lines[:-1]:
                    self.textCursor().insertText(line.rstrip('\n'))
                    self.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key_Return,
                                                Qt.NoModifier, '\r', False, 0))
                self.textCursor().insertText(lines[-1].rstrip('\n'))
        except Exception as e:
            self.log.error(f'file load error: {e}')

    def closeEvent(self, event: QCloseEvent):
        self.save_file()
        self.close_file()
        self.kernel_manager.shutdown_kernel()
        return super().closeEvent(event)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('pypad')
        self.resize(1100, 600)

        self.pypad_text_edit = PyPadTextEdit(self)
        self.setCentralWidget(self.pypad_text_edit)

    def resizeEvent(self, e: QResizeEvent):
        # QTextEdit's resizeEvent fires also upon text overflow in cells
        # no nice way to detect end of resize, use timer
        self.pypad_text_edit.recalculate_columns_timer.start()
        return super().resizeEvent(e)

    def closeEvent(self, event: QCloseEvent):
        self.pypad_text_edit.closeEvent(event)
        return super().closeEvent(event)

def main():
    app = QApplication([])
    main_window = MainWindow()
    sys.exit(app.exec())

if __name__ == '__main__':
    main()

