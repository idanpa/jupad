
import os
import logging
import tempfile
import pytest
from pytestqt.qtbot import QtBot

# avoid DeprecationWarning https://github.com/jupyter/jupyter_core/issues/398
os.environ["JUPYTER_PLATFORM_DIRS"] = "1"

from PyQt6.QtCore import Qt
from jupad import MainWindow, JupadTextEdit

class LogHandler(logging.Handler):
    def emit(self, record):
        if record.levelno > logging.INFO:
            raise AssertionError(self.format(record))

@pytest.fixture
def jupad(qtbot: QtBot):
    with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
        file_path = tmp_file.name
    window = MainWindow(file_path=file_path)
    jupad = window.jupad_text_edit
    jupad.log.addHandler(LogHandler())
    qtbot.waitUntil(lambda: jupad.kernel_info != '', timeout=5000)
    # todo: measure init time
    yield jupad
    window.close()
    os.remove(file_path)

def test_execution(jupad: JupadTextEdit, qtbot: QtBot):
    qtbot.keyClicks(jupad, '1+1')
    qtbot.waitUntil(lambda: jupad.get_cell_out(0) == '2')
    qtbot.keyClick(jupad, Qt.Key_Enter)
    qtbot.keyClicks(jupad, 'print(1)')
    qtbot.waitUntil(lambda: jupad.get_cell_out(1) == '1')
