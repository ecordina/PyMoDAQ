"""The Saving window of the extension, on a Dashboard with Mock modules: where and how an experiment is saved, what is said
about it, and the observations noted while it runs"""
import json
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import h5py
import pytest
from qtpy import QtCore

from pymodaq_gui.utils.custom_app import WorkFlowActions

from pymodaq_plugins_thermospectro import configurations
from pymodaq_plugins_thermospectro.extensions import thermo_spectro
from pymodaq_plugins_thermospectro.saving import NOTES
from pymodaq_plugins_thermospectro.saving_panel import SavingStore

from test_extension import SLOW, H5Experiment, _text, end_run, run, wait_for

pytestmark = pytest.mark.filterwarnings('ignore')


def files_in(folder) -> list[Path]:
    return sorted(Path(folder).rglob('*.h5'))


def only_file(folder) -> Path:
    files = files_in(folder)
    assert len(files) == 1, files
    return files[0]


def notes_in(path) -> tuple[list, list]:
    """The time and the text of the observations of a file"""
    with H5Experiment(None, file=path) as experiment:
        group = experiment.group[NOTES]
        return list(group['time'][:]), [text.decode('utf-8') for text in group['text'][:]]


def type_in(qtbot, edit, text):
    edit.clear()
    qtbot.keyClicks(edit, text)


def set_folder(ext, folder, **values):
    ext.saving_panel.set_values({'folder': str(folder), **values})


class TestTheWindow:
    def test_it_is_the_saving_window_with_the_settings_of_the_h5_file_inside(self, ext):
        panel = ext.saving_panel

        assert ext.docks['saving'].widgets == [panel]
        assert panel.advanced is ext.h5_manager.get_h5saver(create_new_file=False).settings_tree
        assert not panel.advanced_box.isChecked()  # (it is closed: the folder and the name are what is looked for)

    def test_the_folder_and_the_name_are_given_to_the_h5saver_too(self, ext, qtbot, tmp_path):
        settings = ext.h5_manager.get_h5saver(create_new_file=False).settings

        type_in(qtbot, ext.saving_panel.folder, str(tmp_path / 'elsewhere'))
        type_in(qtbot, ext.saving_panel.name, 'Pomme 12')

        assert settings['base_path'] == str(tmp_path / 'elsewhere') and settings['base_name'] == 'Pomme 12'

    def test_the_name_given_to_it_is_a_name_a_file_can_have(self, ext, qtbot):
        type_in(qtbot, ext.saving_panel.name, 'a/b')

        assert ext.h5_manager.get_h5saver(create_new_file=False).settings['base_name'] == 'a_b'

    def test_the_window_says_when_nothing_is_saved(self, ext):
        panel = ext.saving_panel
        assert panel.logging_off.isHidden()

        ext.get_action(WorkFlowActions.LOG).trigger()  # (Do logging, in the toolbar)
        assert not panel.logging_off.isHidden()

        ext.get_action(WorkFlowActions.LOG).trigger()
        assert panel.logging_off.isHidden()


class TestTheFile:
    def test_it_is_where_the_window_says_with_the_name_it_says(self, ext, qtbot, tmp_path):
        set_folder(ext, tmp_path / 'chosen' / 'deeper', name='Pomme 12', add_date=False)

        run(qtbot, ext, **SLOW)
        end_run(qtbot, ext)

        assert files_in(tmp_path) == [tmp_path / 'chosen' / 'deeper' / 'Pomme 12.h5']
        assert ext.current_file == tmp_path / 'chosen' / 'deeper' / 'Pomme 12.h5'
        assert str(ext.current_file) in ext.saving_panel.current.text()

    def test_the_date_and_the_time_are_in_the_name_by_default(self, ext, qtbot, tmp_path):
        set_folder(ext, tmp_path / 'data', name='Pomme')

        run(qtbot, ext, **SLOW)
        end_run(qtbot, ext)

        path = only_file(tmp_path / 'data')
        assert path.parent == tmp_path / 'data' and re.fullmatch(r'Pomme_\d{8}_\d{6}\.h5', path.name)

    def test_the_folders_of_the_year_and_the_day_if_asked(self, ext, qtbot, tmp_path):
        set_folder(ext, tmp_path / 'data', name='Pomme', date_folders=True)

        run(qtbot, ext, **SLOW)
        end_run(qtbot, ext)

        path = only_file(tmp_path / 'data')
        assert re.fullmatch(r'\d{4}', path.parent.parent.name) and re.fullmatch(r'\d{8}', path.parent.name)
        assert path.parent.name.startswith(path.parent.parent.name)

    def test_nothing_is_ever_replaced(self, ext, qtbot, tmp_path):
        set_folder(ext, tmp_path / 'data', name='Pomme', add_date=False)

        for _ in range(3):
            run(qtbot, ext, **SLOW)
            end_run(qtbot, ext)

        assert [path.name for path in files_in(tmp_path / 'data')] == ['Pomme.h5', 'Pomme_001.h5', 'Pomme_002.h5']

    def test_the_name_can_be_changed_between_two_experiments(self, ext, qtbot, tmp_path):
        set_folder(ext, tmp_path / 'data', name='First', add_date=False)
        run(qtbot, ext, **SLOW)
        end_run(qtbot, ext)

        set_folder(ext, tmp_path / 'other', name='Second', add_date=True)
        run(qtbot, ext, **SLOW)
        end_run(qtbot, ext)

        assert (tmp_path / 'data' / 'First.h5').is_file()
        assert re.fullmatch(r'Second_\d{8}_\d{6}\.h5', only_file(tmp_path / 'other').name)

    def test_the_file_is_in_swmr_mode_as_before(self, ext, qtbot, tmp_path):
        set_folder(ext, tmp_path / 'data', name='Pomme', add_date=False)

        run(qtbot, ext, **SLOW)
        swmr = ext._swmr
        end_run(qtbot, ext)

        assert swmr
        with h5py.File(tmp_path / 'data' / 'Pomme.h5', 'r') as f:
            assert _text(f.attrs['swmr_compatible']) in (True, 'True')

    def test_the_file_and_the_experiment_cannot_be_changed_during_the_run(self, ext, qtbot, tmp_path):
        panel = ext.saving_panel
        set_folder(ext, tmp_path / 'data', name='Pomme', add_date=False)

        run(qtbot, ext, **SLOW)
        try:
            assert not panel.folder.isEnabled() and not panel.name.isEnabled() and panel.protocol.isReadOnly()
            assert not panel.advanced_box.isEnabled()
        finally:
            end_run(qtbot, ext)
        assert panel.folder.isEnabled() and panel.name.isEnabled() and not panel.protocol.isReadOnly()

    def test_the_folder_is_checked_before_the_start(self, ext, qtbot, tmp_path):
        (tmp_path / 'a file').write_text('x')
        set_folder(ext, tmp_path / 'a file' / 'below')
        ext.docks['settings'].raiseDock()

        ext.start_run()

        assert not ext.run_active and not ext._pending_start and not ext.monitoring
        title, text, error = ext.notifications[-1]
        assert title == 'Cannot start' and 'The data cannot be saved' in text and 'is not a folder' in text
        assert 'Choose another folder in the Saving window' in text
        assert ext.get_action(WorkFlowActions.START).isEnabled() and not ext.get_action(WorkFlowActions.STOP).isEnabled()
        assert ext.docks['saving'].container().stack.currentWidget() is ext.docks['saving']  # (it is shown)
        assert files_in(tmp_path) == []

    def test_it_starts_once_the_folder_is_right(self, ext, qtbot, tmp_path):
        (tmp_path / 'a file').write_text('x')
        set_folder(ext, tmp_path / 'a file' / 'below')
        ext.start_run()
        assert not ext.run_active

        set_folder(ext, tmp_path / 'fine')
        run(qtbot, ext, **SLOW)
        end_run(qtbot, ext)

        assert len(files_in(tmp_path / 'fine')) == 1

    def test_without_logging_the_folder_does_not_matter(self, ext, qtbot, tmp_path):
        (tmp_path / 'a file').write_text('x')
        set_folder(ext, tmp_path / 'a file' / 'below')
        ext.get_action(WorkFlowActions.LOG).trigger()

        run(qtbot, ext, **SLOW)
        end_run(qtbot, ext)

        assert files_in(tmp_path) == [] and not ext._saving

    @pytest.mark.errors_expected
    def test_a_file_which_cannot_be_made_leaves_the_window_usable(self, ext, qtbot, tmp_path, monkeypatch):
        def failing(*args, **kwargs):
            raise OSError('the disk is full')

        monkeypatch.setattr(thermo_spectro.saving, 'prepare_file', failing)
        set_folder(ext, tmp_path / 'data')

        ext.start_run()
        wait_for(qtbot, lambda: bool(ext.notifications))  # (the experiment starts when the modules have delivered)

        assert not ext.run_active
        assert 'the disk is full' in ext.notifications[-1][1]
        panel = ext.saving_panel
        assert panel.folder.isEnabled() and not panel.note.isEnabled() and not ext.note_edit.isEnabled()
        assert ext.get_action(WorkFlowActions.START).isEnabled()


class TestWhatIsSaidAboutTheExperiment:
    def test_it_is_in_the_attributes_of_the_file(self, ext, qtbot, tmp_path):
        set_folder(ext, tmp_path / 'data', name='Pomme', add_date=False, title='Ramp 3', sample='Pomme 12',
                   operator='Émile', protocol='1. Heat to 80 °C at 2 °C/min\n2. Hold 10 min')

        run(qtbot, ext, **SLOW)
        end_run(qtbot, ext)

        with h5py.File(tmp_path / 'data' / 'Pomme.h5', 'r') as f:
            raw = f['RawData']
            assert _text(raw.attrs['experiment_title']) == 'Ramp 3' and _text(raw.attrs['sample']) == 'Pomme 12'
            assert _text(raw.attrs['operator']) == 'Émile'
            assert _text(raw.attrs['protocol']) == '1. Heat to 80 °C at 2 °C/min\n2. Hold 10 min'
            assert _text(raw.attrs['application']) == 'ThermoSpectro'
            assert raw.attrs['TITLE'] == 'Data from PyMoDAQ modules'  # (the title of the group is not replaced)

    def test_what_is_not_said_is_empty_but_there(self, ext, qtbot, tmp_path):
        set_folder(ext, tmp_path / 'data', name='Pomme', add_date=False)

        run(qtbot, ext, **SLOW)
        end_run(qtbot, ext)

        with h5py.File(tmp_path / 'data' / 'Pomme.h5', 'r') as f:
            assert [_text(f['RawData'].attrs[key]) for key in ('experiment_title', 'sample', 'operator', 'protocol')] \
                == ['', '', '', '']

    def test_the_application_is_named_by_its_command(self, ext, monkeypatch):
        monkeypatch.setattr(configurations, 'configuration_of_class',
                            lambda name: configurations.CONFIGURATIONS['thermo_spectro'])

        assert ext._experiment_metadata()['application'] == 'thermo_spectro'

    def test_the_data_are_the_same_as_before(self, ext, qtbot, tmp_path):
        set_folder(ext, tmp_path / 'data', name='Pomme', add_date=False, protocol='x')

        run(qtbot, ext, **SLOW)
        qtbot.wait(800)
        end_run(qtbot, ext)

        with H5Experiment(None, file=tmp_path / 'data' / 'Pomme.h5') as experiment:
            assert 'Unit1_Spectrum' in experiment.titles and 'Unit1_Temperature' in experiment.titles
            assert len(experiment.time('Unit1_Temperature')) >= 2


class TestTheObservations:
    def test_they_are_saved_with_their_time_while_the_log_runs(self, ext, qtbot, tmp_path):
        set_folder(ext, tmp_path / 'data', name='Pomme', add_date=False)
        run(qtbot, ext, **SLOW)

        before = time.time()
        assert ext.add_note('The sample turned yellow')
        qtbot.wait(300)
        assert ext.add_note('  Door opened  ')
        after = time.time()
        end_run(qtbot, ext)

        times, texts = notes_in(tmp_path / 'data' / 'Pomme.h5')
        assert texts == ['The sample turned yellow', 'Door opened']
        assert before <= times[0] <= times[1] <= after and times[1] - times[0] >= 0.25

    def test_their_time_is_the_one_of_the_data(self, ext, qtbot, tmp_path):
        set_folder(ext, tmp_path / 'data', name='Pomme', add_date=False)
        run(qtbot, ext, **SLOW)
        qtbot.wait(400)

        ext.add_note('in the middle')
        qtbot.wait(400)
        end_run(qtbot, ext)

        with H5Experiment(None, file=tmp_path / 'data' / 'Pomme.h5') as experiment:
            data_times = experiment.time('Unit1_Temperature')
        note_time = notes_in(tmp_path / 'data' / 'Pomme.h5')[0][0]
        assert data_times[0] < note_time < data_times[-1]

    def test_they_are_listed_in_the_window_with_the_time_since_the_start(self, ext, qtbot, tmp_path):
        set_folder(ext, tmp_path / 'data')
        run(qtbot, ext, **SLOW)
        qtbot.wait(300)

        ext.add_note('first')
        ext.add_note('second')

        lines = ext.saving_panel.notes_text().splitlines()
        assert len(lines) == 2 and lines[0].endswith('first') and lines[1].endswith('second')
        assert re.fullmatch(r'\d\d:\d\d:\d\d  \(0:\d\d\)  first', lines[0])
        end_run(qtbot, ext)

    def test_the_window_of_the_toolbar_and_the_one_of_the_saving_tab_do_the_same(self, ext, qtbot, tmp_path):
        set_folder(ext, tmp_path / 'data', name='Pomme', add_date=False)
        run(qtbot, ext, **SLOW)

        ext.note_edit.setText('from the toolbar')
        ext.note_edit.returnPressed.emit()
        assert ext.note_edit.text() == ''
        type_in(qtbot, ext.saving_panel.note, 'from the window')
        qtbot.keyClick(ext.saving_panel.note, QtCore.Qt.Key.Key_Return)
        assert ext.saving_panel.note.text() == ''
        end_run(qtbot, ext)

        assert notes_in(tmp_path / 'data' / 'Pomme.h5')[1] == ['from the toolbar', 'from the window']

    def test_they_can_be_written_only_while_the_log_runs(self, ext, qtbot, tmp_path):
        panel = ext.saving_panel
        assert not ext.note_edit.isEnabled() and not panel.note.isEnabled()

        run(qtbot, ext, **SLOW)
        assert ext.note_edit.isEnabled() and panel.note.isEnabled() and panel.add_button.isEnabled()
        end_run(qtbot, ext)

        assert not ext.note_edit.isEnabled() and not panel.note.isEnabled() and not panel.add_button.isEnabled()

    def test_an_observation_outside_a_run_is_refused_and_not_thrown_away(self, ext):
        messages = []
        ext.status_signal.connect(messages.append)
        ext.note_edit.setText('too early')

        ext._note_from_toolbar()

        assert not ext.add_note('too early') and ext.note_edit.text() == 'too early'
        assert any('was not saved' in message for message in messages)
        assert ext.saving_panel.notes_text() == ''

    def test_an_empty_observation_is_nothing(self, ext, qtbot, tmp_path):
        run(qtbot, ext, **SLOW)

        assert not ext.add_note('') and not ext.add_note('   \n ')
        end_run(qtbot, ext)

        assert notes_in(only_file(tmp_path / 'data'))[1] == []

    def test_a_very_long_observation_is_cut_to_a_thousand_characters(self, ext, qtbot, tmp_path):
        run(qtbot, ext, **SLOW)

        ext.add_note('é' * 3000)
        end_run(qtbot, ext)

        assert notes_in(only_file(tmp_path / 'data'))[1] == ['é' * 1000]

    def test_without_logging_nothing_can_be_noted_and_the_window_says_why(self, ext, qtbot):
        ext.get_action(WorkFlowActions.LOG).trigger()
        run(qtbot, ext, **SLOW)

        assert not ext.note_edit.isEnabled() and not ext.saving_panel.note.isEnabled()
        assert 'No file is written' in ext.saving_panel.notes_hint.text()
        assert not ext.add_note('lost')
        end_run(qtbot, ext)

    def test_one_written_just_before_the_end_is_in_the_file(self, ext, qtbot, tmp_path):
        run(qtbot, ext, **SLOW)

        for index in range(20):
            assert ext.add_note(f'note {index}')
        end_run(qtbot, ext)

        assert notes_in(only_file(tmp_path / 'data'))[1] == [f'note {index}' for index in range(20)]

    def test_the_list_stays_after_the_end_and_is_emptied_by_the_next_start(self, ext, qtbot, tmp_path):
        set_folder(ext, tmp_path / 'data', name='Pomme', add_date=False)
        run(qtbot, ext, **SLOW)
        ext.add_note('first experiment')
        end_run(qtbot, ext)
        assert 'first experiment' in ext.saving_panel.notes_text()

        run(qtbot, ext, **SLOW)
        assert ext.saving_panel.notes_text() == ''
        ext.add_note('second experiment')
        end_run(qtbot, ext)

        assert notes_in(tmp_path / 'data' / 'Pomme.h5')[1] == ['first experiment']
        assert notes_in(tmp_path / 'data' / 'Pomme_001.h5')[1] == ['second experiment']

    def test_the_observations_do_not_disturb_the_data(self, ext, qtbot, tmp_path):
        set_folder(ext, tmp_path / 'data', name='Pomme', add_date=False)
        run(qtbot, ext, **SLOW)
        for index in range(10):
            ext.add_note(f'note {index}')
            qtbot.wait(60)
        end_run(qtbot, ext)

        with H5Experiment(None, file=tmp_path / 'data' / 'Pomme.h5') as experiment:
            times = experiment.time('Unit1_Temperature')
        assert len(times) >= 5 and list(times) == sorted(times)
        assert ext._module_and_data_saver.n_errors == 0

    READER = textwrap.dedent('''
        import h5py, json, sys
        file = h5py.File(sys.argv[1], 'r', swmr=True)
        group = file['RawData/Datalogger000/Notes']
        group['text'].refresh()
        print(json.dumps([text.decode('utf-8') for text in group['text'][:]]))
    ''')

    def test_they_are_saved_while_another_program_reads_the_file(self, ext, qtbot, tmp_path):
        set_folder(ext, tmp_path / 'data', name='Pomme', add_date=False)
        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: ext._module_and_data_saver.swmr_started)

        ext.add_note('while it is read')
        assert ext._worker.drain()  # (it is written)
        result = subprocess.run([sys.executable, '-c', self.READER, str(tmp_path / 'data' / 'Pomme.h5')],
                                capture_output=True, text=True, timeout=120)
        end_run(qtbot, ext)

        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == ['while it is read']


class TestWhatIsRemembered:
    def store(self, tmp_path) -> SavingStore:
        return SavingStore(tmp_path / 'remembered' / 'saving.json')

    def test_a_change_is_remembered_a_moment_later(self, ext, qtbot, tmp_path):
        type_in(qtbot, ext.saving_panel.name, 'Poire')

        wait_for(qtbot, lambda: self.store(tmp_path).load()['name'] == 'Poire')

    def test_ten_edits_in_a_row_are_written_once(self, ext, qtbot, monkeypatch):
        writes = []
        original = ext._saving_store.save
        monkeypatch.setattr(ext._saving_store, 'save', lambda values: (writes.append(values), original(values)))

        type_in(qtbot, ext.saving_panel.name, 'a name of ten')
        wait_for(qtbot, lambda: bool(writes))
        qtbot.wait(800)

        assert len(writes) == 1

    def test_it_comes_back_next_time_but_not_the_title_and_the_sample(self, ext, dashboard, tmp_path):
        from pymodaq.utils.gui_utils.loader_utils import create_extension
        set_folder(ext, tmp_path / 'lab', name='Pomme', add_date=False, date_folders=True, operator='Ana',
                   protocol='Heat\nHold', title='Ramp of Monday', sample='Apple 12')
        ext.remember_saving()

        shared_ui, fresh = create_extension(dashboard, thermo_spectro.ThermoSpectro)
        try:
            assert fresh.saving_panel.values() == {
                'folder': str(tmp_path / 'lab'), 'name': 'Pomme', 'add_date': False, 'date_folders': True,
                'title': '', 'sample': '', 'operator': 'Ana', 'protocol': 'Heat\nHold'}
        finally:
            fresh.quit_fun()

    def test_closing_the_extension_remembers_what_was_just_typed(self, ext, tmp_path):
        ext.saving_panel.set_values({'operator': 'Léa'})

        assert ext._quit_fun()

        assert self.store(tmp_path).load()['operator'] == 'Léa'

    def test_a_file_which_cannot_be_written_stops_nothing_and_is_told_in_the_log(self, ext, tmp_path, monkeypatch, caplog):
        (tmp_path / 'a file').write_text('x')
        monkeypatch.setattr(ext._saving_store, 'path', tmp_path / 'a file' / 'saving.json')

        ext.saving_panel.set_values({'operator': 'Léa'})
        ext.remember_saving()

        assert ext.saving_panel.values()['operator'] == 'Léa'
        assert any('could not be remembered' in record.getMessage() for record in caplog.records)
