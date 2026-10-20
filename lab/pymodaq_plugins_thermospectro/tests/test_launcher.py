"""The five commands (launcher.py): the Dashboard, the modules of an application, its extension, and what is said

Without hardware: the instruments are Mock modules (``--mock``), the simulated Quantum Northwest controller
(``sim://...``), or what a fake detector says is connected. The Dashboard is real (offscreen).
"""
import copy
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from conftest import release
from pymodaq_plugins_thermospectro import launcher
from pymodaq_plugins_thermospectro.configurations import CONFIGURATIONS
from pymodaq_plugins_thermospectro.detect import (Camera, Detections, ModuleSpec, Options, Spectrometer, Thermostat,
                                                  plan)

pytestmark = pytest.mark.filterwarnings('ignore')

COMMANDS = list(CONFIGURATIONS)
PACKAGE = Path(launcher.__file__).parent
#: the units of each application: (number, spectrometer or camera, thermostat) as the extension chose them
UNITS = {'thermo_2spectro': [(1, 'Spectro1', 'Thermo1'), (2, 'Spectro2', 'Thermo2')],
         'thermo_spectro': [(1, 'Spectro1', 'Thermo1')],
         'camera': [(3, 'Camera1', 'None')],
         'thermo_camera': [(3, 'Camera1', 'Thermo1')],
         'thermo_camera_spectro': [(1, 'Spectro1', 'Thermo1'), (3, 'Camera1', 'Thermo1')]}


# ----------------------------------------------------------------------------------------------------------------------
# the command line
# ----------------------------------------------------------------------------------------------------------------------

class TestCommandLine:
    @pytest.mark.parametrize('command', COMMANDS)
    def test_the_defaults(self, command):
        assert launcher.parse_options(command, []) == Options(mock=False, camera_plugin=None,
                                                              thermostat_plugin='QNW_TC', ports=(), detect=True)

    def test_every_option(self):
        options = launcher.parse_options('thermo_2spectro', ['--mock', '--camera-plugin', 'UC480',
                                                             '--thermostat-plugin', 'Lakeshore', '--port', 'COM5',
                                                             '--port', 'COM6', '--no-detect'])

        assert options == Options(mock=True, camera_plugin='UC480', thermostat_plugin='Lakeshore',
                                  ports=('COM5', 'COM6'), detect=False)

    def test_the_command_line_of_the_process_is_the_default(self, monkeypatch):
        monkeypatch.setattr(sys, 'argv', ['thermo_camera', '--port', 'COM3'])

        assert launcher.parse_options('thermo_camera').ports == ('COM3',)

    def test_an_unknown_option_is_refused(self, capsys):
        with pytest.raises(SystemExit) as info:
            launcher.parse_options('camera', ['--nothing'])

        assert info.value.code == 2 and '--nothing' in capsys.readouterr().err

    @pytest.mark.parametrize('command', COMMANDS)
    def test_the_help_names_the_command_and_the_options(self, command, capsys):
        with pytest.raises(SystemExit) as info:
            launcher.main(command, ['--help'])

        told = capsys.readouterr().out
        assert info.value.code == 0 and f'usage: {command}' in told
        for option in ('--mock', '--camera-plugin', '--thermostat-plugin', '--port', '--no-detect'):
            assert option in told
        assert CONFIGURATIONS[command].title in ' '.join(told.split())

    def test_an_unknown_command(self):
        with pytest.raises(ValueError, match='thermospectro'):
            launcher.main('thermospectro', [])

    @pytest.mark.parametrize('command', COMMANDS)
    def test_each_command_is_a_function_of_the_module(self, command, monkeypatch):
        calls = []
        monkeypatch.setattr(launcher, 'main', lambda *args: calls.append(args) or 7)

        assert getattr(launcher, command)(['--mock']) == 7 and getattr(launcher, command)() == 7

        assert calls == [(command, ['--mock']), (command, None)]

    def test_the_commands_are_declared_by_the_package(self):
        import toml
        project = toml.load(PACKAGE.parents[1] / 'pyproject.toml')

        scripts = dict(project['project']['scripts'])

        assert scripts.pop('lab_apps') == 'pymodaq_plugins_thermospectro.apps:main'  # (the list of the applications)
        assert scripts == {command: f'pymodaq_plugins_thermospectro.launcher:{command}' for command in COMMANDS}
        assert 'thermospectro' not in project['project']['scripts']  # (the command of the presets is gone)

    def test_the_presets_are_gone(self):
        assert not (PACKAGE / 'resources' / 'presets').exists()
        assert not (PACKAGE.parents[1] / 'tools' / 'make_camera_presets.py').exists()
        assert not hasattr(launcher, 'install_presets') and not hasattr(launcher, 'PRESETS_FOLDER')


# ----------------------------------------------------------------------------------------------------------------------
# the Qt of opencv-python
# ----------------------------------------------------------------------------------------------------------------------

CV2_PLUGINS = '/env/lib/python3.12/site-packages/cv2/qt/plugins'
CV2_FONTS = '/env/lib/python3.12/site-packages/cv2/qt/fonts'


class TestTheQtOfOpencv:
    """opencv-python (the version with a GUI) points Qt to the Qt 5 it ships when it is imported, which PyMoDAQ does with
    the plugin of the Thorlabs cameras before it makes its application: PyQt6 could not start on Linux ("Could not load
    the Qt platform plugin xcb in .../cv2/qt/plugins")"""

    def test_the_folders_of_cv2_are_taken_out(self, monkeypatch):
        monkeypatch.setenv('QT_QPA_PLATFORM_PLUGIN_PATH', CV2_PLUGINS)
        monkeypatch.setenv('QT_QPA_FONTDIR', CV2_FONTS)

        changed = launcher.drop_opencv_qt_paths()

        assert changed == ['QT_QPA_PLATFORM_PLUGIN_PATH', 'QT_QPA_FONTDIR']
        assert 'QT_QPA_PLATFORM_PLUGIN_PATH' not in os.environ and 'QT_QPA_FONTDIR' not in os.environ

    def test_what_the_user_set_is_kept(self, monkeypatch):
        monkeypatch.setenv('QT_QPA_PLATFORM_PLUGIN_PATH', '/opt/qt6/plugins')
        monkeypatch.delenv('QT_QPA_FONTDIR', raising=False)

        assert launcher.drop_opencv_qt_paths() == [] and os.environ['QT_QPA_PLATFORM_PLUGIN_PATH'] == '/opt/qt6/plugins'

    def test_in_a_list_only_the_folders_of_cv2_go(self, monkeypatch):
        monkeypatch.setenv('QT_QPA_PLATFORM_PLUGIN_PATH', os.pathsep.join(['/opt/qt6/plugins', CV2_PLUGINS, '/usr/lib/qt']))

        assert launcher.drop_opencv_qt_paths() == ['QT_QPA_PLATFORM_PLUGIN_PATH']
        assert os.environ['QT_QPA_PLATFORM_PLUGIN_PATH'] == os.pathsep.join(['/opt/qt6/plugins', '/usr/lib/qt'])

    def test_nothing_is_set_nothing_changes(self, monkeypatch):
        monkeypatch.delenv('QT_QPA_PLATFORM_PLUGIN_PATH', raising=False)
        monkeypatch.delenv('QT_QPA_FONTDIR', raising=False)

        assert launcher.drop_opencv_qt_paths() == []

    def test_a_name_which_only_looks_like_it_is_not_taken(self, monkeypatch):
        monkeypatch.setenv('QT_QPA_PLATFORM_PLUGIN_PATH', '/opt/qt6/cv2_tools/plugins')  # (the folder is called cv2_tools)
        monkeypatch.delenv('QT_QPA_FONTDIR', raising=False)

        assert launcher.drop_opencv_qt_paths() == []

    def test_the_application_is_made_without_them(self, make, monkeypatch):
        from pymodaq_gui import qt_utils
        seen = []
        original = qt_utils.mkQApp

        def spy(*args, **kwargs):
            seen.append((os.environ.get('QT_QPA_PLATFORM_PLUGIN_PATH'), os.environ.get('QT_QPA_FONTDIR')))
            return original(*args, **kwargs)

        monkeypatch.setattr(qt_utils, 'mkQApp', spy)
        monkeypatch.setenv('QT_QPA_PLATFORM_PLUGIN_PATH', CV2_PLUGINS)  # (as `import cv2` leaves them)
        monkeypatch.setenv('QT_QPA_FONTDIR', CV2_FONTS)

        make('thermo_camera', ['--mock', '--camera-plugin', 'Mock'])

        assert seen == [(None, None)]  # (when Qt chose its platform plugin)


# ----------------------------------------------------------------------------------------------------------------------
# the settings of the modules
# ----------------------------------------------------------------------------------------------------------------------

class FakeModule:
    """A control module: the settings of a plugin in the group of its kind"""

    def __init__(self, kind, params):
        from pymodaq_gui.parameter import Parameter
        self._hw_settings_name = f'{kind}_settings'
        self.settings = Parameter.create(name='settings', type='group', children=[
            {'name': self._hw_settings_name, 'type': 'group', 'children': copy.deepcopy(params)}])

    def setting(self, *path):
        return self.settings.child(self._hw_settings_name, *path)


def thermostat_spec(port, plugin='QNW_TC'):
    return ModuleSpec('Thermo1', 'thermostat', plugin, axis_name='Temperature', settings={'port': port},
                      recognized=True)


def camera_spec(serial='08365', rank=0, plugin='Thorlabs_TSI'):
    return ModuleSpec('Camera1', 'camera', plugin, 'DAQ2D', settings={'serial': serial, 'index': rank},
                      recognized=True)


@pytest.fixture
def qnw_module(qapp):
    from pymodaq_plugins_quantumnorthwest.daq_move_plugins.daq_move_QNW_TC import DAQ_Move_QNW_TC
    return FakeModule('actuator', DAQ_Move_QNW_TC.params)


class TestSettings:
    def test_a_port_of_the_list(self, qnw_module):
        assert launcher.apply_settings(qnw_module, thermostat_spec('sim://tc1b')) == []

        port = qnw_module.setting('connection', 'port')
        assert port.value() == 'sim://tc1b' and port.opts['limits'].count('sim://tc1b') == 1
        assert qnw_module.setting('connection', 'other_port').value() == ''

    def test_a_port_which_was_plugged_in_after_pymodaq_started_is_added_to_the_list(self, qnw_module):
        assert launcher.apply_settings(qnw_module, thermostat_spec('COM42')) == []

        port = qnw_module.setting('connection', 'port')
        assert port.value() == 'COM42' and 'COM42' in port.opts['limits'] and 'sim://tc125' in port.opts['limits']

    def test_a_port_which_the_user_typed_in_the_other_port_is_replaced_there(self, qnw_module):
        qnw_module.setting('connection', 'other_port').setValue('socket://host:5000')

        launcher.apply_settings(qnw_module, thermostat_spec('COM42'))

        assert qnw_module.setting('connection', 'other_port').value() == 'COM42'  # (it replaces the list)

    def test_a_spectrometer(self, qapp):
        from pymodaq_plugins_oceaninsight.daq_viewer_plugins.plugins_1D.daq_1Dviewer_Seabreeze import (
            DAQ_1DViewer_Seabreeze)
        module = FakeModule('detector', DAQ_1DViewer_Seabreeze.params)
        found = Detections(spectrometers=[Spectrometer('FLMS12345', 'FLAME-S', 'cseabreeze',
                                                       '<SeaBreezeDevice FLAME-S:FLMS12345>')])
        spec = plan(CONFIGURATIONS['thermo_spectro'], found).module('Spectro1')

        assert launcher.apply_settings(module, spec) == []

        assert module.setting('backend').value() == 'cseabreeze'
        device = module.setting('device')
        assert device.value() == '<SeaBreezeDevice FLAME-S:FLMS12345>'
        assert device.opts['limits'] == ['Auto', '<SeaBreezeDevice FLAME-S:FLMS12345>']  # (Auto is still there)

    def test_a_camera_whose_plugin_has_a_serial_number(self, qapp):
        module = FakeModule('detector', [{'name': 'serial_number', 'type': 'str', 'value': ''}])

        assert launcher.apply_settings(module, camera_spec()) == []

        assert module.setting('serial_number').value() == '08365'

    def test_a_serial_number_which_is_read_only_is_left_alone(self, qapp):
        module = FakeModule('detector', [{'name': 'serial', 'type': 'str', 'value': 'x', 'readonly': True}])

        assert launcher.apply_settings(module, camera_spec()) == []  # (the first camera: the plugin takes it)

        assert module.setting('serial').value() == 'x'

    def test_a_camera_which_is_chosen_in_a_list(self, qapp):
        module = FakeModule('detector', [{'name': 'camera_list', 'type': 'list',
                                          'limits': ['Zelux 08364', 'Zelux 08365'], 'value': 'Zelux 08364'}])

        assert launcher.apply_settings(module, camera_spec()) == []

        assert module.setting('camera_list').value() == 'Zelux 08365'

    def test_a_camera_which_is_chosen_by_its_index(self, qapp):
        module = FakeModule('detector', [{'name': 'group', 'type': 'group', 'children': [
            {'name': 'camera_index', 'type': 'int', 'value': 0}]}])

        assert launcher.apply_settings(module, camera_spec(rank=1)) == []

        assert module.setting('group', 'camera_index').value() == 1

    def test_a_camera_which_cannot_be_chosen_is_told_unless_it_is_the_first(self, qapp):
        module = FakeModule('detector', [{'name': 'exposure', 'type': 'float', 'value': 1.}])

        assert launcher.apply_settings(module, camera_spec()) == []
        problems = launcher.apply_settings(module, camera_spec('08366', 1))

        assert len(problems) == 1 and 'Camera1' in problems[0] and '08366' in problems[0]
        assert 'select it' in problems[0]

    def test_the_port_of_the_controller_of_another_plugin(self, qapp):
        module = FakeModule('actuator', [{'name': 'link', 'type': 'group', 'children': [
            {'name': 'COM_port', 'type': 'str', 'value': ''}]}])

        assert launcher.apply_settings(module, thermostat_spec('COM4', 'Lakeshore')) == []

        assert module.setting('link', 'COM_port').value() == 'COM4'

    def test_the_port_which_cannot_be_found_is_told(self, qapp):
        module = FakeModule('actuator', [{'name': 'gain', 'type': 'float', 'value': 1.}])

        problems = launcher.apply_settings(module, thermostat_spec('COM4', 'Lakeshore'))

        assert len(problems) == 1 and 'Lakeshore' in problems[0] and 'COM4' in problems[0]

    @pytest.mark.errors_expected
    def test_a_module_which_cannot_be_set_up_never_raises(self, qapp):
        module = FakeModule('actuator', [])  # (not a plugin with a connection group)

        problems = launcher.apply_settings(module, thermostat_spec('COM4'))

        assert len(problems) == 1 and 'Thermo1' in problems[0] and 'check them in the Dashboard' in problems[0]

    def test_nothing_to_set_for_a_mock(self, qapp):
        module = FakeModule('actuator', [])
        spec = ModuleSpec('Thermo1', 'thermostat', 'Mock', axis_name='Temp', recognized=True)

        assert launcher.apply_settings(module, spec) == []

    def test_the_plugins_which_are_there(self):
        for kind, name, dim in (('thermostat', 'Mock', None), ('thermostat', 'QNW_TC', None),
                                ('spectrometer', 'Mock', 'DAQ1D'), ('spectrometer', 'Seabreeze', 'DAQ1D'),
                                ('camera', 'Mock', 'DAQ2D')):
            assert launcher.missing_plugin(ModuleSpec('X', kind, name, dim)) is None

    def test_a_plugin_which_is_not_installed(self):
        told = launcher.missing_plugin(ModuleSpec('Camera1', 'camera', 'Thorlabs_NoSuch', 'DAQ2D'))

        assert 'Camera1' in told and 'Thorlabs_NoSuch is not installed' in told and 'install it' in told
        assert 'lab/install_lab.py --camera' in launcher.missing_plugin(ModuleSpec('C', 'camera', 'UC480', 'DAQ1D'))


# ----------------------------------------------------------------------------------------------------------------------
# the application
# ----------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def told(monkeypatch):
    """What the user is told by the extension (the dialogs are replaced): (title, text, error)"""
    from pymodaq_plugins_thermospectro.extensions.thermo_spectro import ThermoSpectro
    messages = []
    monkeypatch.setattr(ThermoSpectro, '_notify',
                        lambda self, title, text, error=False: messages.append((title, text, error)))
    return messages


@pytest.fixture
def make(qtbot, tmp_path, monkeypatch):
    """``make(command, argv, detections)`` starts a command whose search finds ``detections`` and closes it at the end
    of the test. It returns the launcher once it is done with the modules (they may be being initialized)"""
    from pymodaq_plugins_thermospectro.extensions import thermo_spectro
    monkeypatch.setattr(thermo_spectro, 'default_store_path', lambda: tmp_path / 'remembered' / 'rois.json')
    made = []

    def start(command, argv=(), detections=None, wait=True, detector=None, **options):
        calls = []

        def look(configuration, options):
            calls.append(threading.current_thread())
            return detections if detections is not None else Detections()

        lch = launcher.Launcher(CONFIGURATIONS[command], launcher.parse_options(command, list(argv)),
                                detector or look, **options)
        lch.calls = calls
        made.append(lch)
        lch.start()
        if wait:
            qtbot.waitUntil(lambda: lch.state in ('ready', 'failed'), timeout=60000)
            qtbot.wait(300)
        return lch

    yield start
    for lch in made:
        windows = [lch.shared_ui.mainwindow] + ([lch.extension.mainwindow] if lch.extension is not None else [])
        lch.close()
        for window in windows:
            release(window)
        qtbot.wait(150)


def module_of(lch, name):
    from pymodaq.utils.managers.modules import ModuleType
    return lch.dashboard.modules_manager.get_mod_from_name(name, mod=ModuleType.Control)


def connected(qtbot, lch, names=None):
    """Wait for the modules (all of them, or the ones named) to be initialized, then a little more: the threads of the
    modules go on setting up a moment after they say so (see conftest.py)"""
    names = names if names is not None else [spec.name for spec in lch.plan.modules]
    modules = [module_of(lch, name) for name in names]
    qtbot.waitUntil(lambda: all(module.initialized_state for module in modules), timeout=60000)
    qtbot.wait(600)
    return modules


def nothing_runs(lch):
    """No acquisition was started by anything: not by the extension, not by a module"""
    extension = lch.extension
    assert not extension.run_active and not extension.monitoring
    for module in lch.dashboard.modules_manager.modules_all:
        assert not getattr(module, 'grab_state', False)
    assert all(unit.state.value == 'idle' for unit in extension.units)


class TestMock:
    @pytest.mark.parametrize('command', COMMANDS)
    def test_the_application_with_mock_instruments(self, make, qtbot, told, command):
        configuration = CONFIGURATIONS[command]

        lch = make(command, ['--mock'])

        assert lch.state == 'ready' and lch.problems == []
        modules = connected(qtbot, lch)
        # the modules of the application, with their fixed names, and nothing else
        manager = lch.dashboard.modules_manager
        assert sorted(manager.modules_name) == sorted(configuration.module_names)
        assert len(modules) == len(configuration.module_names) and all(m.initialized_state for m in modules)
        assert sorted(manager.actuators_name) == sorted(configuration.thermostat_names)
        cameras = [configuration.camera_name] if configuration.camera else []
        assert sorted(manager.detectors_name) == sorted(configuration.spectrometer_names + cameras)
        for name in configuration.thermostat_names:
            assert module_of(lch, name).actuator == 'Mock' and module_of(lch, name).axis_name == 'Temp'
        for name in configuration.spectrometer_names:
            assert module_of(lch, name).daq_type.name == 'DAQ1D'
        if configuration.camera:
            assert module_of(lch, 'Camera1').daq_type.name == 'DAQ2D'
        # the extension of the application, with its units, which found their modules
        extension = lch.extension
        assert type(extension).__name__ == configuration.class_name
        assert [unit.number for unit in extension.units] == list(configuration.numbers)
        assert [(unit.number, unit.settings.spectrometer, unit.settings.thermostat)
                for unit in extension.units] == UNITS[command]
        assert all(unit.settings.enabled for unit in extension.units)
        for number in configuration.numbers:
            assert extension.dock_visible(f'spectrum{number}') and extension.dock_visible(f'rois{number}')
        if command == 'thermo_camera':  # (the camera runs the ramp of the thermostat, unless a spectrometer does)
            assert extension.unit(3).settings.run_ramp is True
        elif command == 'thermo_camera_spectro':
            assert extension.unit(3).settings.run_ramp is False and extension.unit(1).settings.run_ramp is True
        # it told what it did, and started nothing
        assert [message[0] for message in told] == [lch.plan.title] and told[0][2] is False
        assert told[0][1] == lch.report and 'Nothing was started' in lch.report
        total = len(configuration.module_names)
        assert f'{total} of {total} instruments recognized' in lch.report
        nothing_runs(lch)

    def test_the_report_is_printed_on_the_console(self, make, capsys):
        lch = make('thermo_spectro', ['--mock'])

        assert lch.report in capsys.readouterr().out

    def test_the_report_is_shown_by_the_extension_without_blocking_it(self, make, qtbot):
        from qtpy import QtCore, QtWidgets
        lch = make('thermo_spectro', ['--mock'])  # (the real dialog, not the replaced one)

        boxes = lch.extension.mainwindow.findChildren(QtWidgets.QMessageBox)

        assert len(boxes) == 1 and boxes[0].text() == lch.report and boxes[0].windowTitle() == lch.plan.title
        assert boxes[0].windowModality() == QtCore.Qt.WindowModality.NonModal

    def test_the_window_stays_open_and_the_modules_are_in_it(self, make):
        lch = make('camera', ['--mock'])

        assert lch.shared_ui.mainwindow.isVisible()
        assert lch.dashboard.modules_manager.modules_name == ['Camera1']

    def test_what_is_connected_is_never_looked_for_with_mock(self, make):
        lch = make('thermo_camera_spectro', ['--mock'])

        assert lch.calls == []  # (the detector was not asked)
        assert lch.detections == Detections()


class TestRecognition:
    def test_everything_is_recognized(self, make, qtbot, told):
        """The simulated controller is recognized on its port: it is given to the module, which connects"""
        found = Detections(cameras=[Camera('Thorlabs_TSI', '08365', 'Zelux')],
                           thermostats=[Thermostat('sim://tc1b', '2.6.04')])

        lch = make('thermo_camera', ['--camera-plugin', 'Mock'], found)

        thermo = connected(qtbot, lch, ['Thermo1'])[0]
        info = thermo.settings['actuator_settings', 'connection', 'controller_info']
        assert thermo.actuator == 'QNW_TC' and thermo.axis_name == 'Temperature'
        assert thermo.settings['actuator_settings', 'connection', 'port'] == 'sim://tc1b'
        assert 'SIMULATED CONTROLLER' in info and 'firmware 2.6.04' in info  # (a TC 1B)
        assert 'Quantum Northwest controller on sim://tc1b (firmware 2.6.04)' in lch.report
        assert lch.problems == [] and told[0][2] is False
        nothing_runs(lch)

    def test_the_thermal_controller_is_not_recognized_and_the_user_chooses_its_port(self, make, qtbot, told):
        """The user's own case: what is connected is recognized, the controller is not. The user selects its port in the
        Dashboard, connects it, and starts the acquisition himself"""
        lch = make('thermo_camera', ['--camera-plugin', 'Mock'], Detections())

        camera, thermo = module_of(lch, 'Camera1'), module_of(lch, 'Thermo1')
        qtbot.waitUntil(lambda: camera.initialized_state, timeout=60000)  # (what was recognized is connected)
        qtbot.wait(600)
        assert thermo is not None and not thermo.initialized_state  # (the other one is there, and left alone)
        assert thermo.actuator == 'QNW_TC'
        title, text, error = told[0]
        assert title == lch.plan.title and error is False
        assert 'Camera1: simulated camera (Mock)' in text
        assert 'Thermo1: no thermal controller was recognized' in text
        for words in ('select its port in the settings of the module Thermo1 in the Dashboard', 'Ini button',
                      'Connect the modules', 'Live'):
            assert words in text
        nothing_runs(lch)
        # the user selects the port in the settings of the module, in the Dashboard, and clicks its Ini button
        thermo.settings.child('actuator_settings', 'connection', 'port').setValue('sim://tc125')
        thermo.init_hardware_ui(True)
        qtbot.waitUntil(lambda: thermo.initialized_state, timeout=60000)
        qtbot.wait(600)
        assert 'firmware 9.1' in thermo.settings['actuator_settings', 'connection', 'controller_info']  # (a TC 125)
        nothing_runs(lch)  # (it connects, and waits for the user to start)

    def test_the_user_connects_it_with_the_extension(self, make, qtbot):
        lch = make('thermo_camera', ['--camera-plugin', 'Mock'], Detections())
        thermo = module_of(lch, 'Thermo1')
        thermo.settings.child('actuator_settings', 'connection', 'port').setValue('sim://tc1b')

        lch.extension.initialize_modules([thermo])  # (Connect the modules)

        qtbot.waitUntil(lambda: thermo.initialized_state, timeout=60000)
        qtbot.wait(600)
        nothing_runs(lch)

    def test_nothing_is_recognized(self, make, qtbot, told):
        lch = make('thermo_spectro', [], Detections(notes=['Spectrometers: none found (the pyseabreeze backend...)']))

        assert lch.state == 'ready'
        assert [spec.name for spec in lch.plan.not_recognized] == ['Thermo1', 'Spectro1']
        qtbot.wait(800)
        assert not any(module.initialized_state for module in lch.dashboard.modules_manager.modules_all)
        assert sorted(lch.dashboard.modules_manager.modules_name) == ['Spectro1', 'Thermo1']
        text = told[0][1]
        assert 'Thermo1: no thermal controller was recognized' in text
        assert 'Spectro1: no spectrometer was recognized' in text
        assert 'Spectrometers: none found' in text and '0 of 2 instruments recognized' in text
        assert [(unit.number, unit.settings.spectrometer, unit.settings.thermostat)
                for unit in lch.extension.units] == [(1, 'Spectro1', 'Thermo1')]
        nothing_runs(lch)

    def test_the_port_is_given(self, make, qtbot):
        lch = make('thermo_camera', ['--camera-plugin', 'Mock', '--port', 'sim://tc1b'])

        thermo = connected(qtbot, lch, ['Thermo1'])[0]

        assert thermo.settings['actuator_settings', 'connection', 'port'] == 'sim://tc1b'
        assert 'given on the command line' in lch.report
        assert lch.calls and lch.detections == Detections()  # (the ports were not looked for)

    def test_no_detect_makes_the_modules_and_connects_nothing(self, make, qtbot, told):
        found = Detections(thermostats=[Thermostat('COM1', '9')])

        lch = make('thermo_camera', ['--no-detect', '--camera-plugin', 'Mock', '--port', 'sim://tc1b'], found)

        qtbot.wait(800)
        assert lch.calls == [] and lch.plan.connect is False
        assert sorted(lch.dashboard.modules_manager.modules_name) == ['Camera1', 'Thermo1']
        assert not any(module.initialized_state for module in lch.dashboard.modules_manager.modules_all)
        assert module_of(lch, 'Thermo1').settings['actuator_settings', 'connection', 'port'] == 'sim://tc1b'
        assert 'not connected: --no-detect' in told[0][1]
        nothing_runs(lch)

    def test_the_instruments_are_looked_for_while_the_window_is_alive(self, make, qtbot):
        started = threading.Event()

        def slow(configuration, options):
            started.set()
            time.sleep(0.6)
            return Detections(thermostats=[Thermostat('sim://tc125', '9.1')])

        lch = make('thermo_camera', ['--camera-plugin', 'Mock'], wait=False, detector=slow)

        qtbot.waitUntil(started.is_set, timeout=10000)
        assert lch.state == 'detecting' and lch.plan is None and lch.shared_ui.mainwindow.isVisible()
        qtbot.waitUntil(lambda: lch.state in ('ready', 'failed'), timeout=60000)
        assert lch.state == 'ready' and lch.plan.module('Thermo1').settings == {'port': 'sim://tc125'}
        connected(qtbot, lch)

    @pytest.mark.errors_expected
    def test_a_search_which_hangs_is_given_up(self, make, qtbot, told):
        freed = threading.Event()

        def hung(configuration, options):
            freed.wait(30)
            return Detections(thermostats=[Thermostat('sim://tc125', '9.1')])

        try:
            lch = make('thermo_camera', ['--camera-plugin', 'Mock'], detector=hung, detection_timeout=0.5)

            assert lch.state == 'ready'
            assert 'took more than 0.5 s' in told[0][1] and 'Thermo1: no thermal controller was recognized' in told[0][1]
            freed.set()  # (the driver answers at last: too late, the application went on without it)
            qtbot.wait(400)
            assert lch.detections.thermostats == [] and lch.plan.module('Thermo1').settings == {}
        finally:
            freed.set()

    def test_the_search_is_not_made_by_the_thread_of_the_gui(self, make):
        lch = make('thermo_camera', ['--camera-plugin', 'Mock'], Detections())

        assert len(lch.calls) == 1 and lch.calls[0] is not threading.main_thread()

    @pytest.mark.errors_expected
    def test_a_search_which_fails_does_not_stop_anything(self, make, told):
        def broken(configuration, options):
            raise RuntimeError('the USB bus is on fire')

        lch = make('thermo_camera', ['--camera-plugin', 'Mock'], detector=broken)

        assert lch.state == 'ready' and 'the USB bus is on fire' in told[0][1]
        assert 'Thermo1: no thermal controller was recognized' in told[0][1]

    def test_a_plugin_which_is_not_installed_is_told_and_the_others_are_made(self, make, qtbot, told):
        lch = make('thermo_camera_spectro', ['--camera-plugin', 'NoSuchCamera', '--port', 'sim://tc1b'], Detections())

        assert lch.state == 'ready' and lch.absent == ['Camera1']
        assert sorted(lch.dashboard.modules_manager.modules_name) == ['Spectro1', 'Thermo1']
        assert len(lch.problems) == 1 and 'NoSuchCamera is not installed' in lch.problems[0]
        title, text, error = told[0]
        assert error is True and 'Problems:' in text and 'NoSuchCamera is not installed' in text
        assert 'Camera1: no camera' not in text and 'Camera1: the cameras' not in text  # (it is not in the Dashboard)
        assert [(unit.number, unit.settings.enabled) for unit in lch.extension.units] == [(1, True), (3, False)]
        nothing_runs(lch)


class TestEnd:
    def test_the_modules_are_closed_with_the_application(self, make, qtbot):
        lch = make('thermo_camera', ['--mock'])
        connected(qtbot, lch)

        lch.close()

        assert lch.state == 'closed' and lch.dashboard.modules_manager.modules_all == []

    def test_the_end_of_the_application_closes_everything_once(self, make, qtbot):
        lch = make('thermo_spectro', ['--mock'])
        closed = []
        quit_fun = lch.dashboard.quit_fun
        lch.dashboard.quit_fun = lambda: closed.append(True) or quit_fun()

        lch.app.aboutToQuit.emit()
        lch.close()

        assert lch.state == 'closed' and closed == [True]

    def test_the_command_waits_for_the_user_and_ends_cleanly(self, qtbot, monkeypatch, tmp_path, capsys):
        """main(): the event loop runs until the application is quit, then the modules are closed"""
        from qtpy import QtCore, QtWidgets
        from pymodaq_plugins_thermospectro.extensions import thermo_spectro
        monkeypatch.setattr(thermo_spectro, 'default_store_path', lambda: tmp_path / 'remembered' / 'rois.json')
        monkeypatch.setattr(sys, 'argv', ['thermo_spectro', '--mock'])
        made = []
        original = launcher.Launcher.__init__

        def remember(self, *args, **kwargs):
            original(self, *args, **kwargs)
            made.append(self)

        monkeypatch.setattr(launcher.Launcher, '__init__', remember)
        timer = QtCore.QTimer()
        quitting = []

        def quit_when_ready():
            if made and made[0].state == 'ready':
                modules = made[0].dashboard.modules_manager.modules_all
                if len(modules) == 2 and all(module.initialized_state for module in modules):
                    if not quitting:
                        quitting.append(time.monotonic())
                    elif time.monotonic() - quitting[0] > 1.:  # (a moment for the threads of the modules)
                        QtWidgets.QApplication.quit()

        timer.timeout.connect(quit_when_ready)
        timer.start(100)
        QtCore.QTimer.singleShot(90000, QtWidgets.QApplication.quit)  # (not for ever, whatever happens)
        try:
            code = launcher.main('thermo_spectro')
        finally:
            timer.stop()
        window = made[0].extension.mainwindow

        assert code == 0 and sys.argv == ['thermo_spectro']  # (PyMoDAQ does not see the options)
        assert made[0].state == 'closed' and made[0].report in capsys.readouterr().out
        assert made[0].problems == [] and made[0].dashboard.modules_manager.modules_all == []
        release(made[0].shared_ui.mainwindow)
        release(window)
        qtbot.wait(150)
