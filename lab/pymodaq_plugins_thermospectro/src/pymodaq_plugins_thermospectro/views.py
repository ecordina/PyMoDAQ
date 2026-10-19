"""What the user sees of a unit: its spectrum with the ROIs on it, and the ROIs (and the temperature) in time

Each view owns two docks, both of which can be closed at any time and opened again from the Plots menu of the
extension: a small plot of the spectrum, and a plot of the ROIs with the buttons showing or hiding each curve. The
temperature is drawn on the plot of the ROIs, on its own scale, at the right. It also owns a section
(:attr:`manager_box`) of the ROI manager, the one window where the ROIs of all the spectrometers are set up, added and
deleted.
"""
from __future__ import annotations

from qtpy import QtCore, QtWidgets

from pymodaq_gui.utils import Dock

from pymodaq_plugins_thermospectro.camera_widgets import ImagePlot, RectRoiManager
from pymodaq_plugins_thermospectro.roi_manager import RoiManager
from pymodaq_plugins_thermospectro.unit import Unit
from pymodaq_plugins_thermospectro.widgets import (CurvesPlot, PlotToggles, SpectrumPlot, TEMPERATURE_COLOR,
                                                   roi_color)

TEMPERATURE = 'temperature'  # the name of the button showing or hiding the temperature


class UnitView(QtCore.QObject):
    """The plots of a unit

    Parameters
    ----------
    unit: Unit

    Attributes
    ----------
    roi_edited: Signal(int, str, float, float)
        The user dragged a ROI on the spectrum, or typed its edges in the ROI manager: the number of the unit, the
        name of the ROI, its new edges
    add_requested: Signal(int, float)
        The user double clicked on the spectrum (or chose to add a ROI in its menu): a ROI is wanted there
    plot_toggled: Signal(int, str, bool)
        The user clicked a button (or a box of the ROI manager): the number of the unit, the name of the ROI (or
        ``temperature``), plotted or not
    roi_field_edited: Signal(int, str, str, object)
        The user changed the ``name`` or the ``record`` box of a ROI in the ROI manager: the number of the unit, the
        name of the ROI, the field and its new value
    add_clicked: Signal(int)
        The user clicked *Add a ROI* in the ROI manager
    delete_requested: Signal(int, str)
        The user wants a ROI deleted (the button of its row, or the menu of its window on the spectrum)
    reset_requested: Signal(int)
        The user clicked *Reset to the default ROIs*
    """
    roi_edited = QtCore.Signal(int, str, float, float)
    add_requested = QtCore.Signal(int, float)
    plot_toggled = QtCore.Signal(int, str, bool)
    roi_field_edited = QtCore.Signal(int, str, str, object)
    add_clicked = QtCore.Signal(int)
    delete_requested = QtCore.Signal(int, str)
    reset_requested = QtCore.Signal(int)

    def __init__(self, unit: Unit, parent=None):
        super().__init__(parent)
        self.unit = unit
        self.number = unit.number
        self._plotted: list[int] = []  # the columns of the series of ROIs which are drawn
        self._marker = None  # what was drawn last: nothing is redrawn if nothing changed

        self.spectrum_plot = self._make_plot()
        self._connect_plot()
        self.spectrum_dock = Dock(self.PLOT_NAME.format(self.number), closable=True, autoOrientation=False)
        self.spectrum_dock.addWidget(self.spectrum_plot)

        self.curves_plot = CurvesPlot('time', 'min', 'ROIs (mean of the pixels)', '')
        self.toggles = PlotToggles()
        self.toggles.toggled.connect(lambda name, plotted: self.plot_toggled.emit(self.number, name, plotted))
        container = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.toggles)
        layout.addWidget(self.curves_plot, 1)
        self.curves_dock = Dock(f'ROIs {self.number}', closable=True, autoOrientation=False)
        self.curves_dock.addWidget(container)

        self.manager = self._make_manager()
        self._connect_manager()
        self.manager.name_edited.connect(lambda key, name: self.roi_field_edited.emit(self.number, key, 'name', name))
        self.manager.record_toggled.connect(
            lambda key, record: self.roi_field_edited.emit(self.number, key, 'record', record))
        self.manager.plot_toggled.connect(lambda key, plotted: self.plot_toggled.emit(self.number, key, plotted))
        self.manager.add_clicked.connect(lambda: self.add_clicked.emit(self.number))
        self.manager.delete_clicked.connect(lambda key: self.delete_requested.emit(self.number, key))
        self.manager.reset_clicked.connect(lambda: self.reset_requested.emit(self.number))
        self.spectrum_plot.roi_moving.connect(self.manager.show_edges)  # (its numbers follow a window being dragged)
        self.manager_box = QtWidgets.QGroupBox(self.unit.label)
        box_layout = QtWidgets.QVBoxLayout(self.manager_box)
        box_layout.setContentsMargins(2, 2, 2, 2)
        box_layout.addWidget(self.manager)

    #: The title of the dock of the data (spectrum or image) of a unit
    PLOT_NAME = 'Spectrum {}'

    def _make_plot(self):
        return SpectrumPlot(self.unit.label)

    def _connect_plot(self):
        plot = self.spectrum_plot
        plot.roi_changed.connect(lambda key, start, stop: self.roi_edited.emit(self.number, key, start, stop))
        plot.add_requested.connect(lambda x: self.add_requested.emit(self.number, x))
        plot.delete_requested.connect(lambda key: self.delete_requested.emit(self.number, key))

    def _make_manager(self):
        return RoiManager()

    def _connect_manager(self):
        self.manager.edges_edited.connect(
            lambda key, start, stop: self.roi_edited.emit(self.number, key, start, stop))

    @property
    def docks(self) -> dict[str, Dock]:
        return {f'spectrum{self.number}': self.spectrum_dock, f'rois{self.number}': self.curves_dock}

    # ----------------------------------------------------------------------------------------------------------
    # what is shown
    # ----------------------------------------------------------------------------------------------------------
    @property
    def has_temperature(self) -> bool:
        return self.unit.thermostat is not None

    def update_definitions(self):
        """The ROIs, their plot buttons and the curves follow the settings of the unit"""
        settings = self.unit.settings
        keys, rois = settings.roi_keys, settings.rois
        self._update_roi_widgets(keys, rois)

        units = self.unit.axis_units
        self._plotted = [index for index, roi in enumerate(rois) if roi.plot]
        self.curves_plot.set_curves([(roi.label(units), roi_color(index), False)
                                     for index, roi in enumerate(rois) if roi.plot])
        entries = [(key, f'{index + 1}  {roi.name}', roi_color(index), roi.plot)
                   for index, (key, roi) in enumerate(zip(keys, rois))]
        show_temperature = self.has_temperature and settings.plot_temperature
        if self.has_temperature:
            entries.append((TEMPERATURE, 'T', TEMPERATURE_COLOR, settings.plot_temperature))
        if self.toggles.names != [entry[0] for entry in entries] or self.toggles.texts != [entry[1] for entry in entries]:
            self.toggles.set_entries(entries)
        else:
            for name, _, _, plotted in entries:
                self.toggles.set_plotted(name, plotted)

        self.curves_plot.set_secondary_label('temperature', self.unit.units, TEMPERATURE_COLOR)
        self.curves_plot.set_secondary_curves(
            [('temperature', TEMPERATURE_COLOR, False), ('set-point', TEMPERATURE_COLOR, True)]
            if show_temperature else [])
        self.set_title()
        self._marker = None

    def _update_roi_widgets(self, keys, rois):
        """The windows on the spectrum and the table of the ROI manager"""
        settings = self.unit.settings
        plot = self.spectrum_plot
        for key in plot.roi_keys:
            if key not in keys:
                plot.remove_roi(key)
        for index, (key, roi) in enumerate(zip(keys, rois)):
            plot.set_roi(key, roi.start, roi.stop, roi_color(index), str(index + 1))
        self.manager.set_rois(keys, rois, [roi_color(index) for index in range(len(keys))], settings.axis_units,
                              plot.x_range, plot.pitch)

    def set_title(self):
        title = self.unit.title
        text = f'{self.unit.label}: {title}' if self.unit.detector is not None else self.unit.label
        self.spectrum_plot.set_title(text)
        self.manager_box.setTitle(text)

    def clear(self, message: str = 'waiting for data'):
        self.spectrum_plot.clear_spectrum(message)
        self._marker = None

    def no_detector_message(self) -> str:
        return 'no spectrometer'

    def _show_data(self):
        """Draw the last spectrum"""
        unit = self.unit
        spectrum = unit.spectrum
        if spectrum is None:
            self.spectrum_plot.clear_spectrum('waiting for data' if unit.detector is not None
                                              else self.no_detector_message())
        else:
            before = self.spectrum_plot.x_range
            self.spectrum_plot.set_spectrum(spectrum.x, spectrum.y, spectrum.units, spectrum.axis.label or '')
            if self.spectrum_plot.x_range != before:  # the windows could not know the pixel size before
                self.update_definitions()

    def set_locked(self, locked: bool):
        """Forbid to move the ROIs on the spectrum, and to change them in the ROI manager"""
        self.spectrum_plot.set_locked(locked)
        self.manager.set_locked(locked)

    def refresh(self, span: float = 0.0):
        """Draw what the unit measured

        Parameters
        ----------
        span: float
            Show only the last `span` seconds of the curves (0: everything)
        """
        unit = self.unit
        if unit.spectrum_dirty:
            unit.spectrum_dirty = False
            self._show_data()
        marker = (id(unit.series_rois), len(unit.series_rois), len(unit.series_temperature), span)
        if marker == self._marker:
            return
        self._marker = marker
        series = unit.series_rois
        rows = series.last_rows(span)
        self.curves_plot.set_data(series.t[rows] / 60., [series.column(index)[rows] for index in self._plotted])
        if self.curves_plot.secondary_curves:
            series = unit.series_temperature
            rows = series.last_rows(span)
            self.curves_plot.set_secondary_data(series.t[rows] / 60.,
                                                [series.column(0)[rows], series.column(1)[rows]])


class CameraView(UnitView):
    """The plots of a camera unit: its image with a rectangle for each ROI, and the ROIs (and the temperature) in time

    The signals are the ones of :class:`UnitView`, except that a ROI has four edges, and a new one is asked for at a
    point (x, y) of the image: :attr:`roi_edited` is replaced by :attr:`rect_edited` and :attr:`add_requested` carries
    two numbers.
    """
    rect_edited = QtCore.Signal(int, str, float, float, float, float)
    add_requested = QtCore.Signal(int, float, float)
    PLOT_NAME = 'Image'

    def _make_plot(self):
        return ImagePlot(self.unit.label)

    def _connect_plot(self):
        plot = self.spectrum_plot
        plot.roi_changed.connect(lambda key, *edges: self.rect_edited.emit(self.number, key, *edges))
        plot.add_requested.connect(lambda x, y: self.add_requested.emit(self.number, x, y))
        plot.delete_requested.connect(lambda key: self.delete_requested.emit(self.number, key))

    def _make_manager(self):
        return RectRoiManager()

    def _connect_manager(self):
        self.manager.edges_edited.connect(lambda key, *edges: self.rect_edited.emit(self.number, key, *edges))

    def _update_roi_widgets(self, keys, rois):
        plot = self.spectrum_plot
        for key in plot.roi_keys:
            if key not in keys:
                plot.remove_roi(key)
        for index, (key, roi) in enumerate(zip(keys, rois)):
            plot.set_roi(key, roi.x0, roi.x1, roi.y0, roi.y1, roi_color(index), str(index + 1))
        self.manager.set_rois(keys, rois, [roi_color(index) for index in range(len(keys))], plot.shape)

    def clear(self, message: str = 'waiting for data'):
        self.spectrum_plot.clear_image(message)
        self._marker = None

    def no_detector_message(self) -> str:
        return 'no camera'

    def _show_data(self):
        unit = self.unit
        frame = unit.spectrum
        if frame is None:
            self.spectrum_plot.clear_image('waiting for data' if unit.detector is not None
                                           else self.no_detector_message())
        else:
            before = self.spectrum_plot.shape
            self.spectrum_plot.set_image(frame.y, frame.dwa.data, frame.dwa.labels)
            if self.spectrum_plot.shape != before:  # the rectangles could not know the size of the image before
                self.update_definitions()
