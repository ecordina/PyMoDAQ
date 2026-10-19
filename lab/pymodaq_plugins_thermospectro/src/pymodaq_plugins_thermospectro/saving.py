"""Saving of an experiment into an h5 file (SWMR), with the machinery PyMoDAQ uses for its own extensions

One group per experiment (``Datalogger000``) holds, for each spectrometer (*unit*) taking part, up to four groups
whose title is ``Unit1_Spectrum``, ``Unit1_ROIs``, ``Unit1_Temperature`` and ``Unit1_Program`` (and ``Unit2_...``).
Each one is saved, or not, independently of the others:

``Unit1_Spectrum``
    the full spectrum at each readout (the wavelengths are its axis). For a camera it is ``Unit3_Image``, the full image
``Unit1_ROIs``
    the value of each recorded ROI at each readout, one array per ROI
``Unit1_Temperature``
    the temperature measured by the thermostat at each readout
``Unit1_Program``
    the set-point, the cycle and the phase of the temperature ramp (saved with the temperature, if a ramp is run, or
    the temperature held: the phase is then ``hold``)

All the arrays of a unit are enlarged along a time axis made of the time (epoch seconds) at which each readout
started: the same for the four groups of a unit.

The file is written in SWMR mode (single writer, multiple readers): it can be opened by another program, for instance to
follow the experiment, while it is being written, see :func:`prepare_file`.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace
from typing import Iterable, Optional

import numpy as np

from pymodaq_gui.parameter import Parameter

from pymodaq_data.h5modules.data_saving import DataBundle
from pymodaq_data.data import DataRaw, DataToExport

from pymodaq_utils.logger import set_logger, get_module_name

from pymodaq.utils.data import DataActuator
from pymodaq.utils.h5modules.module_saving import (
    ExtensionSaver, DetectorTimeSaver, ActuatorTimeSaver, GroupModuleType)

from pymodaq_plugins_thermospectro.ramp import PHASE_CODES
from pymodaq_plugins_thermospectro.rois import unique_names

logger = set_logger(get_module_name(__file__))

SPECTRUM = 'Spectrum'
IMAGE = 'Image'  # what the full spectrum is for a camera
ROIS = 'ROIs'
TEMPERATURE = 'Temperature'
PROGRAM = 'Program'


def group_title(number: int, what: str) -> str:
    """The title of a group of the file: ``Unit1_ROIs``"""
    return f'Unit{number}_{what}'


class VirtualModule:
    """Stands for a control module in the file, under the title of the group and with the settings it should hold

    Parameters
    ----------
    title: str
        The title of the group in the file
    settings: Parameter
        What is saved as the settings of the group
    """
    ui = None

    def __init__(self, title: str, settings: Parameter):
        self.title = title
        self.settings = settings


class TimestampedDetectorSaver(DetectorTimeSaver):
    """A DetectorTimeSaver which keeps the time of the data it is given

    The standard one filters the data into a new DataToExport, which is stamped with the time it is written: the
    groups of a unit would then not share the same time axis.

    It also does not use ``DataToExport.append`` to make this DataToExport: this runs in the thread which writes the
    file, and ``append`` is not thread-safe (it is a multipledispatch method, which keeps the object it is called on in
    the dispatcher shared by all the DataToExport). Appending in this thread while the GUI thread does the same (the
    DAQ_Viewers do it each time they show data) can append to the wrong object: a DAQ_Viewer then loses the data it was
    showing and never says it grabbed it.
    """

    def filter_data(self, dte: DataToExport) -> DataToExport:
        kept = []
        for dwa in dte:
            if 'do_save' in dwa.extra_attributes:  # this is the main filter
                if dwa.do_save:
                    kept.append(dwa)
            elif self.filter_data_wrt_settings(dwa):
                kept.append(dwa)
        filtered = DataToExport('Filtered', data=kept)
        filtered.timestamp = dte.timestamp
        return filtered


@dataclass
class UnitRecording:
    """What is saved of a unit during an experiment, as it was decided when the experiment started

    Parameters
    ----------
    number: int
        The number of the unit (1, 2)
    spectrum, temperature: bool
        Whether the full spectrum, and the temperature (with the ramp, if there is one), are saved
    program: bool
        The unit runs a ramp
    rois: list of Roi
        The ROIs which are saved, with names which are all different
    roi_indices: list of int
        Where each of them is in the values of a measurement
    settings: Parameter
        A copy of the settings of the unit, saved with its groups
    detector_settings, thermostat_settings: Parameter
        The settings of the modules, saved with their groups
    units: str
        The units of the temperature
    integration_time, readout_time: float
        In ms and s, saved as attributes of the groups of the unit
    spectrometer, thermostat: str
        The titles of the modules, saved as attributes
    """
    number: int
    spectrum: bool = True
    temperature: bool = True
    program: bool = False
    rois: list = field(default_factory=list)
    roi_indices: list = field(default_factory=list)
    settings: Optional[Parameter] = None
    detector_settings: Optional[Parameter] = None
    thermostat_settings: Optional[Parameter] = None
    units: str = ''
    integration_time: float = math.nan
    readout_time: float = math.nan
    spectrometer: str = ''
    thermostat: str = ''
    main: str = SPECTRUM  # the group of the full data: the spectrum, or the image of a camera
    detector_kind: str = 'spectrometer'

    @classmethod
    def from_unit(cls, unit) -> 'UnitRecording':
        """What a unit will save, from what the user selected in its settings"""
        settings = unit.settings
        rois = settings.rois
        names = unique_names(rois)
        indices = [index for index, roi in enumerate(rois) if roi.record]
        copy = Parameter.create(name=settings.param.name(), type='group', children=[])
        copy.restoreState(settings.param.saveState())
        thermostat = unit.thermostat
        return cls(number=unit.number, spectrum=settings.record_spectrum and unit.detector is not None,
                   temperature=settings.record_temperature and thermostat is not None,
                   program=unit.has_program or unit.has_hold, rois=[replace(rois[i], name=names[i]) for i in indices],
                   roi_indices=indices, settings=copy,
                   detector_settings=unit.detector.settings if unit.detector is not None else None,
                   thermostat_settings=thermostat.settings if thermostat is not None else None,
                   units=unit.units, integration_time=settings.integration_time, readout_time=settings.readout_time,
                   spectrometer=unit.title, thermostat=thermostat.title if thermostat is not None else '',
                   main=IMAGE if settings.KIND == 'camera' else SPECTRUM, detector_kind=settings.KIND)

    @property
    def titles(self) -> list[str]:
        """The groups of the file for this unit"""
        titles = []
        if self.spectrum:
            titles.append(group_title(self.number, self.main))
        if self.rois:
            titles.append(group_title(self.number, ROIS))
        if self.temperature:
            titles.append(group_title(self.number, TEMPERATURE))
            if self.program:
                titles.append(group_title(self.number, PROGRAM))
        return titles

    @property
    def is_saved(self) -> bool:
        return bool(self.titles)


def build_dtes(recording: UnitRecording, measurement) -> list[DataToExport]:
    """What of a measurement goes into the file, one DataToExport per group, all stamped with the time of the readout

    Parameters
    ----------
    recording: UnitRecording
    measurement: Measurement
    """
    number = recording.number
    dtes = []
    if recording.spectrum and measurement.full_due:  # (in a long log the whole spectrum or image is saved now and then)
        title = group_title(number, recording.main)
        spectrum = measurement.spectrum
        dwa = spectrum.dwa.deepcopy()
        dwa.origin = recording.spectrometer
        dwa.name = spectrum.dwa.name.split('/')[-1]
        if spectrum.axis is not None and not [axis for axis in dwa.axes if axis.index == 0]:
            dwa.axes = [spectrum.axis.copy()]  # sent by the plugin only when it changes
        dtes.append(DataToExport(title, data=[dwa]))
    if recording.rois:
        title = group_title(number, ROIS)
        units = measurement.spectrum.units
        dtes.append(DataToExport(title, data=[DataRaw(
            title, data=[np.array([float(measurement.values[index])]) for index in recording.roi_indices],
            labels=[roi.label(units, with_range=True) for roi in recording.rois])]))
    if recording.temperature:
        title = group_title(number, TEMPERATURE)
        dtes.append(DataToExport(title, data=[DataActuator(
            recording.thermostat, data=float(measurement.temperature), units=recording.units)]))
        if recording.program and measurement.phase is not None:
            title = group_title(number, PROGRAM)
            dtes.append(DataToExport(title, data=[DataRaw(
                title, data=[np.array([float(measurement.setpoint)]), np.array([float(measurement.cycle)]),
                             np.array([float(PHASE_CODES[measurement.phase])])],
                labels=['set-point', 'cycle', 'phase code'])]))
    for dte in dtes:
        dte.timestamp = measurement.t
    return dtes


class ThermoSpectroSaver(ExtensionSaver):
    """Save what the extension records, each DataToExport going to the group named like it

    Call :meth:`configure` before the h5saver is set: it tells which groups are saved.

    The file is written in SWMR mode when the h5saver is set up for it (see :func:`prepare_file`): this is switched on
    once every group has received its first data, as nothing can be created in the file afterwards.

    Parameters
    ----------
    module: ThermoSpectro
        The extension
    """
    group_type = GroupModuleType.DATALOGGER

    def __init__(self, module):
        super().__init__(module)
        self.detectors: dict[str, DetectorTimeSaver] = {}
        self.actuators: dict[str, ActuatorTimeSaver] = {}
        self._recordings: list[UnitRecording] = []
        self._detector_modules: list = []
        self._actuator_modules: list = []
        self._seen: set[str] = set()
        self.swmr_started = False
        self.n_errors = 0  # the data that could not be saved

    def configure(self, recordings: Iterable[UnitRecording]):
        """What is saved during the next experiment"""
        self._recordings = [recording for recording in recordings if recording.is_saved]
        self._detector_modules = []
        self._actuator_modules = []
        for recording in self._recordings:
            number = recording.number
            if recording.spectrum:
                self._detector_modules.append(
                    VirtualModule(group_title(number, recording.main), recording.detector_settings))
            if recording.rois:
                self._detector_modules.append(VirtualModule(group_title(number, ROIS), recording.settings))
            if recording.temperature:
                self._actuator_modules.append(
                    VirtualModule(group_title(number, TEMPERATURE), recording.thermostat_settings))
                if recording.program:
                    self._detector_modules.append(VirtualModule(group_title(number, PROGRAM), recording.settings))
        self._seen = set()
        self.swmr_started = False
        self.n_errors = 0

    @property
    def expected(self) -> set[str]:
        """The titles of the groups which are saved"""
        return {module.title for module in self._detector_modules + self._actuator_modules}

    def update_after_h5changed(self):
        self.detectors = {}
        self.actuators = {}
        self.current_nodes = {}
        for module in self._detector_modules:
            self.detectors[module.title] = TimestampedDetectorSaver(module)
            self.detectors[module.title].h5saver = self.h5saver
        for module in self._actuator_modules:
            self.actuators[module.title] = ActuatorTimeSaver(module)
            self.actuators[module.title].h5saver = self.h5saver

    def get_set_node(self, where=None, new=False):
        node = super().get_set_node(where, new)
        if new:
            for recording in self._recordings:
                self._annotate(recording)
        return node

    def _annotate(self, recording: UnitRecording):
        """What the reader of the file needs to know about the groups of a unit"""
        h5saver = self._h5saver
        number = recording.number
        common = {'unit': number, recording.detector_kind: recording.spectrometer, 'thermostat': recording.thermostat,
                  'integration_time_ms': recording.integration_time, 'readout_time_s': recording.readout_time}
        for what in (SPECTRUM, IMAGE, ROIS, TEMPERATURE, PROGRAM):
            node = self.current_nodes.get(group_title(number, what))
            if node is None:
                continue
            for key, value in common.items():
                h5saver.set_attr(node, key, value)
            if what == ROIS:
                h5saver.set_attr(node, 'rois', json.dumps(
                    [{key: value for key, value in roi.to_dict().items() if key not in ('record', 'plot')}
                     for roi in recording.rois]))
            if what == TEMPERATURE:
                h5saver.set_attr(node, 'temperature_units', recording.units)
            if what == PROGRAM:
                h5saver.set_attr(node, 'phase_codes',
                                 json.dumps({phase.value: code for phase, code in PHASE_CODES.items()}))

    def add_data(self, dte: DataToExport, **kwargs):
        """Add the data to the group of the same name as the DataToExport"""
        if dte.name in self.detectors:
            self.detectors[dte.name].add_data(self.current_nodes[dte.name], dte)
        elif dte.name in self.actuators:
            self.actuators[dte.name].add_data(self.current_nodes[dte.name], dte)
        else:
            raise NameError(f"Cannot save the data named {dte.name}: no group of this name is saved")
        self._seen.add(dte.name)

    def add_data_bundle(self, data: DataBundle):
        # runs in the thread of the SaverWorker, which counts the data it did, so it must not raise: the end of
        # the experiment waits for all of them
        try:
            self.add_data(data.dte)
            self._start_swmr_when_complete()
            self._h5saver.tick_flush()
        except Exception:
            self.n_errors += 1
            logger.exception(f'The data {data.dte.name} could not be saved')

    def _start_swmr_when_complete(self):
        """Nothing can be created in a file once SWMR is on: it is switched on when every group has its first data"""
        h5saver = self._h5saver
        if self.swmr_started or not h5saver.swmr_mode or not h5saver.is_swmr_capable:
            return
        if self._seen >= self.expected:
            h5saver.flush()
            h5saver.enable_swmr()
            self.swmr_started = True
            logger.info('The file can be read while it is written (SWMR)')


def prepare_file(h5saver, flush_interval: Optional[int] = None) -> bool:
    """Make a new file for an experiment, in SWMR mode if the h5 backend allows it

    The h5py backend is the only one which can do SWMR: the h5saver is switched to it. A new file is made each time:
    a file which was not written for SWMR cannot be appended to in this mode.

    Parameters
    ----------
    h5saver: H5Saver
        The one of the extension
    flush_interval: int
        Flush the file every that many writes (default: the setting of the h5saver, 1)

    Returns
    -------
    bool
        True if the file is written in SWMR mode
    """
    settings = h5saver.settings
    swmr = False
    try:
        import h5py  # noqa: F401
        if h5saver.backend != 'h5py':
            settings['backend', 'backend_type'] = 'h5py'
        settings['backend', 'swmr_options', 'enable_swmr'] = True
        swmr = True
    except Exception:
        logger.warning('The h5py backend is not available: the file cannot be read while it is written')
    if flush_interval is not None:
        settings['backend', 'swmr_options', 'flush_interval'] = int(flush_interval)
    h5saver.init_file(update_h5=True)
    swmr = swmr and h5saver.swmr_mode
    if swmr:
        h5saver.set_swmr_flush_interval(int(settings['backend', 'swmr_options', 'flush_interval']))
    return swmr


def finalize_file(h5saver) -> bool:
    """End SWMR once the file is closed: its attributes (the shapes of the arrays) are updated

    Returns
    -------
    bool
        True if the file was written in SWMR mode and is now complete
    """
    if not getattr(h5saver, 'swmr_mode', False) or not h5saver.is_swmr_capable:
        return False
    h5saver.finalize_swmr()
    return True
