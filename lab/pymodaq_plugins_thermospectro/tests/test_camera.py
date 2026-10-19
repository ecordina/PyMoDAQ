"""The camera of the extension: images and rectangular ROIs, the settings, the unit, the widgets and the file"""
import math
import time
from types import SimpleNamespace

import numpy as np
import pytest
from qtpy import QtCore

from pymodaq_gui.parameter import Parameter

from pymodaq_data import DataToExport
from pymodaq_data.data import DataRaw

from fakes import FakeCamera, FakeManager, FakeThermostat
from pymodaq_plugins_thermospectro import camera
from pymodaq_plugins_thermospectro.camera import (Frame, RectRoi, default_rect_rois, extract_frame, fit_to_image,
                                                  pixel_range, rect_value)
from pymodaq_plugins_thermospectro.camera_widgets import ImagePlot, RectRoiManager
from pymodaq_plugins_thermospectro.ramp import Phase
from pymodaq_plugins_thermospectro.rois import Roi, RoiStore
from pymodaq_plugins_thermospectro.saving import UnitRecording, build_dtes
from pymodaq_plugins_thermospectro.unit import CameraUnit, Measurement, Problem, UnitState
from pymodaq_plugins_thermospectro.unit_settings import CAMERA, NONE, CameraSettings, unit_definition
from pymodaq_plugins_thermospectro.views import CameraView

pytestmark = pytest.mark.filterwarnings('ignore')


def gradient(width=120, height=80) -> np.ndarray:
    """The pixel of column x and row y is x + 100 y"""
    rows, columns = np.mgrid[0:height, 0:width]
    return columns + 100. * rows


class TestPixels:
    @pytest.mark.parametrize('low, high, size, expected', [
        (10., 20., 100, (10, 20)),
        (20., 10., 100, (10, 20)),  # (any order)
        (10.4, 20.4, 100, (10, 20)),  # (the nearest pixel edge)
        (10.6, 20.6, 100, (11, 21)),
        (30., 30., 100, (30, 31)),  # (a window narrower than a pixel still takes one)
        (-5., 10., 100, (0, 10)),  # (clipped to the image)
        (90., 200., 100, (90, 100)),
        (100., 120., 100, (0, 0)),  # (outside)
        (-20., -10., 100, (0, 0)),
    ])
    def test_the_pixels_of_a_window(self, low, high, size, expected):
        assert pixel_range(low, high, size) == expected

    def test_the_mean_of_a_rectangle(self):
        image = gradient()

        # columns 10..19 (mean 14.5), rows 20..29 (mean 24.5)
        assert rect_value(image, 10, 20, 20, 30) == pytest.approx(14.5 + 100. * 24.5)

    def test_a_rectangle_outside_is_nan(self):
        assert math.isnan(rect_value(gradient(), 200, 300, 10, 20))
        assert math.isnan(rect_value(gradient(), 10, 20, 100, 200))

    def test_a_rectangle_partly_outside_takes_what_is_inside(self):
        image = gradient()

        assert rect_value(image, 110, 200, 0, 1) == pytest.approx(np.mean(image[0:1, 110:120]))

    def test_the_whole_image(self):
        image = gradient()

        assert rect_value(image, 0, 1e9, 0, 1e9) == pytest.approx(image.mean())

    def test_it_is_not_for_a_spectrum(self):
        with pytest.raises(ValueError, match='2D'):
            rect_value(np.arange(10.), 0, 5, 0, 1)


class TestRectRoi:
    def test_value_and_label(self):
        roi = RectRoi('Spot', 10, 20, 20, 30)

        assert roi.value(gradient()) == pytest.approx(14.5 + 100. * 24.5)
        assert roi.label() == 'Spot'
        assert roi.label(with_range=True) == 'Spot (x 10-20, y 20-30 pixel)'
        assert (roi.width, roi.height) == (10, 10)

    def test_what_is_wrong_with_a_roi(self):
        assert RectRoi('a', 1, 2, 3, 4).problems() == []
        assert 'x' in RectRoi('a', 5, 2, 3, 4).problems()[0]
        assert 'y' in RectRoi('a', 1, 2, 9, 4).problems()[0]
        assert 'finite' in RectRoi('a', math.nan, 2, 3, 4).problems()[0]

    def test_it_is_saved_and_read_back(self):
        roi = RectRoi('Spot', 10, 20, 30, 40, record=False, plot=True)

        assert RectRoi.from_dict(roi.to_dict()) == roi

    @pytest.mark.parametrize('values', [{}, {'name': 'x'}, {'name': 'x', 'x0': 'a', 'x1': 1, 'y0': 1, 'y1': 2},
                                        {'name': 'x', 'x0': 5, 'x1': 1, 'y0': 1, 'y1': 2},
                                        {'name': 'x', 'start': 1, 'stop': 2}])  # (the ROI of a spectrum)
    def test_what_is_not_a_roi_is_refused(self, values):
        with pytest.raises(ValueError):
            RectRoi.from_dict(values)

    def test_the_default_ones_are_always_the_same_and_do_not_overlap(self):
        rois = default_rect_rois(3)

        assert rois == default_rect_rois(3)
        assert rois != default_rect_rois(4)
        assert len(rois) == camera.N_DEFAULT
        assert all(roi.problems() == [] for roi in rois)
        assert all(0 <= roi.x0 and roi.x1 <= 1000 and 0 <= roi.y0 and roi.y1 <= 1000 for roi in rois)
        for index, first in enumerate(rois):
            for second in rois[index + 1:]:
                assert first.x1 <= second.x0 or second.x1 <= first.x0 or first.y1 <= second.y0 or second.y1 <= first.y0

    def test_the_default_ones_are_fitted_to_the_image(self):
        rois = default_rect_rois(3)

        fitted = fit_to_image(rois, (500, 2000))

        assert fitted[0].x0 == pytest.approx(rois[0].x0 * 2, abs=1)
        assert fitted[0].y1 == pytest.approx(rois[0].y1 / 2, abs=1)


class TestFrames:
    def test_the_first_2d_data(self):
        dwa = DataRaw('image', data=[gradient(), gradient() + 1])

        found = extract_frame(DataToExport('camera', data=[dwa]))

        assert found[0] is dwa and np.array_equal(found[1], gradient())  # (the first channel)

    def test_no_image_in_a_spectrum(self):
        dwa = DataRaw('spectrum', data=[np.arange(10.)])

        assert extract_frame(DataToExport('camera', data=[dwa])) is None

    def test_a_frame(self):
        frame = Frame(gradient(), 1.0, DataRaw('image', data=[gradient()]))

        assert frame.shape == (80, 120) and frame.image is frame.y and frame.axis is None and frame.units == 'pixel'


class TestRoiStore:
    def test_the_rois_of_a_camera_are_remembered_with_those_of_the_spectrometers(self, tmp_path):
        store = RoiStore(tmp_path / 'rois.json')
        store.save(1, [Roi('A', 500., 510.)])
        rects = [RectRoi('Spot', 10, 20, 30, 40)]

        store.save(3, rects)

        assert store.get(3, RectRoi) == rects
        assert store.get(1) == [Roi('A', 500., 510.)]

    def test_rois_of_the_other_kind_are_not_read(self, tmp_path):
        store = RoiStore(tmp_path / 'rois.json')
        store.save(3, [RectRoi('Spot', 10, 20, 30, 40)])

        assert store.get(3) is None  # (as the ROIs of a spectrometer)
        assert store.get(1, RectRoi) is None

    def test_the_default_rois_are_made_and_kept(self, tmp_path):
        store = RoiStore(tmp_path / 'rois.json')

        first = store.load(3, RectRoi, default_rect_rois)

        assert first == default_rect_rois(3)
        assert store.get(3, RectRoi) == first


class TestCameraSettings:
    @pytest.fixture
    def settings(self):
        return CameraSettings(Parameter.create(**unit_definition(3, default_rect_rois(3), kind=CAMERA)), 3)

    def test_the_tree_is_the_one_of_a_camera(self, settings):
        param = settings.param

        assert param.title() == 'Camera:' and param.name() == 'unit3'
        assert param.child('camera').title() == 'Camera:'
        assert param.child('integration_time').title() == 'Exposure time:'
        assert not param.child('record', 'image').value()  # (an image is large: not by default)
        assert [child.name() for child in param.child('record').children()] == ['image', 'temperature']
        assert not hasattr(param.child('record'), 'spectrum') and 'spectrum' not in param.child('record').names
        assert settings.roi_keys == ['roi00', 'roi01', 'roi02', 'roi03']

    def test_the_spectrometer_tree_is_unchanged(self):
        param = Parameter.create(**unit_definition(1, [Roi('A', 500., 510.)]))

        assert param.title() == 'Spectrometer 1:'
        assert param.child('spectrometer').title() == 'Spectrometer:'
        assert param.child('record', 'spectrum').value() is True and 'image' not in param.child('record').names

    def test_the_rois_are_read(self, settings):
        roi = settings.roi('roi00')

        assert roi == default_rect_rois(3)[0]
        assert settings.rois == default_rect_rois(3)

    def test_the_edges_are_in_the_right_order(self, settings):
        group = settings.param.child('rois', 'roi00')
        group['x0'], group['x1'], group['y0'], group['y1'] = 50., 10., 90., 20.

        assert settings.roi('roi00') == RectRoi('ROI 1', 10., 50., 20., 90.)

    def test_the_modules_are_offered_in_the_camera_list(self, settings):
        settings.set_modules(['Camera1'], ['Thermo1'], 'Camera1', 'Thermo1')

        assert settings.spectrometer == 'Camera1' and settings.thermostat == 'Thermo1'
        assert settings.param.child('camera').opts['limits'] == [NONE, 'Camera1']

    def test_roi_changes_are_classified(self, settings):
        group = settings.param.child('rois', 'roi01')

        assert settings.classify(group.child('x0')) == ('roi_edges', 'roi01')
        assert settings.classify(group.child('y1')) == ('roi_edges', 'roi01')
        assert settings.classify(group.child('name')) == ('roi_name', 'roi01')
        assert settings.classify(group.child('plot')) == ('roi_plot', 'roi01')
        assert settings.classify(settings.param.child('camera')) == ('modules', 'camera')
        assert settings.classify(settings.param.child('record', 'image')) == ('record', 'image')

    def test_a_roi_is_added_around_a_point(self, settings):
        key = settings.add_roi(settings.new_roi(300., 200.))

        roi = settings.roi(key)
        assert key == 'roi04' and roi.name == 'ROI 5'
        assert (roi.x0 + roi.x1) / 2 == pytest.approx(300., abs=1) and (roi.y0 + roi.y1) / 2 == pytest.approx(200., abs=1)

    def test_a_new_roi_is_as_large_as_the_last_one(self, settings):
        last = settings.rois[-1]

        new = settings.new_roi(500., 500.)

        assert new.width == pytest.approx(max(last.width, last.height), abs=1)

    def test_the_edges_are_set(self, settings):
        settings.set_roi_edges('roi00', 1, 2, 3, 4)

        assert settings.roi('roi00') == RectRoi('ROI 1', 1, 2, 3, 4)

    def test_the_default_rois_are_fitted_to_the_first_image(self, settings):
        assert settings.fit_default_rois((500, 2000)) is True

        assert settings.rois == fit_to_image(default_rect_rois(3), (500, 2000))

    def test_rois_which_were_set_are_left_alone(self, settings):
        settings.set_roi_edges('roi00', 1, 2, 3, 4)
        before = settings.rois

        assert settings.fit_default_rois((500, 2000)) is False and settings.rois == before

    def test_the_nominal_image_needs_no_fitting(self, settings):
        assert settings.fit_default_rois(camera.NOMINAL_SHAPE) is False

    def test_locked_while_an_experiment_runs(self, settings):
        settings.lock(True)

        assert not settings.param.child('rois', 'roi00', 'x0').opts['enabled']
        assert settings.param.child('rois', 'roi00', 'plot').opts['enabled']  # (what is plotted can change)


class Bench:
    """A camera unit with a fake camera and a fake thermostat"""

    def __init__(self, qtbot, thermostat=True, **camera_kwargs):
        self.qtbot = qtbot
        self.camera = FakeCamera('Camera1', **camera_kwargs)
        self.thermostat = FakeThermostat('Thermo1') if thermostat else None
        self.settings = CameraSettings(Parameter.create(**unit_definition(3, default_rect_rois(3), kind=CAMERA)), 3)
        self.settings.set_modules(['Camera1'], ['Thermo1'] if thermostat else [], 'Camera1',
                                  'Thermo1' if thermostat else None)
        self.settings.set_rois([RectRoi('Spot', 10, 20, 20, 30), RectRoi('Corner', 100, 120, 60, 80)])
        self.settings.child('integration_time').setValue(20.)
        self.settings.child('readout_time').setValue(0.1)
        self.unit = CameraUnit(self.settings, FakeManager(*[m for m in (self.camera, self.thermostat) if m]))
        self.unit.attach()
        self.measurements = []
        self.notices = []
        self.axis_units = []
        self.unit.measurement.connect(self.measurements.append)
        self.unit.notice.connect(self.notices.append)
        self.unit.axis_units_changed.connect(self.axis_units.append)

    def wait_for(self, n, timeout=10000):
        self.qtbot.waitUntil(lambda: len(self.measurements) >= n, timeout=timeout)


@pytest.fixture
def bench(qtbot):
    bench = Bench(qtbot)
    yield bench
    if bench.unit.state != UnitState.IDLE:
        bench.unit._release()


class TestCameraUnit:
    def test_the_messages_call_it_the_camera(self, bench):
        unit = bench.unit

        assert unit.label == 'Camera' and unit.title == 'Camera1' and unit.units == '°C'
        assert unit.status_text() == 'Camera'

    def test_a_valid_unit_has_no_problem(self, bench):
        assert bench.unit.problems() == []

    def test_no_camera_selected(self, bench):
        bench.settings.child('camera').setValue(NONE)

        assert 'Camera: no camera is selected' in bench.unit.problems()[0]

    def test_a_readout_time_shorter_than_the_exposure(self, bench):
        bench.settings.child('integration_time').setValue(500.)
        bench.settings.child('readout_time').setValue(0.2)

        assert 'exposure time' in bench.unit.problems()[0]

    def test_the_exposure_time_is_set_on_the_camera(self, bench):
        bench.settings.child('integration_time').setValue(33.)

        bench.unit.apply_integration_time()

        assert bench.camera.settings['camera_settings', 'exposure_time'] == pytest.approx(33.)

    def test_it_measures_the_rois_of_the_image(self, bench):
        bench.unit.start_live(time.time())
        bench.wait_for(2)

        measurement = bench.measurements[-1]
        assert isinstance(measurement.spectrum, Frame) and measurement.spectrum.shape == (80, 120)
        assert measurement.values[0] == pytest.approx(14.5 + 100. * 24.5)
        assert measurement.values[1] == pytest.approx(109.5 + 100. * 69.5)
        assert [roi.name for roi in measurement.rois] == ['Spot', 'Corner']
        assert measurement.temperature == pytest.approx(20.)
        assert bench.unit.series_rois.n_columns == 2 and len(bench.unit.series_rois) >= 2

    def test_the_units_of_the_image_are_told_once(self, bench):
        bench.unit.start_live(time.time())
        bench.wait_for(3)

        assert bench.axis_units == ['pixel'] and bench.unit.axis_units == 'pixel'

    def test_a_roi_outside_of_the_image_is_nan_and_said_once(self, bench, caplog):
        bench.settings.set_rois([RectRoi('Far', 500, 600, 500, 600)])
        bench.unit.start_live(time.time())
        bench.wait_for(3)

        assert all(math.isnan(m.values[0]) for m in bench.measurements)
        assert sum('outside of the image' in record.getMessage() for record in caplog.records) == 1

    def test_a_spectrum_is_not_an_image(self, bench):
        bench.unit._store_data(DataToExport('x', data=[DataRaw('spectrum', data=[np.arange(10.)])]))

        assert bench.unit.spectrum is None

    def test_the_silent_camera_is_told(self, bench):
        bench.camera.silent = True
        bench.unit.start_live(time.time())
        bench.unit._started_at -= 20.

        messages = bench.unit.live_notices(time.monotonic())

        assert messages == ['Camera1 has delivered no 2D data (an image)']

    def test_the_thermostat_ramp_runs_as_for_a_spectrometer(self, bench):
        settings = bench.settings
        for name, value in dict(initial=20., final=23., velocity=90., n_cycles=1).items():  # (the thermostat is at 20)
            settings.child('ramp', name).setValue(value)
        settings.refresh_ramp()
        now = {'t': 0.0}
        bench.unit.clock = lambda: now['t']
        bench.unit.start_live(time.time())
        bench.qtbot.waitUntil(lambda: bench.unit.ready, timeout=10000)

        bench.unit.begin_run(time.time(), 0.0)
        now['t'] = 1.0
        state = bench.unit.program_tick(1.0)

        assert state.phase == Phase.RAMP and bench.thermostat.targets[-1] == pytest.approx(21.5)
        assert 'Camera: cycle' in bench.unit.status_text()

    @pytest.mark.errors_expected
    def test_a_camera_which_is_closed_is_a_problem_of_the_camera(self, bench):
        problems = []
        bench.unit.problem.connect(lambda unit, kind, message: problems.append((kind, message)))
        bench.unit.start_live(time.time())
        bench.wait_for(1)
        bench.unit.begin_run(time.time(), 0.0)

        bench.camera.close()

        assert problems and problems[0][0] == Problem.SPECTROMETER and 'The camera Camera1 was closed' in problems[0][1]

    def test_a_unit_without_thermostat(self, qtbot):
        bench = Bench(qtbot, thermostat=False)
        bench.unit.start_live(time.time())
        bench.wait_for(2)

        assert math.isnan(bench.measurements[-1].temperature)
        bench.unit._release()


class TestImagePlot:
    @pytest.fixture
    def plot(self, qtbot):
        widget = ImagePlot('Camera')
        qtbot.addWidget(widget)
        widget.resize(400, 300)
        widget.show()
        widget.set_image(gradient())
        return widget

    def test_the_whole_image_is_visible(self, plot):
        assert plot.shape == (80, 120)
        x_range, y_range = plot.getPlotItem().viewRange()  # (the aspect ratio is kept: one range may be larger)
        assert x_range[0] <= 0.5 and x_range[1] >= 119.5 and y_range[0] <= 0.5 and y_range[1] >= 79.5

    def test_no_image(self, plot):
        plot.clear_image('no camera')

        assert plot.shape is None and plot._message.toPlainText() == 'no camera' and plot._message.isVisible()

    def test_a_rectangle_for_a_roi(self, plot):
        plot.set_roi('roi00', 10, 20, 30, 50, '#1f77b4', '1')

        assert plot.roi_keys == ['roi00']
        assert plot.roi('roi00') == pytest.approx((10, 20, 30, 50))

    def test_a_rectangle_is_never_smaller_than_a_pixel(self, plot):
        plot.set_roi('roi00', 10, 10, 30, 30, '#1f77b4', '1')

        x0, x1, y0, y1 = plot.roi('roi00')
        assert (x1 - x0, y1 - y0) == pytest.approx((1, 1))

    def test_setting_a_rectangle_emits_nothing(self, plot, qtbot):
        with qtbot.assertNotEmitted(plot.roi_changed), qtbot.assertNotEmitted(plot.roi_moving):
            plot.set_roi('roi00', 10, 20, 30, 50, '#1f77b4', '1')
            plot.set_roi('roi00', 11, 21, 31, 51, '#1f77b4', '1')

        assert plot.roi('roi00') == pytest.approx((11, 21, 31, 51))

    def test_dragging_a_rectangle_emits_its_edges(self, plot, qtbot):
        plot.set_roi('roi00', 10, 20, 30, 50, '#1f77b4', '1')

        with qtbot.waitSignal(plot.roi_changed) as signal:
            plot._rois['roi00'].setPos((15, 35))
            plot._rois['roi00'].sigRegionChangeFinished.emit(plot._rois['roi00'])

        assert signal.args == ['roi00', 15, 25, 35, 55]

    def test_a_rectangle_is_removed(self, plot):
        plot.set_roi('roi00', 10, 20, 30, 50, '#1f77b4', '1')

        plot.remove_roi('roi00')

        assert plot.roi_keys == []

    def test_a_locked_plot_does_not_move_its_rectangles(self, plot):
        plot.set_roi('roi00', 10, 20, 30, 50, '#1f77b4', '1')

        plot.set_locked(True)

        assert plot._rois['roi00'].translatable is False
        plot.set_locked(False)
        assert plot._rois['roi00'].translatable is True

    def test_the_colors_of_a_camera_are_shown_as_channels(self, plot):
        red, green, blue = gradient(), gradient() + 1, gradient() + 2

        plot.set_image(red, [red, green, blue], ['R', 'G', 'B'])

        shown = plot.viewer._raw_data
        assert len(shown) == 3 and list(shown.labels) == ['R', 'G', 'B']
        assert np.array_equal(shown.data[2], blue) and plot.shape == (80, 120)

    def test_only_three_channels_are_shown_and_a_channel_of_another_size_is_not(self, plot):
        image = gradient()
        small = np.zeros((5, 5))

        plot.set_image(image, [image, image, image, image])
        assert len(plot.viewer._raw_data) == 3

        plot.set_image(image, [small, small])  # (they do not fit the image the ROIs are measured on)
        assert len(plot.viewer._raw_data) == 1 and plot.viewer._raw_data.data[0].shape == (80, 120)

    def test_the_channels_are_named_when_the_camera_does_not(self, plot):
        image = gradient()

        plot.set_image(image, [image, image])

        assert list(plot.viewer._raw_data.labels) == ['channel 0', 'channel 1']

    def test_the_tools_which_move_the_image_are_not_offered(self, plot):
        """The ROIs are drawn on the raw pixels: flipping or turning the image would put them at the wrong place"""
        for name in ('flip_ud', 'flip_lr', 'rotate'):
            assert not plot.view.get_action(name).isVisible()
        assert plot.view.get_action('autolevels').isVisible()  # (the other tools of the viewer are there)

    def test_the_levels_follow_the_image_by_default(self, plot):
        assert plot.view.is_action_checked('autolevels')

    def test_the_whole_image_comes_back_after_a_zoom(self, plot):
        plot.getPlotItem().setRange(xRange=(10, 20), yRange=(10, 20), padding=0.0)
        plot._user_view = True

        plot.fit_view()

        x_range, _ = plot.getPlotItem().viewRange()
        assert plot._user_view is False and x_range[0] <= 0.5 and x_range[1] >= 119.5

    def test_the_menu_of_a_right_click_offers_to_show_the_whole_image(self, plot, monkeypatch):
        offered = []
        monkeypatch.setattr(plot, '_popup', lambda event, entries: offered.append([text for text, _ in entries]))
        scene_position = plot.getPlotItem().vb.mapViewToScene(QtCore.QPointF(60., 40.))
        click = SimpleNamespace(double=lambda: False, button=lambda: QtCore.Qt.MouseButton.RightButton,
                                isAccepted=lambda: False, scenePos=lambda: scene_position)

        plot._on_clicked(click)
        plot.set_locked(True)
        plot._on_clicked(click)

        assert offered[0] == ['Add a ROI here (60, 40)', 'Show the whole image']
        assert offered[1] == ['Show the whole image']  # (nothing can be added during an experiment)

    def test_a_right_click_on_a_rectangle_is_left_to_it(self, plot, monkeypatch):
        offered = []
        monkeypatch.setattr(plot, '_popup', lambda event, entries: offered.append(entries))
        click = SimpleNamespace(double=lambda: False, button=lambda: QtCore.Qt.MouseButton.RightButton,
                                isAccepted=lambda: True, scenePos=lambda: QtCore.QPointF(0., 0.))

        plot._on_clicked(click)

        assert offered == []

    def test_a_double_click_asks_for_a_roi(self, plot, qtbot):
        scene_position = plot.getPlotItem().vb.mapViewToScene(QtCore.QPointF(60., 40.))
        click = SimpleNamespace(double=lambda: True, button=lambda: QtCore.Qt.MouseButton.LeftButton,
                                scenePos=lambda: scene_position)

        with qtbot.waitSignal(plot.add_requested) as signal:
            plot._on_clicked(click)

        assert signal.args == pytest.approx([60., 40.], abs=0.5)

    def test_a_double_click_outside_of_the_image_asks_for_nothing(self, plot, qtbot):
        scene_position = plot.getPlotItem().vb.mapViewToScene(QtCore.QPointF(500., 40.))
        click = SimpleNamespace(double=lambda: True, button=lambda: QtCore.Qt.MouseButton.LeftButton,
                                scenePos=lambda: scene_position)

        with qtbot.assertNotEmitted(plot.add_requested):
            plot._on_clicked(click)

    def test_the_rectangles_stay_inside_of_the_image(self, plot):
        plot.set_roi('roi00', 10, 20, 30, 50, '#1f77b4', '1')

        bounds = plot._rois['roi00'].maxBounds

        assert (bounds.width(), bounds.height()) == (120., 80.)


class TestRectRoiManager:
    @pytest.fixture
    def manager(self, qtbot):
        widget = RectRoiManager()
        qtbot.addWidget(widget)
        widget.show()
        rois = [RectRoi('A', 10, 20, 30, 40), RectRoi('B', 50, 60, 5, 15, record=False, plot=False)]
        widget.set_rois(['roi00', 'roi01'], rois, ['#1f77b4', '#ff7f0e'], (80, 120))
        return widget

    def test_a_row_for_each_roi(self, manager):
        assert manager.keys == ['roi00', 'roi01']
        row = manager.row('roi01')
        assert row.name.text() == 'B' and row.badge.text() == '2'
        assert [box.value() for box in row.boxes.values()] == [50, 60, 5, 15]
        assert not row.record.isChecked() and not row.plot.isChecked()

    def test_the_headers(self, manager):
        assert [manager.header_text(name) for name in ('x0', 'x1', 'y0', 'y1')] == ['x from', 'x to', 'y from', 'y to']

    def test_typing_an_edge_emits_the_four_edges(self, manager, qtbot):
        row = manager.row('roi00')

        with qtbot.waitSignal(manager.edges_edited) as signal:
            row.boxes['y1'].setValue(45)

        assert signal.args == ['roi00', 10, 20, 30, 45]

    def test_an_edge_past_the_other_pushes_it(self, manager, qtbot):
        row = manager.row('roi00')

        with qtbot.waitSignal(manager.edges_edited) as signal:
            row.boxes['x0'].setValue(25)  # the window was 10 wide

        assert signal.args == ['roi00', 25, 35, 30, 40]

    def test_showing_a_roi_emits_nothing(self, manager, qtbot):
        with qtbot.assertNotEmitted(manager.edges_edited), qtbot.assertNotEmitted(manager.name_edited):
            manager.set_rois(['roi00'], [RectRoi('C', 1, 2, 3, 4)], ['#1f77b4'], (80, 120))

        assert manager.keys == ['roi00'] and manager.row('roi00').name.text() == 'C'

    def test_a_rectangle_outside_of_the_image_is_red(self, manager):
        manager.set_rois(['roi00'], [RectRoi('Far', 500, 600, 5, 15)], ['#1f77b4'], (80, 120))

        assert 'd62728' in manager.row('roi00').boxes['x0'].styleSheet()

    def test_the_numbers_follow_a_rectangle_being_dragged(self, manager):
        manager.show_edges('roi00', 11, 21, 31, 41)

        assert [box.value() for box in manager.row('roi00').boxes.values()] == [11, 21, 31, 41]

    def test_the_buttons_and_the_boxes_say_what_the_user_does(self, manager, qtbot):
        with qtbot.waitSignal(manager.delete_clicked) as deleted:
            manager.row('roi00').delete.click()
        with qtbot.waitSignal(manager.record_toggled) as recorded:
            manager.row('roi01').record.click()
        with qtbot.waitSignal(manager.add_clicked):
            manager.add_button.click()
        with qtbot.waitSignal(manager.reset_clicked):
            manager.reset_button.click()

        assert deleted.args == ['roi00'] and recorded.args == ['roi01', True]

    def test_locked(self, manager):
        manager.set_locked(True)

        row = manager.row('roi00')
        assert not row.boxes['x0'].isEnabled() and not row.name.isEnabled() and row.plot.isEnabled()
        assert not manager.add_button.isEnabled()


class TestCameraView:
    @pytest.fixture
    def view(self, qtbot, bench):
        view = CameraView(bench.unit)
        qtbot.addWidget(view.spectrum_plot)
        return view

    def test_the_view_is_the_one_of_a_camera(self, view):
        assert isinstance(view.spectrum_plot, ImagePlot) and isinstance(view.manager, RectRoiManager)
        assert view.docks.keys() == {'spectrum3', 'rois3'}
        assert view.spectrum_dock.title() == 'Image'
        assert view.manager_box.title() == 'Camera: Camera1' or view.manager_box.title() == 'Camera'

    def test_the_rois_are_drawn_on_the_image_and_listed(self, view, bench):
        view.update_definitions()

        assert view.spectrum_plot.roi_keys == ['roi00', 'roi01']
        assert view.manager.keys == ['roi00', 'roi01']
        assert view.curves_plot.curves[0].name() == 'Spot'

    def test_the_image_is_shown(self, view, bench, qtbot):
        bench.unit.start_live(time.time())
        bench.wait_for(1)

        view.refresh()

        assert view.spectrum_plot.shape == (80, 120)

    def test_the_channels_of_the_camera_are_given_to_the_image(self, view, bench):
        red, green, blue = gradient(), gradient() + 1, gradient() + 2
        dwa = DataRaw('image', data=[red, green, blue], labels=['R', 'G', 'B'])
        bench.unit.spectrum = Frame(red, time.monotonic(), dwa)
        bench.unit.spectrum_dirty = True

        view.refresh()

        assert list(view.spectrum_plot.viewer._raw_data.labels) == ['R', 'G', 'B']

    def test_the_curves_follow_the_measurements(self, view, bench):
        bench.unit.start_live(time.time())
        bench.wait_for(3)
        view.update_definitions()

        view.refresh()

        assert len(view.curves_plot.curves[0].getData()[0]) >= 3

    def test_dragging_a_rectangle_says_the_unit_and_the_edges(self, view, qtbot):
        view.update_definitions()
        roi = view.spectrum_plot._rois['roi00']
        roi.setPos((12, 22))

        with qtbot.waitSignal(view.rect_edited) as signal:
            roi.sigRegionChangeFinished.emit(roi)

        assert signal.args == [3, 'roi00', 12, 22, 22, 32]

    def test_double_clicking_asks_for_a_roi_at_a_point(self, view, qtbot):
        with qtbot.waitSignal(view.add_requested) as signal:
            view.spectrum_plot.add_requested.emit(30., 40.)

        assert signal.args == [3, 30., 40.]

    def test_no_camera(self, view, bench):
        bench.settings.child('camera').setValue(NONE)
        bench.unit.attach()

        view.clear(view.no_detector_message())

        assert view.spectrum_plot._message.toPlainText() == 'no camera'

    def test_the_title_names_the_camera(self, view):
        view.update_definitions()

        assert 'Camera1' in view.spectrum_plot.getPlotItem().titleLabel.text


def measurement_of(unit, index=0, t0=1000.):
    image = gradient() + index
    frame = Frame(image, time.monotonic(), DataRaw('image', data=[image]))
    rois = unit.settings.rois
    return Measurement(unit.number, index, t0 + index, frame, rois, [rois[k].value(image) for k in range(len(rois))],
                       20. + index, math.nan, None, 0)


class TestCameraRecording:
    @pytest.fixture
    def unit(self, qapp):
        bench_unit = CameraSettings(Parameter.create(**unit_definition(3, default_rect_rois(3), kind=CAMERA)), 3)
        camera_module, thermostat = FakeCamera('Camera1'), FakeThermostat('Thermo1')
        bench_unit.set_modules(['Camera1'], ['Thermo1'], 'Camera1', 'Thermo1')
        bench_unit.set_rois([RectRoi('Spot', 10, 20, 20, 30), RectRoi('Corner', 100, 120, 60, 80)])
        unit = CameraUnit(bench_unit, FakeManager(camera_module, thermostat))
        unit.attach()
        return unit

    def test_the_image_is_not_recorded_by_default(self, unit):
        recording = UnitRecording.from_unit(unit)

        assert recording.titles == ['Unit3_ROIs', 'Unit3_Temperature', 'Unit3_Program']
        assert recording.main == 'Image' and recording.detector_kind == 'camera'

    def test_the_image_can_be_recorded(self, unit):
        unit.settings.child('record', 'image').setValue(True)

        recording = UnitRecording.from_unit(unit)

        assert recording.titles == ['Unit3_Image', 'Unit3_ROIs', 'Unit3_Temperature', 'Unit3_Program']

    def test_what_is_made_of_a_measurement(self, unit):
        unit.settings.child('record', 'image').setValue(True)
        recording = UnitRecording.from_unit(unit)

        dtes = {dte.name: dte for dte in build_dtes(recording, measurement_of(unit, 2))}

        assert set(dtes) == {'Unit3_Image', 'Unit3_ROIs', 'Unit3_Temperature'}
        image = dtes['Unit3_Image'][0]
        assert image.origin == 'Camera1' and np.array_equal(image.data[0], gradient() + 2)
        rois = dtes['Unit3_ROIs'][0]
        assert rois.labels == ['Spot (x 10-20, y 20-30 pixel)', 'Corner (x 100-120, y 60-80 pixel)']
        assert rois.data[0][0] == pytest.approx(14.5 + 100. * 24.5 + 2)
        assert all(dte.timestamp == 1002. for dte in dtes.values())
