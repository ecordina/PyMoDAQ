"""Small plot widgets: the compact spectra, where the ROIs are edited, and the curves following them in time"""
from __future__ import annotations

from typing import Optional, Sequence

import numpy as np
import pyqtgraph as pg
from qtpy import QtCore, QtGui, QtWidgets

#: Colors of the ROIs (Tableau 10 without its red, which is the one of the temperature)
PALETTE = ['#1f77b4', '#ff7f0e', '#2ca02c', '#9467bd', '#8c564b',
           '#17becf', '#bcbd22', '#7f7f7f', '#e377c2', '#aec7e8']
#: The color of the temperature, on the scale at the right of the plots
TEMPERATURE_COLOR = '#d62728'


def roi_color(index: int) -> str:
    return PALETTE[index % len(PALETTE)]


class RoiRegion(pg.LinearRegionItem):
    """The window of a ROI on a spectrum: a right click on it (outside of a drag) asks for a menu"""
    context_requested = QtCore.Signal(object)  # the click

    def mouseClickEvent(self, ev):
        if ev.button() == QtCore.Qt.MouseButton.RightButton and not self.moving:
            ev.accept()
            self.context_requested.emit(ev)
            return
        super().mouseClickEvent(ev)


class SpectrumPlot(pg.PlotWidget):
    """A small plot of a spectrum, with a highlighted window for each ROI

    A window is moved by dragging it and resized by dragging one of its edges: this emits :attr:`roi_moving` all along
    and :attr:`roi_changed` when the mouse is released. A double click on the spectrum, or a right click and its menu,
    asks for a new ROI at this position with :attr:`add_requested`; a right click on a window offers to delete it
    (:attr:`delete_requested`).

    Parameters
    ----------
    title: str
        Name of the spectrometer
    """
    roi_changed = QtCore.Signal(str, float, float)  # ROI key, new start, new stop
    roi_moving = QtCore.Signal(str, float, float)  # the same, while the window is being dragged
    add_requested = QtCore.Signal(float)  # position
    delete_requested = QtCore.Signal(str)  # ROI key

    def __init__(self, title: str = '', parent=None):
        super().__init__(parent=parent)
        self._pitch = 0.0  # distance between two pixels
        self._x_range: Optional[tuple[float, float]] = None
        self._regions: dict[str, pg.LinearRegionItem] = {}
        self._labels: dict[str, pg.InfLineLabel] = {}
        self._updating = False
        self._locked = False
        self._menu: Optional[QtWidgets.QMenu] = None

        self.setMinimumHeight(70)
        self.setMinimumWidth(120)
        item = self.getPlotItem()
        item.setMenuEnabled(False)
        item.setMouseEnabled(False, False)
        item.hideButtons()
        item.showGrid(x=True, y=True, alpha=0.15)
        item.setTitle(title, size='8pt')
        for axis in ('left', 'bottom'):
            item.getAxis(axis).setStyle(tickFont=QtGui.QFont(self.font().family(), 7), tickTextOffset=2)
        item.getAxis('left').setWidth(42)
        self.curve = item.plot(pen=pg.mkPen(pg.getConfigOption('foreground'), width=1))
        self._message = pg.TextItem('waiting for data', color='#888888', anchor=(0.5, 0.5))
        item.addItem(self._message, ignoreBounds=True)
        self.scene().sigMouseClicked.connect(self._on_clicked)

    # -- spectrum ----------------------------------------------------------------------------------------------

    def set_title(self, title: str):
        self.getPlotItem().setTitle(title, size='8pt')

    def set_spectrum(self, x: np.ndarray, y: np.ndarray, units: str = '', label: str = ''):
        """Show the spectrum, its whole range is always visible"""
        x = np.asarray(x, dtype=float)
        y = np.asarray(y, dtype=float)
        self.curve.setData(x, y)
        finite = x[np.isfinite(x)]
        if finite.size == 0:
            return
        x_range = (float(finite.min()), float(finite.max()))
        self._pitch = (x_range[1] - x_range[0]) / max(x.size - 1, 1)
        if x_range != self._x_range:
            self._x_range = x_range
            self.getPlotItem().setXRange(*x_range, padding=0.0)
            self.getPlotItem().setLabel('bottom', label, units=units)
            self._message.setVisible(False)
            for region in self._regions.values():
                region.setBounds(x_range)

    def clear_spectrum(self, message: str = 'waiting for data'):
        """Forget the spectrum and tell why there is none"""
        self.curve.setData([], [])
        self._message.setText(message)
        self._message.setVisible(True)
        self._x_range = None

    @property
    def x_range(self) -> Optional[tuple[float, float]]:
        return self._x_range

    @property
    def pitch(self) -> float:
        """The distance between two pixels of the spectrum (0 as long as there is none)"""
        return self._pitch

    # -- ROIs --------------------------------------------------------------------------------------------------

    @staticmethod
    def _fill(color: str):
        fill = QtGui.QColor(color)
        fill.setAlpha(70)
        return pg.mkBrush(fill)

    def _make_movable(self, region: pg.LinearRegionItem):
        region.setMovable(not self._locked)

    def set_roi(self, key: str, start: float, stop: float, color: str, label: str = ''):
        """Create or move the window of a ROI, without emitting :attr:`roi_changed`

        Parameters
        ----------
        key: str
            The name of the ROI
        start, stop: float
            In the unit of the spectrum. A window is never drawn narrower than a pixel
        color: str
        label: str
            Short text written at the top of the window
        """
        low, high = (start, stop) if start <= stop else (stop, start)
        missing = max(self._pitch, 1e-12) - (high - low)
        if missing > 0:
            low, high = low - missing / 2., high + missing / 2.
        region = self._regions.get(key)
        self._updating = True
        try:
            if region is None:
                region = RoiRegion(values=(low, high), brush=self._fill(color), pen=pg.mkPen(color, width=2))
                region.setZValue(-10)
                if self._x_range is not None:
                    region.setBounds(self._x_range)
                region.sigRegionChanged.connect(lambda _=None, k=key: self._on_region_moving(k))
                region.sigRegionChangeFinished.connect(lambda _=None, k=key: self._on_region_finished(k))
                region.context_requested.connect(lambda ev, k=key: self._on_region_menu(k, ev))
                self.getPlotItem().addItem(region, ignoreBounds=True)
                self._regions[key] = region
                self._labels[key] = pg.InfLineLabel(region.lines[0], text=label, position=0.9, color=color,
                                                    anchors=[(1, 0.5), (1, 0.5)])
                self._make_movable(region)
            else:
                region.setRegion((low, high))
                region.setBrush(self._fill(color))
                for line in region.lines:
                    line.setPen(pg.mkPen(color, width=2))
                self._labels[key].setText(label, color=color)
        finally:
            self._updating = False

    def set_locked(self, locked: bool):
        """Forbid (or allow again) to move and resize the ROIs"""
        self._locked = locked
        for region in self._regions.values():
            self._make_movable(region)

    def remove_roi(self, key: str):
        region = self._regions.pop(key, None)
        self._labels.pop(key, None)
        if region is not None:
            self.getPlotItem().removeItem(region)

    @property
    def roi_keys(self) -> list[str]:
        return list(self._regions)

    def roi(self, key: str) -> tuple[float, float]:
        """(start, stop) of a ROI, as displayed"""
        low, high = self._regions[key].getRegion()
        return float(low), float(high)

    def _on_region_moving(self, key: str):
        if self._updating or key not in self._regions:
            return
        self.roi_moving.emit(key, *self.roi(key))

    def _on_region_finished(self, key: str):
        if self._updating or key not in self._regions:
            return
        self.roi_changed.emit(key, *self.roi(key))

    def _popup(self, event, text: str, slot):
        """A one-entry menu at the place of the click"""
        menu = QtWidgets.QMenu(self)
        menu.addAction(text).triggered.connect(lambda _=False: slot())
        menu.aboutToHide.connect(menu.deleteLater)
        self._menu = menu
        menu.popup(event.screenPos().toPoint())

    def _on_region_menu(self, key: str, event):
        """A right click on the window of a ROI: offer to delete it"""
        if self._locked or key not in self._regions:
            return
        label = self._labels[key].textItem.toPlainText()
        self._popup(event, f'Delete the ROI {label}' if label else 'Delete this ROI',
                    lambda: self.delete_requested.emit(key))

    def _position(self, event) -> Optional[float]:
        """Where a click is on the spectrum (None outside of it)"""
        item = self.getPlotItem()
        if not item.vb.sceneBoundingRect().contains(event.scenePos()):
            return None
        x = float(item.vb.mapSceneToView(event.scenePos()).x())
        if self._x_range is not None and self._x_range[0] <= x <= self._x_range[1]:
            return x
        return None

    def _on_clicked(self, event):
        if event.button() == QtCore.Qt.MouseButton.RightButton:
            if event.isAccepted() or self._locked:  # (a window took it: it offers to delete itself)
                return
            x = self._position(event)
            if x is not None:
                self._popup(event, f'Add a ROI here ({x:.1f})', lambda: self.add_requested.emit(x))
            return
        if not event.double():
            return
        x = self._position(event)
        if x is not None:
            self.add_requested.emit(x)


class CurvesPlot(pg.PlotWidget):
    """Curves following the ROIs in time, with a legend, and a second scale at the right for the temperature

    The x values (the time) are increasing: the curves are clipped to the view and downsampled.

    Parameters
    ----------
    x_label, x_units, y_label, y_units: str
    """

    def __init__(self, x_label: str = '', x_units: str = '', y_label: str = '', y_units: str = '', parent=None):
        super().__init__(parent=parent)
        self.curves: list[pg.PlotDataItem] = []
        self.secondary_curves: list[pg.PlotDataItem] = []
        self._right: Optional[pg.ViewBox] = None
        self.setMinimumHeight(80)
        item = self.getPlotItem()
        item.setMenuEnabled(True)
        item.showGrid(x=True, y=True, alpha=0.2)
        item.setLabel('bottom', x_label, units=x_units)
        item.setLabel('left', y_label, units=y_units)
        for axis in ('left', 'bottom'):
            item.getAxis(axis).enableAutoSIPrefix(False)
        self.legend = item.addLegend(offset=(5, 5), labelTextSize='8pt')

    @staticmethod
    def _make_curve(name: str, color: str, dashed: bool, clip: bool = True) -> pg.PlotDataItem:
        style = QtCore.Qt.PenStyle.DashLine if dashed else QtCore.Qt.PenStyle.SolidLine
        curve = pg.PlotDataItem(pen=pg.mkPen(color, width=1.5, style=style), name=name, connect='finite')
        curve.setClipToView(clip)
        curve.setDownsampling(auto=True, method='peak')
        return curve

    def set_curves(self, curves: Sequence[tuple[str, str, bool]]):
        """Replace the curves of the main scale (at the left)

        Parameters
        ----------
        curves: sequence of (name, color, dashed)
        """
        item = self.getPlotItem()
        for curve in self.curves:
            item.removeItem(curve)  # (it leaves the legend too)
        self.curves = []
        for name, color, dashed in curves:
            curve = self._make_curve(name, color, dashed)
            item.addItem(curve)  # (and joins the legend, as it has a name)
            self.curves.append(curve)

    def set_data(self, x: np.ndarray, ys: Sequence[np.ndarray]):
        """Update the curves: one `y` per curve, with a common `x`"""
        for curve, y in zip(self.curves, ys):
            curve.setData(x, y)

    # -- second scale ------------------------------------------------------------------------------------------

    def _ensure_right(self) -> pg.ViewBox:
        if self._right is None:
            item = self.getPlotItem()
            self._right = pg.ViewBox()
            item.scene().addItem(self._right)
            item.getAxis('right').linkToView(self._right)
            item.getAxis('right').enableAutoSIPrefix(False)
            self._right.setXLink(item)
            item.vb.sigResized.connect(self._update_right)
            self._update_right()
        return self._right

    def _update_right(self):
        if self._right is not None:
            item = self.getPlotItem()
            self._right.setGeometry(item.vb.sceneBoundingRect())
            self._right.linkedViewChanged(item.vb, self._right.XAxis)

    def set_secondary_label(self, label: str, units: str = '', color: Optional[str] = None):
        self._ensure_right()
        axis = self.getPlotItem().getAxis('right')
        axis.setLabel(label, units=units, color=color)
        if color is not None:
            axis.setPen(pg.mkPen(color))
            axis.setTextPen(pg.mkPen(color))

    def set_secondary_curves(self, curves: Sequence[tuple[str, str, bool]]):
        """Replace the curves of the second scale (at the right), which is only shown if there are some

        Parameters
        ----------
        curves: sequence of (name, color, dashed)
        """
        item = self.getPlotItem()
        right = self._ensure_right()
        for curve in self.secondary_curves:
            right.removeItem(curve)
            self.legend.removeItem(curve)
        self.secondary_curves = []
        for name, color, dashed in curves:
            curve = self._make_curve(name, color, dashed, clip=False)  # (its scale is fitted to all its data)
            right.addItem(curve)
            self.legend.addItem(curve, name)
            self.secondary_curves.append(curve)
        if self.secondary_curves:
            item.showAxis('right')
        else:
            item.hideAxis('right')
        self._update_right()

    def set_secondary_data(self, x: np.ndarray, ys: Sequence[np.ndarray]):
        """Update the curves of the scale at the right: it always fits all of them"""
        for curve, y in zip(self.secondary_curves, ys):
            curve.setData(x, y)
        finite = [np.asarray(y, dtype=float)[np.isfinite(y)] for y in ys[:len(self.secondary_curves)]]
        finite = np.concatenate(finite) if finite else np.array([])
        if finite.size and self._right is not None:
            low, high = float(finite.min()), float(finite.max())
            if high - low < 1e-9:
                low, high = low - 0.5, high + 0.5
            self._right.setYRange(low, high, padding=0.05)

    @property
    def has_secondary_scale(self) -> bool:
        """The scale at the right is shown"""
        return bool(self.secondary_curves) and self.getPlotItem().getAxis('right').isVisible()


class PlotToggles(QtWidgets.QWidget):
    """A row of buttons, one per curve, to show or hide it

    What is recorded does not depend on these buttons: it is only about what is plotted.
    """
    toggled = QtCore.Signal(str, bool)  # name of the curve, plotted

    def __init__(self, parent=None):
        super().__init__(parent)
        self._buttons: dict[str, QtWidgets.QPushButton] = {}
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.addWidget(QtWidgets.QLabel('Plot:'))
        self._inner = QtWidgets.QWidget()
        self._row = QtWidgets.QHBoxLayout(self._inner)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.addStretch(1)
        area = QtWidgets.QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        area.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidget(self._inner)
        area.setFixedHeight(44)
        layout.addWidget(area, 1)

    def set_entries(self, entries: Sequence[tuple[str, str, str, bool]]):
        """Replace the buttons

        Parameters
        ----------
        entries: sequence of (name, text, color, plotted)
        """
        for button in self._buttons.values():
            self._row.removeWidget(button)
            button.hide()  # (deleteLater() is not enough: the old buttons stay on screen until the event loop runs)
            button.setParent(None)
            button.deleteLater()
        self._buttons = {}
        for position, (name, text, color, plotted) in enumerate(entries):
            button = QtWidgets.QPushButton(text)
            button.setCheckable(True)
            button.setChecked(plotted)
            button.setToolTip('Show or hide this curve in the plot (what is recorded does not change)')
            button.setStyleSheet(
                f'QPushButton {{ border: 2px solid {color}; border-radius: 4px; padding: 2px 8px; }}'
                f'QPushButton:checked {{ background-color: {color}; color: white; }}')
            button.toggled.connect(lambda checked, key=name: self.toggled.emit(key, checked))
            self._row.insertWidget(position, button)
            self._buttons[name] = button

    def button(self, name: str) -> QtWidgets.QPushButton:
        return self._buttons[name]

    @property
    def names(self) -> list[str]:
        return list(self._buttons)

    @property
    def texts(self) -> list[str]:
        return [button.text() for button in self._buttons.values()]

    def set_plotted(self, name: str, plotted: bool):
        """Follow a change made elsewhere, without emitting :attr:`toggled`"""
        button = self._buttons.get(name)
        if button is not None and button.isChecked() != plotted:
            button.blockSignals(True)
            button.setChecked(plotted)
            button.blockSignals(False)
