"""The widgets of a camera unit: the image with a rectangle for each ROI, and the table of the ROIs (ROI manager)

They do for an image what :class:`~.widgets.SpectrumPlot` and :class:`~.roi_manager.RoiManager` do for a spectrum, and
have the same kind of signals. Neither holds the ROIs: they show what they are given and say what the user does.
"""
from __future__ import annotations

from typing import Optional, Sequence

import copy

import numpy as np
import pyqtgraph as pg
from pymodaq_data.data import DataRaw
from pymodaq_gui.plotting.data_viewers.viewer2D import Viewer2D
from qtpy import QtCore, QtGui, QtWidgets

from pymodaq_plugins_thermospectro.camera import (CHANNELS_RULE, MODES, RectRoi, channel_letter, channel_name,
                                                  format_channels, selected_channels)
from pymodaq_plugins_thermospectro.roi_manager import (BADGE_WIDTH, CHECK_WIDTH, DELETE_WIDTH, EDGE_WIDTH,
                                                       VALUE_WIDTH, EdgeSpinBox, _quietly)

#: How the ROIs of a camera are set up, for the user
HINT = ('Drag a rectangle (or one of its corners) on the image, or type its edges here. Double click on the image, '
        'or right click, to add a ROI there. Value: the number a ROI gives (the mean, sum, median, min, max or std '
        'of its pixels), or crop: its pixels are saved as images (its rectangle is dashed). Channels (a camera which gives '
        'colors): the channels the value is measured on; with several, the results of the channels are averaged.')

#: The width of the box of one channel in the table of the ROIs, and the space between two
CHANNEL_WIDTH = 32
CHANNEL_SPACING = 2


def channels_width(n: int) -> int:
    """The width of the cell (and of the title of the column) of the boxes of `n` channels"""
    return n * CHANNEL_WIDTH + max(n - 1, 0) * CHANNEL_SPACING


class _CameraViewer2D(Viewer2D):
    """The Viewer2D of PyMoDAQ, without the tools which move the image: the ROIs are drawn on the raw pixels"""

    def set_image_transform(self):
        for name in ('flip_ud', 'flip_lr', 'rotate'):
            self.view.set_action_visible(name, False)
        return self.transform_image(copy.deepcopy(self._raw_data))

    def transform_image(self, dwa):
        return -dwa if self.view.is_action_checked('opposite') else dwa


class ImagePlot(QtWidgets.QWidget):
    """A view of an image, with a rectangle for each ROI, and the tools of the image viewer of PyMoDAQ

    The image is shown by a :class:`~pymodaq_gui.plotting.data_viewers.viewer2D.Viewer2D`, so it has its toolbar: the
    color channels, the histogram and the levels (auto or not), the isocurve, the aspect ratio, the crosshair...

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
        self._user_view = False  # (the user zoomed or panned: the view is not fitted to the widget any more)
        self._fit_timer = QtCore.QTimer(self)  # (owned by the widget: it is never called after the widget is gone)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self._fit_if_following)

        self.setMinimumHeight(120)
        self.setMinimumWidth(200)
        self.viewer = _CameraViewer2D(self, title)  # (it lays itself out in this widget)
        self.view = self.viewer.view
        item = self.getPlotItem()
        item.setMenuEnabled(False)
        item.vb.setMenuEnabled(False)  # (the right click is for adding a ROI)
        item.hideButtons()
        item.setTitle(title, size='8pt')
        item.invertY(True)  # (the first row is at the top)
        for axis in ('left', 'bottom'):
            item.getAxis(axis).setStyle(tickFont=QtGui.QFont(self.font().family(), 7), tickTextOffset=2)
        item.getAxis('left').setWidth(42)
        self._message = pg.TextItem('waiting for data', color='#888888', anchor=(0.5, 0.5))
        item.addItem(self._message, ignoreBounds=True)
        self.view.set_action_visible('legend', False)  # (it does not paint with this version of pyqtgraph)
        if not self.view.is_action_checked('autolevels'):
            self.view.get_action('autolevels').trigger()  # (as before: the levels follow the image until told not to)
        self.image_widget.scene().sigMouseClicked.connect(self._on_clicked)
        item.vb.sigRangeChangedManually.connect(lambda *_: setattr(self, '_user_view', True))
        self.image_widget.installEventFilter(self)

    @property
    def image_widget(self) -> QtWidgets.QWidget:
        return self.view.image_widget

    def getPlotItem(self) -> pg.PlotItem:
        return self.view.plotitem

    def scene(self):
        return self.image_widget.scene()

    # -- image -------------------------------------------------------------------------------------------------

    def set_title(self, title: str):
        self.getPlotItem().setTitle(title, size='8pt')

    def set_image(self, image: np.ndarray, channels: Optional[Sequence[np.ndarray]] = None,
                  labels: Optional[Sequence[str]] = None):
        """Show the image, whole

        Parameters
        ----------
        image: np.ndarray
            The 2D image (the first channel), what the ROIs are measured on
        channels: sequence of np.ndarray, optional
            All the channels of the camera (up to three: red, green, blue), `image` is shown if there are none
        labels: sequence of str, optional
            Their names
        """
        image = np.asarray(image)
        if image.ndim != 2 or image.size == 0:
            return
        shape = (int(image.shape[0]), int(image.shape[1]))
        # (signed floats: the viewer negates the image on request, which wraps around for the unsigned integers of a camera)
        arrays = [np.asarray(channel, dtype=np.float32) for channel in (channels or [image])[:3]]
        arrays = [array for array in arrays if array.shape == shape] or [image.astype(np.float32)]
        names = [str(name) for name in (labels or [])[:len(arrays)]]
        names += [f'channel {index}' for index in range(len(names), len(arrays))]
        self.viewer.show_data(DataRaw('image', data=arrays, labels=names))
        if shape != self._shape:
            self._shape = shape
            self.fit_view()
            self._message.setVisible(False)
            self._message.setPos(shape[1] / 2., shape[0] / 2.)
            bounds = QtCore.QRectF(0., 0., float(shape[1]), float(shape[0]))
            for roi in self._rois.values():
                roi.maxBounds = bounds

    def fit_view(self):
        """Show the whole image (and follow the size of the widget again, until the user zooms or pans)"""
        self._user_view = False
        if self._shape is not None:
            self.getPlotItem().setRange(xRange=(0, self._shape[1]), yRange=(0, self._shape[0]), padding=0.0)

    def _fit_if_following(self):
        if not self._user_view:
            self.fit_view()

    def eventFilter(self, watched, event):
        # (the plot is resized when the histogram or the lineouts open: the image is not to be cut then)
        if watched is self.image_widget and event.type() == QtCore.QEvent.Type.Resize and not self._user_view:
            self._fit_timer.start(0)
        return super().eventFilter(watched, event)

    def clear_image(self, message: str = 'waiting for data'):
        """Forget the image and tell why there is none"""
        for image_item in self.view.data_displayer.get_images().values():
            image_item.clear()
        self.viewer._raw_data = None  # (a button of the toolbar would draw the last image again)
        self.viewer._datas = None
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

    @staticmethod
    def _pen(color: str, crop: bool = False) -> QtGui.QPen:
        """The pen of a rectangle: dashed for a ROI which is a crop (it gives its pixels, not a number)"""
        return pg.mkPen(color, width=2, style=QtCore.Qt.PenStyle.DashLine if crop else QtCore.Qt.PenStyle.SolidLine)

    def set_roi(self, key: str, x0: float, x1: float, y0: float, y1: float, color: str, label: str = '',
                crop: bool = False):
        """Create or move the rectangle of a ROI, without emitting :attr:`roi_changed`

        A rectangle is never drawn smaller than a pixel; it is dashed if the ROI is a crop.
        """
        x0, x1 = (x0, x1) if x0 <= x1 else (x1, x0)
        y0, y1 = (y0, y1) if y0 <= y1 else (y1, y0)
        x1, y1 = max(x1, x0 + 1.), max(y1, y0 + 1.)
        roi = self._rois.get(key)
        self._updating = True
        try:
            if roi is None:
                roi = pg.RectROI((x0, y0), (x1 - x0, y1 - y0), pen=self._pen(color, crop), movable=True,
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
                roi.setPen(self._pen(color, crop))
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

    def _popup(self, event, entries: Sequence[tuple[str, object]]):
        """A menu at the place of the click, with these (text, slot) entries"""
        menu = QtWidgets.QMenu(self)
        for text, slot in entries:
            menu.addAction(text).triggered.connect(lambda _=False, slot=slot: slot())
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
            if event.isAccepted():  # (a rectangle took it: it offers to delete itself)
                return
            entries = []
            position = self._position(event)
            if position is not None and not self._locked:
                x, y = position
                entries.append((f'Add a ROI here ({x:.0f}, {y:.0f})', lambda: self.add_requested.emit(x, y)))
            if self._shape is not None:
                entries.append(('Show the whole image', self.fit_view))
            if entries:
                self._popup(event, entries)
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
    aggregation_edited: Signal(str, str)
        The user chose what the ROI gives: one of :data:`~.camera.MODES`
    channels_edited: Signal(str, str)
        The user ticked or unticked the box of a channel: the channels as they are written in the settings (``all``, or
        their numbers counted from 1: ``2, 3``)
    record_toggled: Signal(str, bool)
    plot_toggled: Signal(str, bool)
    delete_clicked: Signal(str)

    The boxes of the channels are there only if the images have several (:meth:`set_channel_count`): there is nothing to
    choose with one.
    """
    edges_edited = QtCore.Signal(str, float, float, float, float)
    name_edited = QtCore.Signal(str, str)
    aggregation_edited = QtCore.Signal(str, str)
    channels_edited = QtCore.Signal(str, str)
    record_toggled = QtCore.Signal(str, bool)
    plot_toggled = QtCore.Signal(str, bool)
    delete_clicked = QtCore.Signal(str)

    VALUE_TIP = ('What this ROI gives at each measurement: one number made of its pixels (mean, sum, median, min, max, '
                 'std: the standard deviation), or crop: its pixels themselves, saved as images (a tif file of their '
                 'own), with nothing to plot')
    PLOT_TIP = 'Show the value of this ROI in the plot (what is recorded does not change)'
    EDGE_NAMES = ('x0', 'x1', 'y0', 'y1')
    EDGE_TIPS = ('Left edge of the window (pixel)', 'Right edge of the window (pixel)',
                 'Top edge of the window (pixel, the first row is 0)', 'Bottom edge of the window (pixel)')

    def __init__(self, key: str, parent=None):
        super().__init__(parent)
        self.key = key
        self._name = ''
        self._color = ''
        self._locked = False
        self._crop = False
        self._channels: Optional[tuple] = None  # the channels the ROI is measured on (None: all of them)
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
        self.aggregation = QtWidgets.QComboBox()
        self.aggregation.addItems(MODES)
        self.aggregation.setFixedWidth(VALUE_WIDTH)
        self.aggregation.setToolTip(self.VALUE_TIP)
        self.channel_boxes: list[QtWidgets.QCheckBox] = []
        self.channels_cell = QtWidgets.QWidget()  # (no cell at all for an image with one channel)
        self._channels_layout = QtWidgets.QHBoxLayout(self.channels_cell)
        self._channels_layout.setContentsMargins(0, 0, 0, 0)
        self._channels_layout.setSpacing(CHANNEL_SPACING)
        self.channels_cell.setVisible(False)
        self.record = QtWidgets.QCheckBox()
        self.record.setToolTip('Save this ROI at each measurement: its value, or its pixels if it is a crop')
        self.plot = QtWidgets.QCheckBox()
        self.plot.setToolTip(self.PLOT_TIP)
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
        layout.addWidget(self.aggregation)
        layout.addWidget(self.channels_cell)
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
        self.aggregation.activated.connect(
            lambda _=0: self.aggregation_edited.emit(self.key, self.aggregation.currentText()))
        self.record.toggled.connect(lambda checked: self.record_toggled.emit(self.key, checked))
        self.plot.toggled.connect(lambda checked: self.plot_toggled.emit(self.key, checked))
        self.delete.clicked.connect(lambda _=False: self.delete_clicked.emit(self.key))

    @staticmethod
    def channel_tip(index: int, crop: bool = False) -> str:
        """What the box of a channel says when the mouse is over it"""
        if crop:
            return 'A crop keeps all the channels: the choice is for the ROIs which give a number'
        return f'{channel_name(index).capitalize()} channel (channel {index + 1} of the image). {CHANNELS_RULE}'

    def set_channel_count(self, n: int):
        """The images have `n` channels: a box for each (none, and no cell, for one channel: there is nothing to choose)"""
        n = n if n > 1 else 0
        if n == len(self.channel_boxes):
            return
        for box in self.channel_boxes:
            self._channels_layout.removeWidget(box)
            box.hide()  # (deleteLater() alone leaves it on the screen until the event loop runs)
            box.setParent(None)
            box.deleteLater()
        self.channel_boxes = []
        for index in range(n):
            box = QtWidgets.QCheckBox(channel_letter(index))
            box.toggled.connect(lambda checked, i=index: self._channel_toggled(i, checked))
            self._channels_layout.addWidget(box)
            self.channel_boxes.append(box)
        if n:
            self.channels_cell.setFixedWidth(channels_width(n))
        self.channels_cell.setVisible(n > 0)
        self._show_channels()

    def _show_channels(self):
        """The boxes show the channels of the ROI (all of them for a crop: it keeps them all)"""
        chosen = selected_channels(None if self._crop else self._channels, len(self.channel_boxes))
        for index, box in enumerate(self.channel_boxes):
            if box.isChecked() != (index in chosen):
                _quietly(box, box.setChecked, index in chosen)
            box.setToolTip(self.channel_tip(index, self._crop))
            box.setEnabled(not self._locked and not self._crop)

    def _channel_toggled(self, index: int, checked: bool):
        chosen = [position for position, box in enumerate(self.channel_boxes) if box.isChecked()]
        if not chosen:  # (a ROI is measured on one channel at least: this box stays ticked)
            box = self.channel_boxes[index]
            _quietly(box, box.setChecked, True)
            return
        self._channels = None if len(chosen) == len(self.channel_boxes) else tuple(chosen)
        self.channels_edited.emit(self.key, format_channels(self._channels))

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
        if self.aggregation.currentText() != roi.aggregation:
            _quietly(self.aggregation, self.aggregation.setCurrentText, roi.aggregation)
        # (a crop has no value to plot: its box is off and cannot be ticked until it is an aggregation again)
        for box, value in ((self.record, roi.record), (self.plot, roi.plot and not roi.is_crop)):
            if box.isChecked() != bool(value):
                _quietly(box, box.setChecked, bool(value))
        self.plot.setEnabled(not roi.is_crop)
        self.plot.setToolTip('A crop has no value to plot: its pixels are saved as images' if roi.is_crop
                             else self.PLOT_TIP)
        self._crop = roi.is_crop
        self._channels = roi.channels
        self._show_channels()

    def show_edges(self, x0: float, x1: float, y0: float, y1: float):
        """The rectangle is being dragged on the image: follow it (nothing is emitted)"""
        self._edges = [float(x0), float(x1), float(y0), float(y1)]
        for box, value in zip(self.boxes.values(), self._edges):
            if not self._shows(box, value):
                _quietly(box, box.setValue, value)

    def set_locked(self, locked: bool):
        """What is plotted can still be changed, the rest cannot"""
        self._locked = locked
        for widget in (self.name, *self.boxes.values(), self.aggregation, self.record, self.delete):
            widget.setEnabled(not locked)
        self._show_channels()


class RectRoiManager(QtWidgets.QWidget):
    """The ROIs of a camera, as a table with buttons to add and delete them (see :class:`~.roi_manager.RoiManager`)

    Attributes
    ----------
    edges_edited: Signal(str, float, float, float, float)
    name_edited: Signal(str, str)
    aggregation_edited: Signal(str, str)
    channels_edited: Signal(str, str)
    record_toggled: Signal(str, bool)
    plot_toggled: Signal(str, bool)
    delete_clicked: Signal(str)
    add_clicked: Signal()
    reset_clicked: Signal()
    """
    edges_edited = QtCore.Signal(str, float, float, float, float)
    name_edited = QtCore.Signal(str, str)
    aggregation_edited = QtCore.Signal(str, str)
    channels_edited = QtCore.Signal(str, str)
    record_toggled = QtCore.Signal(str, bool)
    plot_toggled = QtCore.Signal(str, bool)
    delete_clicked = QtCore.Signal(str)
    add_clicked = QtCore.Signal()
    reset_clicked = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: dict[str, RectRoiRow] = {}
        self._locked = False
        self._n_channels = 0  # (the boxes of the channels are shown for more than one)

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
                  [('aggregation', 'Value', VALUE_WIDTH), ('channels', 'Channels', channels_width(3)),
                   ('record', 'Record', CHECK_WIDTH), ('plot', 'Plot', CHECK_WIDTH), ('delete', '', DELETE_WIDTH)]
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
        self._labels['channels'].setVisible(False)  # (only for images which have several channels)
        self._labels['channels'].setToolTip(CHANNELS_RULE)

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

    @property
    def n_channels(self) -> int:
        """How many channels the images have, if there are several (0: the boxes of the channels are not shown)"""
        return self._n_channels

    def set_channels(self, n: int):
        """The images have `n` channels: each ROI gets a box for each of them, with the column titled *Channels*; nothing for
        one channel (there is nothing to choose)"""
        n = n if n > 1 else 0
        if n == self._n_channels:
            return
        self._n_channels = n
        label = self._labels['channels']
        if n:
            label.setFixedWidth(channels_width(n))
        label.setVisible(n > 0)
        for row in self._rows.values():
            row.set_channel_count(n)

    def _new_row(self, key: str) -> RectRoiRow:
        row = RectRoiRow(key)
        row.set_channel_count(self._n_channels)
        row.edges_edited.connect(self.edges_edited)
        row.name_edited.connect(self.name_edited)
        row.aggregation_edited.connect(self.aggregation_edited)
        row.channels_edited.connect(self.channels_edited)
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
