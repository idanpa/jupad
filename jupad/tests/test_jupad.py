
import os
import shutil
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
    tmp_dir = tempfile.mkdtemp(prefix='jupad_')
    window = MainWindow(file_path=os.path.join(tmp_dir,'jupad.py'))
    jupad = window.jupad_text_edit
    jupad.log.addHandler(LogHandler())
    qtbot.waitUntil(lambda: jupad.kernel_info != '', timeout=5000)
    jupad.insert_cell(0)
    jupad.remove_cells(1, jupad.table.rows()-1)
    yield jupad
    window.close()
    shutil.rmtree(tmp_dir)

def test_execution(jupad: JupadTextEdit, qtbot: QtBot):
    qtbot.keyClicks(jupad, '1+1')
    qtbot.waitUntil(lambda: jupad.get_cell_out(0) == '2')
    qtbot.keyClick(jupad, Qt.Key_Enter)
    qtbot.keyClicks(jupad, 'print(1)')
    qtbot.waitUntil(lambda: jupad.get_cell_out(1) == '1')

test_file_content = '''# %%
0
# %%
1
1
# %%
2
'''

def test_file_load_save(jupad: JupadTextEdit, qtbot: QtBot):
    orig_file_path = jupad.file.name
    file_path = os.path.join(os.path.dirname(orig_file_path), 'test_file_load_save.py')
    with open(file_path, 'w') as f:
        f.write(test_file_content)
    jupad.open_file(file_path)
    assert jupad.table.rows() == 3
    assert jupad.get_cell_code(0) == '0'
    assert jupad.get_cell_code(1) == '1\n1'
    assert jupad.get_cell_code(2) == '2'
    jupad.insert_cell(3)
    qtbot.keyClicks(jupad, '3')
    qtbot.waitUntil(lambda: jupad.get_cell_out(3) == '3')
    jupad.open_file(orig_file_path)
    with open(file_path, 'r') as f:
        assert f.read() == test_file_content + '# %%\n3\n'
