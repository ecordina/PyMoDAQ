"""The widgets of a camera unit: the image with a rectangle for each ROI, and the table of the ROIs (ROI manager)

They do for an image what :class:`~.widgets.SpectrumPlot` and :class:`~.roi_manager.RoiManager` do for a spectrum, and
have the same kind of signals. Neither holds the ROIs: they show what they are given and say what the user does.
"""
from __future__ import annotations

from typing import Optional, Sequence

import numpy as np
import pyqtgraph as pg
from qtpy import QtCore, QtGui, QtWidgets

from pymodaq_plugins_thermospectro.camera import RectRoi
from pymodaq_plugins_thermospectro.roi_manager import (BADGE_WIDTH, CHECK_WIDTH, DELETE_WIDTH, EDGE_WIDTH,
                                                       EdgeSpinBox, _quietly)

#: How the ROIs of a camera are set up, for the user
HINT = ('Drag a rectangle (or one of its corners) on the image, or type its edges here. Double click on the image, '
        'or right click, to add a ROI there.')


class ImagePlot(pg.PlotWidget):
    """A small view of an image, with a rectangle for each ROI

    A rectangle is moved by dragging it and resized by dragging its corner: this emits :attr:`roi_moving` all along and
    :attr:`roi_changed` when the mouse is released. A double click on the image, or a right click and its menu, asks for
    a new ROI at this position with :attr:`add_requested`; a right click on a rectangle offers to delete it
    (:attr:`delete_requested`).

    The pixel ``(row, column)`` covers the square from ``(column, row)`` to ``(column + 1, row + 1)``; the first row is
    at the top.

    Parameters
    ----------
    title: str
        Name of the camera
    """
    roi_changed = QtCore.Signal(str, float, float, float, float)  # ROI key, x0, x1, y0, y1
    roi_moving = QtCore.Signal(str, float, float, float, float)  # the same, while the rectangle is dragged
    add_requested = QtCore.Signal(float, float)  # position (x, y)
    delete_requested = QtCore.Signal(str)  # ROI key

    def __init__(self, title: str = '', parent=None):
        super().__init__(parent=parent)
        self._shape: Optional[tuple[int, int]] = None
        self._rois: dict[str, pg.RectROI] = {}
        self._labels: dict[str, pg.TextItem] = {}
        self._updating = False
        self._locked = False
        self._menu: Optional[QtWidgets.QMenu] = None

        self.setMinimumHeight(70)
        self.setMinimumWidth(120)
        item = self.getPlotItem()
        item.setMenuEnabled(False)
        item.hideButtons()
        item.setTitle(title, size='8pt')
        item.setAspectLocked(True)
        item.invertY(True)
        item.vb.setMouseEnabled(False, False)
        for axis in ('left', 'bottom'):
            item.getAxis(axis).setStyle(tickFont=QtGui.QFont(self.font().family(), 7), tickTextOffset=2)
        item.getAxis('left').setWidth(42)
        self.image_item = pg.ImageItem(axisOrder='row-major')
        item.addItem(self.image_item)
        self._message = pg.TextItem('waiting for data', color='#888888', anchor=(0.5, 0.5))
        item.addItem(self._message, ignoreBounds=True)
        self.scene().sigMouseClicked.connect(self._on_clicked)

    # -- image -------------------------------------------------------------------------------------------------

    def set_title(self, title: str):
        self.getPlotItem().setTitle(title, size='8pt')

    def set_image(self, image: np.ndarray):
        """Show the image, whole, with its levels adjusted to it"""
        image = np.asarray(image, dtype=float)
        if image.ndim != 2 or image.size == 0:
            return
        shape = (int(image.shape[0]), int(image.shape[1]))
        self.image_item.setImage(image, autoLevels=True)
        if shape != self._shape:
            self._shape = shape
            self.getPlotItem().setRange(xRange=(0, shape[1]), yRange=(0, shape[0]), padding=0.0)
            self._message.setVisible(False)
            self._message.setPos(shape[1] / 2., shape[0] / 2.)
            bounds = QtCore.QRectF(0., 0., float(shape[1]), float(shape[0]))
            for roi in self._rois.values():
                roi.maxBounds = bounds

    def clear_image(self, message: str = 'waiting for data'):
        """Forget the image and tell why there is none"""
        self.image_item.clear()
        self._message.setText(message)
        self._message.setVisible(True)
        self._shape = None

    @property
    def shape(self) -> Optional[tuple[int, int]]:
        """(rows, columns) of the image shown, None if there is none"""
        return self._shape

    # -- ROIs --------------------------------------------------------------------------------------------------

    def _make_movable(self, roi: pg.RectROI):
        roi.translatable = not self._locked
        for handle in roi.getHandles():
            handle.setVisible(not self._locked)

    def set_roi(self, key: str, x0: float, x1: float, y0: float, y1: float, color: str, label: str = ''):
        """Create or move the rectangle of a ROI, without emitting :attr:`roi_changed`

        A rectangle is never drawn smaller than a pixel.
        """
        x0, x1 = (x0, x1) if x0 <= x1 else (x1, x0)
        y0, y1 = (y0, y1) if y0 <= y1 else (y1, y0)
        x1, y1 = max(x1, x0 + 1.), max(y1, y0 + 1.)
        roi = self._rois.get(key)
        self._updating = True
        try:
            if roi is None:
                roi = pg.RectROI((x0, y0), (x1 - x0, y1 - y0), pen=pg.mkPen(color, width=2), movable=True,
                                 resizable=True, rotatable=False, invertible=False, scaleSnap=True, translateSnap=True,
                                 snapSize=1.0)
                if self._shape is not None:
                    roi.maxBounds = QtCore.QRectF(0., 0., float(self._shape[1]), float(self._shape[0]))
                roi.sigRegionChanged.connect(lambda _=None, k=key: self._on_moving(k))
                roi.sigRegionChangeFinished.connect(lambda _=None, k=key: self._on_finished(k))
                roi.sigRemoveRequested.connect(lambda _=None, k=key: self._on_remove(k))
                self.getPlotItem().addItem(roi)
                text = pg.TextItem(label, color=color, anchor=(0, 0))
                text.setParentItem(roi)
                self._rois[key] = roi
                self._labels[key] = text
                self._make_movable(roi)
            else:
                roi.setPos((x0, y0))
                roi.setSize((x1 - x0, y1 - y0))
                roi.setPen(pg.mkPen(color, width=2))
                self._labels[key].setText(label, color=color)
        finally:
            self._updating = False

    def set_locked(self, locked: bool):
        """Forbid (or allow again) to move and resize the ROIs"""
        self._locked = locked
        for roi in self._rois.values():
            self._make_movable(roi)

    def remove_roi(self, key: str):
        roi = self._rois.pop(key, None)
        self._labels.pop(key, None)
        if roi is not None:
            self.getPlotItem().removeItem(roi)

    @property
    def roi_keys(self) -> list[str]:
        return list(self._rois)

    def roi(self, key: str) -> tuple[float, float, float, float]:
        """(x0, x1, y0, y1) of a ROI, as displayed"""
        roi = self._rois[key]
        pos, size = roi.pos(), roi.size()
        return float(pos.x()), float(pos.x() + size.x()), float(pos.y()), float(pos.y() + size.y())

    def _on_moving(self, key: str):
        if self._updating or key not in self._rois:
            return
        self.roi_moving.emit(key, *self.roi(key))

    def _on_finished(self, key: str):
        if self._updating or key not in self._rois:
            return
        self.roi_changed.emit(key, *self.roi(key))

    def _on_remove(self, key: str):
        if not self._locked and key in self._rois:
            self.delete_requested.emit(key)

    def _popup(self, event, text: str, slot):
        """A one-entry menu at the place of the click"""
        menu = QtWidgets.QMenu(self)
        menu.addAction(text).triggered.connect(lambda _=False: slot())
        menu.aboutToHide.connect(menu.deleteLater)
        self._menu = menu
        menu.popup(event.screenPos().toPoint())

    def _position(self, event) -> Optional[tuple[float, float]]:
        """Where a click is on the image (None outside of it)"""
        item = self.getPlotItem()
        if self._shape is None or not item.vb.sceneBoundingRect().contains(event.scenePos()):
            return None
        point = item.vb.mapSceneToView(event.scenePos())
        x, y = float(point.x()), float(point.y())
        if 0 <= x <= self._shape[1] and 0 <= y <= self._shape[0]:
            return x, y
        return None

    def _on_clicked(self, event):
        if event.button() == QtCore.Qt.MouseButton.RightButton:
            if event.isAccepted() or self._locked:  # (a rectangle took it: it offers to delete itself)
                return
            position = self._position(event)
            if position is not None:
                x, y = position
                self._popup(event, f'Add a ROI here ({x:.0f}, {y:.0f})', lambda: self.add_requested.emit(x, y))
            return
        if not event.double():
            return
        position = self._position(event)
        if position is not None:
            self.add_requested.emit(*position)


class RectRoiRow(QtWidgets.QWidget):
    """The widgets of one ROI of a camera

    Attributes
    ----------
    edges_edited: Signal(str, float, float, float, float)
        The user typed an edge: the key of the ROI and its edges x0, x1, y0, y1
    name_edited: Signal(str, str)
    record_toggled: Signal(str, bool)
    plot_toggled: Signal(str, bool)
    delete_clicked: Signal(str)
    """
    edges_edited = QtCore.Signal(str, float, float, float, float)
    name_edited = QtCore.Signal(str, str)
    record_toggled = QtCore.Signal(str, bool)
    plot_toggled = QtCore.Signal(str, bool)
    delete_clicked = QtCore.Signal(str)

    EDGE_NAMES = ('x0', 'x1', 'y0', 'y1')
    EDGE_TIPS = ('Left edge of the window (pixel)', 'Right edge of the window (pixel)',
                 'Top edge of the window (pixel, the first row is 0)', 'Bottom edge of the window (pixel)')

    def __init__(self, key: str, parent=None):
        super().__init__(parent)
        self.key = key
        self._name = ''
        self._color = ''
        self._locked = False
        self._edges = [0.0, 0.0, 0.0, 0.0]  # as they are in the settings: the boxes only show them rounded

        self.badge = QtWidgets.QLabel()
        self.badge.setFixedWidth(BADGE_WIDTH)
        self.badge.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.name = QtWidgets.QLineEdit()
        self.name.setMinimumWidth(60)
        self.name.setToolTip('The name of the ROI, in the plots and in the file')
        self.boxes = {}
        for name, tip in zip(self.EDGE_NAMES, self.EDGE_TIPS):
            box = EdgeSpinBox()
            box.setDecimals(0)
            box.setFixedWidth(EDGE_WIDTH - 14)
            box.setToolTip(tip)
            self.boxes[name] = box
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
        for box in self.boxes.values():
            layout.addWidget(box)
        for box in (self.record, self.plot):
            cell = QtWidgets.QWidget()
            cell.setFixedWidth(CHECK_WIDTH)
            inner = QtWidgets.QHBoxLayout(cell)
            inner.setContentsMargins(0, 0, 0, 0)
            inner.addWidget(box, 0, QtCore.Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(cell)
        layout.addWidget(self.delete)

        self.name.editingFinished.connect(self._name_finished)
        for index, box in enumerate(self.boxes.values()):
            box.valueChanged.connect(lambda value, i=index: self._edge_changed(i, value))
        self.record.toggled.connect(lambda checked: self.record_toggled.emit(self.key, checked))
        self.plot.toggled.connect(lambda checked: self.plot_toggled.emit(self.key, checked))
        self.delete.clicked.connect(lambda _=False: self.delete_clicked.emit(self.key))

    def _name_finished(self):
        text = self.name.text().strip()
        if not text:  # a ROI always has a name: the old one is kept
            _quietly(self.name, self.name.setText, self._name)
        elif text != self._name:
            self._name = text
            self.name_edited.emit(self.key, text)

    # (An edge typed past the other one of its pair pushes it, so that the rectangle keeps its size.)
    def _edge_changed(self, index: int, value: float):
        low, high = (0, 1) if index < 2 else (2, 3)
        size = max(self._edges[high] - self._edges[low], 0.0)
        self._edges[index] = float(value)
        if index == low and self._edges[low] > self._edges[high]:
            self._edges[high] = self._edges[low] + size
        elif index == high and self._edges[high] < self._edges[low]:
            self._edges[low] = self._edges[high] - size
        self.edges_edited.emit(self.key, *self._edges)

    @staticmethod
    def _shows(box: QtWidgets.QDoubleSpinBox, value: float) -> bool:
        return box.value() == round(value, box.decimals())

    def show_roi(self, index: int, roi: RectRoi, color: str, outside: bool = False):
        """Show a ROI (nothing is emitted)

        Parameters
        ----------
        index: int
            Its place in the list, counted from 0: the number on its rectangle
        roi: RectRoi
        color: str
            The color of its rectangle and of its curve
        outside: bool
            The rectangle is out of the image: its value is empty (nan)
        """
        self._name = roi.name
        self.badge.setText(str(index + 1))
        if color != self._color:
            self._color = color
            self.badge.setStyleSheet(f'background-color: {color}; color: white; font-weight: bold; '
                                     f'border-radius: 3px;')
        if self.name.text() != roi.name and not self.name.hasFocus():
            _quietly(self.name, self.name.setText, roi.name)
        self._edges = [float(roi.x0), float(roi.x1), float(roi.y0), float(roi.y1)]
        for (name, box), value, tip in zip(self.boxes.items(), self._edges, self.EDGE_TIPS):
            if not self._shows(box, value):
                _quietly(box, box.setValue, value)
            box.setStyleSheet('color: #d62728;' if outside else '')
            box.setToolTip('This window is outside of the image: its value is empty' if outside else tip)
        for box, value in ((self.record, roi.record), (self.plot, roi.plot)):
            if box.isChecked() != bool(value):
                _quietly(box, box.setChecked, bool(value))

    def show_edges(self, x0: float, x1: float, y0: float, y1: float):
        """The rectangle is being dragged on the image: follow it (nothing is emitted)"""
        self._edges = [float(x0), float(x1), float(y0), float(y1)]
        for box, value in zip(self.boxes.values(), self._edges):
            if not self._shows(box, value):
                _quietly(box, box.setValue, value)

    def set_locked(self, locked: bool):
        """What is plotted can still be changed, the rest cannot"""
        self._locked = locked
        for widget in (self.name, *self.boxes.values(), self.record, self.delete):
            widget.setEnabled(not locked)


class RectRoiManager(QtWidgets.QWidget):
    """The ROIs of a camera, as a table with buttons to add and delete them (see :class:`~.roi_manager.RoiManager`)

    Attributes
    ----------
    edges_edited: Signal(str, float, float, float, float)
    name_edited: Signal(str, str)
    record_toggled: Signal(str, bool)
    plot_toggled: Signal(str, bool)
    delete_clicked: Signal(str)
    add_clicked: Signal()
    reset_clicked: Signal()
    """
    edges_edited = QtCore.Signal(str, float, float, float, float)
    name_edited = QtCore.Signal(str, str)
    record_toggled = QtCore.Signal(str, bool)
    plot_toggled = QtCore.Signal(str, bool)
    delete_clicked = QtCore.Signal(str)
    add_clicked = QtCore.Signal()
    reset_clicked = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: dict[str, RectRoiRow] = {}
        self._locked = False

        self.add_button = QtWidgets.QPushButton('Add a ROI')
        self.add_button.setToolTip('Add a rectangle in the middle of the image (or double click on the image where '
                                   'you want it)')
        self.reset_button = QtWidgets.QPushButton('Reset to the default ROIs')
        self.reset_button.setToolTip('Replace the ROIs by the four default ones')
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.reset_button)
        buttons.addStretch(1)

        self._labels: dict[str, QtWidgets.QLabel] = {}
        header = QtWidgets.QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(4)
        columns = [('badge', '#', BADGE_WIDTH), ('name', 'Name', 0)] + \
                  [(name, text, EDGE_WIDTH - 14) for name, text in
                   zip(RectRoiRow.EDGE_NAMES, ('x from', 'x to', 'y from', 'y to'))] + \
                  [('record', 'Record', CHECK_WIDTH), ('plot', 'Plot', CHECK_WIDTH), ('delete', '', DELETE_WIDTH)]
        for name, text, width in columns:
            label = QtWidgets.QLabel(text)
            align = QtCore.Qt.AlignmentFlag.AlignLeft if name == 'name' else (
                QtCore.Qt.AlignmentFlag.AlignRight if name in RectRoiRow.EDGE_NAMES
                else QtCore.Qt.AlignmentFlag.AlignHCenter)
            label.setAlignment(align | QtCore.Qt.AlignmentFlag.AlignVCenter)
            font = label.font()
            font.setBold(True)
            label.setFont(font)
            if width:
                label.setFixedWidth(width)
            header.addWidget(label, 0 if width else 1)
            self._labels[name] = label

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

    @property
    def keys(self) -> list[str]:
        """The keys of the ROIs shown, from the first row to the last one"""
        return list(self._rows)

    def row(self, key: str) -> RectRoiRow:
        return self._rows[key]

    def header_text(self, column: str) -> str:
        return self._labels[column].text()

    def _new_row(self, key: str) -> RectRoiRow:
        row = RectRoiRow(key)
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

    def set_rois(self, keys: Sequence[str], rois: Sequence[RectRoi], colors: Sequence[str],
                 shape: Optional[tuple[int, int]] = None):
        """Show the ROIs of the camera

        The rows which are still there are updated in place, so that what the user is doing in one of them (typing,
        the focus) is not disturbed.

        Parameters
        ----------
        keys: sequence of str
            The name of each ROI in the settings
        rois: sequence of RectRoi
        colors: sequence of str
            The color of each ROI
        shape: tuple of int, optional
            (rows, columns) of the image, if it is known: a window out of it is shown in red
        """
        keys = list(keys)
        for key in [key for key in self._rows if key not in keys]:
            self._drop_row(key)
        created = False
        for key in keys:
            if key not in self._rows:
                self._rows[key] = self._new_row(key)
                created = True
        if created or list(self._rows) != keys:
            self._rows = {key: self._rows[key] for key in keys}
            for row in self._rows.values():
                self._rows_layout.removeWidget(row)
            for position, row in enumerate(self._rows.values()):
                self._rows_layout.insertWidget(position, row)
        for index, (key, roi, color) in enumerate(zip(keys, rois, colors)):
            outside = bool(shape) and (roi.x1 < 0 or roi.x0 > shape[1] or roi.y1 < 0 or roi.y0 > shape[0])
            self._rows[key].show_roi(index, roi, color, outside)

    def show_edges(self, key: str, x0: float, x1: float, y0: float, y1: float):
        """A rectangle is being dragged on the image: its numbers follow"""
        row = self._rows.get(key)
        if row is not None:
            row.show_edges(x0, x1, y0, y1)

    def focus_roi(self, key: str):
        """Put the cursor in the name of a ROI (a ROI which was just added is named at once)"""
        row = self._rows.get(key)
        if row is not None:
            row.name.setFocus()
            row.name.selectAll()
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
