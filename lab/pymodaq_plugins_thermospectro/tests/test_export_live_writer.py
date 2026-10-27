"""lab_export must never disturb the experiment which is writing the file it reads

On Windows a program which opens an h5 file with the locks of HDF5 makes the *writes* of the program which writes it fail
(``PermissionError: Driver write request failed``), and the HDF5 of the experiment is then left in a bad state (``slist
already enabled?``). The tests here run a real writer in SWMR mode, as the extension does, and count what it says while the
exporter reads its file in another process.
"""
import subprocess
import sys

import h5py
import numpy as np
import pytest

from pymodaq_plugins_thermospectro import export

pytestmark = pytest.mark.filterwarnings('ignore')

WRITER = r'''
import sys, time, h5py, numpy as np
path, delay, seconds = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
f = h5py.File(path, 'w', libver='latest')
d = f.create_dataset('x', shape=(0, 400), maxshape=(None, 400), chunks=(32, 100), dtype='f8')
f.flush()
print('created', flush=True)
time.sleep(delay)
f.swmr_mode = True
print('swmr', flush=True)
errors, rows, began = [], 0, time.time()
while time.time() - began < seconds:
    try:
        d.resize(rows + 1, axis=0); d[rows] = np.random.rand(400); d.flush(); f.flush()
        rows += 1
    except BaseException as e:
        errors.append(type(e).__name__)
    time.sleep(0.02)
f.close()
print('done', rows, len(errors), flush=True)
'''

READER = r'''
import sys, time
from pathlib import Path
from pymodaq_plugins_thermospectro.export import open_file
began = time.monotonic()
f = open_file(Path(sys.argv[1]), float(sys.argv[2]))
print('opened', round(time.monotonic() - began, 1), f['x'].shape[0], flush=True)
time.sleep(float(sys.argv[3]))  # (the file stays open: this is when the writes of a locked file fail)
f.close()
'''


def start_writer(path, delay, seconds):
    process = subprocess.Popen([sys.executable, '-c', WRITER, str(path), str(delay), str(seconds)],
                               stdout=subprocess.PIPE, text=True)
    assert process.stdout.readline().strip() == 'created'
    return process


def finish(process):
    out, _ = process.communicate(timeout=60)
    done = [line for line in out.splitlines() if line.startswith('done')][0].split()
    return int(done[1]), int(done[2])  # rows written, errors


def read(path, wait=30, hold=3):
    return subprocess.run([sys.executable, '-c', READER, str(path), str(wait), str(hold)], capture_output=True, text=True,
                          timeout=120)


class TestTheWriterIsNotDisturbed:
    def test_reading_a_file_which_is_being_written_makes_no_write_fail(self, tmp_path):
        path = tmp_path / 'running.h5'
        writer = start_writer(path, delay=0, seconds=9)

        reader = read(path, hold=4)

        rows, errors = finish(writer)
        assert reader.stdout.startswith('opened'), reader.stderr
        assert errors == 0 and rows > 50

    def test_nor_does_reading_it_again_and_again(self, tmp_path):
        path = tmp_path / 'running.h5'
        writer = start_writer(path, delay=0, seconds=12)

        for _ in range(3):
            assert read(path, hold=1).stdout.startswith('opened')

        assert finish(writer)[1] == 0

    def test_a_reader_which_waits_for_the_first_seconds_does_not_disturb_the_experiment_either(self, tmp_path):
        path = tmp_path / 'running.h5'
        writer = start_writer(path, delay=4, seconds=6)  # (SWMR is not on for the first 4 s)

        reader = read(path, wait=30, hold=2)

        rows, errors = finish(writer)
        kind, seconds, _ = reader.stdout.split()[:3]
        assert kind == 'opened' and float(seconds) >= 1.0  # (it waited for SWMR: the file was not readable before)
        assert errors == 0 and rows > 20


class TestWhatTheHeaderOfTheFileSays:
    @pytest.fixture
    def path(self, tmp_path):
        return tmp_path / 'file.h5'

    def make(self, path, **kwargs):
        file = h5py.File(path, 'w', **kwargs)
        file.create_dataset('x', shape=(0,), maxshape=(None,), chunks=(16,), dtype='f8')
        file.flush()
        return file

    def test_a_file_in_swmr_mode_says_so(self, path):
        file = self.make(path, libver='latest')
        file.swmr_mode = True
        try:
            assert export.writer_state(path) == 'swmr'
        finally:
            file.close()

    def test_a_closed_file_says_so(self, path):
        self.make(path, libver='latest').close()

        assert export.writer_state(path) == 'closed'

    def test_a_file_which_does_not_say_is_not_guessed(self, path, tmp_path):
        self.make(path).close()  # (the old format has no flags)
        junk = tmp_path / 'junk.h5'
        junk.write_bytes(b'this is not an h5 file')

        assert export.writer_state(path) is None and export.writer_state(junk) is None

    def test_a_file_which_cannot_be_read_now_is_busy(self, tmp_path, monkeypatch):
        def refuses(*args, **kwargs):
            raise PermissionError(13, 'locked')

        monkeypatch.setattr('builtins.open', refuses)

        assert export.writer_state(tmp_path / 'file.h5') == 'busy'

    def test_a_live_file_is_never_opened_with_the_locks(self, path, monkeypatch):
        writer = start_writer(path, delay=0, seconds=6)
        assert writer.stdout.readline().strip() == 'swmr'
        asked = []
        real = h5py.File

        def spy(name, mode='r', **kwargs):
            asked.append(kwargs)
            return real(name, mode, **kwargs)

        monkeypatch.setattr(h5py, 'File', spy)
        try:
            export.open_file(path).close()
        finally:
            monkeypatch.setattr(h5py, 'File', real)
            writer.communicate(timeout=60)

        assert asked and all(kwargs.get('locking') is False for kwargs in asked)
