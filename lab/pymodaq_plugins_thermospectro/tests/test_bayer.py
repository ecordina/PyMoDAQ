"""The raw counts of a color camera: asked of the driver, shown and recorded as the mosaic or as colors without a change"""
import json
import math
import time
from collections import namedtuple

import numpy as np
import pytest

from pymodaq_gui.parameter import Parameter

from fakes import FakeCamera, FakeManager, FakeThermostat
from pymodaq_plugins_thermospectro import bayer
from pymodaq_plugins_thermospectro.camera import RectRoi, default_rect_rois
from pymodaq_plugins_thermospectro.saving import camera_info_of
from pymodaq_plugins_thermospectro.unit import CameraUnit, UnitState
from pymodaq_plugins_thermospectro.unit_settings import BAYER_CHOICE, CAMERA, RGB_CHOICE, CameraSettings, unit_definition

pytestmark = pytest.mark.filterwarnings('ignore')

ColorInfo = namedtuple('ColorInfo', 'filter_array_phase correction_matrix default_white_balance_matrix')


class FakeDriver:
    """What pylablib gives for a Thorlabs camera"""

    def __init__(self, sensor='bayer', phase='red'):
        self.sensor, self.phase = sensor, phase
        self.formats = []

    def get_sensor_info(self):
        return (self.sensor, 10)

    def get_color_info(self):
        return ColorInfo(self.phase, np.eye(3) * 2, np.eye(3) * 3)

    def set_color_format(self, color_output='auto', color_space='linear'):
        self.formats.append(color_output)

    led = True

    def enable_led(self, enabled=True):
        self.led = enabled


class MonochromeDriver:
    """A driver of a camera whose sensor has no colors: it has an LED and nothing else of what the extension uses"""
    led = True

    def enable_led(self, enabled=True):
        self.led = enabled


def mosaic(rows=8, columns=10):
    """Counts of a sensor lit by a red, a green and a blue flat field: red 100, green 200, blue 300 (phase red)"""
    found = bayer.masks((rows, columns), (0, 0))
    return (100. * found[0] + 200. * found[1] + 300. * found[2]).astype(np.uint16)


class TestDebayer:
    def test_a_flat_color_gives_flat_channels_and_the_measured_counts_are_kept(self):
        colors = bayer.debayer(mosaic(), (0, 0))

        assert colors.shape == (3, 8, 10)
        for channel, value in enumerate((100., 200., 300.)):
            assert np.allclose(colors[channel], value)

    def test_nothing_is_scaled_or_clipped(self):
        data = (np.random.default_rng(1).integers(0, 1024, (12, 14))).astype(np.uint16)
        colors = bayer.debayer(data, (1, 0))

        assert colors.min() >= data.min() and colors.max() <= data.max()  # (a mean of counts: never out of their range)
        found = bayer.masks(data.shape, (1, 0))
        for channel in range(3):
            assert np.array_equal(colors[channel][found[channel]], data[found[channel]])

    def test_the_phase_tells_where_the_red_pixels_are(self):
        assert bayer.phase_offset('red') == (0, 0) and bayer.phase_offset('blue') == (1, 1)
        assert bayer.phase_offset('green_left_of_red') == bayer.phase_offset('green_left_or_red') == (0, 1)
        assert bayer.phase_offset('green_left_of_blue') == (1, 0)
        assert bayer.phase_offset('?') == (0, 0)
        red = bayer.masks((4, 4), (0, 1))[0]
        assert red[0, 1] and not red[0, 0] and not red[1, 1]

    def test_a_mosaic_must_be_2d(self):
        with pytest.raises(ValueError):
            bayer.debayer(np.zeros((2, 4, 4)))


class Bench:
    def __init__(self, qtbot, driver=None, **settings):
        self.qtbot = qtbot
        self.camera = FakeCamera('Camera1')
        self.camera.controller = driver
        self.thermostat = FakeThermostat('Thermo1')
        self.settings = CameraSettings(Parameter.create(**unit_definition(3, default_rect_rois(3), kind=CAMERA)), 3)
        self.settings.set_modules(['Camera1'], ['Thermo1'], 'Camera1', 'Thermo1')
        self.settings.set_rois([RectRoi('Spot', 0, 4, 0, 4)])
        self.settings.child('integration_time').setValue(20.)
        self.settings.child('readout_time').setValue(0.1)
        for name, value in settings.items():
            self.settings.child(name).setValue(value)
        self.unit = CameraUnit(self.settings, FakeManager(self.camera, self.thermostat))
        self.unit.attach()
        self.camera.image = lambda: mosaic(self.camera.height, self.camera.width)
        self.measurements = []
        self.unit.measurement.connect(self.measurements.append)

    def live(self):
        self.unit.start_live(time.time())
        self.qtbot.waitUntil(lambda: len(self.measurements) >= 1, timeout=10000)
        return self.unit.spectrum


@pytest.fixture
def release():
    units = []
    yield units.append
    for unit in units:
        if unit.state != UnitState.IDLE:
            unit._release()


class TestACameraWithAColorSensor:
    def test_the_raw_counts_are_asked_of_the_driver_and_the_driver_is_given_back(self, qtbot, release):
        driver = FakeDriver()
        bench = Bench(qtbot, driver)
        release(bench.unit)

        bench.live()
        assert driver.formats == ['raw']
        bench.unit._release()

        assert driver.formats == ['raw', 'auto']

    def test_a_color_image_is_shown_and_the_mosaic_is_recorded_by_default(self, qtbot, release):
        bench = Bench(qtbot, FakeDriver())
        release(bench.unit)

        frame = bench.live()

        assert frame.n_channels == 3 and len(frame.display.data) == 3
        assert frame.display.labels == ['Red', 'Green', 'Blue']
        assert len(frame.dwa.data) == 1  # (what the sensor measured, not interpolated)
        assert np.array_equal(frame.dwa.data[0], mosaic(frame.shape[0], frame.shape[1]))

    def test_the_rois_are_measured_on_what_is_shown(self, qtbot, release):
        bench = Bench(qtbot, FakeDriver())
        release(bench.unit)

        bench.live()

        assert bench.measurements[-1].values[0] == pytest.approx(200.)  # ((100 + 200 + 300) / 3 of the flat colors)

    def test_both_can_be_the_mosaic_or_both_the_colors(self, qtbot, release):
        bench = Bench(qtbot, FakeDriver(), bayer_display=BAYER_CHOICE)
        release(bench.unit)
        frame = bench.live()
        assert frame.n_channels == 1 and len(frame.dwa.data) == 1

        bench.unit._release()
        bench.settings.child('bayer_record').setValue(RGB_CHOICE)
        bench.settings.child('bayer_display').setValue(RGB_CHOICE)
        bench.measurements.clear()
        frame = bench.live()

        assert len(frame.dwa.data) == 3 and frame.dwa.labels == ['Red', 'Green', 'Blue']
        assert frame.n_channels == 3

    def test_the_file_says_the_counts_are_raw_and_which_format_the_images_are(self, qtbot, release):
        driver = FakeDriver(phase='green_left_or_red')
        bench = Bench(qtbot, driver)
        release(bench.unit)
        bench.live()

        info = json.loads(camera_info_of(bench.unit))

        assert info['raw_counts'] is True and info['image_format'] == 'bayer'
        assert info['bayer_phase'] == 'green_left_or_red' and info['sensor_type'] == 'bayer'

    def test_the_choice_is_offered_when_the_camera_gives_a_mosaic(self, qtbot, release):
        bench = Bench(qtbot, FakeDriver())
        release(bench.unit)
        assert not bench.settings.child('bayer_display').opts['visible']

        bench.live()
        bench.unit.bayer_changed.emit(True)  # (the extension does it)
        bench.settings.set_bayer(True)

        assert bench.settings.child('bayer_display').opts['visible'] and bench.settings.child('bayer_record').opts['visible']


class TestFindingTheDriver:
    def test_the_driver_of_a_module_of_the_dashboard_is_in_its_controller_and_thread(self):
        from types import SimpleNamespace
        from pymodaq_plugins_thermospectro.camera_info import find_controller
        driver = FakeDriver()
        module = SimpleNamespace(_controller_and_thread=SimpleNamespace(controller=driver))

        assert find_controller(module) is driver
        assert find_controller(SimpleNamespace(controller=driver)) is driver
        assert find_controller(SimpleNamespace(plugin=SimpleNamespace(controller=driver))) is driver
        assert find_controller(SimpleNamespace()) is None and find_controller(None) is None


class TestOtherCameras:
    def test_a_monochrome_camera_is_left_as_it_is(self, qtbot, release):
        driver = FakeDriver(sensor='mono')
        bench = Bench(qtbot, driver)
        release(bench.unit)

        frame = bench.live()

        assert driver.formats == [] and frame.n_channels == 1 and frame.shown is None

    def test_a_camera_without_a_driver_is_left_as_it_is(self, qtbot, release):
        bench = Bench(qtbot, None)
        release(bench.unit)

        frame = bench.live()

        assert frame.shown is None and camera_info_of(bench.unit) == ''


class TestTheSimulatedColorCamera:
    @staticmethod
    def driver():
        import importlib.util
        import pymodaq_plugins_thermospectro as package
        from pathlib import Path
        path = Path(package.__file__).parent / 'daq_viewer_plugins' / 'plugins_2D' / 'daq_2Dviewer_BayerMock.py'
        spec = importlib.util.spec_from_file_location('bayer_mock_plugin', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.MockBayerDriver(width=64, height=48)

    def test_raw_gives_the_10_bit_mosaic_and_the_default_converts_the_colors(self):
        driver = self.driver()
        assert driver.get_sensor_info() == ('bayer', 10)

        converted = driver.read_frame()
        driver.set_color_format('raw')
        raw = driver.read_frame()

        assert converted.shape == (48, 64, 3) and raw.shape == (48, 64) and raw.dtype == np.uint16
        assert raw.max() <= 1023 and raw.max() > 100  # (the ADC is 10 bits)
        assert converted.max() > raw.max()  # (the white balance of the driver pushes the values up)

    def test_the_unit_gets_the_mosaic_and_can_show_it_as_colors(self, qtbot, release):
        driver = self.driver()
        driver.set_color_format('auto')
        bench = Bench(qtbot, driver)
        bench.camera.image = lambda: driver.read_frame()
        bench.camera.height, bench.camera.width = 48, 64
        release(bench.unit)

        frame = bench.live()

        assert driver.color_output == 'raw'
        assert frame.n_channels == 3 and frame.dwa.data[0].max() <= 1023


class TestPyMoDAQFindsThePlugin:
    def test_the_folder_of_the_plugin_says_where_it_is_as_pymodaq_asks(self):
        import importlib
        import pkgutil
        folder = importlib.import_module('pymodaq_plugins_thermospectro.daq_viewer_plugins.plugins_2D')

        names = [info.name for info in pkgutil.iter_modules([str(folder.path.parent)])]

        assert 'daq_2Dviewer_BayerMock' in names  # (what pymodaq.utils.daq_utils does to list the 2D detectors)


class TestTheLedOfTheCamera:
    def test_the_led_is_switched_off_when_the_camera_is_taken(self, qtbot, release):
        driver = FakeDriver()
        bench = Bench(qtbot, driver)
        release(bench.unit)
        assert driver.led

        bench.live()

        assert not driver.led

    def test_also_for_a_camera_without_colors(self, qtbot, release):
        driver = MonochromeDriver()
        bench = Bench(qtbot, driver)
        release(bench.unit)

        bench.live()

        assert not driver.led and bench.unit.bayer_offset is None

    def test_a_camera_without_an_led_is_left_alone(self, qtbot, release):
        class WithoutLed(FakeDriver):
            @property
            def enable_led(self):
                raise AttributeError('enable_led')

        bench = Bench(qtbot, WithoutLed())
        release(bench.unit)

        bench.live()

        assert bench.unit.state == UnitState.LIVE

    def test_a_camera_whose_led_cannot_be_switched_off_is_still_read(self, qtbot, release):
        driver = FakeDriver()

        def failing(enabled=True):
            raise OSError('no answer')

        driver.enable_led = failing
        bench = Bench(qtbot, driver)
        release(bench.unit)

        frame = bench.live()

        assert frame is not None and driver.formats == ['raw']


class TestWithoutInterpolation:
    def test_each_channel_only_holds_the_pixels_which_measured_its_color(self):
        data = mosaic()
        colors = bayer.debayer(data, (0, 0), interpolate=False)

        assert colors.shape == (3, 8, 10) and colors.dtype == np.float32
        found = bayer.masks(data.shape, (0, 0))
        for channel in range(3):
            assert np.array_equal(colors[channel][found[channel]], data[found[channel]])  # (the count it measured)
            assert np.isnan(colors[channel][~found[channel]]).all()
        assert [int(found[channel].sum()) for channel in range(3)] == [20, 40, 20]  # (a quarter, a half, a quarter)

    def test_the_data_keep_their_axes(self):
        from pymodaq_data.data import Axis, DataRaw
        axes = [Axis(label='y', units='pixels', data=np.arange(8.), index=0), Axis(label='x', units='pixels', data=np.arange(10.), index=1)]
        dwa = DataRaw('image', data=[mosaic()], axes=axes, labels=['intensity'])

        rgb = bayer.rgb_data(dwa, (0, 0), interpolate=False)

        assert rgb.labels == ['Red', 'Green', 'Blue'] and [a.size for a in rgb.axes] == [8, 10]

    def test_the_pixels_which_are_not_there_are_not_part_of_a_measurement(self):
        from pymodaq_plugins_thermospectro.camera import rect_value
        colors = bayer.debayer(mosaic(), (0, 0), interpolate=False)

        assert rect_value(list(colors), 0, 8, 0, 8, 'mean', channels=[0]) == pytest.approx(100.)
        assert rect_value(list(colors), 0, 8, 0, 8, 'mean', channels=[2]) == pytest.approx(300.)
        assert rect_value(list(colors), 0, 8, 0, 8, 'mean') == pytest.approx(200.)
        assert rect_value(list(colors), 0, 8, 0, 8, 'sum', channels=[1]) == pytest.approx(200. * 32)
        assert rect_value(list(colors), 0, 8, 0, 8, 'max', channels=[0, 1, 2]) == pytest.approx(200.)  # ((100 + 200 + 300) / 3)

    def test_a_window_where_a_color_was_not_measured_is_made_of_the_other_colors(self):
        from pymodaq_plugins_thermospectro.camera import rect_value
        colors = bayer.debayer(mosaic(), (0, 0), interpolate=False)

        # (the pixel of the first row and the first column is red: no green nor blue in it)
        assert rect_value(list(colors), 0, 1, 0, 1, 'mean') == pytest.approx(100.)
        assert math.isnan(rect_value(list(colors), 0, 1, 0, 1, 'mean', channels=[2]))  # (nothing of that color)

    def test_the_default_is_to_interpolate_and_a_tree_saved_by_an_older_version_does_too(self, qtbot):
        bench = Bench(qtbot, FakeDriver())
        assert bench.settings.bayer_interpolate

        bench.settings.param.removeChild(bench.settings.child('bayer_interpolate'))

        assert bench.settings.bayer_interpolate

    def test_it_is_offered_with_the_other_choices_and_not_changed_during_an_experiment(self, qtbot):
        bench = Bench(qtbot, FakeDriver())
        assert not bench.settings.child('bayer_interpolate').opts['visible']

        bench.settings.set_bayer(True)
        bench.settings.lock(True)

        assert bench.settings.child('bayer_interpolate').opts['visible']
        assert not bench.settings.child('bayer_interpolate').opts['enabled']

    def test_the_unit_shows_the_colors_which_were_measured_and_measures_the_rois_on_them(self, qtbot, release):
        bench = Bench(qtbot, FakeDriver(), bayer_interpolate=False)
        release(bench.unit)

        frame = bench.live()

        assert frame.n_channels == 3
        shown = frame.display.data
        assert np.isnan(shown[0]).any() and np.isnan(shown[1]).any() and np.isnan(shown[2]).any()
        assert np.nanmin(shown[0]) == np.nanmax(shown[0]) == 100.  # (the red pixels: what the sensor measured)
        assert np.array_equal(frame.dwa.data[0], mosaic(frame.shape[0], frame.shape[1]))  # (the mosaic is recorded)
        assert bench.measurements[-1].values[0] == pytest.approx(200.)

    def test_the_file_says_the_colors_are_not_interpolated(self, qtbot, release):
        bench = Bench(qtbot, FakeDriver(), bayer_interpolate=False, bayer_record=RGB_CHOICE)
        release(bench.unit)
        frame = bench.live()

        info = json.loads(camera_info_of(bench.unit))

        assert info['image_format'] == 'rgb_measured'
        assert len(frame.dwa.data) == 3 and np.isnan(frame.dwa.data[0]).any()
        interpolated = Bench(qtbot, FakeDriver(), bayer_record=RGB_CHOICE)
        release(interpolated.unit)
        interpolated.live()
        assert json.loads(camera_info_of(interpolated.unit))['image_format'] == 'rgb'


class TestTheFileSaysWhatWasInterpolated:
    def info(self, qtbot, release, **settings):
        bench = Bench(qtbot, FakeDriver(), **settings)
        release(bench.unit)
        bench.live()
        return json.loads(camera_info_of(bench.unit))

    def test_interpolated_colors_are_described_in_the_file(self, qtbot, release):
        info = self.info(qtbot, release, bayer_record=RGB_CHOICE)

        assert info['interpolation'] == bayer.BILINEAR and info['roi_interpolation'] == bayer.BILINEAR
        assert 'bilinear' in info['interpolation'] and '8 neighbours' in info['interpolation']

    def test_the_mosaic_is_recorded_as_it_was_measured_but_the_rois_are_measured_on_the_colors_shown(self, qtbot, release):
        info = self.info(qtbot, release)  # (the defaults: the colors are shown, the mosaic is recorded)

        assert info['interpolation'] == 'none' and info['image_format'] == 'bayer'
        assert info['roi_interpolation'] == bayer.BILINEAR

    def test_without_interpolation_the_file_says_none_twice(self, qtbot, release):
        info = self.info(qtbot, release, bayer_record=RGB_CHOICE, bayer_interpolate=False)

        assert info['interpolation'] == 'none' and info['roi_interpolation'] == 'none'

    def test_the_rois_of_the_mosaic_are_measured_without_interpolation(self, qtbot, release):
        info = self.info(qtbot, release, bayer_display=BAYER_CHOICE)

        assert info['roi_interpolation'] == 'none' and info['interpolation'] == 'none'

    def test_the_tif_files_say_it(self):
        from types import SimpleNamespace
        from pymodaq_plugins_thermospectro import export
        camera = SimpleNamespace(camera_info={'raw_counts': True, 'bayer_phase': 'red', 'image_format': 'rgb',
                                              'interpolation': bayer.BILINEAR, 'roi_interpolation': 'none'})

        text = export.camera_sentence(camera, '')

        assert 'interpolation of the colors of the images: bilinear' in text
        assert 'the ROIs were measured on: none' in text

    def test_a_tooltip_explains_the_interpolation(self, qtbot):
        tip = Bench(qtbot, FakeDriver()).settings.child('bayer_interpolate').opts['tip']

        assert 'BILINEAR' in tip and '8 neighbours' in tip and 'no white balance' in tip
