"""The *Saving* window of the extension: where and how the experiments are saved, what the user says about them, and the
observations noted while one is running

Three parts:

* **the file**: the folder, the name, whether the date and the time are added to the name and whether the file goes in the
  folders of the year and of the day (:func:`~.saving.new_file_path` says where it is made);
* **the experiment**: a title, the sample, the operator and the protocol (a text of any length), saved in the attributes of
  the file when it is made;
* **the observations**: a line to write what is noticed while the log runs, which is saved, with its time, in the file
  as soon as Enter is pressed (:attr:`SavingPanel.note_entered`). The most recent one is last in the list.

What describes the way the experiments are saved (the folder, the name, the options, the operator and the protocol) is
remembered from one session to the next (:class:`SavingStore`). The title and the sample are not: they describe the
experiment, and the one of the day before is not the one which starts. The observations are not either.
"""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
from typing import Optional

from qtpy import QtCore, QtGui, QtWidgets

from pymodaq_plugins_thermospectro.ramp import format_duration
from pymodaq_plugins_thermospectro.saving import clean_name, new_file_path

#: The longest protocol which is saved (characters; a million is some five hundred pages): it stops a document pasted by
#: mistake from filling the file. The attribute of an h5 file is a string of any length
PROTOCOL_LIMIT = 1_000_000
#: The longest observation (characters; the file holds :data:`~.saving.NOTE_BYTES` bytes of it)
NOTE_LIMIT = 1000

DEFAULTS = {'folder': '', 'name': 'Dataset', 'add_date': True, 'date_folders': False, 'title': '', 'sample': '',
            'operator': '', 'protocol': ''}
#: What is remembered (the title and the sample describe an experiment: they are not)
REMEMBERED = ('folder', 'name', 'add_date', 'date_folders', 'operator', 'protocol')


class WrappedLabel(QtWidgets.QLabel):
    """A label which wraps its text and is as tall as the wrapped text needs, in any layout

    A dock does not give a label the height of its wrapped text: the last lines are cut off.
    """

    def __init__(self, text: str = '', parent=None):
        super().__init__(text, parent)
        self.setWordWrap(True)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        needed = self.heightForWidth(self.width())
        if needed > 0 and self.minimumHeight() != needed:
            self.setMinimumHeight(needed)


def path_line() -> QtWidgets.QLineEdit:
    """Where the user reads a path which may be long: it scrolls, never gets cut off by the layout, and can be copied"""
    line = QtWidgets.QLineEdit()
    line.setReadOnly(True)
    line.setFrame(False)
    line.setStyleSheet('QLineEdit { background: transparent; }')
    return line


def default_folder() -> Path:
    """Where the files go until the user chooses: the folder of PyMoDAQ for them when it can be one here (``C:\\Data`` is
    not a folder on Linux: it would be a folder named so in the one the application was started in), else
    ``PyMoDAQ_data`` in the home of the user"""
    try:
        from pymodaq_utils.config import GlobalConfig
        configured = Path(str(GlobalConfig()('data', 'data_saving', 'h5file', 'save_path')))
        if configured.is_absolute():
            return configured
    except Exception:
        pass
    return Path.home() / 'PyMoDAQ_data'


def default_saving_path() -> Path:
    """Where what the user typed is remembered: ``~/.pymodaq/thermospectro/saving.json``"""
    from pymodaq_utils.config import get_set_config_dir
    return get_set_config_dir('thermospectro', user=True) / 'saving.json'


class SavingStore:
    """What the user typed in the panel and wants to find again (:data:`REMEMBERED`), in a json file

    As :class:`~.rois.RoiStore` does, it reads the file each time and replaces it as a whole when it writes, and a file
    which cannot be read is kept as ``saving.json.bad`` and ignored.

    Parameters
    ----------
    path: Path
        The json file, made when something is first saved
    """
    VERSION = 1

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict:
        """What was saved, completed with the defaults (the keys which are not remembered are dropped)"""
        values = dict(DEFAULTS)
        try:
            content = json.loads(self.path.read_text(encoding='utf-8'))
            saved = content['values']
            if not isinstance(saved, dict):
                raise TypeError('values')
        except (FileNotFoundError, OSError):
            return values
        except (ValueError, KeyError, TypeError):
            try:
                os.replace(self.path, self.path.with_name(self.path.name + '.bad'))
            except OSError:
                pass
            return values
        for key in REMEMBERED:
            if isinstance(saved.get(key), type(DEFAULTS[key])):
                values[key] = saved[key]
        return values

    def save(self, values: dict) -> bool:
        """Remember `values`

        Returns
        -------
        bool
            False if the file could not be written (nothing is raised: not being remembered is no reason to stop)
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + '.tmp')
            temporary.write_text(json.dumps({'version': self.VERSION, 'values': {
                key: values[key] for key in REMEMBERED if key in values}}, indent=2), encoding='utf-8')
            os.replace(temporary, self.path)
        except OSError:
            return False
        return True


class SavingPanel(QtWidgets.QWidget):
    """The window where the file, the experiment and its observations are set (see the module)

    Attributes
    ----------
    changed: Signal()
        The user changed something (it is to be remembered, and the name of the file shown again)
    note_entered: Signal(str)
        The user wrote an observation and pressed Enter (or *Add*): the text, not empty, at most :data:`NOTE_LIMIT`
        characters. The panel does not list it: :meth:`show_note` does, when it is saved
    """
    changed = QtCore.Signal()
    note_entered = QtCore.Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._locked = False
        self._running = False

        self.folder = QtWidgets.QLineEdit()
        self.folder.setToolTip('The folder of the files. It is made if it does not exist')
        self.browse = QtWidgets.QPushButton('Browse...')
        self.name = QtWidgets.QLineEdit()
        self.name.setToolTip('The name of the file, without .h5 (characters a file name cannot hold are replaced)')
        self.add_date = QtWidgets.QCheckBox('Add the date and the time to the name')
        self.add_date.setToolTip('Sample12_20261022_153012.h5: nothing is ever replaced. Without it the file is '
                                 'Sample12.h5, and Sample12_001.h5 if that one exists')
        self.date_folders = QtWidgets.QCheckBox('Put the file in folders of the year and the day (2026/20261022)')
        self.date_folders.setToolTip('The way PyMoDAQ arranges its files')
        self.pattern = path_line()
        self.pattern.setToolTip('What the file of the next experiment will be called (YYYYMMDD_HHMMSS: the date and the time '
                                'when it starts)')
        self.current = path_line()
        self.current.setToolTip('The file of the experiment which runs, or of the last one')
        self.logging_off = QtWidgets.QLabel('<b>Do logging is off</b> (toolbar): nothing is saved')
        self.logging_off.setStyleSheet('color: #d62728;')
        self.logging_off.setVisible(False)

        self.title = QtWidgets.QLineEdit()
        self.title.setToolTip('A title for this experiment')
        self.sample = QtWidgets.QLineEdit()
        self.sample.setToolTip('The sample (its name, its batch...)')
        self.operator = QtWidgets.QLineEdit()
        self.operator.setToolTip('Who runs the experiment')
        self.protocol = QtWidgets.QPlainTextEdit()
        self.protocol.setPlaceholderText('The protocol: what is done, how, with what...')
        self.protocol.setMinimumHeight(70)

        self.note = QtWidgets.QLineEdit()
        self.note.setPlaceholderText('An observation (Enter): it is saved in the file with its time')
        self.note.setMaxLength(NOTE_LIMIT)
        self.add_button = QtWidgets.QPushButton('Add')
        self.add_button.setToolTip('Save this observation in the file, with the time it was made')
        self.notes = QtWidgets.QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setMinimumHeight(60)
        self.notes_hint = WrappedLabel()
        self.notes_hint.setStyleSheet('color: #888888;')

        where = QtWidgets.QGroupBox('The file')
        form = QtWidgets.QGridLayout(where)
        form.addWidget(QtWidgets.QLabel('Folder:'), 0, 0)
        form.addWidget(self.folder, 0, 1)
        form.addWidget(self.browse, 0, 2)
        form.addWidget(QtWidgets.QLabel('Name:'), 1, 0)
        form.addWidget(self.name, 1, 1, 1, 2)
        form.addWidget(self.add_date, 2, 1, 1, 2)
        form.addWidget(self.date_folders, 3, 1, 1, 2)
        form.addWidget(QtWidgets.QLabel('Next file:'), 4, 0)
        form.addWidget(self.pattern, 4, 1, 1, 2)
        form.addWidget(QtWidgets.QLabel('Current file:'), 5, 0)
        form.addWidget(self.current, 5, 1, 1, 2)
        form.addWidget(self.logging_off, 6, 0, 1, 3)
        about = QtWidgets.QGroupBox('The experiment (saved in the file when the log starts)')
        details = QtWidgets.QFormLayout(about)
        details.addRow('Title:', self.title)
        details.addRow('Sample:', self.sample)
        details.addRow('Operator:', self.operator)
        details.addRow('Protocol:', self.protocol)
        observations = QtWidgets.QGroupBox('Observations (while the log runs)')
        notes = QtWidgets.QGridLayout(observations)
        notes.addWidget(self.note, 0, 0)
        notes.addWidget(self.add_button, 0, 1)
        notes.addWidget(self.notes_hint, 1, 0, 1, 2)
        notes.addWidget(self.notes, 2, 0, 1, 2)
        self.advanced_box = QtWidgets.QGroupBox('The h5 file (advanced)')
        self.advanced_box.setCheckable(True)
        self.advanced_box.setChecked(False)
        self.advanced_box.setFlat(True)
        self.advanced_box.setVisible(False)
        self.advanced_box.setLayout(QtWidgets.QVBoxLayout())
        self.advanced_box.toggled.connect(self._show_advanced)
        self.advanced = None
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(where)
        layout.addWidget(about)
        layout.addWidget(observations, 1)
        layout.addWidget(self.advanced_box)

        self.browse.clicked.connect(lambda _=False: self.choose_folder())
        for edit in (self.folder, self.name, self.title, self.sample, self.operator):
            edit.textEdited.connect(self._edited)
        for box in (self.add_date, self.date_folders):
            box.toggled.connect(self._edited)
        self.protocol.textChanged.connect(self._edited)
        self.note.returnPressed.connect(self._note_entered)
        self.add_button.clicked.connect(lambda _=False: self._note_entered())
        self.set_values(DEFAULTS | {'folder': str(default_folder())})
        self.set_running(False)

    def set_advanced(self, widget: QtWidgets.QWidget, title: str = 'The h5 file (advanced)'):
        """Put a widget (the settings of the h5 file: backend, compression...) in a box which is closed at first"""
        if self.advanced is not None:
            self.advanced.setParent(None)
        self.advanced = widget
        self.advanced_box.setTitle(title)
        self.advanced_box.layout().addWidget(widget)
        self.advanced_box.setVisible(True)
        self._show_advanced(self.advanced_box.isChecked())

    def _show_advanced(self, shown: bool):
        if self.advanced is not None:
            self.advanced.setVisible(shown)

    # -- what the user typed -----------------------------------------------------------------------------------

    def values(self) -> dict:
        """All that is typed (what :class:`SavingStore` remembers)"""
        return {'folder': self.folder.text().strip(), 'name': self.name.text().strip(),
                'add_date': self.add_date.isChecked(), 'date_folders': self.date_folders.isChecked(),
                'title': self.title.text().strip(), 'sample': self.sample.text().strip(),
                'operator': self.operator.text().strip(), 'protocol': self.protocol.toPlainText().strip()}

    def set_values(self, values: dict):
        """Show these values (the keys which are missing are left as they are), without saying that the user changed them"""
        for widget in (self.folder, self.name, self.add_date, self.date_folders, self.title, self.sample, self.operator,
                       self.protocol):
            widget.blockSignals(True)
        try:
            for key, widget in (('folder', self.folder), ('name', self.name), ('title', self.title),
                                ('sample', self.sample), ('operator', self.operator)):
                if key in values:
                    widget.setText(str(values[key]) or (str(default_folder()) if key == 'folder' else ''))
            for key, box in (('add_date', self.add_date), ('date_folders', self.date_folders)):
                if key in values:
                    box.setChecked(bool(values[key]))
            if 'protocol' in values and self.protocol.toPlainText() != values['protocol']:
                self.protocol.setPlainText(str(values['protocol']))
        finally:
            for widget in (self.folder, self.name, self.add_date, self.date_folders, self.title, self.sample,
                           self.operator, self.protocol):
                widget.blockSignals(False)
        self.show_pattern()

    @property
    def folder_path(self) -> Path:
        """The folder of the files (the default one if it is empty; a relative one is relative to where the application
        was started)"""
        return Path(self.folder.text().strip() or str(default_folder())).expanduser().absolute()

    def next_file(self, now: Optional[datetime.datetime] = None) -> Path:
        """The path of the file of the next experiment, which does not exist yet (see :func:`~.saving.new_file_path`)"""
        values = self.values()
        return new_file_path(self.folder_path, values['name'], values['add_date'], values['date_folders'], now)

    def metadata(self, **more) -> dict:
        """What is saved in the file about the experiment: its title (``experiment_title``: ``title`` is the one of the
        group of the h5 file), the sample, the operator and the protocol (cut at :data:`PROTOCOL_LIMIT` characters),
        and what is added (the application...)"""
        values = self.values()
        found = {'experiment_title': values['title'], 'sample': values['sample'], 'operator': values['operator'],
                 'protocol': values['protocol'][:PROTOCOL_LIMIT]}
        found.update(more)
        return found

    def _edited(self, *_):
        self.show_pattern()
        self.changed.emit()

    def choose_folder(self) -> Optional[Path]:
        """Let the user choose the folder (a dialog)"""
        chosen = QtWidgets.QFileDialog.getExistingDirectory(self, 'The folder of the files', str(self.folder_path))
        if chosen:
            self.folder.setText(chosen)
            self._edited()
            return Path(chosen)
        return None

    # -- the file ----------------------------------------------------------------------------------------------

    def pattern_text(self) -> str:
        """What the name of the next file will look like (the time is not known yet)"""
        values = self.values()
        stem = clean_name(values['name'])
        if values['add_date']:
            stem += '_YYYYMMDD_HHMMSS'
        folder = self.folder_path
        if values['date_folders']:
            folder = folder / 'YYYY' / 'YYYYMMDD'
        return str(folder / f'{stem}.h5')

    def show_pattern(self):
        text = self.pattern_text()
        self.pattern.setText(text)
        self.pattern.setCursorPosition(len(text))  # (the end: the name of the file is what matters)

    def set_current_file(self, path: Optional[Path]):
        """The file being written, or the last one (``None``: there is none)"""
        text = str(path) if path else ''
        self.current.setText(text)
        self.current.setCursorPosition(len(text))

    def set_logging(self, logging: bool):
        """Tell that nothing is saved when *Do logging* is off in the toolbar"""
        self.logging_off.setVisible(not logging)

    # -- during a log ------------------------------------------------------------------------------------------

    def set_locked(self, locked: bool):
        """The file and the experiment cannot be changed while the log runs (the file is made when it starts)"""
        self._locked = locked
        for widget in (self.folder, self.browse, self.name, self.add_date, self.date_folders, self.title, self.sample,
                       self.operator, self.advanced_box):
            widget.setEnabled(not locked)
        self.protocol.setReadOnly(locked)

    def set_running(self, running: bool, saving: bool = True):
        """The observations can be written while a log runs and a file is written (only)

        Parameters
        ----------
        running: bool
            A log runs
        saving: bool
            It writes a file (it does not when *Do logging* is off): an observation would be lost
        """
        self._running = running and saving
        self.note.setEnabled(self._running)
        self.add_button.setEnabled(self._running)
        if not running:
            hint = 'Observations can be written while the log runs: each one is saved in the file with its time.'
        elif not saving:
            hint = 'No file is written (see Do logging, in the toolbar): an observation could not be saved.'
        else:
            hint = ''
        self.notes_hint.setText(hint)

    def _note_entered(self):
        text = self.note.text().strip()
        if text and self._running:
            self.note.clear()
            self.note_entered.emit(text[:NOTE_LIMIT])

    def clear_notes(self):
        self.notes.clear()

    def show_note(self, epoch: float, elapsed: float, text: str):
        """List an observation which was saved: the time of the day, the time since the start of the log, and the text"""
        clock = datetime.datetime.fromtimestamp(epoch).strftime('%H:%M:%S')
        self.notes.appendPlainText(f'{clock}  ({format_duration(elapsed)})  {text}')
        self.notes.moveCursor(QtGui.QTextCursor.MoveOperation.End)

    def notes_text(self) -> str:
        return self.notes.toPlainText()
