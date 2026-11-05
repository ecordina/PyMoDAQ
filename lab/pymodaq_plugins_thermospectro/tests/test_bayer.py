"""The raw counts of a color camera: asked of the driver, shown and recorded as the mosaic or as colors without a change"""
import json
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
