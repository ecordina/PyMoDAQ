"""The settings of one unit (a spectrometer and the thermostat which controls its temperature)

A *unit* is what the user sets up in the extension: which spectrometer and which thermostat, the integration and
readout times, what is recorded, the ROIs, and the temperature ramp. This module only deals with the parameter tree of
a unit (pyqtgraph ``Parameter``): building it, reading it as plain objects (:class:`~.rois.Roi`,
:class:`~.ramp.RampProgram`...), editing it, and keeping its fields consistent (a ramp is defined by three numbers, the
fourth one is computed and shown read-only). Nothing here talks to a hardware module: see :mod:`.unit`.
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Iterator, Optional, Sequence

from pymodaq_gui.parameter import Parameter

from pymodaq_plugins_thermospectro.ramp import (END_AT_FINAL, END_AT_INITIAL, BackMode, BackSpec, Phase, RampMode,
                                                RampProgram, RampSpec, format_duration, make_program)
from pymodaq_plugins_thermospectro import camera
from pymodaq_plugins_thermospectro.camera import RectRoi, default_rect_rois
from pymodaq_plugins_thermospectro.rois import Roi, default_rois

NONE = 'None'  # nothing selected in a list of modules
SPECTROMETER = 'spectrometer'  # the kinds of unit: a 1D detector...
CAMERA = 'camera'  # ... or a 2D one
ROI_NAME = re.compile(r'^roi\d+$')

#: What a thermal controller does during a run
CONTROL_RAMP = 'Run a ramp'
CONTROL_HOLD = 'Hold at a temperature'
#: What happens when the ramps are over: the controller stays at the initial (bottom) or at the final (top) value and the
#: log goes on until it is stopped, or the log ends (the controller stays at the initial value)
END_INITIAL = 'Stay at the initial value (bottom), go on logging'
END_FINAL = 'Stay at the final value (top), go on logging'
END_LOG = 'End the log'
#: ... and where the program ends in each case (None: the log ends)
END_CHOICES = {END_INITIAL: END_AT_INITIAL, END_FINAL: END_AT_FINAL, END_LOG: None}
#: The fields of a ramp (they are hidden when the controller is held at one temperature)
RAMP_FIELDS = ('mode', 'initial', 'take_current', 'final', 'velocity', 'duration', 'hold_final', 'back_mode',
               'back_velocity', 'back_duration', 'hold_initial', 'n_cycles', 'end_at', 'cycle_duration')

#: The default times of a spectrometer
DEFAULT_INTEGRATION_TIME = 500.0  # ms
DEFAULT_READOUT_TIME = 1.0  # s


def roi_definition(key: str, roi: Roi, units: str = 'nm') -> dict:
    """The parameter of a ROI (its edges are in the units of the axis of the spectrum)"""
    return {'title': roi.name, 'name': key, 'type': 'group', 'removable': True, 'children': [
        {'title': 'Name:', 'name': 'name', 'type': 'str', 'value': roi.name},
        {'title': 'From:', 'name': 'start', 'type': 'float', 'value': float(roi.start), 'suffix': units,
         'tip': 'Lower edge of the window. Drag the edges on the spectrum, or type the wavelength here'},
        {'title': 'To:', 'name': 'stop', 'type': 'float', 'value': float(roi.stop), 'suffix': units},
        {'title': 'Record:', 'name': 'record', 'type': 'bool', 'value': bool(roi.record),
         'tip': 'Save the value of this ROI (the mean of its pixels) at each measurement'},
        {'title': 'Plot:', 'name': 'plot', 'type': 'bool', 'value': bool(roi.plot),
         'tip': 'Show this ROI in the plot (what is recorded does not change)'},
    ]}


def rect_roi_definition(key: str, roi: RectRoi, units: str = camera.UNITS) -> dict:
    """The parameter of the ROI of a camera (its edges are in pixels: x along the columns, y along the rows)"""
    return {'title': roi.name, 'name': key, 'type': 'group', 'removable': True, 'children': [
        {'title': 'Name:', 'name': 'name', 'type': 'str', 'value': roi.name},
        {'title': 'From x:', 'name': 'x0', 'type': 'float', 'value': float(roi.x0), 'suffix': units,
         'tip': 'Left edge of the window. Drag the window on the image, or type the pixel here'},
        {'title': 'To x:', 'name': 'x1', 'type': 'float', 'value': float(roi.x1), 'suffix': units},
        {'title': 'From y:', 'name': 'y0', 'type': 'float', 'value': float(roi.y0), 'suffix': units,
         'tip': 'Top edge of the window (the first row is 0)'},
        {'title': 'To y:', 'name': 'y1', 'type': 'float', 'value': float(roi.y1), 'suffix': units},
        {'title': 'Record:', 'name': 'record', 'type': 'bool', 'value': bool(roi.record),
         'tip': 'Save the value of this ROI (the mean of its pixels) at each measurement'},
        {'title': 'Plot:', 'name': 'plot', 'type': 'bool', 'value': bool(roi.plot),
         'tip': 'Show this ROI in the plot (what is recorded does not change)'},
    ]}


def unit_definition(number: int, rois: Sequence[Roi], enabled: bool = True, kind: str = SPECTROMETER,
                    thermostat: bool = True, control: str = CONTROL_RAMP) -> dict:
    """The parameter tree of a unit

    Parameters
    ----------
    number: int
        1 or 2: the number of the spectrometer (3: the camera)
    rois: sequence of Roi
        The ROIs it starts with (the :class:`~.camera.RectRoi` of a camera)
    enabled: bool
        Whether the unit is used
    kind: str
        ``spectrometer`` (a 1D detector, the default) or ``camera`` (a 2D one)
    thermostat: bool
        False for an application without thermal controller: what is about the temperature is in the tree (the code
        reads it) but hidden
    control: str
        What the thermal controller does by default: :data:`CONTROL_HOLD` or :data:`CONTROL_RAMP`
    """
    definition = _unit_definition(number, rois, enabled, kind, control)
    if not thermostat:
        _hide_thermostat(definition)
    return definition


#: what is about the thermal controller in the tree of a unit (hidden in the applications which have none)
THERMOSTAT_GROUPS = ('thermostat', 'plot_temperature', 'ramp')
THERMOSTAT_RECORD = ('temperature',)
THERMOSTAT_STATUS = ('phase', 'cycle', 'setpoint', 'temperature')


def _hide_thermostat(definition: dict):
    """No thermal controller in this application: its settings are not shown"""
    for child in definition['children']:
        if child['name'] in THERMOSTAT_GROUPS:
            child['visible'] = False
        elif child['name'] == 'record':
            for leaf in child['children']:
                if leaf['name'] in THERMOSTAT_RECORD:
                    leaf['visible'] = False
                    leaf['value'] = False
        elif child['name'] == 'status':
            for leaf in child['children']:
                if leaf['name'] in THERMOSTAT_STATUS:
                    leaf['visible'] = False


def _unit_definition(number: int, rois: Sequence[Roi], enabled: bool, kind: str, control: str) -> dict:
    cam = kind == CAMERA
    what = 'camera' if cam else 'spectrometer'
    return {'title': 'Camera:' if cam else f'Spectrometer {number}:', 'name': f'unit{number}', 'type': 'group',
            'children': [
        {'title': 'Use it:', 'name': 'enabled', 'type': 'bool', 'value': enabled,
         'tip': f'Untick to leave this {what} (and its thermostat) out of the experiment'},
        {'title': 'Camera:' if cam else 'Spectrometer:', 'name': CAMERA if cam else 'spectrometer',
         'type': 'list', 'limits': [NONE], 'value': NONE,
         'tip': 'The detector module (a 2D detector of the Dashboard) giving the images' if cam
         else 'The detector module (a 1D detector of the Dashboard) giving the spectra'},
        {'title': 'Thermostat:', 'name': 'thermostat', 'type': 'list', 'limits': [NONE], 'value': NONE,
         'tip': f'The actuator module whose position is the temperature of this {what}. It can be the one of '
                f'another unit: only one of them then runs the ramp'},
        {'title': 'Exposure time:' if cam else 'Integration time:', 'name': 'integration_time', 'type': 'float',
         'value': DEFAULT_INTEGRATION_TIME, 'min': 0.0, 'suffix': 'ms',
         'tip': f'Set on the {what} when the live view or an experiment starts'},
        {'title': 'Readout time:', 'name': 'readout_time', 'type': 'float', 'value': DEFAULT_READOUT_TIME,
         'min': 0.05, 'suffix': 's',
         'tip': f'The {what} is read out, and a measurement recorded, once per readout time. '
                f'It must be longer than the {"exposure" if cam else "integration"} time (as many times as the '
                f'module of the {what} averages: its Naverage)'},
        {'title': 'Record to disk:', 'name': 'record', 'type': 'group', 'children': [
            {'title': 'The full image:', 'name': 'image', 'type': 'bool', 'value': False,
             'tip': 'Save the whole image (a lot of data: a camera gives megabytes), every "Full image every" seconds'}
            if cam else
            {'title': 'The full spectrum:', 'name': 'spectrum', 'type': 'bool', 'value': True,
             'tip': 'Save the whole spectrum, every "Full spectrum every" seconds'},
            {'title': 'Full image every:', 'name': 'image_interval', 'type': 'float', 'value': 60.0, 'min': 0.0,
             'suffix': 's', 'tip': 'Only if "The full image" is ticked (it is not by default: an image is megabytes, '
                                   'two days of one every minute are gigabytes). The ROIs and the temperature are '
                                   'saved at each measurement, the whole image only this often (0: at each '
                                   'measurement). A long log of a camera needs it large'}
            if cam else
            {'title': 'Full spectrum every:', 'name': 'spectrum_interval', 'type': 'float', 'value': 0.0, 'min': 0.0,
             'suffix': 's', 'tip': 'The ROIs and the temperature are saved at each measurement, the whole spectrum '
                                   'only this often (0: at each measurement)'},
            {'title': 'The temperature:', 'name': 'temperature', 'type': 'bool', 'value': True,
             'tip': 'Save the measured temperature (and the set-point, the cycle and the phase of the ramp) '
                    'at each measurement'},
        ]},
        {'title': 'Plot the temperature:', 'name': 'plot_temperature', 'type': 'bool', 'value': True,
         'tip': 'Show the temperature on the plot of the ROIs, on its own scale (at the right)'},
        {'title': 'ROIs:', 'name': 'rois', 'type': 'group', 'expanded': False,
         'tip': 'The same ROIs as in the ROI manager (the tab next to the settings), which is the easier way to set '
                'them up',
         'children': [
             {'title': 'Add a ROI', 'name': 'add_roi', 'type': 'action',
              'tip': f'Or use the ROI manager, or double click on the {"image" if cam else "spectrum"}. Right click '
                     'on a ROI here to remove it'},
             {'title': 'Reset to the default ROIs', 'name': 'reset_rois', 'type': 'action',
              'tip': f'Replace the ROIs by the {"four" if cam else "six"} default ones. The ROIs are remembered for '
                     'the next experiment as soon as they change'},
         ] + [(rect_roi_definition if cam else roi_definition)(f'roi{index:02d}', roi)
              for index, roi in enumerate(rois)]},
        {'title': 'Thermal controller:', 'name': 'ramp', 'type': 'group', 'children': [
            {'title': 'Drive it:', 'name': 'run', 'type': 'bool', 'value': True,
             'tip': 'Untick to only record this unit (the temperature of its thermostat), without driving the thermostat'},
            {'title': 'Mode:', 'name': 'control', 'type': 'list', 'limits': [CONTROL_HOLD, CONTROL_RAMP],
             'value': control,
             'tip': 'Hold the thermal controller at one temperature, or run a temperature ramp'},
            {'title': 'Temperature:', 'name': 'hold_temperature', 'type': 'float', 'value': 25.0,
             'tip': 'The temperature the thermal controller is set to when the log starts, and held at until it stops'},
            {'title': 'Defined by:', 'name': 'mode', 'type': 'list', 'limits': [mode.value for mode in RampMode],
             'value': RampMode.VELOCITY.value,
             'tip': 'Three of the four numbers define the ramp, the fourth one is computed (and read-only)'},
            {'title': 'Initial value:', 'name': 'initial', 'type': 'float', 'value': 25.0,
             'tip': 'The value the cycles start from and come back to'},
            {'title': 'Use the current one', 'name': 'take_current', 'type': 'action',
             'tip': 'Take the temperature the thermostat has now as the initial value'},
            {'title': 'Final value:', 'name': 'final', 'type': 'float', 'value': 35.0,
             'tip': 'The value reached at the end of the ramp (above or below the initial one)'},
            {'title': 'Velocity:', 'name': 'velocity', 'type': 'float', 'value': 2.0, 'suffix': '/min',
             'tip': 'Speed of the ramp. Negative to cool down when the final value is not given'},
            {'title': 'Duration:', 'name': 'duration', 'type': 'float', 'value': 5.0, 'min': 0.0, 'suffix': 'min',
             'tip': 'Time to go from the initial to the final value'},
            {'title': 'Wait at the final value:', 'name': 'hold_final', 'type': 'float', 'value': 0.0, 'min': 0.0,
             'suffix': 'min'},
            {'title': 'Way back defined by:', 'name': 'back_mode', 'type': 'list',
             'limits': [mode.value for mode in BackMode], 'value': BackMode.SAME.value,
             'tip': 'How the temperature goes back to the initial value after the wait'},
            {'title': 'Way back velocity:', 'name': 'back_velocity', 'type': 'float', 'value': 2.0, 'min': 0.0,
             'suffix': '/min'},
            {'title': 'Way back duration:', 'name': 'back_duration', 'type': 'float', 'value': 5.0, 'min': 0.0,
             'suffix': 'min'},
            {'title': 'Wait at the initial value:', 'name': 'hold_initial', 'type': 'float', 'value': 0.0,
             'min': 0.0, 'suffix': 'min'},
            {'title': 'Number of cycles:', 'name': 'n_cycles', 'type': 'int', 'value': 1, 'min': 0,
             'tip': 'How many times the cycle (ramp, wait, way back, wait) is run. 0: until "End at the top" or '
                    '"End at the bottom" (toolbar) or Stop is pressed'},
            {'title': 'When the ramps are over:', 'name': 'end_at', 'type': 'list',
             'limits': [END_INITIAL, END_FINAL, END_LOG], 'value': END_INITIAL,
             'tip': 'After the last cycle the controller stays at the initial value (the bottom: the end of the last way '
                    'back) or at the final value (the top: the last cycle stops when it is reached, with no way back) '
                    'and the log goes on, with the controller held there, until you press Stop (or the duration of '
                    'the log is over). "End the log": the log ends with the ramps. "End at the top" and "End at the '
                    'bottom" in the toolbar do the same at any time, Stop brings the ramps back to the initial value '
                    'and ends the log'},
            {'title': 'One cycle lasts:', 'name': 'cycle_duration', 'type': 'str', 'value': '', 'readonly': True},
        ]},
        {'title': 'Status:', 'name': 'status', 'type': 'group', 'children': [
            {'title': 'Phase:', 'name': 'phase', 'type': 'str', 'value': Phase.IDLE.value, 'readonly': True},
            {'title': 'Cycle:', 'name': 'cycle', 'type': 'int', 'value': 0, 'readonly': True},
            {'title': 'Set-point:', 'name': 'setpoint', 'type': 'float', 'value': 0.0, 'readonly': True},
            {'title': 'Temperature:', 'name': 'temperature', 'type': 'float', 'value': 0.0, 'readonly': True},
            {'title': 'Measurements:', 'name': 'measurements', 'type': 'int', 'value': 0, 'readonly': True},
        ]},
    ]}


def leaves(param: Parameter) -> Iterator[Parameter]:
    """The parameters of a tree which have no child"""
    for child in param.children():
        if child.hasChildren():
            yield from leaves(child)
        else:
            yield child


class UnitSettings:
    """Reads and edits the parameter tree of a unit

    Parameters
    ----------
    param: Parameter
        The group of the unit, as built by :func:`unit_definition`
    number: int
        The number of the spectrometer (1, 2)
    """

    #: The kind of unit, the name of the setting holding the detector module, and the one of the box saving the data
    KIND = SPECTROMETER
    DETECTOR = 'spectrometer'
    FULL_DATA = 'spectrum'
    #: The fields of a ROI which are its edges
    EDGES = ('start', 'stop')

    def __init__(self, param: Parameter, number: int):
        self.param = param
        self.number = number
        self.axis_units = 'nm'  # what the edges of the ROIs are in: the units of the axis of the spectrum
        self._busy = 0
        self._known_rois: dict[str, Parameter] = {}
        self.refresh_known_rois()
        self.refresh_ramp()

    # ----------------------------------------------------------------------------------------------------------
    # what is selected
    # ----------------------------------------------------------------------------------------------------------
    def __getitem__(self, path) -> object:
        return self.param[path]

    def child(self, *names: str) -> Parameter:
        return self.param.child(*names)

    @property
    def enabled(self) -> bool:
        return bool(self.param['enabled'])

    @property
    def spectrometer(self) -> str:
        """The name of the detector module (the camera of a camera unit)"""
        return self.param[self.DETECTOR]

    @property
    def thermostat(self) -> str:
        return self.param['thermostat']

    @property
    def integration_time(self) -> float:
        """ms"""
        return float(self.param['integration_time'])

    @property
    def readout_time(self) -> float:
        """s, never less than the minimum of the parameter (a setting loaded from a file does not keep it)"""
        return max(0.05, float(self.param['readout_time']))

    @property
    def record_spectrum(self) -> bool:
        """The full spectrum (the full image of a camera) is saved"""
        return bool(self.param['record', self.FULL_DATA])

    @property
    def record_temperature(self) -> bool:
        return bool(self.param['record', 'temperature'])

    @property
    def plot_temperature(self) -> bool:
        return bool(self.param['plot_temperature'])

    @property
    def run_ramp(self) -> bool:
        """The unit drives its thermostat (it holds it at a temperature or runs a ramp), else it only records"""
        return bool(self.param['ramp', 'run'])

    @property
    def control(self) -> str:
        """What the thermal controller does: :data:`CONTROL_HOLD` or :data:`CONTROL_RAMP`"""
        return str(self.param['ramp', 'control'])

    @property
    def holds(self) -> bool:
        """The thermal controller is held at one temperature (and not run through a ramp)"""
        return self.control == CONTROL_HOLD

    @property
    def hold_temperature(self) -> float:
        return float(self.param['ramp', 'hold_temperature'])

    @property
    def end_at(self) -> str:
        """What happens when the ramps are over: :data:`END_INITIAL`, :data:`END_FINAL` or :data:`END_LOG`"""
        return str(self.param['ramp', 'end_at'])

    @property
    def after_program(self) -> Optional[str]:
        """Where the ramps end and the log goes on (``'initial'`` or ``'final'``), None if the log ends with them"""
        return END_CHOICES.get(self.end_at, END_AT_INITIAL)

    @property
    def full_interval(self) -> float:
        """s, how often the whole spectrum (image) is saved: 0 for each measurement"""
        return max(0.0, float(self.param['record', 'image_interval' if self.KIND == CAMERA else 'spectrum_interval']))

    def set_modules(self, spectrometers: Sequence[str], thermostats: Sequence[str],
                    spectrometer: Optional[str] = None, thermostat: Optional[str] = None):
        """Offer the modules of the Dashboard, keeping the selection (or selecting the given ones) if it still exists

        The caller attaches the unit to the modules: what happens in the lists on the way (a list which has just got
        new limits has no value for a moment) is not to be followed.
        """
        with self.busy():
            for leaf, names, wanted in ((self.DETECTOR, spectrometers, spectrometer),
                                        ('thermostat', thermostats, thermostat)):
                param = self.param.child(leaf)
                current = param.value()
                limits = [NONE] + list(names)
                param.setLimits(limits)
                if wanted is not None and wanted in limits:
                    param.setValue(wanted)
                elif current in limits:
                    param.setValue(current)
                else:
                    param.setValue(NONE)

    # ----------------------------------------------------------------------------------------------------------
    # ROIs
    # ----------------------------------------------------------------------------------------------------------
    @property
    def roi_keys(self) -> list[str]:
        """The names of the parameters of the ROIs, in the order they are numbered, plotted and saved"""
        return [child.name() for child in self.param.child('rois').children() if ROI_NAME.match(child.name())]

    def roi(self, key: str) -> Roi:
        group = self.param.child('rois', key)
        start, stop = sorted((float(group['start']), float(group['stop'])))
        return Roi(str(group['name']), start, stop, bool(group['record']), bool(group['plot']))

    @property
    def rois(self) -> list[Roi]:
        return [self.roi(key) for key in self.roi_keys]

    def refresh_known_rois(self):
        """Remember the parameters of the ROIs, to recognize one which was removed (it has no parent any more)"""
        self._known_rois = {key: self.param.child('rois', key) for key in self.roi_keys}

    def owned_removed(self, param: Parameter) -> bool:
        """Whether a parameter which was just removed from the tree was a ROI of this unit"""
        return any(param is known for known in self._known_rois.values()) and param.parent() is None

    def add_roi(self, roi: Roi) -> str:
        """Add a ROI at the end of the list, returns the name of its parameter"""
        numbers = [int(key[3:]) for key in self.roi_keys] + [int(key[3:]) for key in self._known_rois]
        key = f'roi{(max(numbers) + 1 if numbers else 0):02d}'
        with self.busy():
            self.param.child('rois').addChild(self.roi_definition(key, roi))
        self.refresh_known_rois()
        return key

    def roi_definition(self, key: str, roi) -> dict:
        """The parameter of a ROI"""
        return roi_definition(key, roi, self.axis_units)

    def set_axis_units(self, units: str):
        """Show the units of the wavelengths of the spectrum as the ones of the edges of the ROIs"""
        self.axis_units = units
        with self.busy():
            for key in self.roi_keys:
                for edge in self.EDGES:
                    self.param.child('rois', key, edge).setOpts(suffix=units)

    def remove_roi(self, key: str):
        with self.busy():
            self.param.child('rois', key).remove()
        self.refresh_known_rois()

    def set_rois(self, rois: Sequence[Roi]):
        """Replace all the ROIs"""
        with self.busy():
            for key in self.roi_keys:
                self.param.child('rois', key).remove()
            self._known_rois = {}
            for roi in rois:
                self.add_roi(roi)
        self.refresh_known_rois()

    def set_roi_edges(self, key: str, start: float, stop: float):
        with self.busy():
            group = self.param.child('rois', key)
            group.child('start').setValue(float(start))
            group.child('stop').setValue(float(stop))

    def new_roi(self, center: float, width: Optional[float] = None) -> Roi:
        """A ROI around a wavelength: as wide as the last one by default"""
        if width is None:
            existing = self.rois
            width = existing[-1].width if existing else 5.0
            width = width if width > 0 else 5.0
        number = len(self.roi_keys) + 1
        names = {roi.name for roi in self.rois}
        while f'ROI {number}' in names:
            number += 1
        return Roi(f'ROI {number}', round(center - width / 2., 3), round(center + width / 2., 3))

    def default_rois(self) -> list[Roi]:
        return default_rois(self.number)

    def update_roi_title(self, key: str):
        """The group of a ROI is called as the ROI"""
        group = self.param.child('rois', key)
        if group.title() != group['name']:
            group.setOpts(title=str(group['name']))

    # ----------------------------------------------------------------------------------------------------------
    # the ramp
    # ----------------------------------------------------------------------------------------------------------
    @contextmanager
    def busy(self):
        """While the program itself changes the tree: :meth:`classify` ignores what happens"""
        self._busy += 1
        try:
            yield
        finally:
            self._busy -= 1

    @property
    def is_busy(self) -> bool:
        return self._busy > 0

    def ramp_spec(self) -> RampSpec:
        ramp = self.param.child('ramp')
        return RampSpec(RampMode(ramp['mode']), float(ramp['initial']), float(ramp['final']), float(ramp['velocity']),
                        float(ramp['duration']))

    def back_spec(self) -> BackSpec:
        ramp = self.param.child('ramp')
        return BackSpec(BackMode(ramp['back_mode']), float(ramp['back_velocity']), float(ramp['back_duration']))

    def program(self) -> RampProgram:
        """The temperature program as set

        Raises
        ------
        ValueError
            if it is not defined (the message says why)
        """
        ramp = self.param.child('ramp')
        return make_program(self.ramp_spec(), self.back_spec(), hold_final=float(ramp['hold_final']),
                            hold_initial=float(ramp['hold_initial']), n_cycles=int(ramp['n_cycles']),
                            end_at=self.after_program or END_AT_INITIAL)

    def refresh_ramp(self):
        """Keep the fields of the ramp consistent

        The three numbers which define the ramp (or the way back) are editable, the fourth one is computed from them
        and read-only. A text tells how long a cycle lasts, or what is wrong.
        """
        ramp = self.param.child('ramp')
        mode = RampMode(ramp['mode'])
        back_mode = BackMode(ramp['back_mode'])
        given = {RampMode.VELOCITY: ('final', 'velocity'), RampMode.DURATION: ('final', 'duration'),
                 RampMode.FINAL: ('velocity', 'duration')}[mode]
        back_given = {BackMode.SAME: (), BackMode.VELOCITY: ('back_velocity',),
                      BackMode.DURATION: ('back_duration',)}[back_mode]
        with self.busy():
            for name in ('final', 'velocity', 'duration'):
                ramp.child(name).setOpts(readonly=name not in given)
            for name in ('back_velocity', 'back_duration'):
                ramp.child(name).setOpts(readonly=name not in back_given)
            spec = self.ramp_spec()
            solved = None
            if not spec.problems():
                solved = spec.solve()
                for name in ('final', 'velocity', 'duration'):
                    if name not in given:
                        ramp.child(name).setValue(float(getattr(solved, name)))
                span = abs(solved.final - solved.initial)
                rate = abs(solved.velocity)
                back = self.back_spec()
                if not back.problems():
                    computed = back.rate(span, rate)
                    if back_mode != BackMode.VELOCITY:
                        ramp.child('back_velocity').setValue(float(computed))
                    if back_mode != BackMode.DURATION:
                        ramp.child('back_duration').setValue(float(span / computed))
            ramp.child('cycle_duration').setValue(self.describe_program())
            ramp.child('cycle_duration').setOpts(tip=self.describe_program(full=True))
            self._show_fields()

    def hide_unused(self, thermostat: bool = True):
        """Hide again what is not used: the search box of the settings shows every parameter when it is emptied

        Parameters
        ----------
        thermostat: bool
            False for an application without thermal controller (all that is about it is hidden), True: only the
            fields of the ramp or of the hold which do not apply are hidden
        """
        with self.busy():
            if thermostat:
                self._show_fields()
                return
            for name in THERMOSTAT_GROUPS:
                self.param.child(name).setOpts(visible=False)
            for group, names in (('record', THERMOSTAT_RECORD), ('status', THERMOSTAT_STATUS)):
                for name in names:
                    self.param.child(group, name).setOpts(visible=False)

    def _show_fields(self):
        """A unit which does not drive its thermostat shows nothing of it; one which holds a temperature shows that
        temperature, one which runs a ramp shows the ramp"""
        ramp = self.param.child('ramp')
        drives, holds = bool(ramp['run']), self.holds
        ramp.child('control').setOpts(visible=drives)
        ramp.child('hold_temperature').setOpts(visible=drives and holds)
        for name in RAMP_FIELDS:
            ramp.child(name).setOpts(visible=drives and not holds)

    def describe_program(self, full: bool = False) -> str:
        """How long a cycle lasts (and all the cycles), or why the program is not valid

        The short text is what the tree shows (its column is narrow): what the setting *When the ramps are over* says is
        only repeated when it is not the usual thing. `full` tells everything (the tooltip of the line).
        """
        try:
            program = self.program()
        except ValueError as e:
            return f'invalid: {e}'
        text = (f'{format_duration(program.cycle_duration)} (ramp {format_duration(program.duration_out)}, '
                f'way back {format_duration(program.duration_back)})')
        after = self.after_program
        if program.total_duration is None:
            return text + (', repeated until you end the ramps or press Stop' if full else ', until stopped')
        text += f', {program.n_cycles} cycles: {format_duration(program.total_duration)}'
        if after == END_AT_FINAL:
            text += ' (ends at the top)'
        if full:
            text += {None: ', then the log ends',
                     END_AT_INITIAL: ', then the log goes on at the initial value until you press Stop',
                     END_AT_FINAL: ', then the log goes on at the final value until you press Stop'}[after]
        elif after is None:
            text += ', then the log ends'
        return text

    def set_units(self, units: str):
        """Show the units of the thermostat in the fields of the ramp and of the status"""
        suffix = f' {units}' if units else ''
        with self.busy():
            for name in ('initial', 'final', 'hold_temperature'):
                self.param.child('ramp', name).setOpts(suffix=units)
            for name in ('velocity', 'back_velocity'):
                self.param.child('ramp', name).setOpts(suffix=f'{units}/min' if units else '/min')
            for name in ('setpoint', 'temperature'):
                self.param.child('status', name).setOpts(suffix=units)
        return suffix

    # ----------------------------------------------------------------------------------------------------------
    # what changed
    # ----------------------------------------------------------------------------------------------------------
    def classify(self, param: Parameter) -> Optional[tuple[str, Optional[str]]]:
        """What kind of setting of this unit a parameter is, to know what to update when it changes

        Returns
        -------
        (kind, key) or None
            None if the parameter is not one of this unit, or the program is changing it itself. The kinds are
            ``enabled``, ``modules``, ``integration``, ``readout``, ``record``, ``plot_temperature``, ``ramp`` (key: the
            name of the field) and, for the ROIs (key: the name of the ROI) ``roi_edges``, ``roi_name``, ``roi_record``,
            ``roi_plot``
        """
        if self.is_busy:
            return None
        try:
            path = self.param.childPath(param)
        except Exception:
            return None
        if not path:
            return None
        if path[0] == 'rois' and len(path) == 3 and ROI_NAME.match(path[1]):
            kind = {**{edge: 'roi_edges' for edge in self.EDGES}, 'name': 'roi_name', 'record': 'roi_record',
                    'plot': 'roi_plot'}.get(path[2])
            return (kind, path[1]) if kind else None
        head = path[0]
        if head in ('enabled', 'plot_temperature'):
            return head, None
        if head in (self.DETECTOR, 'thermostat'):
            return 'modules', head
        if head == 'integration_time':
            return 'integration', None
        if head == 'readout_time':
            return 'readout', None
        if head == 'record':
            return 'record', path[-1]
        if head == 'ramp' and len(path) == 2:
            return 'ramp', path[1]
        return None

    # ----------------------------------------------------------------------------------------------------------
    # during an experiment
    # ----------------------------------------------------------------------------------------------------------
    def lock(self, locked: bool):
        """Forbid what would change an experiment which is running

        A group does not disable its children in the tree: every leaf is disabled. The plot buttons stay usable.
        """
        with self.busy():
            for leaf in leaves(self.param):
                path = tuple(self.param.childPath(leaf))
                if leaf.opts.get('readonly') or path[0] == 'status':
                    continue
                if path == ('plot_temperature',) or (len(path) == 3 and path[0] == 'rois' and path[2] == 'plot'):
                    continue  # what is plotted can be changed during the experiment
                leaf.setOpts(enabled=not locked)
            for key in self.roi_keys:
                self.param.child('rois', key).setOpts(removable=not locked)


class CameraSettings(UnitSettings):
    """The settings of a unit whose detector is a camera: the ROIs are rectangles of the image (in pixels)

    The unit works as the one of a spectrometer in every other respect: the exposure time takes the place of the
    integration time, and the full image the one of the full spectrum.
    """
    KIND = CAMERA
    DETECTOR = 'camera'
    FULL_DATA = 'image'
    EDGES = ('x0', 'x1', 'y0', 'y1')

    def __init__(self, param: Parameter, number: int):
        super().__init__(param, number)
        self.axis_units = camera.UNITS

    def roi(self, key: str) -> RectRoi:
        group = self.param.child('rois', key)
        x0, x1 = sorted((float(group['x0']), float(group['x1'])))
        y0, y1 = sorted((float(group['y0']), float(group['y1'])))
        return RectRoi(str(group['name']), x0, x1, y0, y1, bool(group['record']), bool(group['plot']))

    def roi_definition(self, key: str, roi) -> dict:
        return rect_roi_definition(key, roi, self.axis_units)

    def set_roi_edges(self, key: str, x0: float, x1: float, y0: float, y1: float):
        with self.busy():
            group = self.param.child('rois', key)
            for name, value in zip(self.EDGES, (x0, x1, y0, y1)):
                group.child(name).setValue(float(value))

    def new_roi(self, x: float, y: float, side: Optional[float] = None) -> RectRoi:
        """A square ROI around a point of the image: as large as the last one by default"""
        if side is None:
            existing = self.rois
            side = max(existing[-1].width, existing[-1].height) if existing else 100.0
            side = side if side > 0 else 100.0
        number = len(self.roi_keys) + 1
        names = {roi.name for roi in self.rois}
        while f'ROI {number}' in names:
            number += 1
        return RectRoi(f'ROI {number}', round(x - side / 2.), round(x + side / 2.), round(y - side / 2.),
                       round(y + side / 2.))

    def default_rois(self) -> list[RectRoi]:
        return default_rect_rois(self.number)

    def fit_default_rois(self, shape: tuple[int, int]) -> bool:
        """The first image came: if the ROIs are still the default ones, made for a nominal image, fit them to its
        size. Returns True if they were changed"""
        if tuple(shape) == camera.NOMINAL_SHAPE or self.rois != default_rect_rois(self.number):
            return False
        self.set_rois(camera.fit_to_image(self.rois, shape))
        return True
