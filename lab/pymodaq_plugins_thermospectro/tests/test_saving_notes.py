"""The file of an experiment: where it is and what it is called, what the user wrote about the experiment, and the
observations noted while it runs"""
import datetime
import json
import os
import subprocess
import sys
import textwrap

import h5py
import pytest

from pymodaq_data import DataToExport

from test_saving import Experiment, attribute, h5saver, make_measurement, make_unit, titles  # noqa: F401  (h5saver is a fixture)
from pymodaq_plugins_thermospectro import saving
from pymodaq_plugins_thermospectro.saving import (INVALID_IN_NAMES, NOTE_BYTES, NOTES, NoteBundle, clean_name, cut_to_bytes,
                                                  folder_problem, new_file_path)

pytestmark = pytest.mark.filterwarnings('ignore')

NOW = datetime.datetime(2026, 10, 22, 15, 30, 12)


class TestCleanName:
    def test_a_good_name_is_left_alone(self):
        assert clean_name('Sample12') == 'Sample12'
        assert clean_name('Échantillon n°3 (rampe 2)') == 'Échantillon n°3 (rampe 2)'

    @pytest.mark.parametrize('bad', list('\\/:*?"<>|') + ['\n', '\t', '\x00'])
    def test_what_a_file_name_cannot_hold_becomes_an_underscore(self, bad):
        assert clean_name(f'a{bad}b') == 'a_b'

    def test_the_extension_is_dropped_whatever_its_case(self):
        assert clean_name('Run.h5') == clean_name('Run.H5') == clean_name('Run .h5 ') == 'Run'

    def test_nothing_left_is_the_default_name(self):
        assert clean_name('') == clean_name('   ') == clean_name('...') == clean_name('.h5') == 'Dataset'
        assert clean_name('', default='Log') == 'Log'

    def test_spaces_and_dots_around_the_name_go(self):
        assert clean_name('  my run. ') == 'my run'

    def test_the_pattern_of_the_forbidden_characters_is_one_character_wide(self):
        assert INVALID_IN_NAMES.pattern.startswith('[') and INVALID_IN_NAMES.pattern.endswith(']')


class TestNewFilePath:
    def test_the_date_and_the_time_are_in_the_name_by_default(self, tmp_path):
        assert new_file_path(tmp_path, 'Sample12', now=NOW) == tmp_path / 'Sample12_20261022_153012.h5'

    def test_without_date_it_is_the_name(self, tmp_path):
        assert new_file_path(tmp_path, 'Sample12', add_date=False, now=NOW) == tmp_path / 'Sample12.h5'

    def test_the_folders_of_the_year_and_of_the_day(self, tmp_path):
        path = new_file_path(tmp_path, 'Sample12', date_folders=True, now=NOW)

        assert path == tmp_path / '2026' / '20261022' / 'Sample12_20261022_153012.h5'

    def test_nothing_is_made(self, tmp_path):
        path = new_file_path(tmp_path / 'not' / 'there', 'Sample12', date_folders=True, now=NOW)

        assert not (tmp_path / 'not').exists() and not path.exists()

    def test_a_file_is_never_replaced(self, tmp_path):
        for expected in ('Sample12.h5', 'Sample12_001.h5', 'Sample12_002.h5', 'Sample12_003.h5'):
            path = new_file_path(tmp_path, 'Sample12', add_date=False, now=NOW)
            assert path == tmp_path / expected
            path.write_bytes(b'')

    def test_two_experiments_in_the_same_second_have_two_files(self, tmp_path):
        first = new_file_path(tmp_path, 'Sample12', now=NOW)
        first.write_bytes(b'')

        assert new_file_path(tmp_path, 'Sample12', now=NOW) == tmp_path / 'Sample12_20261022_153012_001.h5'

    def test_the_name_is_cleaned(self, tmp_path):
        assert new_file_path(tmp_path, 'a/b: c.h5', add_date=False) == tmp_path / 'a_b_ c.h5'

    def test_the_default_name_if_it_is_empty(self, tmp_path):
        assert new_file_path(tmp_path, '', add_date=False) == tmp_path / 'Dataset.h5'

    def test_the_folder_of_the_user_is_expanded(self, monkeypatch, tmp_path):
        monkeypatch.setenv('HOME', str(tmp_path))
        monkeypatch.setenv('USERPROFILE', str(tmp_path))

        assert new_file_path('~/data', 'Run', add_date=False) == tmp_path / 'data' / 'Run.h5'

    def test_it_is_the_time_of_now_if_none_is_given(self, tmp_path):
        before = datetime.datetime.now().replace(microsecond=0)
        stamp = new_file_path(tmp_path, 'Run').stem.split('_', 1)[1]

        assert before <= datetime.datetime.strptime(stamp, '%Y%m%d_%H%M%S') <= datetime.datetime.now()


class TestFolderProblem:
    def test_a_folder_which_is_there(self, tmp_path):
        assert folder_problem(tmp_path) is None

    def test_a_folder_which_will_be_made(self, tmp_path):
        assert folder_problem(tmp_path / 'a' / 'b' / 'c') is None
        assert not (tmp_path / 'a').exists()

    def test_a_file_is_not_a_folder(self, tmp_path):
        (tmp_path / 'file').write_text('x')

        assert 'is not a folder' in folder_problem(tmp_path / 'file')
        assert 'is not a folder' in folder_problem(tmp_path / 'file' / 'below')

    @pytest.mark.skipif(sys.platform == 'win32' or (hasattr(os, 'geteuid') and os.geteuid() == 0),
                        reason='a folder is always writable for the administrator, and is not made read-only on Windows')
    def test_a_folder_which_cannot_be_written(self, tmp_path):
        locked = tmp_path / 'locked'
        locked.mkdir()
        locked.chmod(0o500)
        try:
            assert 'nothing can be written' in folder_problem(locked)
            assert 'nothing can be written' in folder_problem(locked / 'new')
        finally:
            locked.chmod(0o700)

    def test_it_says_which_folder_is_the_problem(self, tmp_path):
        (tmp_path / 'file').write_text('x')

        assert str(tmp_path / 'file') in folder_problem(tmp_path / 'file' / 'below')


class TestCutToBytes:
    def test_a_short_text_is_left_alone(self):
        assert cut_to_bytes('Une observation') == 'Une observation'

    def test_a_long_text_is_cut(self):
        assert cut_to_bytes('a' * 10, 4) == 'aaaa'

    def test_a_character_is_never_cut_in_two(self):
        assert cut_to_bytes('é' * 10, 5) == 'éé'
        assert cut_to_bytes('😀' * 10, 7) == '😀'

    def test_a_thousand_characters_of_any_language_fit_in_the_file(self):
        for character in ('a', 'é', '€', '😀'):
            text = character * 1000

            assert len(text.encode('utf-8')) <= NOTE_BYTES and cut_to_bytes(text) == text


class TestPrepareFile:
    def metadata(self):
        return {'experiment_title': 'Ramp 1', 'sample': 'Pomme 12', 'operator': 'Émile', 'protocol': 'Chauffer à 2 °C/min',
                'application': 'thermo_spectro'}

    def test_the_file_is_made_where_it_is_told_with_its_folders(self, h5saver, tmp_path):
        path = tmp_path / 'deep' / 'er' / 'Sample12.h5'

        swmr = saving.prepare_file(h5saver, path=path)
        h5saver.close_file()

        assert swmr and path.is_file()
        assert h5saver.h5_file_path == path.parent and h5saver.h5_file_name == 'Sample12.h5'

    def test_what_the_user_wrote_is_in_the_attributes_of_the_raw_data(self, h5saver, tmp_path):
        path = tmp_path / 'Sample12.h5'

        saving.prepare_file(h5saver, path=path, metadata=self.metadata())
        h5saver.close_file()

        with h5py.File(path, 'r') as f:
            raw = f['RawData']
            assert {key: attribute(raw, key) for key in self.metadata()} == self.metadata()

    def test_the_title_of_the_group_is_not_replaced(self, h5saver, tmp_path):
        path = tmp_path / 'Sample12.h5'
        before = tmp_path / 'before.h5'
        saving.prepare_file(h5saver, path=before)
        h5saver.close_file()

        saving.prepare_file(h5saver, path=path, metadata=self.metadata())
        h5saver.close_file()

        with h5py.File(before, 'r') as f, h5py.File(path, 'r') as g:
            assert g['RawData'].attrs['TITLE'] == f['RawData'].attrs['TITLE']

    def test_a_long_protocol_in_any_language_comes_back_whole(self, h5saver, tmp_path):
        path = tmp_path / 'Sample12.h5'
        protocol = 'Rampe 20→80 °C à 2 °C/min — échantillon n°3 😀\n' * 6000  # (276 000 characters, 350 kB: not a small attribute)

        saving.prepare_file(h5saver, path=path, metadata={'protocol': protocol})
        h5saver.close_file()

        with h5py.File(path, 'r') as f:
            assert attribute(f['RawData'], 'protocol') == protocol

    def test_nothing_to_say_nothing_added(self, h5saver, tmp_path):
        path = tmp_path / 'Sample12.h5'

        saving.prepare_file(h5saver, path=path)
        h5saver.close_file()

        with h5py.File(path, 'r') as f:
            assert not {'sample', 'operator', 'protocol', 'application'} & set(f['RawData'].attrs)

    def test_without_a_path_the_h5saver_names_the_file(self, h5saver, tmp_path):
        assert saving.prepare_file(h5saver, metadata={'sample': 'x'})
        h5saver.close_file()

        files = list(tmp_path.rglob('*.h5'))
        assert len(files) == 1
        with h5py.File(files[0], 'r') as f:
            assert attribute(f['RawData'], 'sample') == 'x'


def notes_of(path):
    with h5py.File(path, 'r') as f:
        group = f['RawData/Datalogger000'][NOTES]
        return list(group['time'][:]), [text.decode('utf-8') for text in group['text'][:]]


class TestNotes:
    def add(self, experiment, epoch, text):
        experiment.saver.add_data_bundle(NoteBundle(dte=DataToExport('Note'), epoch=epoch, text=text))

    def test_the_group_is_made_with_the_experiment(self, h5saver, qapp):
        experiment = Experiment(h5saver, [make_unit()])
        assert experiment.saver.notes_ready
        experiment.finish()

        with h5py.File(experiment.path, 'r') as f:
            group = f['RawData/Datalogger000'][NOTES]
            assert group['time'].shape == (0,) and group['text'].shape == (0,)
            assert attribute(group, 'description')

    def test_there_are_no_notes_before_an_experiment_is_configured(self, h5saver, qapp):
        experiment = Experiment(h5saver, [make_unit()])
        assert experiment.saver.notes_ready

        experiment.saver.configure(experiment.recordings)  # (the next experiment: its file is not made yet)

        assert not experiment.saver.notes_ready
        experiment.finish()

    def test_the_time_and_the_text_of_each_observation_are_saved_in_order(self, h5saver, qapp):
        experiment = Experiment(h5saver, [make_unit()])

        self.add(experiment, 1000.5, 'The sample turned yellow')
        self.add(experiment, 1010.25, 'Door opened')
        self.add(experiment, 1012., 'Door closed')
        experiment.finish()

        assert notes_of(experiment.path) == ([1000.5, 1010.25, 1012.], ['The sample turned yellow', 'Door opened',
                                                                         'Door closed'])

    def test_any_language(self, h5saver, qapp):
        experiment = Experiment(h5saver, [make_unit()])

        self.add(experiment, 1., 'Échantillon n°3 : couleur → brun 😀 (湿度 40 %)')
        experiment.finish()

        assert notes_of(experiment.path)[1] == ['Échantillon n°3 : couleur → brun 😀 (湿度 40 %)']

    def test_a_note_which_is_too_long_is_cut_but_not_in_the_middle_of_a_character(self, h5saver, qapp):
        experiment = Experiment(h5saver, [make_unit()])

        self.add(experiment, 1., '€' * 2000)  # (3 bytes each)
        experiment.finish()

        text = notes_of(experiment.path)[1][0]
        assert text == '€' * (NOTE_BYTES // 3) and len(text.encode('utf-8')) <= NOTE_BYTES

    def test_a_note_is_not_data_it_does_not_start_swmr_and_is_not_an_error(self, h5saver, qapp):
        experiment = Experiment(h5saver, [make_unit()])

        self.add(experiment, 1., 'before any data')

        assert not experiment.saver.swmr_started and experiment.saver.n_errors == 0
        experiment.finish()

    def test_notes_are_saved_once_the_file_is_in_swmr_mode_too(self, h5saver, qapp):
        unit = make_unit()
        experiment = Experiment(h5saver, [unit])
        self.add(experiment, 1000., 'before the data')
        for index in range(3):
            experiment.write(unit, index)
        assert experiment.saver.swmr_started and h5saver.is_swmr_active

        self.add(experiment, 1001.5, 'during the experiment')
        experiment.write(unit, 3)
        self.add(experiment, 1003.5, 'later')
        experiment.finish()

        assert notes_of(experiment.path) == ([1000., 1001.5, 1003.5], ['before the data', 'during the experiment',
                                                                      'later'])
        with h5py.File(experiment.path, 'r') as f:  # (and the data went on)
            assert titles(f['RawData/Datalogger000'])['Unit1_Temperature']['Data0D/CH00/EnlData00'].shape[0] == 4

    READER = textwrap.dedent('''
        import h5py, json, sys
        file = h5py.File(sys.argv[1], 'r', swmr=True)
        group = file['RawData/Datalogger000/Notes']
        group['time'].refresh()
        group['text'].refresh()
        print(json.dumps([list(group['time'][:]), [t.decode('utf-8') for t in group['text'][:]]]))
    ''')

    def read_from_another_process(self, path):
        result = subprocess.run([sys.executable, '-c', self.READER, str(path)], capture_output=True, text=True,
                                timeout=120)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    def test_another_program_reads_the_observations_while_the_experiment_runs(self, h5saver, qapp):
        unit = make_unit()
        experiment = Experiment(h5saver, [unit])
        for index in range(3):
            experiment.write(unit, index)
        assert experiment.saver.swmr_started

        self.add(experiment, 5., 'first')

        assert self.read_from_another_process(experiment.path) == [[5.], ['first']]

        self.add(experiment, 6., 'second')

        assert self.read_from_another_process(experiment.path) == [[5., 6.], ['first', 'second']]
        experiment.finish()

    @pytest.mark.errors_expected
    def test_a_note_which_cannot_be_saved_is_counted_as_an_error(self, h5saver, qapp):
        experiment = Experiment(h5saver, [make_unit()])
        experiment.saver._notes = None  # (a file made by another backend than h5py)

        self.add(experiment, 1., 'lost')

        assert experiment.saver.n_errors == 1 and not experiment.saver.add_note(1., 'lost')
        experiment.finish()

    @pytest.mark.errors_expected
    def test_a_broken_file_does_not_raise_in_the_saver_thread(self, h5saver, qapp):
        experiment = Experiment(h5saver, [make_unit()])
        experiment.h5saver.close_file()  # (the arrays are closed: writing in them raises)

        self.add(experiment, 1., 'too late')  # (it must not raise: the saver thread counts what it did)

        assert experiment.saver.n_errors == 1

    def test_the_data_are_not_affected_by_the_notes(self, h5saver, qapp):
        unit = make_unit()
        experiment = Experiment(h5saver, [unit])
        for index in range(6):
            experiment.write(unit, index)
            self.add(experiment, 1000. + index, f'note {index}')
        experiment.finish()

        with h5py.File(experiment.path, 'r') as f:
            times = titles(f['RawData/Datalogger000'])['Unit1_Temperature']['NavAxes/Axis00'][:]
            assert list(times) == [1000. + index for index in range(6)]
        assert notes_of(experiment.path)[0] == [1000. + index for index in range(6)]
