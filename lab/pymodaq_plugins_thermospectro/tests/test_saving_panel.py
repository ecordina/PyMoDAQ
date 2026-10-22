"""The Saving window: the file, the experiment and the observations; and what it remembers"""
import datetime
import json
from pathlib import Path

import pytest
from qtpy import QtCore, QtWidgets

from pymodaq_plugins_thermospectro.saving_panel import (DEFAULTS, NOTE_LIMIT, PROTOCOL_LIMIT, REMEMBERED, SavingPanel,
                                                        SavingStore, default_folder, default_saving_path)

pytestmark = pytest.mark.filterwarnings('ignore')


class FakeConfig:
    """Stands for the configuration of PyMoDAQ, which says where the data are saved"""
    value = ''

    def __call__(self, *keys):
        assert keys == ('data', 'data_saving', 'h5file', 'save_path')
        return self.value


@pytest.fixture
def panel(qtbot, tmp_path):
    widget = SavingPanel()
    qtbot.addWidget(widget)
    widget.set_values({'folder': str(tmp_path / 'data')})
    return widget


def type_in(qtbot, edit, text):
    edit.clear()
    qtbot.keyClicks(edit, text)


class TestStore:
    def test_nothing_remembered_the_defaults(self, tmp_path):
        assert SavingStore(tmp_path / 'saving.json').load() == DEFAULTS

    def test_what_is_remembered_comes_back(self, tmp_path):
        store = SavingStore(tmp_path / 'sub' / 'saving.json')
        values = {'folder': '/data/lab', 'name': 'Pomme', 'add_date': False, 'date_folders': True, 'operator': 'Émile',
                  'protocol': 'Chauffer\nà 2 °C/min'}

        store.save(values)

        assert store.load() == {**DEFAULTS, **values}

    def test_the_title_and_the_sample_are_not_remembered(self, tmp_path):
        """They describe an experiment: the one of the day before is not the one which starts"""
        store = SavingStore(tmp_path / 'saving.json')

        store.save({**DEFAULTS, 'title': 'Ramp of Monday', 'sample': 'Apple 12', 'operator': 'Ana'})

        content = json.loads((tmp_path / 'saving.json').read_text(encoding='utf-8'))
        assert set(content['values']) == set(REMEMBERED) == {'folder', 'name', 'add_date', 'date_folders', 'operator',
                                                             'protocol'}
        assert 'title' not in content['values'] and 'sample' not in content['values']
        assert store.load()['title'] == '' and store.load()['sample'] == ''

    def test_a_title_in_the_file_is_ignored(self, tmp_path):
        (tmp_path / 'saving.json').write_text(json.dumps({'values': {'title': 'old', 'sample': 'old', 'name': 'x'}}))

        loaded = SavingStore(tmp_path / 'saving.json').load()

        assert loaded['name'] == 'x' and loaded['title'] == '' and loaded['sample'] == ''

    def test_a_file_which_cannot_be_read_is_kept_aside(self, tmp_path):
        path = tmp_path / 'saving.json'
        path.write_text('{ not json')

        assert SavingStore(path).load() == DEFAULTS
        assert not path.exists() and (tmp_path / 'saving.json.bad').read_text() == '{ not json'

    @pytest.mark.parametrize('content', ['[]', '{"values": 3}', '{"values": []}', '{}', '3'])
    def test_a_file_of_another_shape_too(self, tmp_path, content):
        path = tmp_path / 'saving.json'
        path.write_text(content)

        assert SavingStore(path).load() == DEFAULTS
        assert (tmp_path / 'saving.json.bad').exists()

    def test_a_value_of_the_wrong_type_is_left_out(self, tmp_path):
        (tmp_path / 'saving.json').write_text(json.dumps({'values': {'add_date': 'yes', 'name': 3, 'operator': 'Ana'}}))

        loaded = SavingStore(tmp_path / 'saving.json').load()

        assert loaded['add_date'] is DEFAULTS['add_date'] and loaded['name'] == DEFAULTS['name']
        assert loaded['operator'] == 'Ana'

    def test_unknown_keys_are_dropped(self, tmp_path):
        (tmp_path / 'saving.json').write_text(json.dumps({'values': {'colour': 'blue', 'operator': 'Ana'}}))

        assert 'colour' not in SavingStore(tmp_path / 'saving.json').load()

    def test_it_never_raises_when_it_cannot_write_and_says_so(self, tmp_path):
        (tmp_path / 'file').write_text('x')

        assert not SavingStore(tmp_path / 'file' / 'saving.json').save({'name': 'x'})  # (below a file)
        assert SavingStore(tmp_path / 'saving.json').save({'name': 'x'})

    def test_no_temporary_file_is_left(self, tmp_path):
        store = SavingStore(tmp_path / 'saving.json')

        store.save({'name': 'x'})
        store.save({'name': 'y'})

        assert sorted(path.name for path in tmp_path.iterdir()) == ['saving.json']
        assert store.load()['name'] == 'y'

    def test_the_default_path_is_in_the_folder_of_the_plugin_of_the_user(self, tmp_path, monkeypatch):
        import pymodaq_utils.config
        asked = []
        monkeypatch.setattr(pymodaq_utils.config, 'get_set_config_dir',
                            lambda name, user=True: asked.append((name, user)) or tmp_path / name)

        assert default_saving_path() == tmp_path / 'thermospectro' / 'saving.json' and asked == [('thermospectro', True)]


class TestDefaultFolder:
    def test_the_folder_of_pymodaq_if_it_is_one_here(self, tmp_path, monkeypatch):
        import pymodaq_utils.config
        config = FakeConfig()
        config.value = str(tmp_path / 'Data')
        monkeypatch.setattr(pymodaq_utils.config, 'GlobalConfig', lambda: config)

        assert default_folder() == tmp_path / 'Data'

    def test_a_folder_which_is_not_one_here_is_not_used(self, tmp_path, monkeypatch):
        """``C:\\Data`` (the default of PyMoDAQ) is a folder named so, in the current folder, on Linux"""
        import pymodaq_utils.config
        config = FakeConfig()
        config.value = 'relative\\Data'
        monkeypatch.setattr(pymodaq_utils.config, 'GlobalConfig', lambda: config)
        monkeypatch.setattr(Path, 'home', lambda: tmp_path)

        assert default_folder() == tmp_path / 'PyMoDAQ_data'

    def test_without_configuration_the_home_of_the_user(self, tmp_path, monkeypatch):
        import pymodaq_utils.config

        def broken():
            raise RuntimeError('no configuration')

        monkeypatch.setattr(pymodaq_utils.config, 'GlobalConfig', broken)
        monkeypatch.setattr(Path, 'home', lambda: tmp_path)

        assert default_folder() == tmp_path / 'PyMoDAQ_data'


class TestTheFile:
    def test_it_starts_with_the_defaults(self, qtbot):
        widget = SavingPanel()
        qtbot.addWidget(widget)

        assert widget.values() == {**DEFAULTS, 'folder': str(default_folder())}
        assert widget.add_date.isChecked() and not widget.date_folders.isChecked()

    def test_an_empty_folder_is_the_default_one(self, panel):
        panel.set_values({'folder': ''})

        assert panel.folder.text() == str(default_folder()) and panel.folder_path == default_folder().absolute()

    def test_the_folder_of_the_user_and_a_relative_one(self, panel, monkeypatch, tmp_path):
        monkeypatch.setenv('HOME', str(tmp_path))
        monkeypatch.setenv('USERPROFILE', str(tmp_path))
        panel.folder.setText('~/data')
        assert panel.folder_path == tmp_path / 'data'

        panel.folder.setText('relative/data')
        assert panel.folder_path.is_absolute() and panel.folder_path.parts[-2:] == ('relative', 'data')

    def test_the_next_file_is_made_of_the_fields(self, panel, tmp_path):
        panel.set_values({'name': 'Pomme 12', 'add_date': True, 'date_folders': False})
        now = datetime.datetime(2026, 10, 22, 15, 30, 12)

        assert panel.next_file(now) == tmp_path / 'data' / 'Pomme 12_20261022_153012.h5'

        panel.set_values({'add_date': False, 'date_folders': True})

        assert panel.next_file(now) == tmp_path / 'data' / '2026' / '20261022' / 'Pomme 12.h5'

    def test_the_next_file_is_not_one_which_exists(self, panel, tmp_path):
        panel.set_values({'name': 'Pomme', 'add_date': False})
        (tmp_path / 'data').mkdir()
        (tmp_path / 'data' / 'Pomme.h5').write_bytes(b'')

        assert panel.next_file() == tmp_path / 'data' / 'Pomme_001.h5'

    def test_the_window_shows_what_the_next_file_will_look_like(self, panel, tmp_path):
        panel.set_values({'name': 'Pomme', 'add_date': True, 'date_folders': False})
        assert panel.pattern.text() == str(tmp_path / 'data' / 'Pomme_YYYYMMDD_HHMMSS.h5')

        panel.set_values({'add_date': False})
        assert panel.pattern.text().endswith(str(Path('data') / 'Pomme.h5'))

        panel.set_values({'date_folders': True})
        assert panel.pattern.text().endswith(str(Path('data') / 'YYYY' / 'YYYYMMDD' / 'Pomme.h5'))

    def test_the_name_which_will_be_used_is_the_one_shown(self, panel):
        panel.set_values({'name': 'a/b:c.h5', 'add_date': False})

        assert panel.pattern.text().endswith('a_b_c.h5')

    def test_typing_changes_the_pattern_and_says_so(self, panel, qtbot):
        with qtbot.waitSignal(panel.changed):
            type_in(qtbot, panel.name, 'Banane')

        assert 'Banane_YYYYMMDD_HHMMSS.h5' in panel.pattern.text()
        with qtbot.waitSignal(panel.changed):
            panel.add_date.click()
        assert panel.pattern.text().endswith('Banane.h5')
        with qtbot.waitSignal(panel.changed):
            panel.date_folders.click()
        assert 'YYYYMMDD' in panel.pattern.text()

    def test_what_the_program_sets_is_not_a_change_of_the_user(self, panel, qtbot):
        with qtbot.assertNotEmitted(panel.changed):
            panel.set_values({'name': 'x', 'folder': '/a', 'add_date': False, 'date_folders': True, 'title': 't',
                              'sample': 's', 'operator': 'o', 'protocol': 'p'})
            panel.set_current_file(Path('/a/x.h5'))
            panel.set_logging(False)

        assert panel.values() == {'folder': '/a', 'name': 'x', 'add_date': False, 'date_folders': True, 'title': 't',
                                  'sample': 's', 'operator': 'o', 'protocol': 'p'}

    def test_values_which_are_missing_are_left_as_they_are(self, panel):
        panel.set_values({'name': 'Kept', 'operator': 'Ana'})

        panel.set_values({'name': 'Changed'})

        assert panel.values()['name'] == 'Changed' and panel.values()['operator'] == 'Ana'

    def test_the_folder_can_be_chosen_in_a_dialog(self, panel, qtbot, monkeypatch, tmp_path):
        monkeypatch.setattr(QtWidgets.QFileDialog, 'getExistingDirectory',
                            staticmethod(lambda parent, title, start: str(tmp_path / 'chosen')))

        with qtbot.waitSignal(panel.changed):
            assert panel.choose_folder() == tmp_path / 'chosen'

        assert panel.folder.text() == str(tmp_path / 'chosen') and str(tmp_path / 'chosen') in panel.pattern.text()

    def test_a_dialog_which_is_cancelled_changes_nothing(self, panel, qtbot, monkeypatch):
        monkeypatch.setattr(QtWidgets.QFileDialog, 'getExistingDirectory', staticmethod(lambda *args: ''))
        before = panel.folder.text()

        with qtbot.assertNotEmitted(panel.changed):
            assert panel.choose_folder() is None

        assert panel.folder.text() == before

    def test_the_browse_button_opens_the_dialog(self, panel, monkeypatch, tmp_path):
        opened = []
        monkeypatch.setattr(QtWidgets.QFileDialog, 'getExistingDirectory',
                            staticmethod(lambda parent, title, start: opened.append(start) or ''))

        panel.browse.click()

        assert opened == [str(tmp_path / 'data')]

    def test_the_current_file_is_shown_until_there_is_none(self, panel):
        panel.set_current_file(Path('/a/b.h5'))
        assert str(Path('/a/b.h5')) in panel.current.text()

        panel.set_current_file(None)
        assert panel.current.text() == ''

    def test_it_says_when_nothing_is_saved(self, panel):
        panel.set_logging(False)
        assert not panel.logging_off.isHidden()

        panel.set_logging(True)
        assert panel.logging_off.isHidden()


class TestTheExperiment:
    def test_what_is_said_goes_in_the_metadata(self, panel):
        panel.set_values({'title': ' Ramp 3 ', 'sample': 'Pomme 12', 'operator': 'Émile', 'protocol': 'Heat\nHold'})

        assert panel.metadata() == {'experiment_title': 'Ramp 3', 'sample': 'Pomme 12', 'operator': 'Émile',
                                    'protocol': 'Heat\nHold'}

    def test_the_title_of_the_group_of_the_h5_file_is_not_a_key(self, panel):
        assert 'title' not in panel.metadata()

    def test_more_can_be_added(self, panel):
        assert panel.metadata(application='thermo_spectro')['application'] == 'thermo_spectro'

    def test_the_protocol_is_cut_at_the_limit(self, panel):
        panel.set_values({'protocol': 'é' * (PROTOCOL_LIMIT + 50)})

        assert len(panel.metadata()['protocol']) == PROTOCOL_LIMIT

    def test_typing_in_any_field_says_so(self, panel, qtbot):
        for edit in (panel.title, panel.sample, panel.operator):
            with qtbot.waitSignal(panel.changed):
                type_in(qtbot, edit, 'x')
        with qtbot.waitSignal(panel.changed):
            qtbot.keyClicks(panel.protocol, 'a protocol')

        assert panel.values()['protocol'] == 'a protocol' and panel.values()['sample'] == 'x'

    def test_the_protocol_keeps_its_cursor_when_it_is_set_to_what_it_is(self, panel):
        panel.set_values({'protocol': 'one\ntwo'})
        cursor = panel.protocol.textCursor()
        cursor.setPosition(2)
        panel.protocol.setTextCursor(cursor)

        panel.set_values({'protocol': 'one\ntwo'})

        assert panel.protocol.textCursor().position() == 2


class TestLocking:
    FIELDS = ('folder', 'browse', 'name', 'add_date', 'date_folders', 'title', 'sample', 'operator')

    def test_during_a_log_the_file_and_the_experiment_are_locked(self, panel):
        panel.set_locked(True)

        assert not any(getattr(panel, name).isEnabled() for name in self.FIELDS)
        assert panel.protocol.isReadOnly()

        panel.set_locked(False)

        assert all(getattr(panel, name).isEnabled() for name in self.FIELDS) and not panel.protocol.isReadOnly()

    def test_the_observations_do_not_depend_on_it(self, panel):
        panel.set_running(True)

        panel.set_locked(True)
        assert panel.note.isEnabled() and panel.add_button.isEnabled()

        panel.set_locked(False)
        assert panel.note.isEnabled() and panel.add_button.isEnabled()

    def test_the_advanced_settings_are_locked_too(self, panel):
        panel.set_advanced(QtWidgets.QLabel('settings'))

        panel.set_locked(True)

        assert not panel.advanced_box.isEnabled()
        panel.set_locked(False)
        assert panel.advanced_box.isEnabled()


class TestAdvanced:
    def test_there_is_nothing_to_show_without_settings(self, panel):
        assert panel.advanced_box.isHidden()

    def test_the_settings_are_in_a_box_which_is_closed_at_first(self, panel):
        settings = QtWidgets.QLabel('settings')

        panel.set_advanced(settings)

        assert not panel.advanced_box.isHidden() and not panel.advanced_box.isChecked()
        assert settings.isHidden()
        panel.advanced_box.setChecked(True)
        assert not settings.isHidden()
        panel.advanced_box.setChecked(False)
        assert settings.isHidden()

    def test_other_settings_replace_them(self, panel):
        first, second = QtWidgets.QLabel('first'), QtWidgets.QLabel('second')
        panel.set_advanced(first)

        panel.set_advanced(second)

        assert first.parent() is None and second.parent() is panel.advanced_box


class TestTheObservations:
    def test_they_cannot_be_written_when_no_log_runs(self, panel, qtbot):
        assert not panel.note.isEnabled() and not panel.add_button.isEnabled()
        assert 'while the log runs' in panel.notes_hint.text()

        with qtbot.assertNotEmitted(panel.note_entered):
            panel.note.setText('lost?')
            panel._note_entered()
            panel.add_button.click()

        assert panel.note.text() == 'lost?'  # (it is not thrown away)

    def test_they_can_be_written_while_a_log_runs(self, panel):
        panel.set_running(True)

        assert panel.note.isEnabled() and panel.add_button.isEnabled() and panel.notes_hint.text() == ''

    def test_not_if_nothing_is_saved_and_it_says_why(self, panel):
        panel.set_running(True, saving=False)

        assert not panel.note.isEnabled() and not panel.add_button.isEnabled()
        assert 'No file is written' in panel.notes_hint.text() and 'Do logging' in panel.notes_hint.text()

    def test_enter_sends_the_observation_and_empties_the_field(self, panel, qtbot):
        panel.set_running(True)

        with qtbot.waitSignal(panel.note_entered) as received:
            type_in(qtbot, panel.note, '  The sample turned yellow  ')
            qtbot.keyClick(panel.note, QtCore.Qt.Key.Key_Return)

        assert received.args == ['The sample turned yellow'] and panel.note.text() == ''

    def test_the_button_does_the_same(self, panel, qtbot):
        panel.set_running(True)
        panel.note.setText('Door opened')

        with qtbot.waitSignal(panel.note_entered) as received:
            panel.add_button.click()

        assert received.args == ['Door opened'] and panel.note.text() == ''

    def test_an_empty_observation_is_not_sent(self, panel, qtbot):
        panel.set_running(True)

        with qtbot.assertNotEmitted(panel.note_entered):
            panel.note.setText('    ')
            panel._note_entered()

    def test_an_observation_is_at_most_a_thousand_characters(self, panel, qtbot):
        panel.set_running(True)

        with qtbot.waitSignal(panel.note_entered) as received:
            panel.note.setText('x' * 5000)
            panel._note_entered()

        assert NOTE_LIMIT == 1000 and len(received.args[0]) == NOTE_LIMIT

    def test_the_panel_lists_what_it_is_told_has_been_saved(self, panel):
        panel.show_note(datetime.datetime(2026, 10, 22, 15, 30, 12).timestamp(), 125., 'Door opened')
        panel.show_note(datetime.datetime(2026, 10, 22, 15, 31, 0).timestamp(), 173., 'Door closed')

        assert panel.notes_text().splitlines() == ['15:30:12  (2:05)  Door opened', '15:31:00  (2:53)  Door closed']

    def test_it_does_not_list_what_was_entered_until_it_is_told(self, panel, qtbot):
        panel.set_running(True)
        panel.note.setText('entered')

        panel._note_entered()

        assert panel.notes_text() == ''

    def test_the_list_is_emptied_for_the_next_experiment(self, panel):
        panel.show_note(1., 1., 'x')

        panel.clear_notes()

        assert panel.notes_text() == ''

    def test_the_list_cannot_be_edited(self, panel):
        assert panel.notes.isReadOnly()
