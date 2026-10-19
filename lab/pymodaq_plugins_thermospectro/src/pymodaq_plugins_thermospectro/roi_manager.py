"""The ROI manager: the ROIs of a spectrometer in a small table, to set them up by hand, add them and delete them

One row per ROI: its number (in the color of its window on the spectrum), its name, the two edges of its window, the
boxes telling whether its value is recorded and plotted, and a button to delete it. The edges can be typed, or stepped
with the arrows (or the wheel, once the box has the focus); an edge taken past the other one pushes it, so the window
moves. A window which is dragged on the spectrum moves its numbers here as it goes.

The ROI manager window of the extension shows one of these tables for each spectrometer (see :data:`HINT` for what
to tell the user). The manager does not hold the ROIs: it shows what it is given with
:meth:`RoiManager.set_rois` and says what the user does with signals. The settings of the extension stay what the
experiment is made from, so nothing here can get out of step with them.
"""
from __future__ import annotations

from typing import Optional, Sequence

from qtpy import QtCore, QtWidgets

from pymodaq_plugins_thermospectro.rois import Roi

#: How the ROIs are set up, for the user
HINT = ('Drag a window (or one of its edges) on the spectrum, or type its edges here. Double click on the spectrum, '
        'or right click, to add a ROI there.')
#: Width of the columns (px), the same for the header and the rows
BADGE_WIDTH = 26
EDGE_WIDTH = 80
CHECK_WIDTH = 46
DELETE_WIDTH = 28
#: The range of the boxes of the edges: wide, so that a ROI which is outside of the spectrum can still be shown as it is
EDGE_LIMIT = 1e9


class EdgeSpinBox(QtWidgets.QDoubleSpinBox):
    """A box for an edge of a window: it only changes when the user types or uses its arrows

    The wheel moves it only once it has the focus, otherwise it scrolls the list the box is in (a ROI must not move
    because the user scrolled past it). The value is taken when Enter is pressed or the box is left, not at each key.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.setKeyboardTracking(False)
        self.setDecimals(2)
        self.setRange(-EDGE_LIMIT, EDGE_LIMIT)
        self.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        self.setFixedWidth(EDGE_WIDTH)

    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


def _quietly(widget: QtWidgets.QWidget, setter, value):
    """Show a value in a widget without it saying that the user changed something"""
    blocked = widget.blockSignals(True)
    try:
        setter(value)
    finally:
        widget.blockSignals(blocked)


class RoiRow(QtWidgets.QWidget):
    """The widgets of one ROI

    Attributes
    ----------
    edges_edited: Signal(str, float, float)
        The user typed an edge: the key of the ROI and its two edges, in increasing order
    name_edited: Signal(str, str)
    record_toggled: Signal(str, bool)
    plot_toggled: Signal(str, bool)
    delete_clicked: Signal(str)
    """
    edges_edited = QtCore.Signal(str, float, float)
    name_edited = QtCore.Signal(str, str)
    record_toggled = QtCore.Signal(str, bool)
    plot_toggled = QtCore.Signal(str, bool)
    delete_clicked = QtCore.Signal(str)

    def __init__(self, key: str, parent=None):
        super().__init__(parent)
        self.key = key
        self._name = ''
        self._color = ''
        self._locked = False
        self._start = 0.0  # the edges as they are in the settings: the boxes only show them rounded
        self._stop = 0.0

        self.badge = QtWidgets.QLabel()
        self.badge.setFixedWidth(BADGE_WIDTH)
        self.badge.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.name = QtWidgets.QLineEdit()
        self.name.setMinimumWidth(60)
        self.name.setToolTip('The name of the ROI, in the plots and in the file')
        self.start = EdgeSpinBox()
        self.stop = EdgeSpinBox()
        self.start.setToolTip('Lower edge of the window')
        self.stop.setToolTip('Upper edge of the window')
        self.record = QtWidgets.QCheckBox()
        self.record.setToolTip('Save the value of this ROI (the mean of its pixels) at each measurement')
        self.plot = QtWidgets.QCheckBox()
        self.plot.setToolTip('Show this ROI in the plot (what is recorded does not change)')
        self.delete = QtWidgets.QToolButton()
        self.delete.setText('✕')
        self.delete.setToolTip('Delete this ROI')
        self.delete.setFixedWidth(DELETE_WIDTH)

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 1, 0, 1)
        layout.setSpacing(4)
        layout.addWidget(self.badge)
        layout.addWidget(self.name, 1)
        layout.addWidget(self.start)
        layout.addWidget(self.stop)
        for box in (self.record, self.plot):
            cell = QtWidgets.QWidget()
            cell.setFixedWidth(CHECK_WIDTH)
            inner = QtWidgets.QHBoxLayout(cell)
            inner.setContentsMargins(0, 0, 0, 0)
            inner.addWidget(box, 0, QtCore.Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(cell)
        layout.addWidget(self.delete)

        self.name.editingFinished.connect(self._name_finished)
        self.start.valueChanged.connect(self._start_changed)
        self.stop.valueChanged.connect(self._stop_changed)
        self.record.toggled.connect(lambda checked: self.record_toggled.emit(self.key, checked))
        self.plot.toggled.connect(lambda checked: self.plot_toggled.emit(self.key, checked))
        self.delete.clicked.connect(lambda _=False: self.delete_clicked.emit(self.key))

    # -- what the user does ------------------------------------------------------------------------------------

    def _name_finished(self):
        text = self.name.text().strip()
        if not text:  # a ROI always has a name: the old one is kept
            _quietly(self.name, self.name.setText, self._name)
        elif text != self._name:
            self._name = text
            self.name_edited.emit(self.key, text)

    # (The other edge is not read from its box, which rounds: it would be changed by the edit of this one.
    # An edge typed past the other one pushes it, so that the window keeps its width: to move a window the user types
    # its two edges one after the other, whichever way it goes.)
    def _start_changed(self, value: float):
        width = max(self._stop - self._start, 0.0)
        self._start = float(value)
        if self._start > self._stop:
            self._stop = self._start + width
        self.edges_edited.emit(self.key, self._start, self._stop)

    def _stop_changed(self, value: float):
        width = max(self._stop - self._start, 0.0)
        self._stop = float(value)
        if self._stop < self._start:
            self._start = self._stop - width
        self.edges_edited.emit(self.key, self._start, self._stop)

    @staticmethod
    def _shows(box: QtWidgets.QDoubleSpinBox, value: float) -> bool:
        """The box displays this value (it rounds to its decimals)"""
        return box.value() == round(value, box.decimals())

    # -- what is shown -----------------------------------------------------------------------------------------

    def show_roi(self, index: int, roi: Roi, color: str, step: float = 1.0, outside: bool = False):
        """Show a ROI (nothing is emitted)

        Parameters
        ----------
        index: int
            Its place in the list, counted from 0: the number on its window
        roi: Roi
        color: str
            The color of its window and of its curve
        step: float
            What the arrows of the edges add
        outside: bool
            The window is out of the spectrum: its value is empty (nan)
        """
        self._name = roi.name
        self.badge.setText(str(index + 1))
        if color != self._color:
            self._color = color
            self.badge.setStyleSheet(f'background-color: {color}; color: white; font-weight: bold; '
                                     f'border-radius: 3px;')
        if self.name.text() != roi.name and not self.name.hasFocus():
            _quietly(self.name, self.name.setText, roi.name)
        self._start, self._stop = float(roi.start), float(roi.stop)
        for box, value in ((self.start, roi.start), (self.stop, roi.stop)):
            if not self._shows(box, value):
                _quietly(box, box.setValue, float(value))
            if abs(box.singleStep() - step) > 1e-12:
                _quietly(box, box.setSingleStep, float(step))
            box.setStyleSheet('color: #d62728;' if outside else '')
            box.setToolTip('This window is outside of the spectrum: its value is empty'
                           if outside else ('Lower edge of the window' if box is self.start
                                            else 'Upper edge of the window'))
        for box, value in ((self.record, roi.record), (self.plot, roi.plot)):
            if box.isChecked() != bool(value):
                _quietly(box, box.setChecked, bool(value))

    def show_edges(self, start: float, stop: float):
        """The window is being dragged on the spectrum: follow it (nothing is emitted)"""
        self._start, self._stop = float(start), float(stop)
        for box, value in ((self.start, start), (self.stop, stop)):
            if not self._shows(box, value):
                _quietly(box, box.setValue, float(value))

    def set_locked(self, locked: bool):
        """What is plotted can still be changed, the rest cannot"""
        self._locked = locked
        for widget in (self.name, self.start, self.stop, self.record, self.delete):
            widget.setEnabled(not locked)


class RoiManager(QtWidgets.QWidget):
    """The ROIs of one spectrometer, as a table with buttons to add and delete them

    Attributes
    ----------
    edges_edited: Signal(str, float, float)
        The user typed an edge of a ROI: its key and its two edges, in increasing order
    name_edited: Signal(str, str)
    record_toggled: Signal(str, bool)
    plot_toggled: Signal(str, bool)
    delete_clicked: Signal(str)
    add_clicked: Signal()
    reset_clicked: Signal()
        The user asks for the default ROIs
    """
    edges_edited = QtCore.Signal(str, float, float)
    name_edited = QtCore.Signal(str, str)
    record_toggled = QtCore.Signal(str, bool)
    plot_toggled = QtCore.Signal(str, bool)
    delete_clicked = QtCore.Signal(str)
    add_clicked = QtCore.Signal()
    reset_clicked = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: dict[str, RoiRow] = {}
        self._locked = False

        self.add_button = QtWidgets.QPushButton('Add a ROI')
        self.add_button.setToolTip('Add a window in the middle of the spectrum (or double click on the spectrum where '
                                   'you want it)')
        self.reset_button = QtWidgets.QPushButton('Reset to the default ROIs')
        self.reset_button.setToolTip('Replace the ROIs by the six default ones')
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.reset_button)
        buttons.addStretch(1)

        self._labels: dict[str, QtWidgets.QLabel] = {}
        header = QtWidgets.QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(4)
        for name, text, width in (('badge', '#', BADGE_WIDTH), ('name', 'Name', 0), ('start', 'From', EDGE_WIDTH),
                                  ('stop', 'To', EDGE_WIDTH), ('record', 'Record', CHECK_WIDTH),
                                  ('plot', 'Plot', CHECK_WIDTH), ('delete', '', DELETE_WIDTH)):
            label = QtWidgets.QLabel(text)
            align = {'name': QtCore.Qt.AlignmentFlag.AlignLeft, 'start': QtCore.Qt.AlignmentFlag.AlignRight,
                     'stop': QtCore.Qt.AlignmentFlag.AlignRight}.get(name, QtCore.Qt.AlignmentFlag.AlignHCenter)
            label.setAlignment(align | QtCore.Qt.AlignmentFlag.AlignVCenter)
            font = label.font()
            font.setBold(True)
            label.setFont(font)
            if width:
                label.setFixedWidth(width)
            header.addWidget(label, 0 if width else 1)
            self._labels[name] = label
        self._labels['record'].setToolTip('Save the value of the ROI at each measurement')
        self._labels['plot'].setToolTip('Show the ROI in the plot')

        self._body = QtWidgets.QWidget()
        self._rows_layout = QtWidgets.QVBoxLayout(self._body)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(0)
        self._rows_layout.addStretch(1)
        self._scroll = QtWidgets.QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self._scroll.setWidget(self._body)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(buttons)
        layout.addLayout(header)
        layout.addWidget(self._scroll, 1)

        self.add_button.clicked.connect(lambda _=False: self.add_clicked.emit())
        self.reset_button.clicked.connect(lambda _=False: self.reset_clicked.emit())

    # -- what is shown -----------------------------------------------------------------------------------------

    @property
    def keys(self) -> list[str]:
        """The keys of the ROIs shown, from the first row to the last one"""
        return list(self._rows)

    def row(self, key: str) -> RoiRow:
        return self._rows[key]

    def header_text(self, column: str) -> str:
        """The title of a column: ``name``, ``start`` (From), ``stop`` (To), ``record``, ``plot``"""
        return self._labels[column].text()

    def _new_row(self, key: str) -> RoiRow:
        row = RoiRow(key)
        row.edges_edited.connect(self.edges_edited)
        row.name_edited.connect(self.name_edited)
        row.record_toggled.connect(self.record_toggled)
        row.plot_toggled.connect(self.plot_toggled)
        row.delete_clicked.connect(self.delete_clicked)
        row.set_locked(self._locked)
        return row

    def _drop_row(self, key: str):
        row = self._rows.pop(key)
        self._rows_layout.removeWidget(row)
        row.hide()  # (deleteLater() alone leaves it on the screen until the event loop runs)
        row.setParent(None)
        row.deleteLater()

    def set_rois(self, keys: Sequence[str], rois: Sequence[Roi], colors: Sequence[str], units: str = '',
                 x_range: Optional[tuple[float, float]] = None, pitch: float = 0.0):
        """Show the ROIs of the spectrometer

        The rows which are still there are updated in place, so that what the user is doing in one of them (typing,
        the focus) is not disturbed.

        Parameters
        ----------
        keys: sequence of str
            The name of each ROI in the settings
        rois: sequence of Roi
        colors: sequence of str
            The color of each ROI
        units: str
            The units of the edges, written in the header
        x_range: tuple of float, optional
            Where the spectrum is, if it is known: a window out of it is shown in red
        pitch: float
            The distance between two pixels of the spectrum, which is what the arrows of the edges add (1 if unknown)
        """
        keys = list(keys)
        for key in [key for key in self._rows if key not in keys]:
            self._drop_row(key)
        created = False
        for key in keys:
            if key not in self._rows:
                self._rows[key] = self._new_row(key)
                created = True
        if created or list(self._rows) != keys:  # (new rows have no place yet, or the order is not the same)
            self._rows = {key: self._rows[key] for key in keys}
            for row in self._rows.values():
                self._rows_layout.removeWidget(row)
            for position, row in enumerate(self._rows.values()):
                self._rows_layout.insertWidget(position, row)
        suffix = f' ({units})' if units else ''
        self._labels['start'].setText(f'From{suffix}')
        self._labels['stop'].setText(f'To{suffix}')
        step = round(pitch, 2) if pitch and round(pitch, 2) > 0 else 1.0
        for index, (key, roi, color) in enumerate(zip(keys, rois, colors)):
            outside = bool(x_range) and (roi.stop < x_range[0] or roi.start > x_range[1])
            self._rows[key].show_roi(index, roi, color, step, outside)

    def show_edges(self, key: str, start: float, stop: float):
        """A window is being dragged on the spectrum: its numbers follow"""
        row = self._rows.get(key)
        if row is not None:
            row.show_edges(start, stop)

    def focus_roi(self, key: str):
        """Put the cursor in the name of a ROI (a ROI which was just added is named at once)"""
        row = self._rows.get(key)
        if row is not None:
            row.name.setFocus()
            row.name.selectAll()
            # (the row has no place yet if it was just added: the scroll waits for the layout)
            QtCore.QTimer.singleShot(0, lambda: self._scroll_to(key))

    def _scroll_to(self, key: str):
        row = self._rows.get(key)
        if row is not None:
            self._scroll.ensureWidgetVisible(row)

    def set_locked(self, locked: bool):
        """Forbid to change the ROIs (an experiment is going on): what is plotted can still be changed"""
        self._locked = locked
        for row in self._rows.values():
            row.set_locked(locked)
        self.add_button.setEnabled(not locked)
        self.reset_button.setEnabled(not locked)
