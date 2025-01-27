
import os
import logging
import tempfile
import pytest
from pytestqt.qtbot import QtBot

# avoid DeprecationWarning https://github.com/jupyter/jupyter_core/issues/398
os.environ["JUPYTER_PLATFORM_DIRS"] = "1"

from PyQt6.QtCore import Qt
from pypad import MainWindow, PyPadTextEdit

class LogHandler(logging.Handler):
    def emit(self, record):
        if record.levelno > logging.INFO:
            raise AssertionError(self.format(record))

@pytest.fixture
def pypad(qtbot: QtBot):
    with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
        file_path = tmp_file.name
    window = MainWindow(file_path=file_path)
    pypad = window.pypad_text_edit
    pypad.log.addHandler(LogHandler())
    qtbot.waitUntil(lambda: pypad.kernel_info != '', timeout=5000)
    # todo: measure init time
    yield pypad
    window.close()
    os.remove(file_path)

def test_execution(pypad: PyPadTextEdit, qtbot: QtBot):
    qtbot.keyClicks(pypad, '1+1')
    qtbot.waitUntil(lambda: pypad.get_cell_out(0) == '2')
    qtbot.keyClick(pypad, Qt.Key_Enter)
    qtbot.keyClicks(pypad, 'print(1)')
    qtbot.waitUntil(lambda: pypad.get_cell_out(1) == '1')
