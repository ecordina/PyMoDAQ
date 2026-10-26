"""Saving of an experiment into an h5 file (SWMR), with the machinery PyMoDAQ uses for its own extensions

One group per experiment (``Datalogger000``) holds, for each spectrometer (*unit*) taking part, up to four groups
whose title is ``Unit1_Spectrum``, ``Unit1_ROIs``, ``Unit1_Temperature`` and ``Unit1_Program`` (and ``Unit2_...``).
Each one is saved, or not, independently of the others:

``Unit1_Spectrum``
    the full spectrum at each readout (the wavelengths are its axis). For a camera it is ``Unit3_Image``, the full image
``Unit1_ROIs``
    the value of each recorded ROI at each readout, one array per ROI
``Unit1_Integration``
    the integration (exposure) time at each readout, in ms: it can be changed during the experiment
``Unit1_Temperature``
    the temperature measured by the thermostat at each readout
``Unit1_Program``
    the set-point, the cycle and the phase of the temperature ramp (saved with the temperature, if a ramp is run, or
    the temperature held: the phase is then ``hold``)
``Unit1_Crop1``, ``Unit1_Crop2``...
    the pixels of each ROI which is a crop (not an aggregation of its pixels: see :mod:`.rois`, :mod:`.camera`), one
    part of the spectrum or one image per readout (or every *Crops every* seconds)

All the arrays of a unit are enlarged along a time axis made of the time (epoch seconds) at which each readout
started: the same for the four groups of a unit.

The units are kept: the arrays hold their ``units`` (the temperature in the unit of the thermostat, the integration time in
ms, the values of the ROIs in the intensity unit of the unit, per ms if they were divided by the integration time), and
the groups of the ROIs say it in their attributes ``values_units``, ``intensity_units`` and ``per_integration``.

What the user wrote about the experiment (``experiment_title``, ``sample``, ``operator``, ``protocol``, and the
``application`` which made the file) is in the attributes of the group ``RawData``. The observations noted while it runs
are in the group ``Notes`` of the experiment, with two arrays enlarged by each observation: ``time`` (epoch seconds, the
one of the data) and ``text``.

The file is written in SWMR mode (single writer, multiple readers): it can be opened by another program, for instance to
follow the experiment, while it is being written, see :func:`prepare_file`.
"""
from __future__ import annotations

import datetime
import json
import math
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable, Optional

import numpy as np

from pymodaq_gui.parameter import Parameter

from pymodaq_data.h5modules.data_saving import DataBundle
from pymodaq_data import Unit
from pymodaq_data.data import Axis, DataRaw, DataToExport

from pymodaq_utils.logger import set_logger, get_module_name

from pymodaq.utils.data import DataActuator
from pymodaq.utils.h5modules.module_saving import (
    ExtensionSaver, DetectorTimeSaver, ActuatorTimeSaver, GroupModuleType)

from pymodaq_plugins_thermospectro.camera import window_ranges
from pymodaq_plugins_thermospectro.ramp import PHASE_CODES
from pymodaq_plugins_thermospectro.rois import unique_names
from pymodaq_plugins_thermospectro.unit_settings import DEFAULT_INTENSITY_UNITS

logger = set_logger(get_module_name(__file__))

NOTES = 'Notes'  # the group of the observations
NOTE_BYTES = 4000  # the room of an observation in the file, in bytes of UTF-8: 1000 characters of any language (an
# array of strings of a fixed size can be enlarged once the file is in SWMR mode, one of strings of any size cannot)
SPECTRUM = 'Spectrum'
IMAGE = 'Image'  # what the full spectrum is for a camera
ROIS = 'ROIs'
TEMPERATURE = 'Temperature'
PROGRAM = 'Program'
INTEGRATION = 'Integration'
CROP = 'Crop'  # followed by the number of the crop: Crop1, Crop2...


#: What is written for "arbitrary units": pint reads ``a.u.`` as an atomic mass unit times a year, and ``au`` as an astronomical unit
ARBITRARY_UNITS = ('a.u.', 'a.u', 'au', 'arb.u.', 'arb. u.', 'arb. units', 'arbitrary units', 'arbitrary unit')


def pint_safe(units: str) -> str:
    """The units as the data model of PyMoDAQ can hold them (a unit which pint does not know, or reads as something else
    than what is meant, is dropped there: the groups say it in their attributes too)

    Arbitrary units (``A.U.``) are no unit: nothing is kept, and ``A.U./ms`` is a rate, ``1/ms``.
    """
    text = units.strip()
    head, slash, tail = text.partition('/')
    if head.strip().lower() in ARBITRARY_UNITS:
        text = f'1/{tail.strip()}' if slash and tail.strip() else ''
    try:
        Unit(text)
    except Exception:
        return ''
    return text


def group_title(number: int, what: str) -> str:
    """The title of a group of the file: ``Unit1_ROIs``"""
    return f'Unit{number}_{what}'


def crop_title(number: int, crop: int) -> str:
    """The title of the group of the pixels of a ROI which is a crop (`crop` counted from 1): ``Unit3_Crop1``"""
    return group_title(number, f'{CROP}{crop}')


#: What a name of file cannot hold on one system or another
INVALID_IN_NAMES = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def clean_name(name: str, default: str = 'Dataset') -> str:
    """A name which can be the one of a file everywhere: its forbidden characters become ``_``, it has no ``.h5``"""
    cleaned = INVALID_IN_NAMES.sub('_', str(name)).strip()
    if cleaned.lower().endswith('.h5'):
        cleaned = cleaned[:-3]
    return cleaned.strip().strip('.').strip() or default


def new_file_path(folder, name: str, add_date: bool = True, date_folders: bool = False,
                  now: Optional[datetime.datetime] = None) -> Path:
    """The path of the file of a new experiment, which does not exist yet (an existing file is never replaced)

    Parameters
    ----------
    folder: str or Path
        Where the files are
    name: str
        The name of the file, without ``.h5``: ``Sample12`` (see :func:`clean_name`)
    add_date: bool
        Add the date and the time to it: ``Sample12_20261022_153012.h5`` (else ``Sample12.h5``)
    date_folders: bool
        Put the file in the folders of the year and of the day, as PyMoDAQ does: ``2026/20261022``
    now: datetime, optional
        The time of the experiment (now)

    Returns
    -------
    Path
        If the file exists (a name without date used twice) a number is added: ``Sample12_001.h5``
    """
    now = now or datetime.datetime.now()
    folder = Path(folder).expanduser()
    if date_folders:
        folder = folder / f'{now:%Y}' / f'{now:%Y%m%d}'
    stem = clean_name(name)
    if add_date:
        stem = f'{stem}_{now:%Y%m%d_%H%M%S}'
    path, number = folder / f'{stem}.h5', 0
    while path.exists():
        number += 1
        path = folder / f'{stem}_{number:03d}.h5'
    return path


def folder_problem(folder) -> Optional[str]:
    """Why files cannot be made in a folder, in words (None: they can; the folder is made if it does not exist)"""
    path = Path(folder).expanduser().absolute()
    probe = path
    while not probe.exists():
        if probe.parent == probe:
            return f'{path} cannot be made ({probe} does not exist)'
        probe = probe.parent
    if not probe.is_dir():
        return f'{probe} is not a folder'
    if not os.access(probe, os.W_OK | os.X_OK):
        return f'nothing can be written in {probe}'
    return None


def cut_to_bytes(text: str, size: int = NOTE_BYTES) -> str:
    """The text, cut so that its UTF-8 holds in `size` bytes (a character is never cut in two)"""
    return text.encode('utf-8')[:size].decode('utf-8', 'ignore')


@dataclass
class NoteBundle(DataBundle):
    """An observation on its way to the thread which writes the file (the data go there in a DataBundle)"""
    epoch: float = 0.
    text: str = ''


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
        The ROIs whose value is saved, with names which are all different
    roi_indices: list of int
        Where each of them is in the values of a measurement
    crops: list of RectRoi
        The ROIs of a camera which are saved as images, not as a value: their pixels (names which are all different)
    integration: bool
        The integration (exposure) time of each measurement is saved
    intensity_units, value_units, spectrum_units: str
        What the detector measures, the units of the values of the ROIs (per ms if they are divided by the integration
        time) and the ones of the full spectra and of the crops of a spectrometer (per ms if they are divided)
    per_integration, spectrum_per_integration: bool
        The values of the ROIs, the full spectra and the crops of a spectrometer are divided by the integration time of
        their measurement
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
    n_channels: int
        How many channels the images of a camera have (the ROIs which give a number are measured on some of them, see
        :class:`~.camera.RectRoi`): saved as an attribute of their group
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
    crops: list = field(default_factory=list)
    integration: bool = False
    intensity_units: str = DEFAULT_INTENSITY_UNITS
    value_units: str = DEFAULT_INTENSITY_UNITS
    per_integration: bool = False
    spectrum_units: str = DEFAULT_INTENSITY_UNITS
    spectrum_per_integration: bool = False
    n_channels: int = 1

    @classmethod
    def from_unit(cls, unit) -> 'UnitRecording':
        """What a unit will save, from what the user selected in its settings"""
        settings = unit.settings
        rois = settings.rois
        names = unique_names(rois)
        crop = [getattr(roi, 'is_crop', False) for roi in rois]  # (a ROI of a camera may give its pixels)
        indices = [index for index, roi in enumerate(rois) if roi.record and not crop[index]]
        crops = [replace(roi, name=names[index]) for index, roi in enumerate(rois) if roi.record and crop[index]]
        cls._check_crops(unit, crops)
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
                   main=IMAGE if settings.KIND == 'camera' else SPECTRUM, detector_kind=settings.KIND, crops=crops,
                   integration=settings.record_integration, intensity_units=settings.intensity_units,
                   value_units=settings.value_units, per_integration=settings.per_integration,
                   spectrum_units=settings.spectrum_units, spectrum_per_integration=settings.spectrum_per_integration,
                   n_channels=getattr(unit, 'n_channels', 1))

    @staticmethod
    def _check_crops(unit, crops: list):
        """A crop which is outside of the image (of the spectrum) would never give anything to save: the experiment does
        not start

        Raises
        ------
        ValueError
            if a crop is outside of the last image (spectrum)
        """
        spectrum = getattr(unit, 'spectrum', None)
        if not crops or spectrum is None:
            return
        for roi in crops:
            if hasattr(roi, 'x0'):  # (a rectangle of the image of a camera)
                shape = spectrum.shape
                if window_ranges(shape, roi.x0, roi.x1, roi.y0, roi.y1) is None:
                    raise ValueError(f'{unit.label}: the crop {roi.name} (x {roi.x0:g}-{roi.x1:g}, y {roi.y0:g}-{roi.y1:g}) '
                                     f'is outside of the image ({shape[1]} x {shape[0]} pixels): move it, or untick '
                                     f'its Record box')
            elif not roi.mask(spectrum.x).any():  # (a window of a spectrum)
                finite = spectrum.x[np.isfinite(spectrum.x)]
                span = f' ({finite.min():g}-{finite.max():g} {spectrum.units})' if finite.size else ''
                raise ValueError(f'{unit.label}: the crop {roi.name} ({roi.start:g}-{roi.stop:g} {spectrum.units}) is '
                                 f'outside of the spectrum{span}: move it, or untick its Record box')

    @property
    def titles(self) -> list[str]:
        """The groups of the file for this unit"""
        titles = []
        if self.spectrum:
            titles.append(group_title(self.number, self.main))
        if self.rois:
            titles.append(group_title(self.number, ROIS))
        titles += [crop_title(self.number, index) for index in range(1, len(self.crops) + 1)]
        if self.integration:
            titles.append(group_title(self.number, INTEGRATION))
        if self.temperature:
            titles.append(group_title(self.number, TEMPERATURE))
            if self.program:
                titles.append(group_title(self.number, PROGRAM))
        return titles

    @property
    def is_saved(self) -> bool:
        return bool(self.titles)


def divided_by(array: np.ndarray, integration: float) -> np.ndarray:
    """The array divided by the integration time (ms); nothing but nan if it is not a time: the arrays of the file stay
    aligned, and nothing is invented"""
    array = np.asarray(array, dtype=float)
    if not (math.isfinite(integration) and integration > 0):
        return np.full_like(array, np.nan)
    return array / integration


def per_ms(units: str) -> str:
    """``A.U.`` -> ``A.U./ms``"""
    return f'{units}/ms' if units else '1/ms'


def _spectrum_crop_dtes(recording: UnitRecording, measurement) -> list[DataToExport]:
    """The part of the spectrum of each crop of a spectrometer, one DataToExport per crop, on the wavelengths of its pixels
    (divided by the integration time if the spectra are)"""
    spectrum = measurement.spectrum
    full = spectrum.dwa
    x_axis = np.asarray(spectrum.axis.get_data() if spectrum.axis is not None else spectrum.x, dtype=float)
    units = full.units or recording.intensity_units
    if recording.spectrum_per_integration:
        units = per_ms(units)
    dtes = []
    for index, roi in enumerate(recording.crops, 1):
        mask = roi.mask(spectrum.x)
        if not mask.any() or mask.size != x_axis.size:  # (the spectrum changed since the start)
            continue
        data = [np.asarray(array, dtype=float).ravel()[mask] for array in full.data]
        if recording.spectrum_per_integration:
            data = [divided_by(array, measurement.integration_time) for array in data]
        axis = Axis(label=spectrum.axis.label if spectrum.axis is not None else 'pixel',
                    units=spectrum.axis.units if spectrum.axis is not None else '', data=x_axis[mask], index=0)
        dwa = DataRaw(roi.name, data=data, axes=[axis], labels=list(full.labels) or None, units=pint_safe(units))
        dwa.origin = recording.spectrometer
        dtes.append(DataToExport(crop_title(recording.number, index), data=[dwa]))
    return dtes


def _crop_dtes(recording: UnitRecording, measurement) -> list[DataToExport]:
    """The pixels of each crop, one DataToExport per crop: the part of the spectrum of a spectrometer, the rectangle of the
    image of a camera (the channels of a color camera are kept)"""
    if recording.detector_kind != 'camera':
        return _spectrum_crop_dtes(recording, measurement)
    full = measurement.spectrum.dwa
    dtes = []
    for index, roi in enumerate(recording.crops, 1):
        found = window_ranges(measurement.spectrum.shape, roi.x0, roi.x1, roi.y0, roi.y1)
        if found is None:  # (the image changed its size since the start)
            continue
        (row0, row1), (column0, column1) = found
        data = [np.ascontiguousarray(np.asarray(array)[row0:row1, column0:column1]) for array in full.data]
        axes = [Axis(label='y', units='pixel', data=np.arange(row0, row1, dtype=float), index=0),
                Axis(label='x', units='pixel', data=np.arange(column0, column1, dtype=float), index=1)]
        dwa = DataRaw(roi.name, data=data, axes=axes, labels=list(full.labels) or None,
                      units=full.units or pint_safe(recording.intensity_units))
        dwa.origin = recording.spectrometer
        dtes.append(DataToExport(crop_title(recording.number, index), data=[dwa]))
    return dtes


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
        units = dwa.units or recording.intensity_units  # (the plugins do not say what their instrument measures: the user does)
        if recording.spectrum_per_integration and recording.detector_kind != 'camera':
            dwa.data = [divided_by(array, measurement.integration_time) for array in dwa.data]
            units = per_ms(units)
        if units != dwa.units:
            dwa.force_units(pint_safe(units))  # (units_as would convert the numbers)
        if spectrum.axis is not None and not [axis for axis in dwa.axes if axis.index == 0]:
            dwa.axes = [spectrum.axis.copy()]  # sent by the plugin only when it changes
        dtes.append(DataToExport(title, data=[dwa]))
    if recording.rois:
        title = group_title(number, ROIS)
        units = measurement.spectrum.units
        dtes.append(DataToExport(title, data=[DataRaw(
            title, data=[np.array([float(measurement.values[index])]) for index in recording.roi_indices],
            labels=[roi.label(units, with_range=True) for roi in recording.rois],
            units=pint_safe(recording.value_units))]))
    if recording.crops and measurement.crop_due:
        dtes += _crop_dtes(recording, measurement)
    if recording.integration:
        title = group_title(number, INTEGRATION)
        dtes.append(DataToExport(title, data=[DataRaw(
            title, data=[np.array([float(measurement.integration_time)])], labels=['integration time'], units='ms')]))
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
        self._notes = None  # the two arrays of the observations (time, text) of the file being written
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
            for index in range(1, len(recording.crops) + 1):
                self._detector_modules.append(VirtualModule(crop_title(number, index), recording.detector_settings))
            if recording.integration:
                self._detector_modules.append(VirtualModule(group_title(number, INTEGRATION), recording.settings))
            if recording.temperature:
                self._actuator_modules.append(
                    VirtualModule(group_title(number, TEMPERATURE), recording.thermostat_settings))
                if recording.program:
                    self._detector_modules.append(VirtualModule(group_title(number, PROGRAM), recording.settings))
        self._seen = set()
        self._notes = None
        self.swmr_started = False
        self.n_errors = 0

    @property
    def notes_ready(self) -> bool:
        """The file being written has its arrays of observations (they are made with its group)"""
        return self._notes is not None

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
            self._make_notes(node)
        return node

    def _make_notes(self, node):
        """The group of the observations, with its two arrays: they are made now, nothing can be created once SWMR is on"""
        self._notes = None
        try:
            import h5py
            group = self._h5saver.get_set_group(node, NOTES, title='Observations')
            self._h5saver.set_attr(group, 'description', 'what the user noted during the experiment: the time (epoch '
                                                         'seconds) and the text of each observation')
            h5_group = group.node
            self._notes = (
                h5_group.create_dataset('time', shape=(0,), maxshape=(None,), dtype='float64', chunks=(64,)),
                h5_group.create_dataset('text', shape=(0,), maxshape=(None,), chunks=(16,),
                                        dtype=h5py.string_dtype('utf-8', NOTE_BYTES)))
        except Exception:  # (an other backend than h5py: the observations are only in the window)
            logger.warning('The observations cannot be saved in this file (it needs the h5py backend)', exc_info=True)

    def add_note(self, epoch: float, text: str) -> bool:
        """Append an observation to the file (in the thread which writes it)"""
        if self._notes is None:
            return False
        times, texts = self._notes
        size = times.shape[0]
        times.resize((size + 1,))
        texts.resize((size + 1,))
        times[size] = float(epoch)
        texts[size] = cut_to_bytes(text).encode('utf-8')
        times.flush()
        texts.flush()
        return True

    def _annotate(self, recording: UnitRecording):
        """What the reader of the file needs to know about the groups of a unit"""
        h5saver = self._h5saver
        number = recording.number
        common = {'unit': number, recording.detector_kind: recording.spectrometer, 'thermostat': recording.thermostat,
                  'integration_time_ms': recording.integration_time, 'readout_time_s': recording.readout_time}
        crops = {f'{CROP}{index}': roi for index, roi in enumerate(recording.crops, 1)}
        for what in (SPECTRUM, IMAGE, ROIS, *crops, INTEGRATION, TEMPERATURE, PROGRAM):
            node = self.current_nodes.get(group_title(number, what))
            if node is None:
                continue
            for key, value in common.items():
                h5saver.set_attr(node, key, value)
            if what in crops:
                roi = crops[what]
                h5saver.set_attr(node, 'roi', json.dumps(
                    {key: value for key, value in roi.to_dict().items() if key not in ('record', 'plot')}))
            if what == ROIS:
                h5saver.set_attr(node, 'rois', json.dumps(
                    [{key: value for key, value in roi.to_dict().items() if key not in ('record', 'plot')}
                     for roi in recording.rois]))
                if recording.detector_kind == 'camera':  # (the channels the ROIs are measured on are counted from 0)
                    h5saver.set_attr(node, 'n_channels', int(recording.n_channels))
            if what in (ROIS, SPECTRUM, IMAGE, *crops):
                h5saver.set_attr(node, 'intensity_units', recording.intensity_units)
            if what == ROIS:
                h5saver.set_attr(node, 'values_units', recording.value_units)
                h5saver.set_attr(node, 'per_integration', bool(recording.per_integration))
            elif what in (SPECTRUM, IMAGE, *crops):  # (what a group of spectra or images holds: divided, or as it came)
                h5saver.set_attr(node, 'values_units', recording.spectrum_units)
                h5saver.set_attr(node, 'per_integration', bool(recording.spectrum_per_integration))
            if what == INTEGRATION:
                h5saver.set_attr(node, 'integration_units', 'ms')
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
        if isinstance(data, NoteBundle):
            try:
                if not self.add_note(data.epoch, data.text):
                    raise RuntimeError('this file has no array of observations')
            except Exception:
                self.n_errors += 1
                logger.exception('An observation could not be saved')
            return
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


def prepare_file(h5saver, flush_interval: Optional[int] = None, path=None, metadata: Optional[dict] = None) -> bool:
    """Make a new file for an experiment, in SWMR mode if the h5 backend allows it

    The h5py backend is the only one which can do SWMR: the h5saver is switched to it. A new file is made each time:
    a file which was not written for SWMR cannot be appended to in this mode.

    Parameters
    ----------
    h5saver: H5Saver
        The one of the extension
    flush_interval: int
        Flush the file every that many writes (default: the setting of the h5saver, 1)
    path: str or Path, optional
        The file to make (see :func:`new_file_path`), its folders are made if needed. Default: the one the h5saver
        chooses from its base path and base name
    metadata: dict, optional
        Attributes of the group ``RawData`` of the file: what the user wrote about the experiment (not ``title``: it
        would replace the title of the group)

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
    if path is not None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        h5saver.init_file(update_h5=True, addhoc_file_path=path, metadata=dict(metadata or {}))
    else:
        h5saver.init_file(update_h5=True, metadata=dict(metadata or {}))
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
