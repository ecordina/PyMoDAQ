# -*- coding: utf-8 -*-
"""The five commands of the lab: ``thermo_2spectro``, ``thermo_spectro``, ``camera``, ``thermo_camera`` and
``thermo_camera_spectro``

Each one is an application (see :mod:`.configurations`): it opens the Dashboard of PyMoDAQ with a control module for
each of its instruments (*Thermo1*, *Spectro1*, *Camera1*...), looks at what is connected to the computer
(:mod:`.detect`), gives each module the instrument it recognized, connects the recognized modules (and only those) and
opens the extension of the application. It then says, for each instrument, what was recognized (its name and its serial
number or port) and what was not, and what to do about it: select the port of the thermal controller in the settings of
its module in the Dashboard, click its *Ini* button (or *Connect the modules* in the extension), and press *Live* or
*Start*. It never starts anything by itself: the Dashboard stays open, it is where the instruments which were not
recognized are chosen.

    thermo_camera_spectro                  # what is connected is recognized
    thermo_camera_spectro --mock           # no hardware: every instrument is a Mock module
    thermo_camera_spectro --port COM6      # the thermal controller is on COM6 (it is not looked for)
    thermo_camera_spectro --no-detect      # the modules are made, nothing is looked for nor connected
"""
from __future__ import annotations

import argparse
import functools
import random
import sys
import threading
import time
from typing import Any, Optional, Sequence

from qtpy import QtCore

from pymodaq_utils.logger import set_logger, get_module_name

from pymodaq_plugins_thermospectro import detect
from pymodaq_plugins_thermospectro.configurations import CONFIGURATIONS, Configuration
from pymodaq_plugins_thermospectro.detect import Detections, ModuleSpec, Options, Plan

logger = set_logger(get_module_name(__file__))

POLL_INTERVAL = 100  # ms: how often the detection, which runs in a thread, is looked at
DETECTION_TIMEOUT = 60.  # s: a driver which does not answer must not keep the application from opening
#: the names a plugin gives to the settings which tell its instrument, for the plugins which are not the usual ones
PORT_NAMES = ('port', 'com_port', 'serial_port', 'comport', 'resource', 'visa_resource', 'address')
SERIAL_NAMES = ('serial', 'serial_number', 'serial_no', 'camera_serial', 'camera_serial_number')
INDEX_NAMES = ('camera_index', 'index', 'camera_number', 'device_index')
CHOICE_NAMES = ('camera', 'camera_list', 'camera_name', 'camera_id', 'device', 'devices')
PLUGIN_HINTS = {'Thorlabs_TSI': 'python lab/install_lab.py --camera', 'UC480': 'python lab/install_lab.py --camera',
                'Seabreeze': 'python lab/install_lab.py', 'QNW_TC': 'python lab/install_lab.py'}


# ----------------------------------------------------------------------------------------------------------------------
# the command line
# ----------------------------------------------------------------------------------------------------------------------

def make_parser(command: str) -> argparse.ArgumentParser:
    """The command line of a command"""
    configuration = CONFIGURATIONS[command]
    parser = argparse.ArgumentParser(
        prog=command,
        description=f'{configuration.title}. Opens the Dashboard of PyMoDAQ with a module for each instrument and the '
                    f'extension of the application, looks at what is connected to the computer, connects what it '
                    f'recognizes and tells which instruments it did not: select them in the settings of their module '
                    f'in the Dashboard, click their Ini button, then press Live or Start in the extension. Nothing is '
                    f'started by the command itself.')
    parser.add_argument('--mock', action='store_true',
                        help='no hardware: every instrument is a Mock module (a Mock 1D detector, a Mock 2D detector, '
                             'a Mock actuator with the axis Temp) and nothing is looked for')
    parser.add_argument('--camera-plugin', metavar='NAME',
                        help='the plugin of the camera (default: the one of the camera which was found, else '
                             'Thorlabs_TSI; the cameras of the plugins Thorlabs_TSI and UC480 are recognized)')
    parser.add_argument('--thermostat-plugin', metavar='NAME', default=detect.THERMOSTAT_PLUGIN,
                        help=f'the plugin of the thermal controllers, any actuator whose position is a temperature '
                             f'(default: {detect.THERMOSTAT_PLUGIN}, the only one which is recognized)')
    parser.add_argument('--port', metavar='PORT', action='append', dest='ports', default=[],
                        help='the port of a thermal controller (COM6, /dev/ttyUSB0, sim://tc125): once for each '
                             'controller, in order; the ports are not looked for then')
    parser.add_argument('--no-detect', action='store_true',
                        help='make the modules and connect nothing (nothing is looked for)')
    return parser


def parse_options(command: str, argv: Optional[Sequence[str]] = None) -> Options:
    """The options of the user, from the command line (``argv`` is the command line without the name of the command,
    ``sys.argv[1:]`` by default)"""
    args = make_parser(command).parse_args(argv)
    return Options(mock=args.mock, camera_plugin=args.camera_plugin, thermostat_plugin=args.thermostat_plugin,
                   ports=tuple(args.ports), detect=not args.no_detect)


# ----------------------------------------------------------------------------------------------------------------------
# the settings of the modules
# ----------------------------------------------------------------------------------------------------------------------

def _walk(param):
    for child in param.children():
        yield child
        yield from _walk(child)


def _find(root, names: Sequence[str], types: Sequence[str]):
    """The first setting under ``root`` (looked for level by level) which can be written, has one of these names and is
    one of these types, None if there is none"""
    level = list(root.children())
    while level:
        for param in level:
            if param.name().lower() in names and param.type() in types and not param.opts.get('readonly', False):
                return param
        level = [child for param in level for child in param.children()]
    return None


def _set_choice(param, value) -> None:
    """Set a setting. What a list does not offer (a port which was plugged in after PyMoDAQ started) is added to it"""
    if param.type() == 'list':
        limits = param.opts.get('limits', [])
        if isinstance(limits, dict):
            if value not in limits and value not in limits.values():
                param.setLimits({**limits, str(value): value})
        elif value not in limits:
            param.setLimits(list(limits) + [value])
    param.setValue(value)


def _apply_thermostat(root, spec: ModuleSpec, problems: list) -> None:
    port = spec.settings.get('port')
    if port is None:
        return
    if spec.plugin == detect.THERMOSTAT_PLUGIN:
        other = root.child('connection', 'other_port')  # (when it is filled it replaces the port of the list)
        _set_choice(other if other.value() else root.child('connection', 'port'), port)
        return
    param = _find(root, PORT_NAMES, ('list', 'str'))
    if param is None:
        problems.append(f'{spec.name}: the plugin {spec.plugin} has no setting for its port which could be found: '
                        f'select {port} in its settings')
    else:
        _set_choice(param, port)


def _apply_spectrometer(root, spec: ModuleSpec, problems: list) -> None:
    for key in ('backend', 'device'):  # (the device is chosen by its serial number: see the Seabreeze plugin)
        if key in spec.settings:
            _set_choice(root.child(key), spec.settings[key])


def _apply_camera(root, spec: ModuleSpec, problems: list) -> None:
    serial, rank = spec.settings.get('serial'), spec.settings.get('index', 0)
    if serial:
        param = _find(root, SERIAL_NAMES, ('str',))
        if param is not None:
            param.setValue(serial)
            return
        for param in _walk(root):  # (a list of the cameras: the one which has the serial number in it)
            if param.type() == 'list' and param.name().lower() in CHOICE_NAMES and not param.opts.get('readonly'):
                limits = param.opts.get('limits', [])
                for item in (limits.values() if isinstance(limits, dict) else limits):
                    if serial in str(item):
                        param.setValue(item)
                        return
    param = _find(root, INDEX_NAMES, ('int',))
    if param is not None:
        param.setValue(rank)
    elif rank:  # (the first camera is the one the plugin takes when it is not told)
        problems.append(f'{spec.name}: it is the camera number {rank + 1} of the plugin {spec.plugin} (serial number '
                        f'{serial}), which cannot be chosen from here: select it in the settings of the module')


def apply_settings(module, spec: ModuleSpec) -> list[str]:
    """Set what a plan says in a module, before it is connected

    Parameters
    ----------
    module: DAQ_Move or DAQ_Viewer
        The module of the Dashboard which is the one of ``spec``
    spec: ModuleSpec

    Returns
    -------
    The settings which could not be set, as what to tell the user (an empty list when all went well). Never raises.
    """
    problems: list[str] = []
    handlers = {'thermostat': _apply_thermostat, 'spectrometer': _apply_spectrometer, 'camera': _apply_camera}
    try:
        name = getattr(module, '_hw_settings_name', None) or ('actuator_settings' if spec.is_actuator
                                                              else 'detector_settings')
        handlers[spec.kind](module.settings.child(name), spec, problems)
    except Exception as e:
        logger.exception(f'The settings of {spec.name} could not be set')
        problems.append(f'{spec.name}: its settings could not be set ({e}): check them in the Dashboard')
    return problems


def missing_plugin(spec: ModuleSpec) -> Optional[str]:
    """Why the module of ``spec`` cannot be made when its plugin is not installed (None if it is)"""
    from pymodaq.control_modules.instruments import ACTUATOR_NAMES, DET_TYPES
    names = ACTUATOR_NAMES if spec.is_actuator else [det['name'] for det in DET_TYPES[spec.dim]]
    if spec.plugin in names:
        return None
    hint = PLUGIN_HINTS.get(spec.plugin)
    return (f'{spec.name}: the plugin {spec.plugin} is not installed' + (f' ({hint})' if hint else '') +
            ', so its module could not be made: install it and start the command again')


# ----------------------------------------------------------------------------------------------------------------------
# the application
# ----------------------------------------------------------------------------------------------------------------------

def guarded(method):
    """A slot which logs what it raises and tells the user: an exception which escapes from a slot aborts a Qt
    application"""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception as e:
            logger.exception(f'{method.__name__} failed')
            self.fail(f'{method.__name__.strip("_").replace("_", " ")} failed: {e}')
    return wrapper


class Launcher:
    """A command: the Dashboard, the modules of an application and its extension

    :meth:`start` opens the Dashboard and goes on by itself, in the event loop: the instruments are looked for in a
    thread (the Dashboard stays alive), the modules are made and set up, the extension is opened, the recognized modules
    are connected and the report is shown. :attr:`state` says where it is: ``created``, ``detecting``, ``creating``,
    ``ready`` (all done: the user takes over), ``failed`` or ``closed``.

    Parameters
    ----------
    configuration: Configuration
        The application
    options: Options
        What the user asked
    detector: callable, optional
        ``detector(configuration, options)`` gives the :class:`.detect.Detections`, :func:`.detect.detect_all` by
        default (replaced in tests)
    detection_timeout: float, optional
        The time (s) after which the looking is given up (a driver which hangs): the application goes on as if nothing
        was found, and says so

    Attributes
    ----------
    dashboard, shared_ui, extension:
        The Dashboard, its window and the extension, once they exist
    plan: Plan or None
        What was decided
    detections: Detections or None
        What was found
    problems: list of str
        What went wrong while the modules were made (a plugin which is not installed...)
    absent: list of str
        The names of the modules which could not be made
    report: str
        What the user was told
    """

    def __init__(self, configuration: Configuration, options: Options, detector=None,
                 detection_timeout: float = DETECTION_TIMEOUT):
        self.configuration = configuration
        self.options = options
        self._detector = detector
        self._detection_timeout = detection_timeout
        self._detection_started = 0.
        self._detection_given_up = False
        self.app = None
        self.dashboard = None
        self.shared_ui = None
        self.extension = None
        self.plan: Optional[Plan] = None
        self.detections: Optional[Detections] = None
        self.problems: list[str] = []
        self.absent: list[str] = []
        self.report = ''
        self.state = 'created'
        self._thread: Optional[threading.Thread] = None
        self._timer: Optional[QtCore.QTimer] = None
        self._loader = None

    # ----- the steps ------------------------------------------------------------------------------------------

    def start(self, app=None) -> None:
        """Open the Dashboard and begin to look for the instruments"""
        from pymodaq_gui.qt_utils import mkQApp
        from pymodaq.dashboard import create_load_dashboard  # (it registers the extensions, which takes a moment)

        self.app = app or mkQApp('Dashboard')
        self.shared_ui, self.dashboard = create_load_dashboard(show_dashboard=True)
        self.app.aboutToQuit.connect(self.close)  # (otherwise Qt aborts: "QThread: Destroyed while ... running")
        self.state = 'detecting'
        if self.options.mock or not self.options.detect:
            self.detections = Detections()
            QtCore.QTimer.singleShot(0, self._detected)  # (the window is shown first)
            return
        self._status('Looking for the instruments...')
        self._detection_started = time.monotonic()
        self._thread = threading.Thread(target=self._detect, name='detect-instruments', daemon=True)
        self._thread.start()
        self._timer = QtCore.QTimer()
        self._timer.setInterval(POLL_INTERVAL)
        self._timer.timeout.connect(self._poll)
        self._timer.start()

    def _detect(self):
        """What the thread does (it must not touch the GUI)"""
        try:
            detections = (self._detector or detect.detect_all)(self.configuration, self.options)
        except Exception as e:  # (detect_all does not raise: this is for a detector which does)
            logger.exception('Looking for the instruments failed')
            detections = Detections(notes=[f'The instruments could not be looked for: {e}'])
        if not self._detection_given_up:  # (too late: the application went on without it)
            self.detections = detections

    @guarded
    def _poll(self):
        if self._thread is not None and self._thread.is_alive():
            if time.monotonic() - self._detection_started < self._detection_timeout:
                return
            self._detection_given_up = True
            logger.error(f'Looking for the instruments took more than {self._detection_timeout:g} s: given up')
            self.detections = Detections(notes=[
                f'Looking for the instruments took more than {self._detection_timeout:g} s (a driver does not answer): '
                'it was given up, nothing was recognized. Select the instruments by hand in the Dashboard.'])
        self._timer.stop()
        self._detected()

    @guarded
    def _detected(self):
        """What is connected is known: decide, and make the modules"""
        if self.state == 'closed':
            return
        from pymodaq.control_modules.enums import DAQTypesEnum
        from pymodaq.utils.managers.modules import ModuleType
        from pymodaq.utils.managers.modules.loader import ModuleLoader, PluginInfo

        self.plan = detect.plan(self.configuration, self.detections or Detections(), self.options)
        self.state = 'creating'
        manager = self.dashboard.modules_manager
        taken = {module.id for module in manager.modules_all}
        ids = iter(random.sample([number for number in range(1, 1000) if number not in taken], len(self.plan.modules)))
        infos = []
        for spec in self.plan.modules:
            problem = missing_plugin(spec)
            if problem is not None:
                self.problems.append(problem)
                continue
            infos.append(PluginInfo(
                id=next(ids), name=spec.name, class_name=spec.plugin,
                type=ModuleType.Actuator if spec.is_actuator else ModuleType.Detector, is_master=True, do_init=False,
                ui=None, daq_type=None if spec.dim is None else DAQTypesEnum[spec.dim], axis_name=spec.axis_name))
        self._status('Creating the modules...')
        self._loader = ModuleLoader(self.dashboard.module_creator, [[info] for info in infos])
        self._loader.all_instruments_added.connect(manager.add_modules)
        self._loader.all_instruments_added.connect(self._modules_created)
        self._loader.load_failed.connect(self.fail)
        self._loader.start()

    @guarded
    def _modules_created(self, modules: list):
        """The modules exist (not connected): set them up, open the extension, connect what was recognized"""
        from pymodaq.utils.managers.modules import ModuleType
        if self.state == 'closed':
            return
        manager = self.dashboard.modules_manager
        made = {}
        for spec in self.plan.modules:
            if spec.name in manager.modules_name:  # (not if its plugin is not installed)
                made[spec.name] = manager.get_mod_from_name(spec.name, mod=ModuleType.Control)
                self.problems.extend(apply_settings(made[spec.name], spec))
        self.absent = [spec.name for spec in self.plan.modules if spec.name not in made]
        self._load_extension()
        if self.extension is not None:
            self.extension.initialize_modules([made[spec.name] for spec in self.plan.to_connect if spec.name in made])
        self._tell()
        self.state = 'ready'

    def _load_extension(self):
        from pymodaq.extensions import ExtensionEnum
        key = self.configuration.class_name.upper()
        if key not in ExtensionEnum.names():
            self.problems.append(f'The extension {self.configuration.class_name} of PyMoDAQ was not found: is '
                                 f'pymodaq_plugins_thermospectro installed (python lab/install_lab.py)?')
            return
        self.extension = self.dashboard.load_extension(ExtensionEnum[key])

    def _tell(self):
        """Tell the user what was recognized and what was not: in the extension, in the log and on the console"""
        self.report = self.plan.report(self.problems, self.absent)
        print(self.report, flush=True)
        logger.info(self.report)
        if self.extension is not None:
            self.extension.show_message(self.plan.title, self.report, error=bool(self.problems))
        else:
            self._status(self.problems[-1] if self.problems else self.plan.title)

    def _status(self, text: str):
        try:
            self.dashboard.update_status(text)
        except Exception:
            logger.debug(f'Status: {text}')

    def fail(self, text: Any) -> None:
        """Something which cannot be gone on from happened: say it (the Dashboard stays open)"""
        self.problems.append(str(text))
        self.state = 'failed'
        print(f'{self.configuration.command}: {text}', file=sys.stderr, flush=True)
        self._status(str(text))

    # ----- the end --------------------------------------------------------------------------------------------

    def close(self):
        """Close the modules and the extension (it is what the end of the application does)"""
        if self.state == 'closed':
            return
        self.state = 'closed'
        if self._timer is not None:
            self._timer.stop()
        if self.app is not None:
            try:
                self.app.aboutToQuit.disconnect(self.close)
            except (TypeError, RuntimeError):
                pass
        if self.dashboard is not None:
            try:
                self.dashboard.quit_fun()
            except Exception:
                logger.exception('The Dashboard could not be closed properly')


# ----------------------------------------------------------------------------------------------------------------------
# the commands
# ----------------------------------------------------------------------------------------------------------------------

def main(command: str, argv: Optional[Sequence[str]] = None) -> int:
    """Run a command: read its command line, open the Dashboard and the application, and wait for the user

    Parameters
    ----------
    command: str
        ``thermo_2spectro``, ``thermo_spectro``, ``camera``, ``thermo_camera`` or ``thermo_camera_spectro``
    argv: sequence of str, optional
        The command line without the name of the command (``sys.argv[1:]`` by default)

    Returns
    -------
    int
        The exit code of the application
    """
    if command not in CONFIGURATIONS:
        raise ValueError(f'No command {command}, they are: {", ".join(CONFIGURATIONS)}')
    options = parse_options(command, sys.argv[1:] if argv is None else argv)
    sys.argv = sys.argv[:1]  # (PyMoDAQ reads the command line too)
    launcher = Launcher(CONFIGURATIONS[command], options)
    launcher.start()
    return launcher.app.exec()


def thermo_2spectro(argv: Optional[Sequence[str]] = None) -> int:
    """Two spectrometers, each with a thermal controller"""
    return main('thermo_2spectro', argv)


def thermo_spectro(argv: Optional[Sequence[str]] = None) -> int:
    """A spectrometer with a thermal controller"""
    return main('thermo_spectro', argv)


def camera(argv: Optional[Sequence[str]] = None) -> int:
    """A camera"""
    return main('camera', argv)


def thermo_camera(argv: Optional[Sequence[str]] = None) -> int:
    """A camera with a thermal controller"""
    return main('thermo_camera', argv)


def thermo_camera_spectro(argv: Optional[Sequence[str]] = None) -> int:
    """A camera and a spectrometer with a thermal controller"""
    return main('thermo_camera_spectro', argv)
