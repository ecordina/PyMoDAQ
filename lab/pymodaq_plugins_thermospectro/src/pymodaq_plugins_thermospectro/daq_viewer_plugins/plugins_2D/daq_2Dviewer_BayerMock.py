"""A simulated color camera with a Bayer sensor, to try the raw counts and the Bayer options without a camera

It behaves like a Thorlabs scientific color camera (CS165CU) seen through pylablib: 10 bits, a Bayer mosaic, and a *color
format*: ``raw`` gives the mosaic (``uint16``, one channel), anything else gives what the driver makes by default: the
interpolated colors multiplied by a white balance (so that values may exceed 1023), three channels. Use it with
``thermo_camera --camera-plugin BayerMock`` (or choose BayerMock for the camera in the Dashboard).
"""
from collections import namedtuple

import numpy as np

from pymodaq.control_modules.viewer_utility_classes import DAQ_Viewer_base, comon_parameters, main
from pymodaq.utils.data import DataFromPlugins, Axis, DataToExport

from pymodaq_plugins_thermospectro import bayer

BIT_DEPTH = 10
WHITE_BALANCE = (1.8, 1.0, 1.5)  # what a color camera multiplies the red, green and blue by
ColorInfo = namedtuple('ColorInfo', 'filter_array_phase correction_matrix default_white_balance_matrix')
DeviceInfo = namedtuple('DeviceInfo', 'model name serial_number manufacturer')


class MockBayerDriver:
    """What the plugin needs of pylablib's ``ThorlabsTLCamera``, for a simulated sensor"""

    def __init__(self, width=240, height=160, phase='red'):
        self.width, self.height, self.phase = width, height, phase
        self.color_output = 'auto'  # (pylablib's default for a color camera: it converts the colors itself)
        self.exposure = 0.01  # s
        self.frames = 0

    def get_sensor_info(self):
        return ('bayer', BIT_DEPTH)

    def get_color_info(self):
        return ColorInfo(self.phase, np.eye(3), np.diag(WHITE_BALANCE))

    def get_device_info(self):
        return DeviceInfo('CS165CU (simulated)', 'BayerMock', 'SIM0001', 'Thorlabs')

    def set_color_format(self, color_output='auto', color_space='linear'):
        self.color_output = 'raw' if color_output == 'raw' else 'rgb'

    def scene(self) -> np.ndarray:
        """The light on the sensor, red, green and blue in 0 to 1 (a colored gradient with a bright spot which moves)"""
        y, x = np.mgrid[0:self.height, 0:self.width]
        spot = np.exp(-((x - (self.width / 2 + 40 * np.sin(self.frames / 20))) ** 2 + (y - self.height / 2) ** 2) / 800.)
        return np.stack([x / self.width, y / self.height, 1. - x / self.width]) * 0.5 + spot * 0.6

    def read_frame(self) -> np.ndarray:
        """The next frame: the mosaic (H, W) in ``raw``, else the colors (H, W, 3) converted as the driver does"""
        self.frames += 1
        rng = np.random.default_rng(self.frames)
        light = np.clip(self.scene() * self.exposure / 0.01, 0, None)
        found = bayer.masks((self.height, self.width), bayer.phase_offset(self.phase))
        counts = (light * found).sum(axis=0) * (2 ** BIT_DEPTH - 1) + rng.normal(0, 2, (self.height, self.width))
        mosaic = np.clip(np.rint(counts), 0, 2 ** BIT_DEPTH - 1).astype(np.uint16)  # (the ADC saturates)
        if self.color_output == 'raw':
            return mosaic
        colors = bayer.debayer(mosaic, bayer.phase_offset(self.phase)) * np.array(WHITE_BALANCE, dtype=np.float32)[:, None, None]
        return np.moveaxis(colors, 0, -1).clip(0, None).astype(np.uint16)  # (not capped at 1023 any more)


class DAQ_2DViewer_BayerMock(DAQ_Viewer_base):
    """Simulated camera with a Bayer color sensor (10 bits), see the module"""
    params = comon_parameters + [
        {'title': 'Exposure Time (ms):', 'name': 'exposure_time', 'type': 'float', 'value': 10., 'min': 0.04,
         'max': 1000.},
        {'title': 'Color format:', 'name': 'color_format', 'type': 'str', 'value': 'auto', 'readonly': True},
    ]

    def ini_attributes(self):
        self.controller: MockBayerDriver = None
        self.x_axis = self.y_axis = None

    def ini_detector(self, controller=None):
        self.controller = MockBayerDriver() if self.is_master else controller
        self.x_axis = Axis('x_axis', size=self.controller.width, index=1)
        self.y_axis = Axis('y_axis', size=self.controller.height, index=0)
        return 'Simulated Bayer camera', True

    def commit_settings(self, param):
        if param.name() == 'exposure_time':
            self.controller.exposure = param.value() / 1000.

    def close(self):
        pass

    def grab_data(self, Naverage=1, **kwargs):
        self.controller.exposure = self.settings['exposure_time'] / 1000.
        frame = self.controller.read_frame()
        self.settings.child('color_format').setValue(self.controller.color_output)
        if frame.ndim == 3:
            arrays, labels = [frame[..., index] for index in range(3)], ['Red', 'Green', 'Blue']
        else:
            arrays, labels = [frame], ['Intensity']
        self.dte_signal.emit(DataToExport('Camera', data=[DataFromPlugins(
            name='Camera', data=arrays, dim='Data2D', labels=labels, axes=[self.y_axis, self.x_axis])]))

    def stop(self):
        return ''


if __name__ == '__main__':
    main(__file__)
