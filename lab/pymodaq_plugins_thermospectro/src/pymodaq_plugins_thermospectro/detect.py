"""What is connected to the computer, and what the modules of an application are made of

The commands of the lab (``thermo_camera_spectro``...) look at what is connected before they start, recognize what they
can and leave the rest to the user. This module does the looking and the deciding, without Qt and without PyMoDAQ (they
are imported by the functions which need them, so that a missing driver is a note and not a crash):

* :func:`detect_spectrometers`: the Ocean Insight spectrometers (python-seabreeze, the backend of the lab first), sorted
  by serial number;
* :func:`detect_cameras`: the Thorlabs scientific cameras (``Thorlabs_TSI``) and the uc480 / uEye cameras (``UC480``);
* :func:`detect_thermostats`: the Quantum Northwest controllers, on the serial ports of an FTDI USB-serial converter
  (nothing is ever sent to another port: other instruments of the lab may be on it; a controller on another kind of
  port is selected by hand, or given with ``--port``);
* :func:`plan`: from what was found and from the options of the command, which module of which plugin is made for each
  instrument of an application (see :mod:`.configurations`), with which settings, and what the user has to do for the
  instruments which were not recognized. It is pure: no instrument is touched.

Every function which looks at something is fault tolerant: it never raises. What it could not do (a driver which is
not installed, a port which another program holds) is a human readable note.

Two things to know about looking: the pyseabreeze backend opens the USB devices of Ocean Insight to read their serial
number (that can reset a spectrometer which another program, OceanView for instance, is using: close it first), and
the serial port of a controller is opened exclusively on Linux and macOS, which only keeps out the programs which do the
same (an advisory lock): do not run two programs on one controller.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, NamedTuple, Optional, Sequence

from pymodaq_plugins_thermospectro.configurations import Configuration

#: the backends of python-seabreeze, in the order they are tried (the first is the default of the Seabreeze plugin)
BACKENDS = ('pyseabreeze', 'cseabreeze')
#: the plugins of the cameras which are recognized (what pylablib lists), the first one is the default
CAMERA_PLUGINS = ('Thorlabs_TSI', 'UC480')
SPECTROMETER_PLUGIN = 'Seabreeze'
THERMOSTAT_PLUGIN = 'QNW_TC'  # the only thermal controller which is recognized
MOCK = 'Mock'
#: the axis of the actuator which is the temperature, for the plugins which have several
AXIS_NAMES = {THERMOSTAT_PLUGIN: 'Temperature', MOCK: 'Temp'}
FTDI_VENDOR = 0x0403  # the vendor of the USB-serial converters of the Quantum Northwest controllers
KIND_DETECTORS = {'spectrometer': 'DAQ1D', 'camera': 'DAQ2D'}  # the dimension of the detector of each kind


class Spectrometer(NamedTuple):
    """A spectrometer which is connected"""
    serial: str
    model: str
    backend: str = BACKENDS[0]  # the python-seabreeze backend which sees it
    device: str = ''  # how the Device list of the Seabreeze plugin writes it (<SeaBreezeDevice FLAMES:FLMS12345>)


class Camera(NamedTuple):
    """A camera which is connected: the plugin which drives it, its serial number and a few words about it"""
    plugin: str
    serial: str
    label: str


class Thermostat(NamedTuple):
    """A thermal controller which answered: the serial port and the version of its firmware"""
    port: str
    firmware: str


@dataclass
class Detections:
    """What was found, and what could not be done to find it"""
    spectrometers: list[Spectrometer] = field(default_factory=list)
    cameras: list[Camera] = field(default_factory=list)
    thermostats: list[Thermostat] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # for the user: a driver which is missing, a port which is held...


@dataclass(frozen=True)
class Options:
    """What the user asked on the command line

    Parameters
    ----------
    mock: bool
        Every instrument is a Mock module and nothing is looked for (``--mock``)
    camera_plugin: str or None
        The plugin of the camera (``--camera-plugin``): the one which found a camera by default, else Thorlabs_TSI
    thermostat_plugin: str
        The plugin of the thermal controllers (``--thermostat-plugin``): any actuator whose position is a temperature
    ports: tuple of str
        The ports of the thermal controllers, in order (``--port``): they are not looked for then
    detect: bool
        False for ``--no-detect``: the modules are made and nothing is connected
    """
    mock: bool = False
    camera_plugin: Optional[str] = None
    thermostat_plugin: str = THERMOSTAT_PLUGIN
    ports: tuple = ()
    detect: bool = True


# ----------------------------------------------------------------------------------------------------------------------
# looking
# ----------------------------------------------------------------------------------------------------------------------

def _why(error: BaseException) -> str:
    """What an exception says, on one line"""
    text = ' '.join(str(error).split()) or type(error).__name__
    return text if len(text) <= 300 else text[:297] + '...'


def _text(value: Any) -> str:
    return '' if value is None else str(value).strip()


def natural_key(text: str) -> list:
    """To sort ports the way a person does: COM2 before COM10, /dev/ttyUSB2 before /dev/ttyUSB10"""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r'(\d+)', str(text))]


def _spectrometer_of(device: Any, backend: str) -> Spectrometer:
    return Spectrometer(_text(getattr(device, 'serial_number', None)), _text(getattr(device, 'model', None)), backend,
                        str(device))


def detect_spectrometers(notes: Optional[list] = None,
                         list_devices: Optional[Callable[[str], Sequence]] = None) -> list[Spectrometer]:
    """The spectrometers which are connected, sorted by serial number (never raises)

    The pyseabreeze backend looks first, the cseabreeze one if the first finds nothing or cannot work.

    Parameters
    ----------
    notes: list of str, optional
        What could not be done is added to it
    list_devices: callable, optional
        Lists the devices (with a ``serial_number`` and a ``model``) which a backend sees, for the tests. By default
        the one of the plugin ``pymodaq_plugins_oceaninsight``, which opens the USB devices one at a time
    """
    notes = [] if notes is None else notes
    if list_devices is None:
        try:
            from pymodaq_plugins_oceaninsight.hardware.seabreeze_utils import list_devices
        except Exception as e:
            notes.append(f'Spectrometers: the plugin pymodaq_plugins_oceaninsight cannot be loaded ({_why(e)}).')
            return []
    reasons = []
    for backend in BACKENDS:
        try:
            devices = list(list_devices(backend))
        except Exception as e:  # (no seabreeze, no pyusb, no libusb, a USB bus which cannot be read...)
            reasons.append(f'the {backend} backend cannot be used: {_why(e)}')
            continue
        if not devices:
            reasons.append(f'the {backend} backend sees none')
            continue
        found = sorted((_spectrometer_of(device, backend) for device in devices),
                       key=lambda spectrometer: (spectrometer.serial == '', spectrometer.serial))
        if reasons:
            notes.append(f'Spectrometers: found with the {backend} backend ({"; ".join(reasons)}).')
        return found
    notes.append(f'Spectrometers: none found ({"; ".join(reasons)}).')
    return []


def _list_thorlabs_tsi() -> Sequence:
    from pylablib.devices import Thorlabs
    return Thorlabs.list_cameras_tlcam()


def _list_uc480() -> Sequence:
    from pylablib.devices import uc480
    return uc480.list_cameras()


def _camera_of(plugin: str, item: Any, notes: list) -> Camera:
    """The camera which an item of the list of pylablib stands for (a serial number, or a record with more)"""
    if isinstance(item, (str, int)):
        serial, model = _text(item), ''
    else:
        serial = next((_text(getattr(item, name, None)) for name in ('serial_number', 'serial', 'cam_id', 'id')
                       if _text(getattr(item, name, None))), '')
        if not serial and isinstance(item, (tuple, list)) and item:
            serial = _text(item[0])
        model = _text(getattr(item, 'model', None))
    family = 'Thorlabs scientific camera' if plugin == 'Thorlabs_TSI' else 'camera'
    label = f'{model} {serial}'.strip() if model else f'{family} {serial}'
    if getattr(item, 'in_use', False):
        label += ' (in use by another program)'
        notes.append(f'Cameras: the camera {serial} is in use by another program: close it first.')
    return Camera(plugin, serial, label)


def detect_cameras(notes: Optional[list] = None, tsi: Optional[Callable[[], Sequence]] = None,
                   uc480: Optional[Callable[[], Sequence]] = None, plugin: Optional[str] = None) -> list[Camera]:
    """The cameras which are connected: Thorlabs scientific cameras (Zelux, Kiralux...) then uc480 / uEye ones (never
    raises)

    Each family is looked for on its own: a driver which is missing for one does not hide the cameras of the other (and
    it is only told when no camera at all is found). The cameras of a family are in the order of the driver, which is
    the order the plugin numbers them in.

    Parameters
    ----------
    notes: list of str, optional
        What could not be done is added to it
    tsi, uc480: callable, optional
        List what the driver of each family sees (serial numbers, or records with a ``serial_number``), to replace
        what ``pylablib`` says in tests
    plugin: str, optional
        Look for the cameras of this plugin only (``--camera-plugin``): the driver of the other family is not asked
    """
    notes = [] if notes is None else notes
    cameras, problems = [], []
    for family_plugin, family, lister in (('Thorlabs_TSI', 'Thorlabs scientific cameras', tsi or _list_thorlabs_tsi),
                                          ('UC480', 'uc480 / uEye cameras', uc480 or _list_uc480)):
        if plugin in CAMERA_PLUGINS and plugin != family_plugin:
            continue
        try:
            items = list(lister())
        except ImportError as e:
            problems.append(f'Cameras: the {family} cannot be looked for, pylablib is not installed '
                            f'(python -m pip install pylablib): {_why(e)}')
        except OSError as e:  # (the DLL of the driver is not there: ThorCam, the uc480 / uEye driver)
            problems.append(f'Cameras: the driver of the {family} cannot be loaded ({_why(e)}).')
        except Exception as e:
            problems.append(f'Cameras: the {family} cannot be listed ({type(e).__name__}: {_why(e)}).')
        else:
            cameras.extend(_camera_of(family_plugin, item, notes) for item in items)
    if not cameras:
        notes.extend(problems or ['Cameras: none found.'])
    return cameras


def looks_like_ftdi(port: Any) -> bool:
    """Whether a serial port is an FTDI USB-serial converter, which is what the Quantum Northwest controllers are
    connected through: its vendor is FTDI (0x0403), or FTDI is in what the system says about it

    The generic *USB Serial* of a description is not enough: any adapter has it (Arduinos, GPS, other instruments).
    """
    if getattr(port, 'vid', None) == FTDI_VENDOR:
        return True
    text = ' '.join(_text(getattr(port, name, None)) for name in ('description', 'manufacturer', 'product')).lower()
    return 'ftdi' in text


def _device_of(port: Any) -> str:
    return port if isinstance(port, str) else _text(getattr(port, 'device', None))


def _describe_port(port: Any) -> str:
    device = _device_of(port)
    description = '' if isinstance(port, str) else _text(getattr(port, 'description', None))
    return f'{device} ({description})' if description and description.lower() != 'n/a' and description != device \
        else device


def _serial_ports(notes: list) -> list:
    try:
        from serial.tools import list_ports
        return list(list_ports.comports())
    except ImportError:
        notes.append('Thermal controllers: pyserial is not installed (python -m pip install pyserial), the serial '
                     'ports cannot be listed.')
    except Exception as e:
        notes.append(f'Thermal controllers: the serial ports cannot be listed ({_why(e)}).')
    return []


def detect_thermostats(notes: Optional[list] = None, ports: Optional[Sequence] = None,
                       probe: Optional[Callable[..., Optional[str]]] = None) -> list[Thermostat]:
    """The Quantum Northwest controllers which answer on the serial ports (never raises)

    Only the ports of an FTDI USB-serial converter are asked (see :func:`looks_like_ftdi`): the other
    ports may have other instruments, which are none of our business. They are asked one at a time, in the order a
    person sorts them (COM2 before COM10), one question each (see ``probe_port`` of the QNW plugin). A port which
    another program holds is skipped, with a note.

    Parameters
    ----------
    notes: list of str, optional
        What could not be done, and the ports there are when nothing is found, are added to it
    ports: sequence, optional
        The serial ports (objects with ``device``, ``vid``, ``description``, ``manufacturer``, as the ones of
        ``serial.tools.list_ports.comports()``), to replace them in tests
    probe: callable, optional
        ``probe(port, problems=list)`` which says the firmware of the controller on a port, None if there is none and
        adds to ``problems`` why it could not use the port, to replace ``probe_port`` in tests
    """
    notes = [] if notes is None else notes
    ports = _serial_ports(notes) if ports is None else list(ports)
    if probe is None:
        try:
            from pymodaq_plugins_quantumnorthwest.hardware import qnw_tc
            probe = qnw_tc.probe_port
        except Exception as e:
            notes.append(f'Thermal controllers: the plugin pymodaq_plugins_quantumnorthwest cannot be loaded '
                         f'({_why(e)}).')
            return []
    ports = sorted((port for port in ports if _device_of(port)), key=lambda port: natural_key(_device_of(port)))
    candidates = [port for port in ports if looks_like_ftdi(port)]
    found, asked = [], []
    for port in candidates:
        device = _device_of(port)
        problems: list[str] = []
        try:
            firmware = probe(device, problems=problems)
        except Exception as e:  # (probe_port does not raise, a replacement may)
            firmware = None
            problems.append(f'{device} cannot be used: {_why(e)}')
        for problem in problems:
            notes.append(f'Thermal controllers: {problem} (another program may hold it): it was skipped.')
        if firmware:
            found.append(Thermostat(device, str(firmware)))
        elif not problems:
            asked.append(device)
    if not found:
        if not ports:
            notes.append('Thermal controllers: this computer has no serial port.')
        else:
            listing = ', '.join(_describe_port(port) for port in ports)
            if not candidates:
                notes.append(f'Thermal controllers: none of the serial ports is an FTDI USB-serial converter '
                             f'({listing}): none was asked. A controller on another kind of port is selected by hand '
                             f'(or given with --port).')
            elif asked:
                notes.append(f'Thermal controllers: nothing answered like a controller on {", ".join(asked)}. The '
                             f'serial ports of this computer are {listing}.')
    return found


def detect_all(configuration: Configuration, options: Optional[Options] = None) -> Detections:
    """Look for the instruments an application uses, and only those (never raises)

    Nothing is looked for with ``--mock`` or ``--no-detect``, and the thermal controllers are not looked for when their
    ports are given or when their plugin is not the one of Quantum Northwest (it is the only one which can be
    recognized).
    """
    options = options or Options()
    found = Detections()
    if options.mock or not options.detect:
        return found
    wanted = [(configuration.spectrometers > 0, detect_spectrometers, 'spectrometers', {}),
              (configuration.camera and options.camera_plugin != MOCK, detect_cameras, 'cameras',
               {'plugin': options.camera_plugin}),
              (configuration.thermostats > 0 and not options.ports and options.thermostat_plugin == THERMOSTAT_PLUGIN,
               detect_thermostats, 'thermostats', {})]
    for needed, function, attribute, arguments in wanted:
        if not needed:
            continue
        try:
            setattr(found, attribute, function(notes=found.notes, **arguments))
        except Exception as e:  # (the functions do not raise: this is for the day one does)
            found.notes.append(f'The instruments of the kind {attribute} could not be looked for ({_why(e)}).')
    return found


# ----------------------------------------------------------------------------------------------------------------------
# deciding
# ----------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ModuleSpec:
    """One control module of the Dashboard: what to create, how to set it up and whether to connect it

    Parameters
    ----------
    name: str
        The name of the module (``Thermo1``, ``Spectro2``, ``Camera1``): the units of the extension find it by this
    kind: str
        ``thermostat``, ``spectrometer`` or ``camera``
    plugin: str
        The plugin of the module: ``QNW_TC``, ``Seabreeze``, ``Thorlabs_TSI``, ``UC480``, ``Mock``...
    dim: str or None
        ``DAQ1D`` (spectrometer) or ``DAQ2D`` (camera) for a detector, None for an actuator (the thermostat)
    axis_name: str or None
        The axis of the actuator which is the temperature
    settings: dict
        What is set in the module before it is connected, under a name the launcher knows how to apply to the plugin:
        ``port`` (thermostat), ``backend``, ``device`` and ``serial`` (spectrometer), ``serial`` and ``index`` (camera,
        the rank among the cameras of its plugin)
    recognized: bool
        Whether the instrument is known: what it is and where (found, given on the command line, or a Mock module).
        The module is connected when it is, left to the user when it is not
    identity: str
        What tells the instrument apart, to show: the serial number, the port, Mock
    found: str
        What it is, when it is known
    instruction: str
        What the user has to do, when it is not known
    """
    name: str
    kind: str
    plugin: str
    dim: Optional[str] = None
    axis_name: Optional[str] = None
    settings: dict = field(default_factory=dict)
    recognized: bool = False
    identity: str = ''
    found: str = ''
    instruction: str = ''

    @property
    def is_actuator(self) -> bool:
        return self.kind == 'thermostat'


@dataclass
class Plan:
    """The modules of an application, and what to tell the user

    Attributes
    ----------
    configuration: Configuration
    modules: list of ModuleSpec
        In the order they are created: the thermal controllers, the spectrometers, the camera
    pairs: list of str
        Which instrument goes with which thermal controller (``Spectro1 (serial X) <-> Thermo1 (COM5)``)
    notes: list of str
        What the user should know besides: what could not be looked for, what was found and is not used
    connect: bool
        Whether the recognized modules are connected (False with ``--no-detect``)
    """
    configuration: Configuration
    modules: list = field(default_factory=list)
    pairs: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    connect: bool = True

    def module(self, name: str) -> Optional[ModuleSpec]:
        return next((spec for spec in self.modules if spec.name == name), None)

    @property
    def to_connect(self) -> list:
        """The modules to connect: the ones which were recognized"""
        return [spec for spec in self.modules if spec.recognized] if self.connect else []

    @property
    def not_recognized(self) -> list:
        return [spec for spec in self.modules if not spec.recognized]

    @property
    def title(self) -> str:
        return f'{self.configuration.command}: the instruments'

    def report(self, problems: Sequence[str] = (), absent: Sequence[str] = ()) -> str:
        """What was recognized (name, serial number or port) and what was not, with what to do about it

        Parameters
        ----------
        problems: sequence of str, optional
            What went wrong when the modules were made (a plugin which is not installed...), told last
        absent: sequence of str, optional
            The names of the modules which could not be made: they are not said to be in the Dashboard (their problem
            is told instead)
        """
        recognized = [spec for spec in self.modules if spec.recognized]
        present = [spec for spec in self.modules if spec.name not in absent]
        lines = [f'{self.configuration.command}: {len(recognized)} of {len(self.modules)} instruments recognized.']
        known = [spec for spec in present if spec.recognized]
        if known:
            lines += ['', 'Recognized' + ('' if self.connect else ' (not connected: --no-detect)') + ':']
            lines += [f'  {spec.name}: {spec.found}' for spec in known]
        unknown = [spec for spec in present if not spec.recognized]
        if unknown:
            lines += ['', 'Not recognized (their module is in the Dashboard, not connected):']
            lines += [f'  {spec.instruction}' for spec in unknown]
        if self.pairs:
            lines += ['', 'Pairs: ' + '; '.join(self.pairs)]
        if self.notes:
            lines += [''] + list(self.notes)
        if problems:
            lines += ['', 'Problems:'] + [f'  {problem}' for problem in problems]
        lines += ['', 'Nothing was started: press Live (or Start) when the instruments are ready.']
        return '\n'.join(lines)


def port_example() -> str:
    """A serial port as it is written on this computer, to show how to fill a setting"""
    return 'COM6' if sys.platform.startswith('win') else '/dev/ttyUSB0'


def _ini(name: str) -> str:
    return (f'click the Ini button of the module {name} in the Dashboard (or Connect the modules in the extension), '
            f'then press Live (or Start)')


def _plan_thermostat(name: str, index: int, options: Options, detections: Detections) -> ModuleSpec:
    plugin = options.thermostat_plugin or THERMOSTAT_PLUGIN
    axis = AXIS_NAMES.get(plugin)
    if options.mock or plugin == MOCK:
        return ModuleSpec(name, 'thermostat', MOCK, axis_name=AXIS_NAMES[MOCK], recognized=True, identity=MOCK,
                          found='simulated thermal controller (Mock)')
    port = options.ports[index] if index < len(options.ports) else None
    if port is not None:
        return ModuleSpec(name, 'thermostat', plugin, axis_name=axis, settings={'port': port}, recognized=True,
                          identity=port, found=f'{plugin} on {port} (given on the command line)')
    if options.detect and plugin == THERMOSTAT_PLUGIN and not options.ports and index < len(detections.thermostats):
        thermostat = detections.thermostats[index]
        return ModuleSpec(name, 'thermostat', plugin, axis_name=axis, settings={'port': thermostat.port},
                          recognized=True, identity=thermostat.port,
                          found=f'Quantum Northwest controller on {thermostat.port} (firmware {thermostat.firmware})')
    if not options.detect:
        why = 'nothing was looked for (--no-detect)'
    elif plugin != THERMOSTAT_PLUGIN:
        why = f'the controllers of the plugin {plugin} cannot be recognized'
    elif options.ports:
        why = 'it was not looked for (the ports are given with --port, and there is none for it)'
    else:
        why = 'no thermal controller was recognized'
    return ModuleSpec(name, 'thermostat', plugin, axis_name=axis,
                      instruction=f'{name}: {why}. Plug the controller in and select its port in the settings of the '
                                  f'module {name} in the Dashboard (for instance {port_example()}), {_ini(name)}.')


def _plan_spectrometer(name: str, index: int, options: Options, detections: Detections) -> ModuleSpec:
    dim = KIND_DETECTORS['spectrometer']
    if options.mock:
        return ModuleSpec(name, 'spectrometer', MOCK, dim, recognized=True, identity=MOCK,
                          found='simulated spectrometer (Mock)')
    if options.detect and index < len(detections.spectrometers):
        spectrometer = detections.spectrometers[index]
        settings = {'backend': spectrometer.backend, 'serial': spectrometer.serial}
        if spectrometer.device:
            settings['device'] = spectrometer.device
        return ModuleSpec(name, 'spectrometer', SPECTROMETER_PLUGIN, dim, settings=settings, recognized=True,
                          identity=spectrometer.serial,
                          found=f'Ocean Insight {spectrometer.model or "spectrometer"}, serial number '
                                f'{spectrometer.serial or "unknown"} ({spectrometer.backend} backend)')
    why = 'no spectrometer was recognized' if options.detect else 'nothing was looked for (--no-detect)'
    return ModuleSpec(name, 'spectrometer', SPECTROMETER_PLUGIN, dim,
                      instruction=f'{name}: {why}. Connect the spectrometer (USB), {_ini(name)}.')


def _plan_camera(name: str, options: Options, detections: Detections, notes: list) -> ModuleSpec:
    dim = KIND_DETECTORS['camera']
    wanted = options.camera_plugin
    if options.mock or wanted == MOCK:
        return ModuleSpec(name, 'camera', MOCK, dim, recognized=True, identity=MOCK, found='simulated camera (Mock)')
    candidates = [camera for camera in detections.cameras if wanted is None or camera.plugin == wanted] \
        if options.detect else []
    if candidates:
        camera = candidates[0]
        rank = [other for other in detections.cameras if other.plugin == camera.plugin].index(camera)
        if len(candidates) > 1:
            others = ', '.join(f'{other.label} ({other.plugin})' for other in candidates[1:])
            notes.append(f'Several cameras were found: {name} is {camera.label}, the others are not used ({others}).')
        return ModuleSpec(name, 'camera', camera.plugin, dim, settings={'serial': camera.serial, 'index': rank},
                          recognized=True, identity=camera.serial, found=f'{camera.label} (plugin {camera.plugin})')
    plugin = wanted or CAMERA_PLUGINS[0]
    if not options.detect:
        why = 'nothing was looked for (--no-detect)'
    elif wanted is not None and wanted not in CAMERA_PLUGINS:
        why = f'the cameras of the plugin {wanted} cannot be recognized'
    else:
        why = 'no camera was recognized'
    hint = '' if wanted else f' (it is made with the plugin {plugin}: --camera-plugin chooses another one)'
    return ModuleSpec(name, 'camera', plugin, dim,
                      instruction=f'{name}: {why}. Connect the camera and select it in the settings of the module '
                                  f'{name} in the Dashboard{hint}, {_ini(name)}.')


def plan(configuration: Configuration, detections: Detections, options: Optional[Options] = None) -> Plan:
    """The modules of an application, from what was found and what the user asked (nothing is touched)

    The instruments are matched in order: the first spectrometer (by serial number) is *Spectro1*, the second one
    *Spectro2*; the first controller (by port) is *Thermo1*... and *Spectro1* goes with *Thermo1*, *Spectro2* with
    *Thermo2*. The camera shares *Thermo1*. What cannot be known (which controller is on which holder) is told.

    Parameters
    ----------
    configuration: Configuration
        The application
    detections: Detections
        What :func:`detect_all` found
    options: Options, optional
        What the user asked on the command line

    Returns
    -------
    Plan
    """
    options = options or Options()
    notes = [] if options.mock else list(detections.notes)
    specs: dict[str, ModuleSpec] = {}
    for index, name in enumerate(configuration.thermostat_names):
        specs[name] = _plan_thermostat(name, index, options, detections)
    for index, name in enumerate(configuration.spectrometer_names):
        specs[name] = _plan_spectrometer(name, index, options, detections)
    if configuration.camera:
        specs[configuration.camera_name] = _plan_camera(configuration.camera_name, options, detections, notes)
    notes.extend(_surplus_notes(configuration, detections, options))

    pairs = []
    for index, name in enumerate(configuration.spectrometer_names):
        if index < configuration.thermostats:
            pairs.append(_pair(specs[name], specs[configuration.thermostat_names[index]]))
    if configuration.camera and configuration.thermostats:
        pairs.append(_pair(specs[configuration.camera_name], specs[configuration.thermostat_names[0]]))
    recognized = [name for name in configuration.thermostat_names if specs[name].recognized]
    if configuration.thermostats == 2 and not options.mock and recognized:
        if len(recognized) == 2:
            notes.append('Nothing says which controller is on which cuvette holder: Thermo1 and Thermo2 follow the '
                         'order of the ports, Spectro1 and Spectro2 the serial numbers. Look at the temperature of '
                         'each unit and swap the ports of Thermo1 and Thermo2 in their settings if they go the wrong '
                         'way.')
        else:
            notes.append(f'Nothing says which cuvette holder {recognized[0]} is on (Spectro1 and Spectro2 follow the '
                         f'serial numbers): look at the temperature of each unit, and select the port of the other '
                         f'controller by hand.')
    return Plan(configuration, [specs[name] for name in configuration.module_names], pairs, notes, options.detect)


def _surplus_notes(configuration: Configuration, detections: Detections, options: Options) -> list[str]:
    """What was found and is not used by this application"""
    notes = []
    if options.mock:
        return notes
    if options.detect:
        found = {'spectrometers': (detections.spectrometers, configuration.spectrometers),
                 'thermal controllers': (detections.thermostats if not options.ports
                                         and options.thermostat_plugin == THERMOSTAT_PLUGIN else [],
                                         configuration.thermostats)}
        for what, (seen, used) in found.items():
            if used and len(seen) > used:
                which = 'the first one' if used == 1 else f'the first {used}'
                notes.append(f'{len(seen)} {what} were found, this application uses {used}: {which} (by serial number '
                             f'or port) {"is" if used == 1 else "are"} used, the others are left alone.')
    if options.camera_plugin and not configuration.camera:
        notes.append(f'--camera-plugin {options.camera_plugin} is ignored: this application has no camera.')
    if len(options.ports) > configuration.thermostats:
        notes.append(f'{len(options.ports)} ports were given, this application has {configuration.thermostats} '
                     f'thermal controller(s): {", ".join(options.ports[configuration.thermostats:])} not used.')
    return notes


def _pair(instrument: ModuleSpec, thermostat: ModuleSpec) -> str:
    def label(spec: ModuleSpec) -> str:
        if not spec.recognized or not spec.identity:
            return f'{spec.name} (not recognized)'
        if spec.kind == 'thermostat' or spec.identity == MOCK:
            return f'{spec.name} ({spec.identity})'
        return f'{spec.name} (serial {spec.identity})'
    return f'{label(instrument)} <-> {label(thermostat)}'
