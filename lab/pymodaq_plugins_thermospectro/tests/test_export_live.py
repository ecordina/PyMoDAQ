"""``lab_export`` on the file of an experiment which is running: it is read in SWMR mode on every system, the first seconds
(the file is not readable yet) and the last moments (it is being closed) are waited for, and the text says whether the file
is complete"""
import subprocess
import sys
import time
from types import SimpleNamespace

import h5py
import numpy as np
import pytest

from pymodaq_plugins_thermospectro import export

from test_export import read_csv, write_experiment
from test_saving import Experiment, h5saver, make_unit  # noqa: F401  (h5saver is a fixture)

pytestmark = pytest.mark.filterwarnings('ignore')

BUSY = BlockingIOError(11, "Unable to synchronously open file (unable to lock file, errno = 11, error message = "
                           "'Resource temporarily unavailable')")


class Opener:
    """A stand-in for h5py.File: what each way of opening answers (an exception, or the file)"""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, path, mode='r', **kwargs):
        self.calls.append((mode, kwargs))
        answer = self.answers.pop(0) if self.answers else self.answers_left
        if isinstance(answer, BaseException):
            raise answer
        return answer

    answers_left = BUSY


@pytest.fixture
def an_existing_file(tmp_path):
    path = tmp_path / 'file.h5'
    path.write_bytes(b'not read: h5py is replaced')
    return path


class TestHowTheFileIsOpened:
    """The order: SWMR with the locks of the library, SWMR without them (Windows), then as an ordinary file"""

    def test_swmr_is_asked_for_first(self, monkeypatch, an_existing_file):
        opener = Opener('the file')
        monkeypatch.setattr(h5py, 'File', opener)

        assert export.open_file(an_existing_file) == 'the file'
        assert opener.calls == [('r', {'swmr': True})]

    def test_the_modes_in_their_order(self):
        assert export.OPEN_MODES == ({'swmr': True}, {'swmr': True, 'locking': False}, {})

    def test_when_the_locks_of_the_library_refuse_swmr_is_asked_for_again_without_them(self, monkeypatch, an_existing_file):
        opener = Opener(BUSY, 'the file')
        monkeypatch.setattr(h5py, 'File', opener)

        assert export.open_file(an_existing_file) == 'the file'
        assert opener.calls == [('r', {'swmr': True}), ('r', {'swmr': True, 'locking': False})]

    def test_a_file_which_is_not_in_swmr_mode_is_read_as_an_ordinary_one(self, monkeypatch, an_existing_file):
        opener = Opener(OSError('swmr read access flag not allowed'), OSError('swmr read access flag not allowed'),
                        'the file')
        monkeypatch.setattr(h5py, 'File', opener)

        assert export.open_file(an_existing_file) == 'the file'
        assert opener.calls[-1] == ('r', {})

    def test_an_h5py_which_does_not_know_the_locking_option_goes_on(self, monkeypatch, an_existing_file):
        opener = Opener(BUSY, TypeError("unexpected keyword argument 'locking'"), 'the file')
        monkeypatch.setattr(h5py, 'File', opener)

        assert export.open_file(an_existing_file) == 'the file'

    def test_a_busy_file_is_told_clearly(self, monkeypatch, an_existing_file):
        monkeypatch.setattr(h5py, 'File', Opener())

        with pytest.raises(export.FileBusy) as error:
            export.open_file(an_existing_file, wait=0)

        text = str(error.value)
        assert 'a running experiment is creating it' in text and 'closing it' in text and 'Try again' in text
        assert 'unable to lock file' in text  # (what HDF5 said is kept)
        assert isinstance(error.value, OSError)

    def test_it_waits_for_a_file_which_is_being_created(self, monkeypatch, an_existing_file, capsys):
        opener = Opener(*([BUSY] * 6), 'the file')  # (two rounds of three modes, then the file)
        monkeypatch.setattr(h5py, 'File', opener)
        monkeypatch.setattr(export, 'time', SimpleNamespace(monotonic=time.monotonic, sleep=lambda seconds: None))

        assert export.open_file(an_existing_file, wait=30) == 'the file'

        assert len(opener.calls) == 7
        assert capsys.readouterr().err.count('waiting for it') == 1  # (told once, not at each attempt)

    def test_it_gives_up_after_the_time_it_was_given(self, monkeypatch, an_existing_file):
        clock = iter(float(second) for second in range(1000))
        monkeypatch.setattr(export, 'time', SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda seconds: None))
        monkeypatch.setattr(h5py, 'File', Opener())

        with pytest.raises(export.FileBusy):
            export.open_file(an_existing_file, wait=5)

    def test_a_file_which_is_not_the_one_of_an_experiment_is_not_waited_for(self, monkeypatch, an_existing_file):
        monkeypatch.setattr(h5py, 'File', Opener(*([OSError('file signature not found')] * 9)))
        monkeypatch.setattr(export, 'time', SimpleNamespace(
            monotonic=time.monotonic, sleep=lambda seconds: pytest.fail('it waited for a file which is not busy')))

        with pytest.raises(OSError, match='signature') as error:
            export.open_file(an_existing_file, wait=30)

        assert not isinstance(error.value, export.FileBusy)

    def test_there_is_nothing_to_wait_for_if_there_is_no_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            export.open_file(tmp_path / 'no_such_file.h5', wait=30)

    @pytest.mark.parametrize('error, busy', [
        (BlockingIOError(11, 'x'), True), (OSError('Unable to open file (unable to lock file, errno = 33)'), True),
        (OSError('file is already open for write (may use <h5clear file>)'), True),
        (OSError('file is not already open for SWMR writing'), True), (OSError('file signature not found'), False),
        (OSError('bad superblock version number'), False), (OSError('swmr read access flag not allowed'), False),
        (OSError('no such file'), False)])
    def test_what_says_that_a_file_is_being_written(self, error, busy):
        assert export.looks_busy(error) is busy


class TestTheOptionToWait:
    def test_it_waits_a_few_seconds_unless_told_otherwise(self):
        assert export.parser().parse_args(['file.h5']).wait == export.WAIT == 10.0
        assert export.parser().parse_args(['file.h5', '--wait', '0']).wait == 0.0
        assert export.parser().parse_args(['file.h5', '--wait', '2.5']).wait == 2.5

    def test_a_busy_file_is_an_error_message_and_not_a_traceback(self, monkeypatch, an_existing_file, capsys):
        monkeypatch.setattr(h5py, 'File', Opener())

        status = export.main([str(an_existing_file), '--wait', '0'])

        assert status == 1
        assert 'a running experiment is creating it' in capsys.readouterr().err


def fake_file(**attrs):
    return SimpleNamespace(attrs=attrs)


def units_with(times):
    series = export.Series('ROIs', 'Unit1_ROIs', np.asarray(times, dtype=float), [np.zeros((len(times), 1))], ['a'])
    return {1: export.UnitData(1, {'ROIs': series})}


class TestWhetherTheFileIsComplete:
    NOW = 100000.0

    def test_a_file_which_was_closed_is_complete(self):
        assert export.file_status(fake_file(swmr_active=False), units_with([1, 2, 3]), self.NOW) == ('complete', '')

    def test_so_is_a_file_which_does_not_say(self):
        assert export.file_status(fake_file(), units_with([1, 2, 3]), self.NOW) == ('complete', '')

    def test_an_experiment_which_writes_is_running(self):
        times = [self.NOW - 12, self.NOW - 11, self.NOW - 10, self.NOW - 2]

        state, text = export.file_status(fake_file(swmr_active=True), units_with(times), self.NOW)

        assert state == 'running' and 'still running' in text and '2 s ago' in text and 'saved so far' in text

    def test_a_file_which_says_it_is_written_and_has_not_been_for_a_long_time_was_not_closed(self):
        start = self.NOW - 3 * 3600 - 31 * 60 - 12
        times = np.linspace(start, start + 12000, 12001)  # (a readout a second for 3 h 20 min, the last one 11 min ago)

        state, text = export.file_status(fake_file(swmr_active=True), units_with(times), self.NOW)

        assert state == 'abnormal' and 'was not closed' in text and 'stopped abnormally after 3:20:00' in text
        assert 'everything which was saved is converted' in text

    def test_a_slow_experiment_is_not_taken_for_a_dead_one(self):
        times = [self.NOW - 700, self.NOW - 640, self.NOW - 580, self.NOW - 520, self.NOW - 100]  # (a readout a minute)

        assert export.file_status(fake_file(swmr_active=True), units_with(times), self.NOW)[0] == 'running'

    def test_an_experiment_which_has_saved_nothing_yet(self):
        state, text = export.file_status(fake_file(swmr_active=True), units_with([]), self.NOW)

        assert state == 'running' and 'nothing is saved yet' in text

    def test_the_interval_between_two_readouts(self):
        units = units_with([0, 1, 2, 3, 4])
        units[2] = export.UnitData(2, {'Spectrum': export.Series('Spectrum', 'Unit2_Spectrum', np.array([0., 60., 120.]),
                                                                  [np.zeros((3, 4))], ['s'])})

        assert export.typical_interval(units) == 1.0 and export.typical_interval({}) == 0.0


@pytest.fixture
def running(h5saver, qapp):
    """The file of an experiment which is running: written by this process, in SWMR mode, and not closed"""
    unit = make_unit(integration=True)
    experiment = Experiment(h5saver, [unit])
    now = time.time()
    for index in range(4):
        experiment.write(unit, index, t0=now - 4.0, integration=20.)
    assert experiment.saver.swmr_started
    yield experiment, unit, now
    try:
        experiment.finish()
    except Exception:  # (the test closed it)
        pass


def convert(path, out, *arguments):
    """lab_export as a command of its own, as the user runs it while the experiment goes on"""
    return subprocess.run([sys.executable, export.__file__, str(path), '-o', str(out), '--wait', '0', *arguments],
                          capture_output=True, text=True, timeout=120)


class TestTheFileOfAnExperimentWhichIsRunning:
    def test_it_is_converted_while_it_is_written(self, running, tmp_path):
        experiment, unit, now = running

        done = convert(experiment.path, tmp_path / 'live')

        assert done.returncode == 0, done.stderr
        assert 'still running' in done.stdout
        header, rows = read_csv(next((tmp_path / 'live').glob('*_spectrometer1.csv')))
        assert len(rows) >= 3 and header[0] == 'time (s)'

    def test_the_text_says_it_is_what_was_saved_so_far(self, running, tmp_path):
        experiment, unit, now = running

        convert(experiment.path, tmp_path / 'live')

        text = next((tmp_path / 'live').glob('*_experiment.txt')).read_text(encoding='utf-8-sig')
        assert 'Status: the experiment is still running' in text

    def test_listing_what_it_has_says_so_too(self, running, tmp_path):
        experiment, unit, now = running

        done = subprocess.run([sys.executable, export.__file__, str(experiment.path), '--list', '--wait', '0'],
                              capture_output=True, text=True, timeout=120)

        assert done.returncode == 0 and 'still running' in done.stdout and 'ROIs (' in done.stdout

    def test_the_file_is_the_same_once_the_experiment_is_over_with_all_the_rows(self, running, tmp_path):
        experiment, unit, now = running
        for index in range(4, 9):
            experiment.write(unit, index, t0=now - 4.0, integration=20.)
        experiment.finish()

        done = convert(experiment.path, tmp_path / 'done')

        assert done.returncode == 0, done.stderr
        assert 'still running' not in done.stdout and 'not closed' not in done.stdout
        text = next((tmp_path / 'done').glob('*_experiment.txt')).read_text(encoding='utf-8-sig')
        assert 'Status:' not in text
        assert len(read_csv(next((tmp_path / 'done').glob('*_spectrometer1.csv')))[1]) == 9

    def test_what_was_saved_is_not_disturbed_by_the_reader(self, running, tmp_path):
        experiment, unit, now = running
        convert(experiment.path, tmp_path / 'live')

        for index in range(4, 9):
            experiment.write(unit, index, t0=now - 4.0, integration=20.)
        experiment.finish()

        with h5py.File(experiment.path, 'r') as f:
            assert len(export.read_experiment(f)[1].series['ROIs']) == 9


class TestAFileWhichWasNotClosed:
    def test_it_is_converted_and_says_it_was_not_closed(self, h5saver, qapp, tmp_path):
        path = write_experiment(h5saver, camera=False)  # (its times are of 1970: nothing was written for a long time)
        with h5py.File(path, 'r+') as f:
            f.attrs['swmr_active'] = True  # (what a crashed experiment leaves)

        done = convert(path, tmp_path / 'crashed')

        assert done.returncode == 0, done.stderr
        assert 'the file was not closed' in done.stdout and 'stopped abnormally after' in done.stdout
        text = next((tmp_path / 'crashed').glob('*_experiment.txt')).read_text(encoding='utf-8-sig')
        assert 'Status: the file was not closed' in text
        assert len(read_csv(next((tmp_path / 'crashed').glob('*_spectrometer1.csv')))[1]) == 6


WRITER = r'''
import os, sys, time, h5py
path, delay, stop = sys.argv[1], float(sys.argv[2]), sys.argv[3]
f = h5py.File(path, 'w', libver='latest')
d = f.create_group('RawData').create_dataset('x', shape=(0,), maxshape=(None,), dtype='f8', chunks=(16,))
f.flush()
print('created', flush=True)
time.sleep(delay)
f.attrs['swmr_active'] = True
f.swmr_mode = True
print('swmr', flush=True)
n = 0
while not os.path.exists(stop):
    d.resize((n + 1,)); d[n] = n; d.flush(); n += 1
    time.sleep(0.05)
f.attrs['swmr_active'] = False
f.close()
'''

READER = r'''
import importlib.util, sys, time
spec = importlib.util.spec_from_file_location('lab_export_module', sys.argv[1])
export = importlib.util.module_from_spec(spec)
sys.modules['lab_export_module'] = export
spec.loader.exec_module(export)
began = time.monotonic()
try:
    f = export.open_file(sys.argv[2], float(sys.argv[3]))
    rows = f['RawData/x'].shape[0]
    f.close()
    print('opened', round(time.monotonic() - began, 1), rows)
except Exception as e:
    print(type(e).__name__, str(e)[:300])
'''


def read_in_another_process(path, wait):
    done = subprocess.run([sys.executable, '-c', READER, export.__file__, str(path), str(wait)], capture_output=True,
                          text=True, timeout=120)
    return done.stdout.strip() or done.stderr.strip()


@pytest.fixture
def writer(tmp_path):
    started = []

    def start(delay):
        path, stop = tmp_path / 'busy.h5', tmp_path / 'stop'
        process = subprocess.Popen([sys.executable, '-c', WRITER, str(path), str(delay), str(stop)],
                                   stdout=subprocess.PIPE, text=True)
        started.append((process, stop))
        assert process.stdout.readline().strip() == 'created'
        return process, path

    yield start
    for process, stop in started:
        stop.write_text('x')
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()


class TestTheFirstSecondsAndTheLastMoments:
    def test_a_file_which_is_just_created_cannot_be_read_and_it_says_so(self, writer, tmp_path):
        process, path = writer(30)  # (SWMR is not on: the first seconds of an experiment)

        answer = read_in_another_process(path, 0)

        assert answer.startswith('FileBusy') and 'a running experiment is creating it' in answer

    def test_it_waits_until_the_experiment_makes_the_file_readable(self, writer, tmp_path):
        process, path = writer(5)  # (the reader needs a moment to start: it still has to wait)

        answer = read_in_another_process(path, 30)

        kind, seconds, rows = answer.split()
        assert kind == 'opened' and 0.1 < float(seconds) < 25.0 and int(rows) >= 0

    def test_a_file_in_swmr_mode_is_read_at_once(self, writer, tmp_path):
        process, path = writer(0)
        assert process.stdout.readline().strip() == 'swmr'
        time.sleep(0.3)

        answer = read_in_another_process(path, 0)

        kind, seconds, rows = answer.split()
        assert kind == 'opened' and float(seconds) < 2.0 and int(rows) >= 1

    def test_a_plain_open_of_that_file_is_refused_by_hdf5_and_swmr_is_why_it_works(self, writer, tmp_path):
        process, path = writer(0)
        assert process.stdout.readline().strip() == 'swmr'
        time.sleep(0.3)

        with pytest.raises(OSError):
            h5py.File(path, 'r')  # (what the old way of opening did first: refused)
        with export.open_file(path) as f:  # (the new one: SWMR)
            assert f['RawData/x'].shape[0] >= 1
