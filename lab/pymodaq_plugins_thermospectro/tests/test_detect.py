"""Recognizing the instruments (detect.py): what is connected, and what the modules of each application are made of

No hardware: the backends, the list of the serial ports and the probe of a port are replaced by fakes (and the real
probe of the Quantum Northwest plugin is tried on its simulated controllers).
"""
import ast
import itertools
import sys
import time
import types
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, Optional

import pytest

from pymodaq_plugins_thermospectro import detect
from pymodaq_plugins_thermospectro.configurations import CONFIGURATIONS
from pymodaq_plugins_thermospectro.detect import (Camera, Detections, Options, Spectrometer, Thermostat, detect_all,
                                                  detect_cameras, detect_spectrometers, detect_thermostats,
                                                  looks_like_ftdi, natural_key, plan)

FTDI = 0x0403


@dataclass
class FakeDevice:
    """A spectrometer as a backend of python-seabreeze lists it"""
    serial_number: str
    model: str = 'FLAME-S'

    def __str__(self):
        return f'<SeaBreezeDevice {self.model}:{self.serial_number}>'


@dataclass
class FakePort:
    """A serial port as serial.tools.list_ports.comports() lists it"""
    device: str
    description: str = 'n/a'
    vid: Optional[int] = None
    manufacturer: Optional[str] = None


def usb_port(device: str, **kwargs) -> FakePort:
    return FakePort(device, kwargs.pop('description', 'USB Serial Port'), kwargs.pop('vid', FTDI), **kwargs)


class Backends:
    """list_devices(backend) of the plugin, with what each backend sees (or the exception it raises)"""

    def __init__(self, **sees):
        self.sees = sees
        self.called = []

    def __call__(self, backend):
        self.called.append(backend)
        seen = self.sees.get(backend, [])
        if isinstance(seen, BaseException):
            raise seen
        return list(seen)


class Probe:
    """probe_port(port, problems=...) with what answers on each port: a firmware, None, or why it cannot be used"""

    def __init__(self, **answers):
        self.answers = answers
        self.asked = []

    def __call__(self, port, problems=None):
        self.asked.append(port)
        answer = self.answers.get(port)
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, str) and answer.startswith('!'):
            problems.append(f'{port} cannot be opened: {answer[1:]}')
            return None
        return answer


class TsiRecord(NamedTuple):
    cam_id: int
    model: str
    serial_number: str
    in_use: bool = False


# ----------------------------------------------------------------------------------------------------------------------
# looking
# ----------------------------------------------------------------------------------------------------------------------

class TestSpectrometers:
    def test_none_are_connected(self):
        backends, notes = Backends(), []

        assert detect_spectrometers(notes, backends) == []

        assert backends.called == ['pyseabreeze', 'cseabreeze']  # (both were asked)
        assert len(notes) == 1 and 'none found' in notes[0] and 'pyseabreeze' in notes[0] and 'cseabreeze' in notes[0]

    def test_one_is_connected(self):
        backends, notes = Backends(pyseabreeze=[FakeDevice('FLMS12345')]), []

        found = detect_spectrometers(notes, backends)

        assert found == [Spectrometer('FLMS12345', 'FLAME-S', 'pyseabreeze', '<SeaBreezeDevice FLAME-S:FLMS12345>')]
        assert found[0].serial == 'FLMS12345' and found[0].model == 'FLAME-S'
        assert backends.called == ['pyseabreeze'] and notes == []  # (the other backend is not needed)

    def test_two_are_sorted_by_serial_number(self):
        backends = Backends(pyseabreeze=[FakeDevice('FLMS20000', 'FLAME-T'), FakeDevice('FLMS10000'),
                                         FakeDevice('')])  # (one which does not tell its serial number: last)

        found = detect_spectrometers([], backends)

        assert [spectrometer.serial for spectrometer in found] == ['FLMS10000', 'FLMS20000', '']
        assert found[1].model == 'FLAME-T'

    def test_the_other_backend_looks_when_the_first_sees_none(self):
        backends, notes = Backends(cseabreeze=[FakeDevice('USB2+H01')]), []

        found = detect_spectrometers(notes, backends)

        assert [(s.serial, s.backend) for s in found] == [('USB2+H01', 'cseabreeze')]
        assert backends.called == ['pyseabreeze', 'cseabreeze']
        assert len(notes) == 1 and 'cseabreeze' in notes[0] and 'pyseabreeze' in notes[0]

    def test_the_other_backend_looks_when_the_first_cannot_work(self):
        backends = Backends(pyseabreeze=ImportError('No module named usb'), cseabreeze=[FakeDevice('A1')])
        notes = []

        found = detect_spectrometers(notes, backends)

        assert [s.backend for s in found] == ['cseabreeze']
        assert 'No module named usb' in notes[0]

    def test_a_driver_which_is_missing_is_a_note(self):
        backends = Backends(pyseabreeze=ImportError('The seabreeze backend cannot be loaded'),
                            cseabreeze=ImportError('No module named seabreeze'))
        notes = []

        assert detect_spectrometers(notes, backends) == []

        assert len(notes) == 1 and 'No module named seabreeze' in notes[0] and 'cannot be loaded' in notes[0]

    @pytest.mark.parametrize('error', [OSError('The device does not recognize the command'), RuntimeError('boom'),
                                       KeyError('usb'), ValueError('')])
    def test_it_never_raises(self, error):
        notes = []

        assert detect_spectrometers(notes, Backends(pyseabreeze=error, cseabreeze=error)) == []

        assert len(notes) == 1

    def test_the_backends_of_the_plugin_are_used_by_default(self, monkeypatch):
        """The plugin lists the devices through the API of each backend: the API is replaced"""
        from pymodaq_plugins_oceaninsight.hardware import seabreeze_utils

        class Api:
            def __init__(self, name):
                self.name = name

            def list_devices(self):
                return [FakeDevice('FLMS00002'), FakeDevice('FLMS00001')] if self.name == 'pyseabreeze' else []

        monkeypatch.setattr(seabreeze_utils, 'get_api', Api)

        found = detect_spectrometers()

        assert [s.serial for s in found] == ['FLMS00001', 'FLMS00002'] and found[0].backend == 'pyseabreeze'

    def test_a_plugin_which_is_not_installed_is_a_note(self, monkeypatch):
        monkeypatch.setitem(sys.modules, 'pymodaq_plugins_oceaninsight.hardware.seabreeze_utils', None)
        notes = []

        assert detect_spectrometers(notes) == []

        assert len(notes) == 1 and 'pymodaq_plugins_oceaninsight' in notes[0]


class TestCameras:
    def test_none_are_connected(self):
        notes = []

        assert detect_cameras(notes, lambda: [], lambda: []) == []

        assert notes == ['Cameras: none found.']

    def test_thorlabs_scientific_cameras_are_listed_by_their_serial_number(self):
        found = detect_cameras([], lambda: ['08365', '08366'], lambda: [])

        assert found == [Camera('Thorlabs_TSI', '08365', 'Thorlabs scientific camera 08365'),
                         Camera('Thorlabs_TSI', '08366', 'Thorlabs scientific camera 08366')]
        assert found[0].plugin == 'Thorlabs_TSI' and found[0].serial == '08365'

    def test_uc480_cameras_are_records(self):
        found = detect_cameras([], lambda: [], lambda: [TsiRecord(1, 'DCC1545M', '4102912345')])

        assert found == [Camera('UC480', '4102912345', 'DCC1545M 4102912345')]

    def test_the_two_families_together_thorlabs_first(self):
        found = detect_cameras([], lambda: ['08365'], lambda: [TsiRecord(1, 'UI-1220', '4100000001')])

        assert [(c.plugin, c.serial) for c in found] == [('Thorlabs_TSI', '08365'), ('UC480', '4100000001')]

    def test_a_missing_driver_does_not_hide_the_other_family(self):
        def no_pylablib():
            raise ImportError("No module named 'pylablib'")

        notes = []
        found = detect_cameras(notes, no_pylablib, lambda: [TsiRecord(1, 'UI-1220', '4100000001')])

        assert [c.plugin for c in found] == ['UC480'] and notes == []  # (the camera is there: no need to say more)

    @pytest.mark.parametrize('error, words', [
        (ImportError("No module named 'pylablib'"), 'pylablib is not installed'),
        (OSError('ThorCam is not installed: tl_camera_sdk_native.dll'), 'cannot be loaded'),
        (RuntimeError('the SDK is in a bad mood'), 'RuntimeError'),
    ])
    def test_nothing_found_and_why(self, error, words):
        def broken():
            raise error

        notes = []

        assert detect_cameras(notes, broken, broken) == []

        assert len(notes) == 2 and all(words in note for note in notes)
        assert 'Thorlabs' in notes[0] and 'uc480' in notes[1]

    def test_only_the_family_of_the_camera_plugin_is_asked(self):
        asked = []

        def lister(name):
            def call():
                asked.append(name)
                return ['08365'] if name == 'tsi' else [TsiRecord(1, 'UI-1220', '4100000001')]
            return call

        found = detect_cameras([], lister('tsi'), lister('uc480'), plugin='UC480')

        assert asked == ['uc480'] and [(c.plugin, c.serial) for c in found] == [('UC480', '4100000001')]
        assert detect_cameras([], lister('tsi'), lister('uc480'), plugin='Basler') != [] and asked[-2:] == ['tsi', 'uc480']

    def test_a_camera_which_another_program_uses_is_told(self):
        notes = []

        found = detect_cameras(notes, lambda: [], lambda: [TsiRecord(1, 'UI-1220', '4100000001', True)])

        assert found[0].label.endswith('(in use by another program)')
        assert len(notes) == 1 and '4100000001' in notes[0] and 'in use' in notes[0]

    def test_pylablib_is_used_by_default(self, monkeypatch):
        thorlabs = types.SimpleNamespace(list_cameras_tlcam=lambda: ['08365'])
        uc480 = types.SimpleNamespace(list_cameras=lambda: [TsiRecord(1, 'UI-1220', '4100000001')])
        devices = types.ModuleType('pylablib.devices')
        devices.Thorlabs, devices.uc480 = thorlabs, uc480
        monkeypatch.setitem(sys.modules, 'pylablib', types.ModuleType('pylablib'))
        monkeypatch.setitem(sys.modules, 'pylablib.devices', devices)

        assert [(c.plugin, c.serial) for c in detect_cameras()] == [('Thorlabs_TSI', '08365'), ('UC480', '4100000001')]

    def test_pylablib_which_is_not_installed(self, monkeypatch):
        monkeypatch.setitem(sys.modules, 'pylablib', None)
        monkeypatch.setitem(sys.modules, 'pylablib.devices', None)
        notes = []

        assert detect_cameras(notes) == []

        assert len(notes) == 2 and all('pylablib is not installed' in note for note in notes)


class TestSerialPorts:
    @pytest.mark.parametrize('port', [
        FakePort('/dev/ttyUSB0', 'FT232R USB UART - FT232R USB UART', FTDI),
        FakePort('COM5', 'USB Serial Port (COM5)', FTDI),  # (what Windows says of an FTDI converter)
        FakePort('COM6', 'USB-Serial Controller', manufacturer='FTDI'),
        FakePort('COM7', 'FTDI USB Serial Device'),
    ])
    def test_ftdi_converters(self, port):
        assert looks_like_ftdi(port)

    @pytest.mark.parametrize('port', [
        FakePort('COM1', 'Communications Port (COM1)'),
        FakePort('/dev/ttyS0', 'n/a'),
        FakePort('COM3', 'Prolific USB-to-Serial Comm Port', 0x067b),
        FakePort('COM4', 'Silicon Labs CP210x USB to UART Bridge', 0x10c4),
        FakePort('COM8', 'Arduino Uno', 0x2341, 'Arduino LLC'),
        FakePort('COM9'),
        FakePort('COM10', 'USB Serial Port (COM10)'),  # (any adapter says that: it is not enough)
        FakePort('/dev/ttyACM0', 'USB serial device'),
    ])
    def test_the_others(self, port):
        assert not looks_like_ftdi(port)

    def test_ports_are_sorted_the_way_a_person_does(self):
        names = ['COM10', 'COM2', 'COM1', 'COM11', '/dev/ttyUSB10', '/dev/ttyUSB2']

        assert sorted(names, key=natural_key) == ['/dev/ttyUSB2', '/dev/ttyUSB10', 'COM1', 'COM2', 'COM10', 'COM11']


class TestThermostats:
    def test_a_controller_answers_on_a_port(self):
        probe, notes = Probe(COM5='9.1'), []

        found = detect_thermostats(notes, [usb_port('COM5')], probe)

        assert found == [Thermostat('COM5', '9.1')] and found[0].port == 'COM5' and found[0].firmware == '9.1'
        assert notes == []

    def test_the_ports_are_asked_in_the_natural_order_and_found_in_it(self):
        probe = Probe(COM10='2.6.04', COM2='9.1', COM1='9.1')
        ports = [usb_port('COM10'), usb_port('COM2'), usb_port('COM1')]

        found = detect_thermostats([], ports, probe)

        assert probe.asked == ['COM1', 'COM2', 'COM10']
        assert [t.port for t in found] == ['COM1', 'COM2', 'COM10']

    def test_the_ports_which_are_not_ftdi_converters_are_never_asked(self):
        probe = Probe(COM5='9.1', COM1='9.1', COM3='9.1')
        ports = [FakePort('COM1', 'Communications Port (COM1)'), usb_port('COM5'),
                 FakePort('COM2', 'n/a'), FakePort('COM3', 'Prolific USB-to-Serial Comm Port', 0x067b),
                 FakePort('COM4', 'Arduino Uno', 0x2341, 'Arduino LLC'), FakePort('COM7', 'USB Serial Port (COM7)')]

        found = detect_thermostats([], ports, probe)

        assert probe.asked == ['COM5']  # (a lab instrument may be on any other port: nothing is sent to it)
        assert found == [Thermostat('COM5', '9.1')]

    def test_nothing_is_asked_when_there_is_no_converter(self):
        probe, notes = Probe(), []

        assert detect_thermostats(notes, [FakePort('COM1', 'Communications Port (COM1)')], probe) == []

        assert probe.asked == []
        assert len(notes) == 1 and 'none of the serial ports is an FTDI USB-serial converter' in notes[0]
        assert 'COM1' in notes[0] and '--port' in notes[0]

    def test_a_computer_without_serial_port(self):
        notes = []

        assert detect_thermostats(notes, [], Probe()) == []

        assert notes == ['Thermal controllers: this computer has no serial port.']

    def test_nothing_answers(self):
        notes = []
        ports = [usb_port('COM6'), FakePort('COM1', 'Communications Port (COM1)')]

        assert detect_thermostats(notes, ports, Probe()) == []

        assert len(notes) == 1 and 'nothing answered' in notes[0] and 'COM6' in notes[0]
        assert 'COM1 (Communications Port (COM1))' in notes[0]  # (the ports there are, to choose from)

    def test_a_port_which_another_program_holds_is_skipped(self):
        probe, notes = Probe(COM3='!Access is denied', COM4='9.1'), []

        found = detect_thermostats(notes, [usb_port('COM3'), usb_port('COM4')], probe)

        assert found == [Thermostat('COM4', '9.1')]
        assert probe.asked == ['COM3', 'COM4']  # (it goes on with the next one)
        assert len(notes) == 1 and 'COM3' in notes[0] and 'Access is denied' in notes[0] and 'skipped' in notes[0]

    def test_a_probe_which_raises_is_a_note(self):
        notes = []

        found = detect_thermostats(notes, [usb_port('COM3'), usb_port('COM4')],
                                   Probe(COM3=RuntimeError('boom'), COM4='9.1'))

        assert found == [Thermostat('COM4', '9.1')]
        assert len(notes) == 1 and 'COM3' in notes[0] and 'boom' in notes[0]

    def test_the_probe_of_the_plugin_is_used_by_default(self, monkeypatch):
        from pymodaq_plugins_quantumnorthwest.hardware import qnw_tc
        probe = Probe(COM5='9.1')
        monkeypatch.setattr(qnw_tc, 'probe_port', probe)

        found = detect_thermostats([], [usb_port('COM5'), FakePort('COM1', 'Communications Port (COM1)')])

        assert found == [Thermostat('COM5', '9.1')] and probe.asked == ['COM5']

    def test_the_serial_ports_of_the_computer_are_used_by_default(self, monkeypatch):
        from serial.tools import list_ports
        monkeypatch.setattr(list_ports, 'comports', lambda: [usb_port('COM7'), FakePort('COM1')])
        probe = Probe(COM7='2.6.04')

        assert detect_thermostats([], None, probe) == [Thermostat('COM7', '2.6.04')]
        assert probe.asked == ['COM7']

    def test_pyserial_which_cannot_list_the_ports(self, monkeypatch):
        from serial.tools import list_ports

        def broken():
            raise OSError('no udev')

        monkeypatch.setattr(list_ports, 'comports', broken)
        notes = []

        assert detect_thermostats(notes, None, Probe()) == []

        assert 'no udev' in notes[0]

    def test_pyserial_which_is_not_installed(self, monkeypatch):
        monkeypatch.setitem(sys.modules, 'serial.tools', None)
        notes = []

        assert detect_thermostats(notes, None, Probe()) == []

        assert 'pyserial is not installed' in notes[0]

    def test_the_plugin_of_the_controllers_which_is_not_installed(self, monkeypatch):
        monkeypatch.setitem(sys.modules, 'pymodaq_plugins_quantumnorthwest.hardware', None)
        notes = []

        assert detect_thermostats(notes, [usb_port('COM5')]) == []

        assert len(notes) == 1 and 'pymodaq_plugins_quantumnorthwest' in notes[0]

    def test_the_real_probe_on_simulated_controllers(self):
        """The real probe_port of the plugin, on its simulated controllers (sim://...) and on one which is silent"""
        ports = [usb_port('sim://tc1b'), usb_port('sim://qpod'), usb_port('sim://tc125'),
                 usb_port('sim://tc125?latency=30'),  # (it never answers)
                 FakePort('sim://tc1b?speed=1', 'Communications Port')]  # (not a converter: not asked)
        start = time.monotonic()
        notes = []

        found = detect_thermostats(notes, ports)

        assert found == [Thermostat('sim://qpod', '9.1'), Thermostat('sim://tc1b', '2.6.04'),
                         Thermostat('sim://tc125', '9.1')]  # (tc1b before tc125: the numbers are sorted as numbers)
        assert time.monotonic() - start < 3.
        assert len(notes) == 0


class Looked(list):
    """The searches made by detect_all, with the arguments each one was given"""

    def __init__(self):
        super().__init__()
        self.arguments = {}


class TestDetectAll:
    @pytest.fixture
    def looked(self, monkeypatch):
        """What detect_all looked for"""
        looked = Looked()

        def replace(name, result):
            def function(notes=None, **arguments):
                looked.append(name)
                looked.arguments[name] = arguments
                return result
            monkeypatch.setattr(detect, f'detect_{name}', function)

        replace('spectrometers', [Spectrometer('S1', 'FLAME-S')])
        replace('cameras', [Camera('Thorlabs_TSI', 'C1', 'camera C1')])
        replace('thermostats', [Thermostat('COM5', '9.1')])
        return looked

    @pytest.mark.parametrize('command, wanted', [
        ('thermo_2spectro', ['spectrometers', 'thermostats']), ('thermo_spectro', ['spectrometers', 'thermostats']),
        ('camera', ['cameras']), ('thermo_camera', ['cameras', 'thermostats']),
        ('thermo_camera_spectro', ['spectrometers', 'cameras', 'thermostats'])])
    def test_only_the_instruments_of_the_application_are_looked_for(self, looked, command, wanted):
        found = detect_all(CONFIGURATIONS[command])

        assert looked == wanted
        assert bool(found.spectrometers) == ('spectrometers' in wanted) and bool(found.cameras) == ('cameras' in wanted)
        assert bool(found.thermostats) == ('thermostats' in wanted)

    @pytest.mark.parametrize('options', [Options(mock=True), Options(detect=False)])
    def test_nothing_is_looked_for_with_mock_or_no_detect(self, looked, options):
        found = detect_all(CONFIGURATIONS['thermo_camera_spectro'], options)

        assert looked == [] and found == Detections()

    def test_the_ports_which_are_given_are_not_looked_for(self, looked):
        detect_all(CONFIGURATIONS['thermo_camera'], Options(ports=('COM6',)))

        assert looked == ['cameras']

    def test_a_camera_plugin_which_is_asked_for_limits_the_search_to_its_family(self, looked):
        detect_all(CONFIGURATIONS['camera'], Options(camera_plugin='UC480'))

        assert looked == ['cameras'] and looked.arguments['cameras'] == {'plugin': 'UC480'}

    def test_the_mock_camera_is_not_looked_for(self, looked):
        detect_all(CONFIGURATIONS['thermo_camera'], Options(camera_plugin='Mock'))

        assert looked == ['thermostats']

    def test_the_controllers_of_another_plugin_cannot_be_recognized(self, looked):
        detect_all(CONFIGURATIONS['thermo_camera'], Options(thermostat_plugin='Lakeshore'))

        assert looked == ['cameras']

    def test_what_fails_is_a_note(self, monkeypatch):
        def broken(notes=None, **arguments):
            raise RuntimeError('boom')

        monkeypatch.setattr(detect, 'detect_cameras', broken)
        monkeypatch.setattr(detect, 'detect_thermostats', lambda notes=None, **kw: [Thermostat('COM5', '9.1')])

        found = detect_all(CONFIGURATIONS['thermo_camera'])

        assert found.cameras == [] and found.thermostats == [Thermostat('COM5', '9.1')]
        assert len(found.notes) == 1 and 'boom' in found.notes[0]

    def test_the_notes_of_each_search_are_kept(self, monkeypatch):
        monkeypatch.setattr(detect, 'detect_spectrometers', lambda notes=None, **kw: notes.append('no seabreeze') or [])
        monkeypatch.setattr(detect, 'detect_thermostats', lambda notes=None, **kw: notes.append('no port') or [])

        found = detect_all(CONFIGURATIONS['thermo_spectro'])

        assert found.notes == ['no seabreeze', 'no port']


# ----------------------------------------------------------------------------------------------------------------------
# deciding
# ----------------------------------------------------------------------------------------------------------------------

SPECTROMETERS = [Spectrometer('FLMS10000', 'FLAME-S', 'pyseabreeze', '<SeaBreezeDevice FLAME-S:FLMS10000>'),
                 Spectrometer('FLMS20000', 'FLAME-T', 'pyseabreeze', '<SeaBreezeDevice FLAME-T:FLMS20000>')]
THERMOSTATS = [Thermostat('COM5', '9.1'), Thermostat('COM6', '2.6.04')]
CAMERAS = [Camera('Thorlabs_TSI', '08365', 'Thorlabs scientific camera 08365')]


def combinations():
    """Every configuration with every combination of recognized and not recognized instruments"""
    for command, configuration in CONFIGURATIONS.items():
        cameras = (False, True) if configuration.camera else (False,)
        for spectrometers, thermostats, camera in itertools.product(range(configuration.spectrometers + 1),
                                                                    range(configuration.thermostats + 1), cameras):
            name = f'{command}-{spectrometers}spectro-{thermostats}thermo-{"cam" if camera else "nocam"}'
            yield pytest.param(command, spectrometers, thermostats, camera, id=name)


class TestPlan:
    @pytest.mark.parametrize('command, spectrometers, thermostats, camera', list(combinations()))
    def test_every_combination_of_instruments(self, command, spectrometers, thermostats, camera):
        configuration = CONFIGURATIONS[command]
        found = Detections(SPECTROMETERS[:spectrometers], CAMERAS if camera else [], THERMOSTATS[:thermostats])

        result = plan(configuration, found)

        assert [spec.name for spec in result.modules] == configuration.module_names
        for index, name in enumerate(configuration.spectrometer_names):
            spec = result.module(name)
            assert (spec.kind, spec.plugin, spec.dim, spec.axis_name) == ('spectrometer', 'Seabreeze', 'DAQ1D', None)
            assert spec.recognized is (index < spectrometers)
            if spec.recognized:
                assert spec.settings == {'backend': 'pyseabreeze', 'serial': SPECTROMETERS[index].serial,
                                         'device': SPECTROMETERS[index].device}
                assert SPECTROMETERS[index].serial in spec.found and spec.identity == SPECTROMETERS[index].serial
        for index, name in enumerate(configuration.thermostat_names):
            spec = result.module(name)
            assert (spec.kind, spec.plugin, spec.dim, spec.axis_name) == ('thermostat', 'QNW_TC', None, 'Temperature')
            assert spec.is_actuator and spec.recognized is (index < thermostats)
            if spec.recognized:
                assert spec.settings == {'port': THERMOSTATS[index].port} and THERMOSTATS[index].port in spec.found
        if configuration.camera:
            spec = result.module('Camera1')
            assert (spec.kind, spec.plugin, spec.dim) == ('camera', 'Thorlabs_TSI', 'DAQ2D')
            assert spec.recognized is camera
            assert spec.settings == ({'serial': '08365', 'index': 0} if camera else {})
        else:
            assert result.module('Camera1') is None
        # what is to be connected is what was recognized, and nothing else
        assert [spec.name for spec in result.to_connect] == [spec.name for spec in result.modules if spec.recognized]
        assert [spec.name for spec in result.not_recognized] == [spec.name for spec in result.modules
                                                                 if not spec.recognized]
        # what to do about the others
        for spec in result.not_recognized:
            assert spec.instruction.startswith(f'{spec.name}: ') and spec.instruction in result.report()
            assert 'Ini' in spec.instruction and 'Live' in spec.instruction and 'Dashboard' in spec.instruction
        assert result.connect is True

    def test_the_example_of_the_user(self):
        """thermo_camera_spectro: the controller is not recognized, the spectrometer and the camera are"""
        found = Detections(SPECTROMETERS[:1], CAMERAS, [])

        result = plan(CONFIGURATIONS['thermo_camera_spectro'], found)

        assert [spec.name for spec in result.to_connect] == ['Spectro1', 'Camera1']
        thermo = result.module('Thermo1')
        assert not thermo.recognized and thermo.plugin == 'QNW_TC'
        for words in ('Thermo1', 'select its port', 'settings of the module Thermo1 in the Dashboard', 'Ini button',
                      'Connect the modules', 'Live'):
            assert words in thermo.instruction
        assert detect.port_example() in thermo.instruction
        assert result.pairs == ['Spectro1 (serial FLMS10000) <-> Thermo1 (not recognized)',
                                'Camera1 (serial 08365) <-> Thermo1 (not recognized)']
        report = result.report()
        assert 'FLAME-S' in report and 'FLMS10000' in report and '08365' in report and 'Nothing was started' in report

    def test_the_pairs_of_each_application(self):
        found = Detections(SPECTROMETERS, CAMERAS, THERMOSTATS)

        pairs = {command: plan(configuration, found).pairs for command, configuration in CONFIGURATIONS.items()}

        assert pairs == {
            'thermo_2spectro': ['Spectro1 (serial FLMS10000) <-> Thermo1 (COM5)',
                                'Spectro2 (serial FLMS20000) <-> Thermo2 (COM6)'],
            'thermo_spectro': ['Spectro1 (serial FLMS10000) <-> Thermo1 (COM5)'],
            'camera': [],
            'thermo_camera': ['Camera1 (serial 08365) <-> Thermo1 (COM5)'],
            'thermo_camera_spectro': ['Spectro1 (serial FLMS10000) <-> Thermo1 (COM5)',
                                      'Camera1 (serial 08365) <-> Thermo1 (COM5)'],
        }

    def test_the_pairing_of_two_controllers_is_not_a_certainty(self):
        result = plan(CONFIGURATIONS['thermo_2spectro'], Detections(SPECTROMETERS, [], THERMOSTATS))

        assert any('Nothing says which controller is on which cuvette holder' in note for note in result.notes)
        assert 'swap the ports of Thermo1 and Thermo2' in result.report()
        one = plan(CONFIGURATIONS['thermo_spectro'], Detections(SPECTROMETERS[:1], [], THERMOSTATS[:1]))
        assert one.notes == []

    def test_the_pairing_is_told_too_when_only_one_of_two_controllers_is_recognized(self):
        result = plan(CONFIGURATIONS['thermo_2spectro'], Detections(SPECTROMETERS, [], THERMOSTATS[:1]))

        assert any('Nothing says which cuvette holder Thermo1 is on' in note for note in result.notes)
        none = plan(CONFIGURATIONS['thermo_2spectro'], Detections(SPECTROMETERS, [], []))
        assert not any('cuvette holder' in note for note in none.notes)  # (nothing to pair)

    def test_a_controller_without_a_port_is_not_said_to_be_looked_for_when_ports_are_given(self):
        result = plan(CONFIGURATIONS['thermo_2spectro'], Detections(SPECTROMETERS, [], THERMOSTATS),
                      Options(ports=('COM9',)))

        assert result.module('Thermo1').settings == {'port': 'COM9'}
        instruction = result.module('Thermo2').instruction
        assert 'was not looked for' in instruction and '--port' in instruction and 'no thermal controller' not in instruction

    def test_what_was_found_and_is_not_used_is_told(self):
        result = plan(CONFIGURATIONS['thermo_spectro'], Detections(SPECTROMETERS, [], THERMOSTATS))

        assert [spec.identity for spec in result.modules] == ['COM5', 'FLMS10000']  # (the first of each, in order)
        assert any('2 spectrometers were found, this application uses 1' in note for note in result.notes)
        assert any('2 thermal controllers were found, this application uses 1' in note for note in result.notes)

    def test_the_notes_of_the_search_are_told(self):
        result = plan(CONFIGURATIONS['thermo_camera'], Detections(notes=['Cameras: pylablib is not installed']))

        assert 'Cameras: pylablib is not installed' in result.notes
        assert 'pylablib is not installed' in result.report()

    def test_the_report(self):
        result = plan(CONFIGURATIONS['thermo_camera_spectro'], Detections(SPECTROMETERS[:1], [], THERMOSTATS[:1],
                                                                          ['a note']))

        report = result.report(['a problem'])

        assert report.startswith('thermo_camera_spectro: 2 of 3 instruments recognized.')
        assert 'Recognized:' in report and 'Quantum Northwest controller on COM5 (firmware 9.1)' in report
        assert 'Not recognized' in report and 'Camera1: no camera was recognized' in report
        assert 'Pairs: Spectro1 (serial FLMS10000) <-> Thermo1 (COM5)' in report
        assert 'a note' in report and 'Problems:\n  a problem' in report
        assert report.endswith('Nothing was started: press Live (or Start) when the instruments are ready.')
        assert result.title.startswith('thermo_camera_spectro')

    @pytest.mark.parametrize('command', list(CONFIGURATIONS))
    def test_mock(self, command):
        configuration = CONFIGURATIONS[command]
        found = Detections(SPECTROMETERS, CAMERAS, THERMOSTATS, ['something'])  # (nothing is looked at with --mock)

        result = plan(configuration, found, Options(mock=True))

        assert [spec.name for spec in result.modules] == configuration.module_names
        assert all(spec.recognized and spec.plugin == 'Mock' and spec.settings == {} for spec in result.modules)
        expected = {'thermostat': (None, 'Temp'), 'spectrometer': ('DAQ1D', None), 'camera': ('DAQ2D', None)}
        assert {spec.kind: (spec.dim, spec.axis_name) for spec in result.modules} == {
            spec.kind: expected[spec.kind] for spec in result.modules}
        assert result.to_connect == result.modules and result.notes == []
        assert all('Mock' in spec.found or 'simulated' in spec.found for spec in result.modules)

    def test_mock_pairs(self):
        result = plan(CONFIGURATIONS['thermo_camera_spectro'], Detections(), Options(mock=True))

        assert result.pairs == ['Spectro1 (Mock) <-> Thermo1 (Mock)', 'Camera1 (Mock) <-> Thermo1 (Mock)']

    def test_the_ports_which_are_given_replace_the_search(self):
        found = Detections(SPECTROMETERS, [], THERMOSTATS)  # (they are not looked at)

        result = plan(CONFIGURATIONS['thermo_2spectro'], found, Options(ports=('COM9', '/dev/ttyUSB3')))

        assert [(spec.settings, spec.recognized) for spec in result.modules[:2]] == [({'port': 'COM9'}, True),
                                                                                    ({'port': '/dev/ttyUSB3'}, True)]
        assert 'given on the command line' in result.module('Thermo1').found
        assert result.pairs == ['Spectro1 (serial FLMS10000) <-> Thermo1 (COM9)',
                                'Spectro2 (serial FLMS20000) <-> Thermo2 (/dev/ttyUSB3)']

    def test_one_port_for_two_controllers(self):
        result = plan(CONFIGURATIONS['thermo_2spectro'], Detections(SPECTROMETERS, [], THERMOSTATS),
                      Options(ports=('COM9',)))

        assert result.module('Thermo1').recognized and not result.module('Thermo2').recognized  # (no guessing)
        assert 'Thermo2' in result.module('Thermo2').instruction

    def test_too_many_ports(self):
        result = plan(CONFIGURATIONS['thermo_spectro'], Detections(SPECTROMETERS[:1]), Options(ports=('COM9', 'COM8')))

        assert result.module('Thermo1').settings == {'port': 'COM9'}
        assert any('2 ports were given' in note and 'COM8 not used' in note for note in result.notes)

    def test_a_port_for_an_application_without_controller(self):
        result = plan(CONFIGURATIONS['camera'], Detections(cameras=CAMERAS), Options(ports=('COM9',)))

        assert [spec.name for spec in result.modules] == ['Camera1']
        assert any('COM9 not used' in note for note in result.notes)

    def test_the_default_plugin_of_the_camera_is_the_one_which_found_it(self):
        found = Detections(cameras=[Camera('UC480', '4100000001', 'UI-1220 4100000001')])

        spec = plan(CONFIGURATIONS['camera'], found).module('Camera1')

        assert (spec.plugin, spec.recognized, spec.settings) == ('UC480', True, {'serial': '4100000001', 'index': 0})

    def test_the_default_plugin_of_the_camera_when_none_is_found(self):
        spec = plan(CONFIGURATIONS['camera'], Detections()).module('Camera1')

        assert (spec.plugin, spec.recognized) == ('Thorlabs_TSI', False)
        assert 'Camera1: no camera was recognized' in spec.instruction and '--camera-plugin' in spec.instruction

    def test_the_plugin_of_the_camera_can_be_chosen(self):
        found = Detections(cameras=[Camera('Thorlabs_TSI', '08365', 'Zelux'), Camera('UC480', '41', 'UI-1220 41'),
                                    Camera('UC480', '42', 'UI-1220 42')])

        spec = plan(CONFIGURATIONS['camera'], found, Options(camera_plugin='UC480')).module('Camera1')

        assert (spec.plugin, spec.recognized, spec.settings) == ('UC480', True, {'serial': '41', 'index': 0})
        first = plan(CONFIGURATIONS['camera'], found)  # (without a choice: the first one, the others are told)
        assert first.module('Camera1').plugin == 'Thorlabs_TSI'
        assert any('Several cameras were found' in note and 'UI-1220 41' in note for note in first.notes)

    def test_a_plugin_of_the_camera_which_was_chosen_and_found_nothing(self):
        found = Detections(cameras=CAMERAS)

        spec = plan(CONFIGURATIONS['camera'], found, Options(camera_plugin='UC480')).module('Camera1')

        assert (spec.plugin, spec.recognized) == ('UC480', False)  # (the Thorlabs one is another camera)
        assert 'Camera1: no camera was recognized' in spec.instruction

    def test_a_plugin_of_the_camera_which_cannot_be_recognized(self):
        spec = plan(CONFIGURATIONS['camera'], Detections(cameras=CAMERAS), Options(camera_plugin='Basler')).module(
            'Camera1')

        assert (spec.plugin, spec.recognized) == ('Basler', False)
        assert 'cannot be recognized' in spec.instruction

    def test_a_mock_camera_with_a_real_controller(self):
        result = plan(CONFIGURATIONS['thermo_camera'], Detections(thermostats=THERMOSTATS[:1]),
                      Options(camera_plugin='Mock'))

        assert [(spec.name, spec.plugin, spec.recognized) for spec in result.modules] == [
            ('Thermo1', 'QNW_TC', True), ('Camera1', 'Mock', True)]

    def test_the_plugin_of_the_controllers_can_be_chosen(self):
        mock = plan(CONFIGURATIONS['thermo_spectro'], Detections(), Options(thermostat_plugin='Mock'))
        other = plan(CONFIGURATIONS['thermo_spectro'], Detections(thermostats=THERMOSTATS), Options(
            thermostat_plugin='Lakeshore'))
        given = plan(CONFIGURATIONS['thermo_spectro'], Detections(), Options(thermostat_plugin='Lakeshore',
                                                                             ports=('COM4',)))

        assert (mock.module('Thermo1').plugin, mock.module('Thermo1').recognized,
                mock.module('Thermo1').axis_name) == ('Mock', True, 'Temp')
        assert (other.module('Thermo1').plugin, other.module('Thermo1').recognized) == ('Lakeshore', False)
        assert 'cannot be recognized' in other.module('Thermo1').instruction  # (only the controllers of QNW are)
        assert (given.module('Thermo1').plugin, given.module('Thermo1').recognized, given.module('Thermo1').settings,
                given.module('Thermo1').axis_name) == ('Lakeshore', True, {'port': 'COM4'}, None)

    def test_no_detect_connects_nothing(self):
        result = plan(CONFIGURATIONS['thermo_camera_spectro'], Detections(SPECTROMETERS, CAMERAS, THERMOSTATS),
                      Options(detect=False))

        assert not any(spec.recognized for spec in result.modules) and result.to_connect == []
        assert result.connect is False
        assert all('--no-detect' in spec.instruction for spec in result.modules)

    def test_no_detect_with_a_port_and_with_mock_connects_nothing_either(self):
        with_port = plan(CONFIGURATIONS['thermo_camera'], Detections(), Options(detect=False, ports=('COM9',)))
        mock = plan(CONFIGURATIONS['thermo_camera'], Detections(), Options(detect=False, mock=True))

        assert with_port.module('Thermo1').settings == {'port': 'COM9'} and with_port.to_connect == []
        assert 'not connected: --no-detect' in with_port.report()
        assert mock.module('Thermo1').plugin == 'Mock' and mock.to_connect == []

    def test_a_camera_plugin_for_an_application_without_camera(self):
        result = plan(CONFIGURATIONS['thermo_spectro'], Detections(), Options(camera_plugin='UC480'))

        assert any('--camera-plugin UC480 is ignored' in note for note in result.notes)

    def test_the_plan_is_pure(self):
        found = Detections(list(SPECTROMETERS), list(CAMERAS), list(THERMOSTATS), ['note'])

        plan(CONFIGURATIONS['thermo_camera_spectro'], found)

        assert found == Detections(list(SPECTROMETERS), list(CAMERAS), list(THERMOSTATS), ['note'])  # (not changed)


class TestModule:
    def test_no_qt_and_no_pymodaq_when_the_module_is_imported(self):
        """The module is imported by the command before anything else: it must not pull Qt or PyMoDAQ in"""
        tree = ast.parse(Path(detect.__file__).read_text(encoding='utf-8'))
        modules = set()
        for node in tree.body:  # (the top level only: the functions import what they need when they are called)
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.add(node.module)

        assert not [name for name in modules if name.split('.')[0] in ('qtpy', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6',
                                                                       'pymodaq', 'pymodaq_gui', 'pymodaq_data',
                                                                       'pyqtgraph', 'serial', 'seabreeze', 'pylablib',
                                                                       'pymodaq_plugins_oceaninsight',
                                                                       'pymodaq_plugins_quantumnorthwest')]
        assert 'pymodaq_plugins_thermospectro.configurations' in modules
