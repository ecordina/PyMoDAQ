"""Stand-ins for the modules of the Dashboard, to test a unit without building one (see test_unit.py)"""
import numpy as np
from qtpy import QtCore

from pymodaq_gui.parameter import Parameter

from pymodaq_data import DataToExport
from pymodaq_data.data import Axis, DataRaw
from pymodaq_utils.utils import ThreadCommand

from pymodaq.control_modules.thread_commands import ControlToHardwareMove
from pymodaq.utils.data import DataActuator


class FakeDetector(QtCore.QObject):
    """A spectrometer: answers a snap with a spectrum after `delay` ms. The intensity of a pixel is its wavelength
    minus 400 nm (plus `offset`), so that the mean over a ROI is known"""
    grab_done_signal = QtCore.Signal(object)
    init_signal = QtCore.Signal(bool)

    def __init__(self, title='Spectro1', delay=10, start=400., stop=900., pixels=1001, integration=True):
        super().__init__()
        self.title = title
        self.initialized_state = True
        self.grab_state = False
        self.delay = delay
        self.snaps = 0
        self.stopped_grabs = 0
        self.silent = False  # does not answer
        self.drop_next = 0  # the next snaps which are not answered
        self.in_flight = 0  # snaps waiting for their answer
        self.max_in_flight = 0
        self.offset = 0.
        self.axis_on_first_only = False
        self.x = np.linspace(start, stop, pixels)
        children = []
        if integration:
            children = [{'name': 'detector_settings', 'type': 'group', 'children': [
                {'name': 'integration', 'type': 'float', 'value': 3.8, 'limits': [3.8, 60000.]}]}]
        self.settings = Parameter.create(name='settings', type='group', children=children)

    def spectrum(self) -> np.ndarray:
        return self.x - 400. + self.offset

    def snap(self):
        self.snaps += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        QtCore.QTimer.singleShot(self.delay, self._answer)

    def _answer(self):
        self.in_flight -= 1
        if self.drop_next > 0:
            self.drop_next -= 1
            return
        if self.silent or not self.initialized_state:
            return
        axes = [Axis(label='wavelength', units='nm', data=self.x, index=0)]
        if self.axis_on_first_only and self.snaps > 1:
            axes = []
        dwa = DataRaw('spectrum', data=[self.spectrum()], axes=axes, labels=['intensity'])
        self.grab_done_signal.emit(DataToExport(self.title, data=[dwa]))

    def stop_grab(self):
        self.stopped_grabs += 1
        self.grab_state = False

    def close(self):
        self.initialized_state = False
        self.init_signal.emit(False)


class FakeCamera(FakeDetector):
    """A camera: answers a snap with an image after `delay` ms. The value of the pixel of column x and row y is
    ``x + 100 * y`` (plus `offset`), so that the mean over a rectangle is known"""

    def __init__(self, title='Camera1', delay=10, width=120, height=80, exposure=True):
        super().__init__(title, delay=delay, integration=False)
        self.width, self.height = width, height
        if exposure:
            self.settings = Parameter.create(name='settings', type='group', children=[
                {'name': 'camera_settings', 'type': 'group', 'children': [
                    {'name': 'exposure_time', 'type': 'float', 'value': 10., 'limits': [0.1, 5000.]}]}])

    def image(self) -> np.ndarray:
        rows, columns = np.mgrid[0:self.height, 0:self.width]
        return columns + 100. * rows + self.offset

    def _answer(self):
        self.in_flight -= 1
        if self.drop_next > 0:
            self.drop_next -= 1
            return
        if self.silent or not self.initialized_state:
            return
        dwa = DataRaw('image', data=[self.image()], labels=['intensity'])
        self.grab_done_signal.emit(DataToExport(self.title, data=[dwa]))


class FakeThermostat(QtCore.QObject):
    """A thermostat whose temperature follows its set-point at once"""
    current_value_signal = QtCore.Signal(object)
    init_signal = QtCore.Signal(bool)
    command_hardware = QtCore.Signal(object)

    def __init__(self, title='Thermostat1', value=20., units='°C'):
        super().__init__()
        self.title = title
        self.units = units
        self.initialized_state = True
        self.value = value
        self.targets = []
        self.reads = 0
        self.silent = False
        self.settings = Parameter.create(name='settings', type='group', children=[
            {'name': 'move_settings', 'type': 'group', 'children': [{'name': 'axis', 'type': 'str', 'value': 'T'}]}])
        self.command_hardware.connect(self._on_command)

    def _on_command(self, command: ThreadCommand):
        if command.command == ControlToHardwareMove.MOVE_ABS:
            self.value = float(command.attribute[0].value())
            self.targets.append(self.value)

    def get_actuator_value(self):
        self.reads += 1
        if not self.silent:
            self.current_value_signal.emit(DataActuator(self.title, data=self.value, units=self.units))

    def close(self):
        self.initialized_state = False
        self.init_signal.emit(False)


class FakeManager:
    """What a Unit asks of the ModulesManager"""

    def __init__(self, *modules):
        self.modules = {module.title: module for module in modules}

    def get_mod_from_name(self, name, mod=None):
        return self.modules.get(name)
