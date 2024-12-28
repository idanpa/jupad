
import os
import pytest

from pypad import MainWindow, PyPadTextEdit

@pytest.fixture
def pypad(qtbot):
    # todo: clear all cells (e.g. if loaded from file)
    os.environ['PYPAD_SCRIPT'] = ''
    window = MainWindow()
    pypad = window.pypad_text_edit
    qtbot.waitUntil(lambda: pypad.kernel_info != '', timeout=5000)
    yield pypad
    window.close()

def test_execution(pypad: PyPadTextEdit, qtbot):
    qtbot.keyClicks(pypad, '1+1')
    qtbot.wait(2000)
    assert pypad.get_cell_out(0) == '2'
