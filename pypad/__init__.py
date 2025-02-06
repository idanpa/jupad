import os
import sys
import io
import re
import traceback
import logging
from base64 import b64decode
from contextlib import contextmanager

from PyQt6.QtWidgets import QMainWindow, QTextEdit, QFrame, QMessageBox, QFileDialog
from PyQt6.QtCore import (Qt, QObject, QRect, QMimeData, QEvent, QUrl, QSize,
                          QVariantAnimation, QEasingCurve,
                          QTimer, QRunnable, QThreadPool, pyqtSlot, pyqtSignal)
from PyQt6.QtGui import (QFont, QFontMetrics, QFontDatabase, QImage, QGuiApplication,
    QPainter, QColor, QKeyEvent, QResizeEvent, QCloseEvent,
    QTextCursor, QTextLength, QTextCharFormat, QTextFrameFormat, QTextBlockFormat,
    QTextDocument, QTextImageFormat, QTextTableCell, QTextTableFormat, QTextTableCellFormat)

from qtconsole.pygments_highlighter import PygmentsHighlighter
from qtconsole.base_frontend_mixin import BaseFrontendMixin
from qtconsole.manager import QtKernelManager
from qtconsole.completion_widget import CompletionWidget
from qtconsole.call_tip_widget import CallTipWidget

from ansi2html import Ansi2HTMLConverter

light_theme = {
    'code_background': QColor('#ffffff'),
    'out_background': QColor('#f6f6f6'),
    'separator_color': QColor('#f0f0f0'),
    'done_color': QColor('#f6f6f6'),
    'pending_color': QColor('#f6f6f6'),
    'executing_color': QColor('#f5ca6e'),
    'error_color': QColor('#f4bdbd'),
    'inactive_color': QColor('#ffffff'),
    'active_color': QColor('#f6f6f6'),
    'splash_color': QColor('#a0a0a0'),
    'is_dark': False,
}
theme = light_theme

class AnimateExecutingCell(QVariantAnimation):
    def __init__(self, pypad):
        super().__init__()
        self.pypad = pypad
        self.setLoopCount(-1)
        self.setDuration(2000)
        self.setEasingCurve(QEasingCurve.InOutSine)
        self.setKeyValues([(0.0, theme['out_background']), (0.5, theme['executing_color']), (1.0, theme['out_background'])])

    def updateCurrentValue(self, value):
        # this is also called upon construction, update only if execute running
        if self.pypad.execute_running:
            with self.pypad.join_edit_block():
                self.pypad.set_cell_color(self.pypad.execute_cell_idx, value)

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
        try:
            from IPython.lib.latextools import latex_to_png
            image_data = latex_to_png(self.latex, wrap=False, backend='dvipng')
            if image_data is None:
                image_data = latex_to_png(self.latex, wrap=False, backend='matplotlib')
            if image_data:
                self.signals.result.emit(self.cell_idx, self.latex, image_data)
        except Exception:
            logging.getLogger('pypad').exception('latex error')

class Highlighter(PygmentsHighlighter):
    def highlightBlock(self, string):
        # don't highlight output cells
        cursor = QTextCursor(self.currentBlock())
        cell = self.parent().table.cellAt(cursor)
        if cell.isValid() and cell.column() == 0:
            return super().highlightBlock(string)

class CompletionWidget_(CompletionWidget):
    def _complete_current(self):
        super()._complete_current()
        self._text_edit.execute(self._text_edit.complete_cell_idx)

class CallTipWidget_(CallTipWidget):
    def __init__(self, text_edit):
        super().__init__(text_edit)

    def _format_tooltip(self, doc):
        return self._text_edit.html_converter.convert(doc)

class PyPadTextEdit(QTextEdit, BaseFrontendMixin):
    def __init__(self, parent, file_path, debug=False):
        self.log = logging.getLogger('pypad')
        self.log.setLevel(logging.DEBUG if debug else logging.INFO)
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter('%(relativeCreated)d %(message)s'))
        handler.setLevel(logging.DEBUG)
        self.log.addHandler(handler)
        self.log.debug('start')
        sys.excepthook = self.exception_hook
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

        self.setFrameStyle(QFrame.Shape.NoFrame)

        self.edit_block_cursor = self.textCursor()
        table_format = QTextTableFormat()
        table_format.setBorder(0)
        table_format.setPadding(-3)
        table_format.setMargin(0)
        table_format.setWidth(QTextLength(QTextLength.PercentageLength, 100))
        table_format.setColumnWidthConstraints([
            QTextLength(QTextLength.PercentageLength, 40),
            QTextLength(QTextLength.PercentageLength, 60)])
        self.table = self.textCursor().insertTable(1, 2, table_format)

        self.highlighter = Highlighter(self)
        self.highlighter.set_style('vs')

        self.in_undo_redo = False
        # last execution count of each cell
        self.execution_count = [None]
        # if cell has an image
        self.has_image = [False]
        # latex code of cell
        self.latex = ['']
        self.pending_newline = ['']
        self.execute_running = False
        self.prev_execute_msg_id = ''
        self.execute_msg_id = ''
        self.prev_execute_cell_idx = -1
        self.execute_cell_idx = -1
        self.splash_visible = False
        self.kernel_info = ''
        self.divider_drag = False
        self.file = None
        self.setMouseTracking(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)

        # for setting cells format:
        self.insert_cell(0)
        self.remove_cells(1, 1)

        self.executing_animation = AnimateExecutingCell(self)

        self.document().begin().setVisible(False) # https://stackoverflow.com/questions/76061158

        self.cursorPositionChanged.connect(self.position_changed)

        self.open_file(file_path)

        if self.table.rows() == 1 and self.get_cell_code(0) == '':
            self.set_splash(True)
            self.splash_visible = True

        self.log.debug('show')
        self.parent().setCentralWidget(self)
        self.parent().show()
        self.setReadOnly(True)

        self.log.debug('launching kernel')
        kernel_manager = QtKernelManager(kernel_name='python3')
        kernel_manager.start_kernel()

        kernel_client = kernel_manager.client()
        kernel_client.start_channels()

        self.kernel_manager = kernel_manager
        self.kernel_client = kernel_client

        self.html_converter = Ansi2HTMLConverter(inline=True, line_wrap=False, dark_bg=theme['is_dark'])

        self._control = self # for CompletionWidget
        self.completion_widget = CompletionWidget_(self, 0)
        self.call_tip_widget = CallTipWidget_(self)

        self.transformer_manager = None
        self.thread_pool = QThreadPool()

        self.log.debug('kernel_info')
        # we finish startup upon kernel_info_reply, when kernel is ready
        kernel_client.kernel_info()

    def exception_hook(self, etype, value, tb):
        sys.__excepthook__(etype, value, tb)
        msg = ''.join(traceback.format_exception(etype, value, tb))
        QMessageBox(QMessageBox.Icon.Critical, 'Exception', msg).exec()
        try: self.log.error(msg)
        except: pass
        try: self.save_file()
        except: pass
        sys.exit(1)

    def set_splash(self, visible):
        with self.join_edit_block():
            cursor = self.table.lastCursorPosition()
            cursor.movePosition(QTextCursor.Right)
            cursor.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)
            if visible:
                width = 40
                format = QTextCharFormat()
                format.setForeground(theme['splash_color'])
                cursor.insertText('\n\n\n\n\n' + 'pypad - Python Notepad'.center(width) + '\n\n'
                                + self.kernel_info.center(width) + '\n\n'
                                '       [Ctrl]+O - Open File\n'
                                '       [Ctrl]+S - Save File As\n'
                                '       [Ctrl]+[Shift]+S - Save File As\n'
                                '       [Ctrl]+R - Restart Kernel\n'
                                , format)
            else:
                cursor.removeSelectedText()

    def is_complete(self, code):
        if self.transformer_manager is None:
            from IPython.core.inputtransformer2 import TransformerManager
            self.transformer_manager = TransformerManager()
        return self.transformer_manager.check_complete(code)

    def code_cell(self, cell_idx):
        cell = self.table.cellAt(cell_idx, 0)
        assert cell.isValid(), cell_idx
        return cell

    def out_cell(self, cell_idx):
        cell = self.table.cellAt(cell_idx, 1)
        assert cell.isValid(), cell_idx
        return cell

    def insert_cell(self, cell_idx):
        '''insert cell before the given index'''
        self.table.insertRows(cell_idx, 1)
        self.execution_count.insert(cell_idx, None)
        self.has_image.insert(cell_idx, False)
        self.latex.insert(cell_idx, '')
        self.pending_newline.insert(cell_idx, '')

        out_cell_format = QTextTableCellFormat()
        out_cell_format.setLeftBorder(3)
        out_cell_format.setLeftBorderStyle(QTextTableFormat.BorderStyle_Solid)
        out_cell_format.setLeftBorderBrush(theme['pending_color'])
        out_cell_format.setLeftPadding(4)
        out_cell_format.setBottomBorder(1)
        out_cell_format.setBottomBorderStyle(QTextTableFormat.BorderStyle_Solid)
        out_cell_format.setBottomBorderBrush(theme['separator_color'])
        self.out_cell(cell_idx).setFormat(out_cell_format)

        code_cell_format = QTextTableCellFormat()
        code_cell_format.setLeftBorder(3)
        code_cell_format.setLeftBorderStyle(QTextTableFormat.BorderStyle_Solid)
        code_cell_format.setLeftBorderBrush(theme['inactive_color'])
        code_cell_format.setBottomBorder(1)
        code_cell_format.setBottomBorderStyle(QTextTableFormat.BorderStyle_Solid)
        code_cell_format.setBottomBorderBrush(theme['separator_color'])
        self.code_cell(cell_idx).setFormat(code_cell_format)

        self.setTextCursor(self.code_cell(cell_idx).firstCursorPosition())

    def remove_cells(self, cell_idx, count):
        if self.execute_running: # stop execution
            self.executing_animation.stop()
            self.execute_msg_id = ''
            self.kernel_manager.interrupt_kernel()

        self.table.removeRows(cell_idx, count)
        self.execution_count[cell_idx:cell_idx+count] = []
        self.has_image[cell_idx:cell_idx+count] = []
        self.latex[cell_idx:cell_idx+count] = []
        self.pending_newline[cell_idx:cell_idx+count] = []

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
                    self.setTextCursor(cursor)
                else:
                    cursor.setPosition(self.table.firstCursorPosition().position())
                    self.setTextCursor(cursor)
            elif cursor.position() > self.table.lastCursorPosition().position():
                cell = self.table.cellAt(cursor.anchor())
                if cell.isValid():
                    cursor.setPosition(self.table.cellAt(self.table.rows()-1, cell.column()).lastCursorPosition().position(), QTextCursor.KeepAnchor)
                    self.setTextCursor(cursor)
                else:
                    cursor.setPosition(self.code_cell(self.table.rows()-1).lastCursorPosition().position())
                    self.setTextCursor(cursor)

        elif cursor.position() < self.table.firstCursorPosition().position():
            cursor.setPosition(self.table.firstCursorPosition().position())
            self.setTextCursor(cursor)
        elif cursor.position() > self.table.lastCursorPosition().position():
            cursor.setPosition(self.code_cell(self.table.rows()-1).lastCursorPosition().position())
            self.setTextCursor(cursor)

        mrow, mrow_num, mcol, mcol_num = cursor.selectedTableCells()
        cell = self.table.cellAt(cursor)
        if not cell.isValid():
            self.log.error(f'ERROR: Invalid pos {cursor.position()}')
            return
        if mcol_num > 1 or mrow_num > 1 or cell.column() == 1:
            cell_idx = -1
        else:
            cell_idx = cell.row()

        with self.join_edit_block():
            for i in range(self.table.rows()):
                self.set_cell_active(i, i == cell_idx)

    def cell_idx_and_pos_in_cell(self, cursor):
        cell = self.table.cellAt(cursor)
        if cell.isValid():
            return cell.row(), cursor.position() - cell.firstCursorPosition().position()
        return -1, -1

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

    def clear_cell(self, cell_idx):
        cell = self.out_cell(cell_idx)
        cursor = cell.firstCursorPosition()
        cursor.setPosition(cell.lastCursorPosition().position(), QTextCursor.KeepAnchor)
        cursor.removeSelectedText()
        self.has_image[cell_idx] = False
        self.pending_newline[cell_idx] = ''
        cell_format = cell.format().toTableCellFormat()
        cell_format.setToolTip('')
        cell.setFormat(cell_format)

    def append_text(self, cell_idx, txt):
        if self.pending_newline[cell_idx]:
            txt = self.pending_newline[cell_idx] + txt
            self.pending_newline[cell_idx] = ''
        if txt and txt[-1] == '\n':
            self.pending_newline[cell_idx] = txt[-1]
            txt = txt[:-1]
        cell = self.out_cell(cell_idx)
        cursor = cell.lastCursorPosition()
        cursor.insertText(txt)

    def append_img(self, cell_idx, img, format, name):
        try:
            # name should be unique to allow undo/redo
            cell = self.out_cell(cell_idx)
            cursor = cell.lastCursorPosition()
            # add image in new line if cell is not empty
            if cursor != cell.firstCursorPosition():
                cursor.insertText('\n')
            image = QImage()
            image.loadFromData(img, format.upper())
            self.document().addResource(QTextDocument.ImageResource, QUrl(name), image)
            image_format = QTextImageFormat()
            image_format.setName(name)
            image_format.setMaximumWidth(self.table.format().columnWidthConstraints()[1])
            cursor.insertImage(image_format)
            self.has_image[self.execute_cell_idx] = True
        except Exception:
            self.log.exception('set image error')

    def set_cell_active(self, cell_idx, active):
        cell = self.code_cell(cell_idx)
        cell_format = cell.format().toTableCellFormat()
        cell_format.setLeftBorderBrush(theme['active_color'] if active else theme['inactive_color'])
        cell.setFormat(cell_format)

    def set_cell_color(self, cell_idx, color):
        cell = self.out_cell(cell_idx)
        cell_format = cell.format().toTableCellFormat()
        cell_format.setLeftBorderBrush(color)
        cell.setFormat(cell_format)

    def set_cell_tooltip(self, cell_idx, tooltip):
        cell = self.out_cell(cell_idx)
        cell_format = cell.format().toTableCellFormat()
        cell_format.setToolTip(tooltip)
        cell.setFormat(cell_format)

    def restart_kernel(self):
        self.kernel_manager.restart_kernel()
        self.execute(0)

    def _execute(self, cell_idx, code=None):
        if code is None:
            code = self.get_cell_code(cell_idx)
        with self.join_edit_block():
            self.clear_cell(cell_idx)

        self.latex[cell_idx] = ''
        # set '_', '__', '___' to hold the previous cells output:
        prep_code = ''
        for i, var_name in ((cell_idx-1, '_'), (cell_idx-2, '__'), (cell_idx-3, '___')):
            if i >= 0 and self.execution_count[i] is not None:
                prep_code += f'{var_name} = Out.get({self.execution_count[i]}, None)\n'
        self.kernel_client.execute(prep_code, silent=True, stop_on_error=False)
        # don't stop on error, we interrupt kernel and execute a new cell immediately after, otherwise might get aborted

        self.prev_execute_cell_idx = self.execute_cell_idx
        self.prev_execute_msg_id = self.execute_msg_id
        self.execute_cell_idx = cell_idx
        self.execute_msg_id = self.kernel_client.execute(code, stop_on_error=False)
        self.executing_animation.start()
        self.log.debug(f'execute [{cell_idx}] ({self.execute_msg_id.split("_")[-1]}): {code}')

    def execute(self, cell_idx, code=None):
        if self.execute_running:
            if self.execute_cell_idx < cell_idx:
                return # eventually we will execute this cell
            else:
                self.log.debug('interrupt kernel: new code')
                self.kernel_manager.interrupt_kernel()
        self.execute_running = True
        self._execute(cell_idx, code)
        with self.join_edit_block():
            for i in range(cell_idx+1, self.table.rows()):
                self.set_cell_color(i, theme['pending_color'])

    def inspect(self):
        cursor = self.textCursor()
        self.inspect_cell_idx, self.inspect_pos_in_cell = self.cell_idx_and_pos_in_cell(cursor)
        if self.inspect_cell_idx >= 0:
            self.inspect_code = self.get_cell_code(self.inspect_cell_idx)
            self.inspect_msg_id = self.kernel_client.inspect(self.inspect_code, self.inspect_pos_in_cell)
            self.log.debug(f'inspect [{self.inspect_cell_idx}] ({self.inspect_msg_id.split("_")[-1]}): {self.inspect_code}')

    @pyqtSlot(int, str, bytes)
    def set_cell_latex_img(self, cell_idx, latex, img):
        try:
            if self.latex[cell_idx] == latex:
                with self.join_edit_block():
                    self.clear_cell(cell_idx)
                    self.append_img(cell_idx, img, 'PNG', latex)
        except IndexError:
            pass

    def _handle_execute_result(self, msg):
        msg_id = msg['parent_header']['msg_id']
        self.log.debug(f'execute_result ({msg_id.split("_")[-1]}): {msg["content"]}')
        self._handle_execute_result_or_display_data(msg['content'], msg_id)

    def _handle_display_data(self, msg):
        msg_id = msg['parent_header']['msg_id']
        self.log.debug(f'display_data ({msg_id.split("_")[-1]})')
        self._handle_execute_result_or_display_data( msg['content'], msg_id)

    def _handle_execute_result_or_display_data(self, content, msg_id):
        if msg_id == self.execute_msg_id:
            cell_idx = self.execute_cell_idx
        elif msg_id == self.prev_execute_msg_id and self.prev_execute_cell_idx != self.execute_cell_idx:
            # execute_reply and execute_results are using different sockets, and their order is not guaranteed
            cell_idx = self.prev_execute_cell_idx
        else:
            return

        with self.join_edit_block():
            data = content['data']
            if 'image/png' in data:
                image_data = b64decode(data['image/png'].encode('ascii'))
                self.append_img(cell_idx, image_data, 'PNG', msg_id)
            elif 'image/jpeg' in data:
                image_data = b64decode(data['image/jpeg'].encode('ascii'))
                self.append_img(cell_idx, image_data, 'JPG', msg_id)
            elif 'text/plain' in data:
                if not self.has_image[cell_idx]:
                    self.append_text(cell_idx, data['text/plain'])
            else:
                self.log.error(f'unsupported type {data}')

            if 'text/latex' in data:
                self.latex[cell_idx] = data['text/latex']
                latex_worker = LatexWorker(cell_idx, data['text/latex'])
                latex_worker.signals.result.connect(self.set_cell_latex_img)
                self.thread_pool.start(latex_worker)

    def _handle_error(self, msg):
        msg_id = msg['parent_header']['msg_id']
        content = msg['content']
        ename = content['ename']
        self.log.debug(f'error ({msg_id.split("_")[-1]}): {ename}')
        if msg_id != self.execute_msg_id:
            return
        self.executing_animation.stop()
        with self.join_edit_block():
            self.set_cell_color(self.execute_cell_idx, theme['error_color'])
            self.set_cell_tooltip(self.execute_cell_idx, self.html_converter.convert(''.join(content['traceback'])))
            self.append_text(self.execute_cell_idx, ename)

    def _handle_execute_reply(self, msg):
        msg_id = msg['parent_header']['msg_id']
        content = msg['content']
        status = content['status']
        self.log.debug(f'execute_reply ({msg_id.split("_")[-1]}): {status}')
        if msg_id != self.execute_msg_id:
            return
        self.execution_count[self.execute_cell_idx] = content['execution_count']
        # no guarantee that reply comes after error message
        self.executing_animation.stop()
        with self.join_edit_block():
            if status == 'ok':
                if self.execute_cell_idx == 0 and self.table.rows() == 1 and self.get_cell_code(0) == '':
                    self.set_splash(True)
                    self.splash_visible = True
                elif self.splash_visible:
                    self.set_splash(False)
                    self.splash_visible = False
                self.set_cell_color(self.execute_cell_idx, theme['done_color'])
            else:
                self.set_cell_color(self.execute_cell_idx, theme['error_color'])
        if self.execute_cell_idx+1 < self.table.rows():
            self._execute(self.execute_cell_idx+1)
        else:
            self.execute_running = False

    def _handle_complete_reply(self, msg):
        # code from qtconsole:
        msg_id = msg['parent_header']['msg_id']
        self.log.debug(f'complete_reply ({msg_id.split("_")[-1]})')
        cursor = self.textCursor()
        if  (msg_id == self.complete_msg_id and
             self.cell_idx_and_pos_in_cell(cursor) == (self.complete_cell_idx, self.complete_pos_in_cell) and
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
                self.execute(self.complete_cell_idx)
            elif len(matches) > 1:
                current_pos = self.textCursor().position()
                prefix = os.path.commonprefix(matches)
                if prefix:
                    cursor.setPosition(current_pos, QTextCursor.KeepAnchor)
                    cursor.insertText(prefix)
                    current_pos = cursor.position()
                self.completion_widget.show_items(cursor, matches, prefix_length=len(prefix))

    def _handle_inspect_reply(self, msg):
        msg_id = msg['parent_header']['msg_id']
        self.log.debug(f'inspect_reply ({msg_id.split("_")[-1]})')
        content = msg['content']
        cursor = self.textCursor()
        if  (msg_id == self.inspect_msg_id and
             self.cell_idx_and_pos_in_cell(cursor) == (self.inspect_cell_idx, self.inspect_pos_in_cell) and
             self.get_cell_code(self.inspect_cell_idx) == self.inspect_code and
             content.get('status') == 'ok' and content.get('found', False)):
                self.call_tip_widget.show_inspect_data(content)

    def _handle_kernel_info_reply(self, msg):
        self.log.debug(f'kernel_info_reply')
        language_info = msg['content']['language_info']
        self.kernel_info = language_info['name'] + ' ' + language_info['version']

        if self.splash_visible:
            # update splash
            self.set_splash(True)

        self.execute(0)

        self.setUndoRedoEnabled(True)
        self.setReadOnly(False)

    def _handle_clear_output(self, msg):
        self.log.debug(f'clear_output')

    def _handle_input_request(self, msg):
        self.log.debug(f'input_request')

    def _handle_shutdown_reply(self, msg):
        self.log.debug(f'shutdown_reply')

    def _handle_status(self, msg):
        # self.kernel_status = msg['content']['execution_state']
        return

    def _handle_stream(self, msg):
        msg_id = msg['parent_header'].get('msg_id', 'IGNORE_ME')
        if msg_id != self.execute_msg_id:
            return
        with self.join_edit_block():
            self.append_text(self.execute_cell_idx, msg['content']['text'])

    def _handle_kernel_restarted(self, died=True):
        self.log.debug(f'kernel_restarted')

    def _handle_kernel_died(self, since_last_heartbeat):
        self.log.debug(f'kernel_died {since_last_heartbeat}')

    def keyPressEvent(self, e):
        # self.log.debug(f'keyPress {Qt.Key(e.key()).name} {Qt.KeyboardModifier(e.modifiers()).name} {e.text()}')
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
        elif e.key() == Qt.Key_O and (e.modifiers() & Qt.ControlModifier):
            self.user_open_file()
            return
        elif e.key() == Qt.Key_S and (e.modifiers() & Qt.ControlModifier):
            if (e.modifiers() & Qt.ShiftModifier):
                self.user_save_file_as()
            else:
                self.save_file()
            return
        elif e.key() == Qt.Key_Space and (e.modifiers() & Qt.ControlModifier):
            self.inspect()
            return
        elif e.key() == Qt.Key_Escape:
            # escape key gives e.text()='\x1b', ignore it
            return

        cursor = self.textCursor()
        if e.key() == Qt.Key_C and (e.modifiers() & Qt.ControlModifier):
            if cursor.hasSelection():
                return super().keyPressEvent(e) # copy handled by createMimeDataFromSelection
            else:
                # todo: copy line that would be pasted as a complete cell above
                pass
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
        assert cell.isValid(), cell_idx
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
            # if multiple cells selected, start with deleting them
            if mrow_num > 1 and (e.key() in [Qt.Key_Return, Qt.Key_Enter, Qt.Key_Backspace, Qt.Key_Delete] or e.text() != ''):
                self.insert_cell(mrow)
                cell_idx = mrow
                self.remove_cells(mrow+1, mrow_num)
                if e.key() in [Qt.Key_Return, Qt.Key_Enter, Qt.Key_Backspace, Qt.Key_Delete]:
                    if cell_idx == 0:
                        self.execute(0) # to show splash
                    return
                # else, add text and execute
            elif e.key() in [Qt.Key_Return, Qt.Key_Enter] and not (e.modifiers() & Qt.ShiftModifier):
                # shift+enter always adds a new line
                cursor.removeSelectedText()
                cursor.setPosition(self.code_cell(cell_idx).firstCursorPosition().position(), QTextCursor.KeepAnchor)
                is_complete, indent = self.is_complete(cursor.selection().toPlainText())
                if is_complete == 'incomplete':
                    cursor.setPosition(cursor.anchor())
                    self.textCursor().insertText('\n' + indent*' ')
                    self.execute(cell_idx)
                else: # 'complete' or 'invalid', add a new cell below
                    self.insert_cell(cell_idx+1)
                    cursor.setPosition(self.code_cell(cell_idx).lastCursorPosition().position(), QTextCursor.KeepAnchor)
                    code_after_cursor = cursor.selection().toPlainText()
                    cursor.removeSelectedText()
                    cursor = self.code_cell(cell_idx+1).firstCursorPosition()
                    cursor.insertText(code_after_cursor)
                    self.setTextCursor(self.code_cell(cell_idx+1).firstCursorPosition())
                    if code_after_cursor == '': # no need to re-execute current
                        self.execute(cell_idx + 1)
                    else:
                        self.execute(cell_idx)
                return
            elif e.key() == Qt.Key_Backspace:
                if (not cursor.hasSelection() and
                        cursor.position() == self.code_cell(cell_idx).firstCursorPosition().position() and
                        cell_idx > 0):
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
                if (not cursor.hasSelection() and
                        cursor.position() == self.code_cell(cell_idx).lastCursorPosition().position() and
                        cell_idx+1 < self.table.rows()):
                    pos = cursor.position()
                    cursor.insertText(self.get_cell_code(cell_idx+1))
                    cursor.setPosition(pos)
                    self.setTextCursor(cursor)
                    self.remove_cells(cell_idx+1, 1)
                    self.execute(cell_idx)
                    self.save_timer.start()
                    return
            # shift+tab gives backtab:
            elif e.key() == Qt.Key_Backtab or (e.key() == Qt.Key_Tab and (e.modifiers() & Qt.ShiftModifier)):
                if cursor.hasSelection():
                    start_pos = cursor.selectionStart()
                    cursor.insertText(re.sub(r'\n {1,4}', r'\n', cursor.selection().toPlainText()))
                    cursor.setPosition(start_pos, QTextCursor.KeepAnchor)
                    self.setTextCursor(cursor)

                cursor.movePosition(QTextCursor.StartOfLine)
                for i in range(4):
                    cursor.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor)
                    if cursor.selectedText() == ' ':
                        cursor.removeSelectedText()
                self.execute(cell_idx)
                self.save_timer.start()
                return
            elif e.key() == Qt.Key_Tab:
                if cursor.hasSelection():
                    start_pos = cursor.selectionStart()
                    cursor.insertText(cursor.selection().toPlainText().replace('\n', '\n    '))
                    cursor.setPosition(start_pos, QTextCursor.KeepAnchor)
                    self.setTextCursor(cursor)
                    cursor.movePosition(QTextCursor.StartOfLine)
                    cursor.insertText('    ')
                else:
                    check_cursor = QTextCursor(cursor)
                    check_cursor.movePosition(QTextCursor.Left, QTextCursor.KeepAnchor)
                    if check_cursor.selectedText().isspace() or check_cursor.selectedText() == '':
                        cursor.insertText('    ')
                        self.execute(cell_idx)
                        self.save_timer.start()
                    else:
                        self.complete_cell_idx, self.complete_pos_in_cell = self.cell_idx_and_pos_in_cell(cursor)
                        self.complete_code = self.get_cell_code(self.complete_cell_idx)
                        self.complete_msg_id = self.kernel_client.complete(code=self.complete_code, cursor_pos=self.complete_pos_in_cell)
                return

            old_code = self.get_cell_code(cell_idx)
            super().keyPressEvent(e)
            code = self.get_cell_code(cell_idx)
            if code != old_code:
                self.execute(cell_idx, code)
                self.save_timer.start()

            if e.text() == '(':
                self.inspect()

    def createMimeDataFromSelection(self) -> QMimeData:
        mime_data = QMimeData()
        # '\u2028'=newline, '\n'=new cell
        cursor = self.textCursor()
        mrow, mrow_num, mcol, mcol_num = cursor.selectedTableCells()
        if mcol_num > 1:
            # doesn't make much sense to copy multiple columns
            text = cursor.selection().toPlainText()
        elif mrow_num > 1 and mcol == 0:
            # copy code
            text = ''
            for cell_idx in range(mrow, mrow+mrow_num):
                text += self.get_cell_code(cell_idx).replace('\n', '\u2028') + '\n'
            text = text
        elif mrow_num > 1 and mcol == 1:
            # copy output
            text = ''
            for cell_idx in range(mrow, mrow+mrow_num):
                if self.latex[cell_idx] != '':
                    text += self.latex[cell_idx] + '\n'
                else:
                    text += self.get_cell_out(cell_idx) + '\n'
            text = text
        else:
            # copy within single cell
            cell = self.table.cellAt(cursor)
            if cell.isValid():
                cell_idx = cell.row()
                if cell.column() == 1 and self.latex[cell_idx] != '':
                    text = self.latex[cell_idx]
                else:
                    text = cursor.selection().toPlainText().replace('\n', '\u2028')

        mime_data.setData('pypad', text.encode())
        mime_data.setText(text.replace('\u2028', '\n'))
        return mime_data

    def insertFromMimeData(self, mime_data: QMimeData):
        if mime_data.hasUrls():
            if mime_data.urls()[0].isLocalFile():
                self.open_file(mime_data.urls()[0].toLocalFile(), retry=False)
            return
        if mime_data.hasFormat('pypad'):
            # where \u2028=newline, \n=new cell
            text = mime_data.data('pypad').data().decode()
        else:
            text = mime_data.text()
        lines = text.split('\n')
        if lines[-1] == '': # ignore last new line
            lines.pop()
        lines.reverse()

        with self.edit_block():
            cursor = self.textCursor()
            cell = self.table.cellAt(cursor)
            if not cell.isValid():
                return
            mrow, mrow_num, mcol, mcol_num = cursor.selectedTableCells()
            if cell.column() == 1 or mcol_num > 1:
                return
            cell_idx = cell.row()
            # if multiple cells selected, start with deleting them
            if mrow_num > 0:
                self.insert_cell(mrow)
                self.remove_cells(mrow+1, mrow_num)
                cell_idx = mrow
                cursor = self.textCursor()

            execute_cell_idx = cell_idx
            if len(lines) == 1:
                cursor.insertText(lines.pop())
            else:
                cursor.setPosition(self.code_cell(cell_idx).lastCursorPosition().position(), QTextCursor.KeepAnchor)
                code_after_cursor = cursor.selection().toPlainText()
                cursor.removeSelectedText()

                lines_to_insert = ''
                while lines:
                    lines_to_insert += lines.pop()
                    is_complete, indent = self.is_complete(lines_to_insert.replace('\u2028', '\n'))
                    if is_complete == 'incomplete' and lines:
                        lines_to_insert += '\u2028'
                    else: # complete or invalid or no more lines
                        if mrow_num <= 0: # no need if we already inserted before
                            cell_idx += 1
                            self.insert_cell(cell_idx)
                        mrow_num = -1
                        self.textCursor().insertText(lines_to_insert)
                        self.log.debug(f'inserting: {lines_to_insert}')
                        lines_to_insert = ''
                if code_after_cursor != '':
                    self.insert_cell(cell_idx+1)
                    self.textCursor().insertText(code_after_cursor)
                self.setTextCursor(self.code_cell(cell_idx).lastCursorPosition())

            self.execute(execute_cell_idx)

    @pyqtSlot()
    def recalculate_columns(self):
        if QGuiApplication.mouseButtons() == Qt.LeftButton:
            self.recalculate_columns_timer.start()
            return
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

    def user_open_file(self):
        dir = os.path.dirname(self.file.name) if self.file else ''
        file_path, sel_filter = QFileDialog.getOpenFileName(self, 'Open File', dir, 'Python Files (*.py);;All Files (*)')
        if file_path:
            self.open_file(file_path)

    def open_file(self, file_path, retry=True, load=True):
        self.save_file()
        try:
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            self.file = open(file_path, 'a+')
        except Exception as e:
            self.log.exception('file open error')
            QMessageBox(QMessageBox.Icon.Critical, 'File Open Error', f'Failed to open "{file_path}"\n{type(e).__name__}: {e}').exec()
            if retry:
                self.user_open_file()
            return
        self.parent().setWindowTitle(os.path.basename(file_path))
        self.log.debug(f'open_file: {self.file.name}')
        if load:
            self.load_file(self.file)

    def user_save_file_as(self):
        dir = os.path.dirname(self.file.name) if self.file else ''
        file_path, sel_filter = QFileDialog.getSaveFileName(self, 'Save File As', dir, 'Python Files (*.py);;All Files (*)')
        if file_path:
            self.open_file(file_path, retry=False, load=False)
            self.save_file()

    @pyqtSlot()
    def save_file(self):
        self.log.debug('save_file')
        if self.file is None:
            return
        try:
            self.file.seek(0)
            self.file.truncate()
            for i in range(self.table.rows()):
                self.file.write('# %%\n')
                self.file.write(self.get_cell_code(i))
                self.file.write('\n')
            self.file.flush()
            # os.fsync(self.file.fileno()) # not really needed and io.StringIO has no fileno
        except Exception:
            self.log.exception(f'file save error')

    def load_file(self, file):
        self.log.debug('load_file')
        self.insert_cell(0)
        self.remove_cells(1, self.table.rows()-1)
        def rstrip(s):
            # remove single new line
            return s[:-1] if s[-1] == '\n' else s
        try:
            file.seek(0)
            cells = re.split(r'^[ \t]*#[ \t]*%%.*\n', file.read(), flags=re.MULTILINE)
            if cells and cells[0] == '':
                cells.pop(0) # first empty from split
            if cells:
                for cell in cells[:-1]:
                    self.textCursor().insertText(rstrip(cell))
                    self.insert_cell(self.table.rows())
                self.textCursor().insertText(rstrip(cells[-1]))
        except Exception:
            self.log.exception(f'file load error')

    def closeEvent(self, event: QCloseEvent):
        self.log.debug('close event')
        self.parent().hide()
        self.save_file()
        self.kernel_manager.shutdown_kernel()
        return super().closeEvent(event)

class MainWindow(QMainWindow):
    def __init__(self, **kwargs):
        self.init_done = False
        super().__init__()
        self.resize(1100, 600)
        self.setWindowTitle('pypad')
        self.pypad_text_edit = PyPadTextEdit(self, **kwargs)
        self.init_done = True

    def resizeEvent(self, e: QResizeEvent):
        # QTextEdit's resizeEvent fires also upon text overflow in cells
        # no nice way to detect end of resize, use timer
        # ignore the initial resize (from -1,-1), when pypad_text_edit is not ready
        if self.init_done:
            self.pypad_text_edit.recalculate_columns_timer.start()
        return super().resizeEvent(e)

    def closeEvent(self, event: QCloseEvent):
        self.pypad_text_edit.closeEvent(event)
        return super().closeEvent(event)
