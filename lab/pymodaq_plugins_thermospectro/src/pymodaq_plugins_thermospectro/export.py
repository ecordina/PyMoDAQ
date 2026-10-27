#!/usr/bin/env python
"""``lab_export``: the h5 file of an experiment of the lab as csv, tif and text files

    lab_export Pomme_20261022_153012.h5

The h5 file written by the extension is made of one small array per kind of data. This puts them in the formats the
usual tools read, next to the h5 file (or in the folder of ``-o``), with its name as the beginning of the names:

* **the values** of each spectrometer (``Unit1_...``, ``Unit2_...``), ``<file>_spectrometer1.csv``: one row per readout,
  ``time (s) | temperature (°C) | integration time (ms) | ROI 1 mean (450-455 nm) [A.U.] | ROI 2 sum ... [A.U.] | ...``.
  The temperature is there if the unit has a thermal controller, the integration time if it was saved, the ROIs which were
  recorded and give a number (their mean, sum, median, min, max or standard deviation, as their name says);
* **the full spectra** (when they were saved), ``<file>_spectrometer1_spectra.csv``: one row per spectrum, ``time (s) |
  temperature (°C) | integration time (ms) | 450.123 nm | 450.456 nm | ...``, and the ROIs of a spectrometer which are
  *crops* (the part of the spectrum in their window), each in a file of its own,
  ``<file>_spectrometer1_crop_<name>.csv``, the same with the wavelengths of the window;
* **the camera** (``Unit3_...``): the values of its ROIs which give a number (their mean, sum, median, min, max or
  standard deviation) in ``<file>_camera.csv``, one row per readout like the one of a spectrometer, plus the number of
  the image in the tif file where there is one; the images (*The full image*, when it was saved) in
  ``<file>_camera.tif``, a time series of the usual axes **T, C, Y, X** (an ImageJ hyperstack: open it with Fiji /
  ImageJ, or ``tifffile.imread``); and the pixels of each ROI which is a *crop* in ``<file>_camera_crop_<name>.tif``,
  the same kind of tif. A series is always **one file**: with more than 3.5 GB of pixels (a classic tif is limited to 4 GB)
  it is a BigTIFF, which ImageJ alone does not read (Fiji does, with the Bio-Formats importer: *File > Import >
  Bio-Formats*; so do ``tifffile.imread`` and napari), and the csv file has the column ``tif frame``. A tif also says
  the size of a pixel (the calibration of ImageJ, in um: the pitch of the sensor the camera told, times its binning, divided
  by ``--magnification``, or ``--pixel-size`` UM measured on the sample), the display range, the camera (make, model, serial
  number, window and binning of the sensor), the software, the date of the first image and, in the label of each image, its
  time, the temperature and the exposure time;
* **what was written about the experiment**, ``<file>_experiment.txt``: the title, the sample, the operator, the
  protocol, the observations noted while it ran with their time, the instruments, the ROIs, the units, and the files
  which were written; the observations are also in ``<file>_notes.csv`` (``time (s) | clock time | observation``).

What is not in the file (a spectrometer or a camera which was not used, a temperature, a full spectrum, an image which
were not recorded) is left out. The units are the ones the file holds: in the header of the columns of the csv files, in
square brackets for what is measured, and in the text file. The values of the ROIs divided by the integration time in the
extension (*Per ms of integration*) are in ``A.U./ms``, and so are the full spectra and the crops of the spectra if
*Spectra per ms of integration* was ticked; ``--per-integration-time`` does it here, with the integration time of each
readout, for the values, the spectra and the crops which were saved as they were measured.

The time is in seconds from the first measurement of the experiment, the same zero in all the files
(``--absolute-time`` gives the epoch seconds instead).

**The file of an experiment which is running** can be converted: it is read in SWMR mode (single writer, multiple readers),
asked for on every system, and the files hold what is saved so far (the text says so, and the program says it too). The
first seconds after Start (the file is not readable yet) and the last moments of the experiment (the file is closed and
finished) are waited for, up to ``--wait`` seconds. Do not leave the file open in another program (or start a long
conversion) when the experiment ends: its last step needs the file to itself.

Needs ``numpy``, ``h5py`` and, for the images, ``tifffile`` (``python -m pip install tifffile``). It does not need
PyMoDAQ: this file can be copied alone to any computer and run with ``python export.py FILE.h5``.
"""
from __future__ import annotations

import argparse
import ast
import csv
import datetime
import glob
import json
import re
import sys
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence

import numpy as np

TITLE = re.compile(r'^Unit(\d+)_(Spectrum|Image|ROIs|Integration|Temperature|Program|Crop\d+)$')
#: What the user wrote about the experiment: the attributes of the group RawData (and the application which made the file)
DESCRIPTION = ('experiment_title', 'sample', 'operator', 'protocol', 'application')
NOTES = 'Notes'  # the group of the observations, in the group of the experiment
IMAGEJ_TYPES = (np.uint8, np.uint16, np.float32)
CHANNEL_COLORS = ('red', 'green', 'blue')
#: A classic tif is limited to 4 GB, which ImageJ reads (it does not read BigTIFF): a series of images with more bytes of
#: pixels than this is written as a single BigTIFF file (Fiji reads it with Bio-Formats, so do tifffile, napari...)
CLASSIC_TIF_BYTES = 3_500_000_000
BIGTIFF_WORDS = ', a BigTIFF: Fiji opens it with the Bio-Formats importer, ImageJ alone does not read it'
#: a value of another detector is left empty when it is further than this many readout intervals
MAX_GAP = 3.0
#: The integration time is in ms in the file
INTEGRATION_UNITS = 'ms'
#: How long (s) to wait for the file of a running experiment which is being created (its first seconds) or closed (its last
#: moments), and how often to try again meanwhile
WAIT = 10.0
RETRY_EVERY = 0.25
#: What HDF5 says (in some words) of a file which is being written
BUSY_WORDS = ('lock file', 'locking', 'already open')
#: How the file is read, in this order. 1: in SWMR mode (single writer, multiple readers), which a file being written needs
#: and a finished file does not mind, with the locks of the library. 2: the same without the locks, when they refuse (what
#: PyMoDAQ does on Windows): HDF5 itself refuses it if the file is not in SWMR mode, so nothing half written is read.
#: 3: as an ordinary file (one which was not written in SWMR mode, an old one)
OPEN_MODES = ({'swmr': True}, {'swmr': True, 'locking': False}, {})
#: How a file whose header says an experiment is writing it in SWMR mode (or which was closed) is read: **without any lock**.
#: On Windows a reader which holds the lock of HDF5 makes the *writes* of the experiment fail (``Permission denied``, then
#: ``slist already enabled?``: the HDF5 of the experiment is left in a bad state, which can crash it hours later), so a file
#: which is being written is never locked by this program: SWMR does not need it
UNLOCKED_MODES = ({'swmr': True, 'locking': False}, {})
#: The consistency flags in the header (superblock, versions 2 and 3) of an h5 file, which HDF5 keeps up to date while the
#: file is open: open for writing, and open for writing in SWMR mode
SUPERBLOCK_SIGNATURE = b'\x89HDF\r\n\x1a\n'
FLAG_WRITING, FLAG_SWMR_WRITING = 0x01, 0x04
#: The what the aggregations of the ROIs of a camera are called in the text
AGGREGATION_WORDS = {'mean': 'mean', 'sum': 'sum', 'median': 'median', 'min': 'minimum', 'max': 'maximum',
                     'std': 'standard deviation', 'crop': 'crop'}


# ----------------------------------------------------------------------------------------------------------------------
# reading the file
# ----------------------------------------------------------------------------------------------------------------------
def attribute(value):
    """An attribute of the file as a python value (PyMoDAQ writes them as ``{"type": "str", "data": "'text'"}``)"""
    if isinstance(value, bytes):
        value = value.decode('utf-8', errors='replace')
    if isinstance(value, np.ndarray) and value.size == 1:
        value = value.item()
    if not isinstance(value, str):
        return value
    try:
        content = json.loads(value)
        if isinstance(content, dict) and 'data' in content:
            try:
                return ast.literal_eval(content['data'])
            except (ValueError, SyntaxError):
                return content['data']
    except ValueError:
        pass
    return value


def natural(name: str) -> list:
    return [int(part) if part.isdigit() else part for part in re.split(r'(\d+)', name)]


@dataclass
class Series:
    """What a group of the file holds: arrays along a time axis"""
    kind: str
    title: str
    time: np.ndarray  # epoch seconds
    arrays: list  # h5py datasets or arrays: the first axis is the time
    labels: list[str]
    axis: Optional[np.ndarray] = None  # the x axis of a spectrum (wavelengths)
    attrs: dict = field(default_factory=dict)
    units: list[str] = field(default_factory=list)  # of each array

    def __len__(self) -> int:
        return min([len(self.time)] + [len(array) for array in self.arrays])

    def column(self, index: int, rows: Optional[int] = None) -> np.ndarray:
        return np.asarray(self.arrays[index][:rows if rows is not None else len(self)], dtype=float).reshape(
            len(self) if rows is None else rows, -1)[:, 0]


@dataclass
class UnitData:
    """The groups of a unit (1 and 2 are spectrometers, 3 is the camera)"""
    number: int
    series: dict = field(default_factory=dict)  # kind: Series (the crops are Crop1, Crop2...)

    @property
    def is_camera(self) -> bool:
        return 'Image' in self.series or any('camera' in s.attrs for s in self.series.values())

    @property
    def kind(self) -> str:
        return 'camera' if self.is_camera else 'spectrometer'

    @property
    def name(self) -> str:
        return 'camera' if self.is_camera else f'spectrometer {self.number}'

    @property
    def crops(self) -> list[Series]:
        """The groups of the pixels of the ROIs which are a crop, in the order of their number"""
        return [self.series[kind] for kind in sorted((k for k in self.series if k.startswith('Crop')), key=natural)]

    def attr(self, key: str, default=''):
        for series in self.series.values():
            if key in series.attrs:
                return series.attrs[key]
        return default

    @property
    def camera_info(self) -> dict:
        """What the camera said about itself when the experiment started (its model, the pitch of its pixels, its binning...,
        see ``camera_info.py``): empty for an older file and for a camera which said nothing (a Mock one)"""
        try:
            info = json.loads(str(self.attr('camera_info', '')))
        except ValueError:
            return {}
        return info if isinstance(info, dict) else {}

    @property
    def temperature_units(self) -> str:
        return str(self.attr('temperature_units', ''))

    @property
    def per_integration(self) -> bool:
        """The values of the ROIs were divided by the integration time in the extension"""
        series = self.series.get('ROIs')
        return was_divided(series) if series is not None else False

    @property
    def intensity_units(self) -> str:
        """What the detector measures (the unit the user gave it, or the one of the arrays)"""
        units = str(self.attr('intensity_units', ''))
        if units:
            return units
        for kind in ('Spectrum', 'Image'):
            if kind in self.series and self.series[kind].units:
                return self.series[kind].units[0]
        return ''

    @property
    def value_units(self) -> str:
        """The units of the values of the ROIs, as they are in the file"""
        series = self.series.get('ROIs')
        return series_units(series, self.intensity_units) if series is not None else self.intensity_units


def divided(units: str) -> str:
    """The units of a measurement per ms of integration: ``A.U.`` -> ``A.U./ms``"""
    return f'{units}/{INTEGRATION_UNITS}' if units else f'1/{INTEGRATION_UNITS}'


def series_units(series: Series, default: str = '') -> str:
    """The units of what a group holds, as the group says it (``values_units``), else the ones of its arrays"""
    units = str(series.attrs.get('values_units', ''))
    if units:
        return units
    return series.units[0] if series.units and series.units[0] else default


def was_divided(series: Series) -> bool:
    """What the group holds was divided by the integration time in the extension"""
    return bool(series.attrs.get('per_integration', False))


class FileBusy(OSError):
    """The file is being created or closed by a running experiment, or another program has it open for writing"""


def looks_busy(error: Exception) -> bool:
    """What HDF5 said is about a file which is being written (a lock it could not take, a file already open for writing, not
    in SWMR mode yet)"""
    return isinstance(error, BlockingIOError) or any(word in str(error).lower() for word in BUSY_WORDS)


def writer_state(path: Path) -> Optional[str]:
    """Whether an experiment is writing the file, as the header of the file says it: read with plain python, so that no lock
    of HDF5 is taken (a lock would make the writes of the experiment fail on Windows)

    Returns
    -------
    ``'swmr'`` (open for writing in SWMR mode: it can be read), ``'writing'`` (open for writing, not in SWMR mode yet: the
    first seconds of an experiment, it cannot be read), ``'busy'`` (the header cannot even be read: on Windows the
    experiment which creates the file holds it), ``'closed'``, or None when the file does not say (an old format, or not an
    h5 file)
    """
    try:
        with open(path, 'rb') as stream:
            head = stream.read(12)
    except OSError:
        return 'busy'
    if len(head) < 12 or head[:8] != SUPERBLOCK_SIGNATURE or head[8] not in (2, 3):
        return None
    flags = head[11]
    if flags & FLAG_SWMR_WRITING:
        return 'swmr'
    return 'writing' if flags & FLAG_WRITING else 'closed'


def open_file(path: Path, wait: float = 0.0):
    """Open the h5 file of an experiment for reading, **in SWMR mode**: the file of an experiment which is running can be read

    The header of the file tells whether an experiment is writing it (:func:`writer_state`). A file which is being written
    (or which was closed) is opened **without any lock** (:data:`UNLOCKED_MODES`): the locks of HDF5 make the writes of the
    experiment fail on Windows, and the reader must never disturb the experiment. A file which does not say is opened in
    the modes of :data:`OPEN_MODES`, the same on every system. A file which a running experiment is creating (its first
    seconds, until every group has its first data) or closing (its last moments) cannot be read: it is tried again every
    quarter of a second for `wait` seconds.

    Parameters
    ----------
    path: Path
    wait: float
        How long to try again when the file is busy, in seconds

    Raises
    ------
    FileNotFoundError
        if there is no such file
    FileBusy
        if the file is still busy after `wait` seconds
    OSError
        if the file cannot be read for another reason (it is not an h5 file...)
    """
    import h5py
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError('no such file')
    deadline = time.monotonic() + max(float(wait), 0.0)
    told = False
    while True:
        errors = []
        state = writer_state(path)
        if state in ('writing', 'busy'):  # (not in SWMR mode yet: HDF5 is not asked, as it could take a lock)
            errors.append(BlockingIOError(11, 'the file is open for writing and not in SWMR mode yet'))
        for kwargs in (UNLOCKED_MODES if state in ('swmr', 'closed') else OPEN_MODES if state is None else ()):
            try:
                return h5py.File(path, 'r', **kwargs)
            except TypeError:  # (an h5py too old to know the option)
                continue
            except OSError as e:
                errors.append(e)
        if not errors:
            raise OSError('the file cannot be opened')
        if not any(looks_busy(e) for e in errors):
            raise errors[0]  # (not the file of a running experiment: what HDF5 says is the message)
        if time.monotonic() >= deadline:
            raise FileBusy('the file cannot be read for now: a running experiment is creating it (the first seconds after '
                           'Start) or closing it (its last moments), or another program has it open for writing. Try '
                           f'again in a few seconds ({str(errors[0]).strip()})') from errors[0]
        if not told:
            told = True
            print(f'  {path.name}: a running experiment is creating or closing the file: waiting for it (up to '
                  f'{wait:g} s)', file=sys.stderr)
        time.sleep(RETRY_EVERY)


def typical_interval(units: dict[int, 'UnitData']) -> float:
    """The time between two readouts (s): the shortest of the groups which have a few rows (0 if none has)"""
    gaps = []
    for unit in units.values():
        for series in unit.series.values():
            rows = len(series)
            if rows >= 3:
                gaps.append(float(np.median(np.diff(series.time[:min(rows, 200)]))))
    return min(gaps) if gaps else 0.0


def file_status(file, units: dict[int, 'UnitData'], now: Optional[float] = None) -> tuple[str, str]:
    """Whether the file is complete, still being written or was not closed

    The writer flags the file as being written (the attribute ``swmr_active`` of the root) until the end of the experiment.

    Returns
    -------
    (state, text)
        ``complete`` (no text), ``running`` (the last data are recent) or ``abnormal`` (the file was not closed and nothing
        was written for a while: the program or the experiment stopped), with a sentence
    """
    try:
        active = bool(np.asarray(file.attrs.get('swmr_active', False)))
    except (TypeError, ValueError):
        active = False
    if not active:
        return 'complete', ''
    now = time.time() if now is None else now
    try:
        first, last = experiment_start(units), experiment_end(units)
    except ValueError:  # (nothing was saved yet)
        return 'running', 'the experiment has just started: nothing is saved yet'
    if now - last <= max(30.0, 5 * typical_interval(units)):
        return 'running', (f'the experiment is still running (the last data are from {clock_time(last)}, {max(now - last, 0):.0f} '
                           f's ago): this is what is saved so far, convert again later for more')
    return 'abnormal', (f'the file was not closed: the experiment, or the program, stopped abnormally after '
                        f'{duration_text(last - first)} (the last data are from {clock_time(last)}); everything which was '
                        f'saved is converted')


def read_series(title: str, kind: str, group) -> Series:
    """The arrays of a group of the file"""
    time = np.asarray(group['NavAxes']['Axis00'][:], dtype=float) if 'NavAxes' in group else None
    arrays, labels, units, axis = [], [], [], None
    for dim in ('Data0D', 'Data1D', 'Data2D', 'DataND'):
        if dim not in group:
            continue
        for channel in sorted(group[dim], key=natural):
            node = group[dim][channel]
            if axis is None and 'Axis00' in node and dim == 'Data1D':
                axis = np.asarray(node['Axis00'][:], dtype=float)
            for name in sorted((n for n in node if n.startswith('EnlData')), key=natural):
                arrays.append(node[name])
                labels.append(str(attribute(node[name].attrs.get('label', name))))
                units.append(str(attribute(node[name].attrs.get('units', ''))))
    if time is None:
        time = np.arange(len(arrays[0]), dtype=float) if arrays else np.zeros(0)
    attrs = {key: attribute(value) for key, value in group.attrs.items()}
    return Series(kind, title, time, arrays, labels, axis, attrs, units)


def experiment_group(file, index: int = -1):
    """The group of an experiment of the file (`index` -1: the last one) and its name"""
    root = file['RawData']
    names = sorted((name for name in root if name.startswith('Datalogger')), key=natural)
    if not names:
        raise ValueError('There is no experiment of ThermoSpectro in this file (no Datalogger group)')
    name = names[index] if index < 0 else f'Datalogger{index:03d}'
    if name not in root:
        raise ValueError(f'There is no experiment {index} in this file: it has {", ".join(names)}')
    return root[name], name


def read_description(file) -> dict[str, str]:
    """What the user wrote about the experiment (a file made before it could be written has nothing)"""
    attrs = file['RawData'].attrs
    return {key: str(attribute(attrs[key])) for key in DESCRIPTION if key in attrs}


def read_notes(group) -> tuple[np.ndarray, list[str]]:
    """The observations noted during an experiment: their time (epoch seconds) and their text (none in a file made before
    they could be noted)"""
    if NOTES not in group or 'time' not in group[NOTES] or 'text' not in group[NOTES]:
        return np.zeros(0), []
    notes = group[NOTES]
    times = np.asarray(notes['time'][:], dtype=float)
    texts = [text.decode('utf-8', errors='replace') if isinstance(text, bytes) else str(text)
             for text in notes['text'][:]]
    rows = min(len(times), len(texts))  # (the file of an experiment which is running: a row may be half written)
    return times[:rows], texts[:rows]


def read_experiment(file, index: int = -1) -> dict[int, UnitData]:
    """The units of an experiment (`index` -1: the last one of the file)"""
    experiment, name = experiment_group(file, index)
    units: dict[int, UnitData] = {}
    for group in experiment.values():
        if 'TITLE' not in group.attrs:
            continue
        title = str(attribute(group.attrs['TITLE']))
        match = TITLE.match(title)
        if match:
            number, kind = int(match.group(1)), match.group(2)
            units.setdefault(number, UnitData(number)).series[kind] = read_series(title, kind, group)
    if not units:
        raise ValueError(f'The experiment {name} has no data of ThermoSpectro (no group named Unit1_ROIs...)')
    return units


# ----------------------------------------------------------------------------------------------------------------------
# putting the data side by side
# ----------------------------------------------------------------------------------------------------------------------
def nearest(time: np.ndarray, values: np.ndarray, at: np.ndarray, interpolate: bool = False,
            max_gap: Optional[float] = None) -> np.ndarray:
    """The values at the times `at`: those of the closest sample, or interpolated

    With `max_gap`, a time which is further than that many typical intervals of `time` (the median one) from its closest
    sample gets NaN: nothing was measured around then.
    """
    if len(time) == 0:
        return np.full(len(at), np.nan)
    right = np.clip(np.searchsorted(time, at), 1, len(time) - 1) if len(time) > 1 else np.zeros(len(at), int)
    left = np.maximum(right - 1, 0)
    pick = np.where(np.abs(time[right] - at) < np.abs(time[left] - at), right, left)
    result = np.interp(at, time, values) if interpolate and len(time) > 1 else values[pick]
    if max_gap and len(time) > 1:
        interval = float(np.median(np.diff(time)))
        if interval > 0:
            result = np.where(np.abs(time[pick] - at) <= max_gap * interval, result, np.nan)
    return result


def short(label: str) -> str:
    """``ROI 1 mean (x 100-200, y 50-80 pixel)`` -> ``ROI 1 mean`` (only the last group in parentheses goes: ``Peak (main)
    (1-2 nm)`` is ``Peak (main)``)"""
    return re.sub(r'\s*\([^()]*\)\s*$', '', label)


def with_units(name: str, units: str) -> str:
    """The name of a column followed by its units in square brackets (nothing if there are none)"""
    return f'{name} [{units}]' if units else name


#: The groups which give the times of the rows of a unit: what is recorded at each readout comes first (the spectrum and
#: the image may be saved only now and then)
MASTER_KINDS = ('ROIs', 'Integration', 'Temperature', 'Program', 'Spectrum', 'Image')


def master_series(unit: UnitData) -> Series:
    """The group which gives the times of the rows of a unit"""
    for kind in MASTER_KINDS + tuple(series.kind for series in unit.crops):
        if kind in unit.series and len(unit.series[kind]):
            return unit.series[kind]
    raise ValueError(f'Nothing was recorded for the {unit.name}')


def temperature_name(unit: UnitData) -> str:
    return f'temperature ({unit.temperature_units})' if unit.temperature_units else 'temperature'


def integration_column(unit: UnitData, at: np.ndarray) -> Optional[np.ndarray]:
    """The integration time (ms) the unit was set to at the times `at`, None if it was not saved"""
    series = unit.series.get('Integration')
    if series is None or not len(series):
        return None
    return nearest(series.time[:len(series)], series.column(0), at, False, MAX_GAP)


def unit_columns(unit: UnitData, at: np.ndarray, options) -> tuple[list[str], list[np.ndarray]]:
    """The temperature, the integration time, the ROIs (and the program, if asked for) of a unit, at the times `at`"""
    names, columns = [], []

    def add(series: Series, column_names: list[str], values: list[np.ndarray]):
        for column_name, value in zip(column_names, values):
            names.append(column_name)
            columns.append(nearest(series.time[:len(value)], value, at, options.interpolate, MAX_GAP))

    if 'Temperature' in unit.series and len(unit.series['Temperature']):
        series = unit.series['Temperature']
        add(series, [temperature_name(unit)], [series.column(0)])
    integration = integration_column(unit, at)
    if integration is not None:
        names.append(f'integration time ({INTEGRATION_UNITS})')
        columns.append(integration)
    if 'ROIs' in unit.series:
        series = unit.series['ROIs']
        units = unit.value_units
        divide = getattr(options, 'per_integration_time', False)
        if divide and unit.per_integration:
            print(f'  {unit.name}: the values of the ROIs were divided by the integration time when they were measured '
                  f'({units})', file=sys.stderr)
            divide = False
        elif divide and integration is None:
            print(f'  {unit.name}: no integration time was saved: the values of the ROIs cannot be divided by it',
                  file=sys.stderr)
            divide = False
        if divide:
            units = divided(units)
        names_of_rois = [with_units(short(label) if options.short_names else label, units) for label in series.labels]
        values = [series.column(index) for index in range(len(series.arrays))]
        add(series, names_of_rois, values)
        if divide:  # (the integration time is at the times `at` already: so are the columns just added)
            columns[-len(values):] = [column / integration for column in columns[-len(values):]]
    if options.program and 'Program' in unit.series:
        series = unit.series['Program']
        add(series, ['set-point', 'cycle', 'phase code'][:len(series.arrays)],
            [series.column(index) for index in range(len(series.arrays))])
    return names, columns


def time_column(at: np.ndarray, t0: float, options) -> tuple[str, list[str]]:
    """The time of each row as text with microseconds (epoch seconds have ten digits: the 9 significant digits of the
    other columns would round them to ten seconds)"""
    name, values = ('time (epoch s)', at) if options.absolute_time else ('time (s)', at - t0)
    return name, [f'{value:.6f}' for value in values]


def experiment_start(units: dict[int, UnitData]) -> float:
    return min(float(s.time[0]) for unit in units.values() for s in unit.series.values() if len(s.time))


def experiment_end(units: dict[int, UnitData]) -> float:
    return max(float(s.time[len(s) - 1]) for unit in units.values() for s in unit.series.values() if len(s))


def cell(value) -> str:
    """A value as text: the text itself, 9 significant digits for a number, nothing for NaN (no measure there)"""
    if isinstance(value, str):
        return value
    value = float(value)
    return f'{value:.9g}' if np.isfinite(value) else ''


def write_csv(path: Path, header: Sequence[str], columns: Sequence, delimiter: str = ','):
    """A csv file with one row per sample (the columns are arrays or lists of text), readable by Excel (utf-8 with
    a signature, for the degree sign)"""
    rows = min(len(column) for column in columns)
    with open(path, 'w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.writer(stream, delimiter=delimiter)
        writer.writerow(header)
        for row in range(rows):
            writer.writerow([cell(column[row]) for column in columns])


def clock_time(epoch: float) -> str:
    """The time of the day of an instant (epoch seconds), in the time zone of this computer"""
    return datetime.datetime.fromtimestamp(epoch).strftime('%Y-%m-%d %H:%M:%S')


def duration_text(seconds: float) -> str:
    """``2:03:11`` (h:mm:ss; m:ss under an hour)"""
    seconds = int(round(max(seconds, 0.0)))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f'{hours:d}:{minutes:02d}:{seconds:02d}' if hours else f'{minutes:d}:{seconds:02d}'


def file_part(name: str) -> str:
    """A name of a ROI as a piece of the name of a file (what a file name cannot hold becomes ``_``)"""
    return re.sub(r'[^\w.+-]+', '_', name.strip()).strip('_.') or 'crop'


# ----------------------------------------------------------------------------------------------------------------------
# the notes and the report
# ----------------------------------------------------------------------------------------------------------------------
def export_notes(times: np.ndarray, texts: Sequence[str], t0: float, stem: Path, options) -> list[Path]:
    """``<file>_notes.csv``: the observations, one row each: time | clock time | observation (nothing if there are none)"""
    if not len(times):
        return []
    time_name, shown = time_column(times, t0, options)
    path = stem.with_name(f'{stem.name}_notes.csv')
    write_csv(path, [time_name, 'clock time', 'observation'], [shown, [clock_time(t) for t in times], list(texts)],
              options.delimiter)
    return [path]


def started_at(file) -> str:
    """When the file was made, as ``2026-10-22 15:30:12`` ('' if the file does not say)"""
    try:
        return f"{attribute(file.attrs['date'])} {str(attribute(file.attrs['time']))[:8]}"
    except KeyError:
        return ''


def channel_words(channels) -> str:
    """``green and blue``: the channels of an image (counted from 0) as they are called in the text"""
    names = [CHANNEL_COLORS[index] if 0 <= index < len(CHANNEL_COLORS) else f'ch{index + 1}'
             for index in sorted(int(index) for index in channels)]
    return ' and '.join(names) if len(names) < 3 else ', '.join(names[:-1]) + ' and ' + names[-1]


def which_channels(roi: dict, n_channels: int) -> str:
    """What the text says of the channels a ROI of a camera was measured on, if the images have several: ``, on the green
    and blue channels (the results are averaged)``"""
    chosen = roi.get('channels')
    if chosen:
        return f', on the {channel_words(chosen)} channel' + ('s (the results are averaged)' if len(chosen) > 1 else '')
    if n_channels > 1:
        return f', on all {n_channels} channels (the results are averaged)'
    return ''


def roi_lines(unit: UnitData, files: dict[str, str]) -> list[str]:
    """What each ROI of a unit is, one line each (`files`: the tif file of each crop, by the name of the ROI)"""
    series = unit.series.get('ROIs')
    lines = []
    try:
        recorded = json.loads(series.attrs['rois']) if series is not None and 'rois' in series.attrs else []
    except (TypeError, ValueError):
        recorded = []
    try:
        n_channels = int(series.attrs.get('n_channels', 0)) if series is not None else 0
    except (TypeError, ValueError):
        n_channels = 0
    for roi in recorded:
        name = roi.get('name', '')
        what = AGGREGATION_WORDS.get(roi.get('aggregation', 'mean'), 'mean')
        if 'x0' in roi:
            lines.append(f'    {name}: {what} of the pixels in x {roi["x0"]:g}-{roi["x1"]:g}, y {roi["y0"]:g}-{roi["y1"]:g} '
                         f'pixel{which_channels(roi, n_channels)}')
        else:
            lines.append(f'    {name}: {what} of the pixels in {roi.get("start", 0):g}-{roi.get("stop", 0):g} nm')
    for crop in unit.crops:
        roi = roi_of_crop(crop)
        name = str(roi.get('name', crop.kind))
        if 'x0' in roi:
            where = f'the pixels in x {roi["x0"]:g}-{roi["x1"]:g}, y {roi["y0"]:g}-{roi["y1"]:g} pixel'
        elif 'start' in roi:
            where = f'the spectrum in {roi["start"]:g}-{roi["stop"]:g} nm'
        else:
            where = 'the pixels'
        target = f' -> {files[name]}' if name in files else ''
        lines.append(f'    {name}: crop of {where}{target}')
    return lines


def unit_lines(unit: UnitData, files: dict[str, str]) -> list[str]:
    """The lines of the report about a unit: its instruments, its times, its units, what was saved"""
    module = unit.attr('camera' if unit.is_camera else 'spectrometer', '')
    thermostat = unit.attr('thermostat', '')
    lines = [f'{unit.name.capitalize()}: {module}' + (f', thermal controller {thermostat}' if thermostat else '')]
    integration = unit.series.get('Integration')
    what = 'exposure' if unit.is_camera else 'integration'
    if integration is not None and len(integration):
        values = integration.column(0)
        low, high = float(np.nanmin(values)), float(np.nanmax(values))
        lines.append(f'  {what} time: {low:g} ms' if low == high else
                     f'  {what} time: from {low:g} to {high:g} ms (it was changed during the experiment; saved with each '
                     f'readout)')
    elif unit.attr('integration_time_ms', ''):
        lines.append(f'  {what} time: {float(unit.attr("integration_time_ms")):g} ms (at the start)')
    if unit.attr('readout_time_s', ''):
        lines.append(f'  readout time: {float(unit.attr("readout_time_s")):g} s')
    if unit.intensity_units:
        lines.append(f'  intensity unit: {unit.intensity_units}')
    if 'ROIs' in unit.series or unit.crops:
        divide = ' (divided by the integration time in the extension)' if unit.per_integration else ''
        lines.append(f'  ROIs, values in {unit.value_units}{divide}:' if 'ROIs' in unit.series else '  ROIs:')
        lines += roi_lines(unit, files)
    for kind, label in (('Spectrum', 'full spectra'), ('Image', 'full images')):
        if kind in unit.series and was_divided(unit.series[kind]):
            lines.append(f'  the {label} were divided by the integration time in the extension '
                         f'({series_units(unit.series[kind], unit.intensity_units)})')
    saved = []
    for kind, label in (('ROIs', 'ROIs'), ('Temperature', 'temperature'), ('Integration', f'{what} time')):
        if kind in unit.series and len(unit.series[kind]):
            saved.append(f'{len(unit.series[kind])} readouts of the {label}')
    if 'Spectrum' in unit.series and len(unit.series['Spectrum']):
        saved.append(f'{len(unit.series["Spectrum"])} full spectra')
    if 'Image' in unit.series and len(unit.series['Image']):
        saved.append(f'{len(unit.series["Image"])} full images')
    for crop in unit.crops:
        saved.append(f'{len(crop)} crops')
    if saved:
        lines.append('  saved: ' + ', '.join(saved))
    return lines


def report_text(file, units: dict[int, UnitData], description: dict[str, str], times: np.ndarray, texts: Sequence[str],
                t0: float, written: Sequence[tuple[Path, str]], crop_files: dict[int, dict[str, str]],
                status: str = '') -> str:
    """What the user wrote about the experiment, the observations, the instruments, the units and the files, as a text"""
    said = {key: description.get(key, '').strip() for key in DESCRIPTION}
    lines = ['ThermoSpectro experiment', '========================']
    lines += [f'{label}: {said[key]}' for label, key in (('Experiment', 'experiment_title'), ('Sample', 'sample'),
                                                         ('Operator', 'operator'), ('Application', 'application'))
              if said[key]]
    started = started_at(file)
    if started:
        lines.append(f'Started: {started}')
    try:
        lines.append(f'Duration: {duration_text(experiment_end(units) - t0)}')
    except ValueError:
        pass
    lines.append(f'File: {Path(file.filename).name}')
    if status:  # (the experiment is running, or did not end properly: the data may stop before the end of the experiment)
        lines.append(f'Status: {status}')
    lines.append(f'Exported: {clock_time(datetime.datetime.now().timestamp())}')
    if said['protocol']:
        lines += ['', 'Protocol', '--------', said['protocol']]
    if len(times):
        lines += ['', 'Observations', '------------', 'from the start   clock time            observation']
        lines += [f'{duration_text(t - t0):>14}   {clock_time(t)}   {text}' for t, text in zip(times, texts)]
    lines += ['', 'Instruments and data', '--------------------']
    for number in sorted(units):
        lines += unit_lines(units[number], crop_files.get(number, {}))
    thermostats = {unit.temperature_units for unit in units.values() if unit.temperature_units}
    lines += ['', 'Units', '-----', 'time: s (from the first measurement; --absolute-time: epoch seconds)']
    if thermostats:
        lines.append(f'temperature: {", ".join(sorted(thermostats))}')
    if any('Integration' in unit.series for unit in units.values()):
        lines.append(f'integration (exposure) time: {INTEGRATION_UNITS}')
    for number in sorted(units):
        unit = units[number]
        if unit.series.get('Spectrum') is not None and unit.series['Spectrum'].axis is not None:
            lines.append(f'wavelength: nm ({unit.name})')
        if 'ROIs' in unit.series or unit.crops or 'Spectrum' in unit.series or 'Image' in unit.series:
            held = [f'values of the ROIs: {unit.value_units}'] if 'ROIs' in unit.series else []
            for kind, label in (('Spectrum', 'full spectra'), ('Image', 'full images')):
                if kind in unit.series:
                    held.append(f'{label}: {series_units(unit.series[kind], unit.intensity_units)}')
            if unit.crops and not unit.is_camera:
                held.append(f'crops: {series_units(unit.crops[0], unit.intensity_units)}')
            lines.append(f'intensity ({unit.name}): {unit.intensity_units or "not given"}'
                         + (f'; {"; ".join(held)}' if held else ''))
    if written:
        lines += ['', 'Files', '-----']
        width = max(len(path.name) for path, _ in written)
        lines += [f'{path.name.ljust(width)}  {what}' for path, what in written]
    return '\n'.join(lines) + '\n'


# ----------------------------------------------------------------------------------------------------------------------
# a spectrometer: the values, and the full spectra
# ----------------------------------------------------------------------------------------------------------------------
def export_values(unit: UnitData, t0: float, stem: Path, options) -> list[tuple[Path, str]]:
    """``<file>_spectrometer1.csv``: time | temperature | integration time | ROIs (one row per readout)"""
    master = master_series(unit)
    at = master.time[:len(master)]
    names, columns = unit_columns(unit, at, options)
    if not names:  # (only the full spectra were saved: they have their own file)
        return []
    time_name, times = time_column(at, t0, options)
    path = stem.with_name(f'{stem.name}_{unit.kind}{unit.number}.csv')
    write_csv(path, [time_name] + names, [times] + columns, options.delimiter)
    return [(path, f'{unit.name}: one row per readout, with the time, the temperature, the integration time and the ROIs')]


class SpectraRows:
    """The spectra of a unit read from the file a block of rows at a time while the csv is written (the spectra of a log
    of days do not fit in memory), divided by the integration time of each if asked"""
    BLOCK = 256

    def __init__(self, dataset, divisor: Optional[np.ndarray] = None):
        self.dataset, self.divisor = dataset, divisor

    def __len__(self) -> int:
        return len(self.dataset)

    def blocks(self) -> Iterator[tuple[int, np.ndarray]]:
        for start in range(0, len(self.dataset), self.BLOCK):
            block = np.asarray(self.dataset[start:start + self.BLOCK], dtype=float)
            if self.divisor is not None:
                block = block / self.divisor[start:start + len(block), None]
            yield start, block


def spectral_csv(unit: UnitData, series: Series, array: int, path: Path, what: str, t0: float, options
                 ) -> Optional[tuple[Path, str]]:
    """A csv with one row per spectrum (or part of a spectrum) of a group: its time, the temperature, the integration time
    and one column per wavelength

    The group may hold what was divided by the integration time in the extension, or not: ``--per-integration-time`` divides
    what was not, with the integration time of each row.
    """
    at = series.time[:len(series)]
    time_name, times = time_column(at, t0, options)
    context_names, context = [time_name], [times]
    if 'Temperature' in unit.series and len(unit.series['Temperature']):
        temperature = unit.series['Temperature']
        context_names.append(temperature_name(unit))
        context.append(nearest(temperature.time[:len(temperature)], temperature.column(0), at, False, MAX_GAP))
    integration = integration_column(unit, at)
    if integration is not None:
        context_names.append(f'integration time ({INTEGRATION_UNITS})')
        context.append(integration)
    units = series_units(series, unit.intensity_units)
    divide = getattr(options, 'per_integration_time', False)
    if divide and was_divided(series):
        print(f'  {unit.name}: {what} were divided by the integration time when they were measured ({units})',
              file=sys.stderr)
        divide = False
    elif divide and integration is None:
        print(f'  {unit.name}: no integration time was saved: {what} cannot be divided by it', file=sys.stderr)
        divide = False
    if divide:
        units = divided(units)
    wavelengths = series.axis
    n_pixels = series.arrays[array].shape[1]
    header = context_names + [f'{wavelengths[pixel]:.3f} nm' if wavelengths is not None and pixel < len(wavelengths)
                              else f'pixel {pixel}' for pixel in range(n_pixels)]
    reader = SpectraRows(series.arrays[array][:len(series)], integration if divide else None)
    with open(path, 'w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.writer(stream, delimiter=options.delimiter)
        writer.writerow(header)
        for start, block in reader.blocks():
            for row, intensities in enumerate(block):
                writer.writerow([cell(column[start + row]) for column in context]
                                + [f'{value:.9g}' if np.isfinite(value) else '' for value in intensities])
    return path, f'{unit.name}: {what}, one row each (intensities in {units or "?"}, one column per wavelength)'


def export_spectra(unit: UnitData, t0: float, stem: Path, options) -> list[tuple[Path, str]]:
    """``<file>_spectrometer1_spectra.csv``: one row per full spectrum saved, with its time, temperature and integration
    time, and one column per wavelength"""
    spectra = unit.series.get('Spectrum')
    if spectra is None or not len(spectra) or options.no_spectra:
        return []
    written = []
    for index in range(len(spectra.arrays)):
        suffix = '' if index == 0 else str(index + 1)
        path = stem.with_name(f'{stem.name}_{unit.kind}{unit.number}_spectra{suffix}.csv')
        written.append(spectral_csv(unit, spectra, index, path, 'the full spectra', t0, options))
    return written


def roi_of_crop(crop: Series) -> dict:
    """The ROI of a group of a crop, as the group says it (its name, its window...)"""
    try:
        roi = json.loads(crop.attrs['roi'])
    except (KeyError, TypeError, ValueError):
        return {}
    return roi if isinstance(roi, dict) else {}


def export_spectrum_crops(unit: UnitData, t0: float, stem: Path, options) -> tuple[list[tuple[Path, str]], dict[str, str]]:
    """``<file>_spectrometer1_crop_<name>.csv`` for each ROI which is a crop: one row per measurement (or every *Crops every*
    seconds), with its time, temperature and integration time, and one column per wavelength of its window

    Returns
    -------
    (written, files): the files and what they are; the file of each crop by the name of its ROI
    """
    written, files = [], {}
    for crop in unit.crops:
        if not len(crop):
            continue
        name = str(roi_of_crop(crop).get('name', crop.kind))
        path = stem.with_name(f'{stem.name}_{unit.kind}{unit.number}_crop_{file_part(name)}.csv')
        for array in range(len(crop.arrays)):
            target = path if array == 0 else path.with_name(f'{path.stem}{array + 1}.csv')
            written.append(spectral_csv(unit, crop, array, target, f'the part of the spectrum of the crop {name}', t0,
                                        options))
        files[name] = path.name
    return written, files


# ----------------------------------------------------------------------------------------------------------------------
# the camera: tif files and a csv
# ----------------------------------------------------------------------------------------------------------------------
def imagej_dtype(arrays) -> np.dtype:
    """The type of the pixels in the tif: ImageJ reads 8 and 16 bits integers and 32 bits floats"""
    dtype = arrays[0].dtype
    if dtype in IMAGEJ_TYPES:
        return np.dtype(dtype)
    if np.issubdtype(dtype, np.integer):
        limits = [min_max(array) for array in arrays if len(array)]
        if limits and min(low for low, _ in limits) >= 0 and max(high for _, high in limits) <= 65535:
            return np.dtype(np.uint16)
    return np.dtype(np.float32)


def min_max(array, block_bytes: int = 2 ** 26) -> tuple[float, float]:
    """The smallest and the largest value of a dataset of images, read a few images at a time (a series of images may be
    much bigger than the memory)"""
    step = max(1, block_bytes // max(1, np.asarray(array[0]).nbytes))
    low, high = np.inf, -np.inf
    for start in range(0, len(array), step):
        block = np.asarray(array[start:start + step])
        low, high = min(low, float(block.min())), max(high, float(block.max()))
    return low, high


def channel_labels(n_channels: int) -> list[str]:
    return ['R', 'G', 'B'][:n_channels] if n_channels == 3 else [f'channel {index + 1}' for index in range(n_channels)]


@dataclass
class TifExtras:
    """What the tif files say besides the pixels: the size of a pixel, the camera, when the images were taken"""
    pixel_um: Optional[tuple[float, float]] = None  # the size of a pixel of the image (along x, along y), in um
    make: str = ''
    model: str = ''
    bit_depth: int = 0  # of the camera: the display range of the images is 0 to 2^bit_depth - 1
    start: Optional[float] = None  # epoch seconds of the first image of the experiment
    exposure_of: Optional[Callable[[np.ndarray], Optional[np.ndarray]]] = None  # epoch seconds -> exposure time (ms)


def pixel_size(camera: UnitData, options) -> tuple[Optional[tuple[float, float]], str]:
    """The size of a pixel of the images of the camera (um, along x and along y) and how it is known

    In this order: ``--pixel-size`` (what the user measured on the sample, the pixel of the image as it is, binning
    included); else the pitch of the sensor the camera said, times its binning, divided by ``--magnification`` (1 by
    default: **the sensor plane**, the size of a pixel on the sample is this divided by the magnification of the optics,
    which the camera cannot know); else nothing, and the tif is in pixels.
    """
    given = getattr(options, 'pixel_size', None)
    if given:
        return (float(given), float(given)), f'{given:g} um per pixel, given with --pixel-size'
    info = camera.camera_info
    sensor = effective_pixel_um(info)
    if sensor is None:
        return None, ''
    magnification = getattr(options, 'magnification', None) or 1.0
    size = (sensor[0] / magnification, sensor[1] / magnification)
    where = {'camera': 'said by the camera', 'table': 'from the table of the models of Thorlabs'}.get(
        str(info.get('pixel_source', '')), 'of the camera')
    if magnification == 1.0:
        how = (f'{size[0]:.4g} um per pixel: the pitch of the sensor ({where}) times the binning; this is the SENSOR PLANE, '
               f'give the magnification of the optics (--magnification) or the size of a pixel on the sample '
               f'(--pixel-size) for the sample plane')
    else:
        how = f'{size[0]:.4g} um per pixel: the pitch of the sensor ({where}) times the binning, divided by the ' \
              f'magnification {magnification:g}'
    return size, how


def effective_pixel_um(info: dict) -> Optional[tuple[float, float]]:
    """The size of a pixel of the image on the sensor: the pitch of the sensor times the binning (None: not known)"""
    pitch, binning = info.get('pixel_um'), info.get('binning') or (1, 1)
    try:
        return float(pitch[0]) * max(1, int(binning[0])), float(pitch[1]) * max(1, int(binning[1]))
    except (TypeError, ValueError, IndexError):
        return None


def camera_sentence(camera: UnitData, how: str) -> str:
    """What the camera said about itself, in a few words, for the description of the tif"""
    info = camera.camera_info
    if not info:
        return ''
    parts = []
    name = ' '.join(str(info[key]) for key in ('manufacturer', 'model') if info.get(key))
    if name or info.get('serial'):
        parts.append(f'camera {name}'.strip() + (f' (serial {info["serial"]})' if info.get('serial') else ''))
    if info.get('pixel_um'):
        parts.append(f'sensor pixels {info["pixel_um"][0]:g} x {info["pixel_um"][1]:g} um')
    if info.get('bit_depth'):
        parts.append(f'{info["bit_depth"]} bits')
    if info.get('sensor_type'):
        parts.append(f'sensor {info["sensor_type"]}')
    binning = info.get('binning') or (1, 1)
    if tuple(binning) != (1, 1):
        parts.append(f'binning {binning[0]} x {binning[1]}')
    if info.get('window'):
        x0, x1, y0, y1 = info['window']
        parts.append(f'sensor window x {x0}-{x1}, y {y0}-{y1} pixel')
    if how:
        parts.append(f'calibration {how}')
    return '; '.join(parts) + '.'


def camera_extras(camera: UnitData, t0: float, options, exposure_of=None) -> TifExtras:
    size, _ = pixel_size(camera, options)
    info = camera.camera_info
    return TifExtras(pixel_um=size, make=str(info.get('manufacturer', '') or ''), model=str(info.get('model', '') or ''),
                     bit_depth=int(info.get('bit_depth', 0) or 0), start=t0, exposure_of=exposure_of)


def ascii_text(text: str) -> str:
    """ImageJ reads the description and the labels of a tif as 7 bits ASCII (tifffile refuses the rest)"""
    return str(text).encode('ascii', errors='replace').decode('ascii')


def tif_name(stem: Path, what: str) -> Path:
    """``<file>_camera.tif`` (`what` is ``camera``, or ``camera_crop_<name>`` for a crop)"""
    return stem.with_name(f'{stem.name}_{what}.tif')


def write_tif(path: Path, images: Series, times: np.ndarray, temperature: Optional[np.ndarray], info: str,
              frames: Optional[range] = None, dtype: Optional[np.dtype] = None, extras: Optional[TifExtras] = None,
              exposure: Optional[np.ndarray] = None) -> bool:
    """The images (the frames `frames`, all by default) in an ImageJ hyperstack: axes Time, Channel, Y (rows), X (columns)

    A series of more than :data:`CLASSIC_TIF_BYTES` is one BigTIFF file, whatever its size; returns whether it is one.

    What the file says, besides the pixels: the frame interval and, in the label of each image, its time, the temperature
    and the exposure time; the colors (the lookup tables of the red, green and blue channels); with `extras` the size of a
    pixel (the calibration of ImageJ, in um), the display range, the camera (make and model), the software and the date
    of the first image; and the description: the camera, the thermostat, the exposure and the readout times.
    """
    try:
        import tifffile
    except ImportError:
        raise SystemExit('The tif files need tifffile: python -m pip install tifffile') from None
    extras = extras or TifExtras()
    n_channels = len(images.arrays)
    frames = range(len(images)) if frames is None else frames
    n_frames = len(frames)
    shape = tuple(images.arrays[0].shape[1:])
    if len(shape) != 2:
        raise ValueError(f'{images.title} holds {len(shape)}D data, not images')
    dtype = imagej_dtype(images.arrays) if dtype is None else dtype
    step = float(np.median(np.diff(times[frames.start:frames.stop]))) if n_frames > 1 else 0.0
    labels = []
    for frame in frames:
        for channel in range(n_channels):
            text = f't={times[frame]:.3f} s'
            if temperature is not None and np.isfinite(temperature[frame]):
                text += f', T={temperature[frame]:.3f}'
            if exposure is not None and np.isfinite(exposure[frame]):
                text += f', exp={exposure[frame]:.3f} ms'
            labels.append(f'{text}, {channel_labels(n_channels)[channel]}')
    metadata = {'axes': 'TCYX', 'finterval': step, 'Info': ascii_text(info), 'Labels': [ascii_text(x) for x in labels],
                'unit': 'pixel'}
    arguments: dict = {'software': 'lab_export (ThermoSpectro)'}
    if extras.pixel_um:  # the calibration of ImageJ: pixels per um along x and y, and the unit
        metadata['unit'] = 'um'
        arguments['resolution'] = (1.0 / extras.pixel_um[0], 1.0 / extras.pixel_um[1])
    if extras.bit_depth and dtype in (np.uint8, np.uint16) and extras.bit_depth <= 8 * np.dtype(dtype).itemsize:
        metadata['Ranges'] = (0.0, float(2 ** extras.bit_depth - 1)) * n_channels  # (as the camera made them)
    if extras.start is not None and n_frames:
        arguments['datetime'] = datetime.datetime.fromtimestamp(extras.start + float(times[frames.start]))
    tags = [(code, 's', 0, ascii_text(text), True) for code, text in ((271, extras.make), (272, extras.model)) if text]
    if tags:
        arguments['extratags'] = tags
    if n_channels == 3:  # shown as the colors of an image
        metadata['mode'] = 'composite'
        ramp = np.arange(256, dtype=np.uint8)
        metadata['LUTs'] = [np.stack([ramp if color == channel else np.zeros(256, np.uint8) for color in range(3)])
                            for channel in range(3)]

    def pages() -> Iterator[np.ndarray]:
        for frame in frames:
            for array in images.arrays:
                yield np.asarray(array[frame]).astype(dtype, copy=False)

    big = n_frames * n_channels * int(np.prod(shape)) * np.dtype(dtype).itemsize > CLASSIC_TIF_BYTES
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', message='.*nonconformant BigTIFF ImageJ')  # (said in the text of the experiment)
        with tifffile.TiffWriter(path, imagej=True, bigtiff=big) as writer:
            writer.write(pages(), shape=(n_frames, n_channels) + shape, dtype=dtype, metadata=metadata, **arguments)
    return big


def find_temperature(camera: UnitData, units: dict[int, UnitData], at: np.ndarray, options
                     ) -> Optional[tuple[str, np.ndarray]]:
    """The temperature at the times `at` and its column name: the one of the camera if it has one, else the one of a
    spectrometer (None if there is none)"""
    candidates = [camera] + [units[number] for number in sorted(units) if not units[number].is_camera]
    for unit in candidates:
        series = unit.series.get('Temperature')
        if series is not None and len(series):
            return temperature_name(unit), nearest(series.time[:len(series)], series.column(0), at, options.interpolate,
                                                   MAX_GAP)
    return None


def write_images(series: Series, what: str, at_zero: float, temperature_of, stem: Path, info: str, options,
                 extras: Optional[TifExtras] = None) -> tuple[Path, bool]:
    """The tif file of a series of images, and whether it is a BigTIFF (more than 4 GB of pixels)"""
    dtype = imagej_dtype(series.arrays)
    path = tif_name(stem, what)
    times = series.time[:len(series)]
    temperature = temperature_of(times)
    exposure = extras.exposure_of(times) if extras is not None and extras.exposure_of is not None else None
    return path, write_tif(path, series, times - at_zero, temperature, info, None, dtype, extras, exposure)


def export_camera(camera: UnitData, units: dict[int, UnitData], t0: float, stem: Path, options
                  ) -> tuple[list[tuple[Path, str]], dict[str, str]]:
    """The tif of the images, the tif of each crop, and ``<file>_camera.csv`` (one row per readout, with the number of the
    image in the tif where there is one)

    Returns
    -------
    (written, crops): the files and what they are; the tif file of each crop by the name of its ROI
    """
    written: list[tuple[Path, str]] = []
    images = camera.series.get('Image')
    master = master_series(camera)
    at = master.time[:len(master)]
    names, columns = unit_columns(camera, at, options)
    if not any(name.startswith('temperature') for name in names):  # (a camera which shares the thermostat of a spectrometer)
        found = find_temperature(camera, units, at, options)
        if found is not None:
            names.insert(0, found[0])
            columns.insert(0, found[1])
    if options.with_spectrometers:  # (the values of the spectrometers at the readouts of the camera)
        for number in sorted(units):
            if units[number].is_camera:
                continue
            other_names, other_columns = unit_columns(units[number], at, options)
            for name, column in zip(other_names, other_columns):
                if not name.startswith('temperature') or not any(own.startswith('temperature') for own in names):
                    names.append(f'spectrometer {number} {name}')
                    columns.append(column)

    def temperature_of(times: np.ndarray) -> Optional[np.ndarray]:
        found = find_temperature(camera, units, times, options)
        return None if found is None else found[1]

    def exposure_of(times: np.ndarray) -> Optional[np.ndarray]:  # (the exposure time of each image, if it was recorded)
        series = camera.series.get('Integration')
        if series is None or not len(series):
            return None
        return nearest(series.time[:len(series)], series.column(0), times, False, MAX_GAP)

    extras = camera_extras(camera, t0, options, exposure_of)
    about_camera = camera_sentence(camera, pixel_size(camera, options)[1])
    if camera.camera_info and extras.pixel_um is None and not options.no_tif:
        print('  the camera did not say the size of its pixels: the tif files are in pixels (--pixel-size UM gives it)',
              file=sys.stderr)
    tif = None
    if images is None and not camera.crops:
        print('  the images of the camera were not recorded (tick "The full image" under "Record to disk" of the '
              'camera, or make a ROI a crop): no tif file', file=sys.stderr)
    if images is not None and len(images) and not options.no_tif:
        info = (f'ThermoSpectro: camera {camera.attr("camera", "")}, thermostat {camera.attr("thermostat", "")}, '
                f'exposure {camera.attr("integration_time_ms", "")} ms (at the start), readout '
                f'{camera.attr("readout_time_s", "")} s, {len(images)} images, pixels in {camera.intensity_units or "?"}. '
                f'The frames of the tif are the images of the csv file of the same name. {about_camera}')
        tif, big = write_images(images, 'camera', t0, temperature_of, stem, info, options, extras)
        written.append((tif, f'camera: the full images, axes T, C, Y, X ({len(images)} frames{BIGTIFF_WORDS if big else ""})'))

    crop_files: dict[str, str] = {}
    if not options.no_tif:
        for crop in camera.crops:
            if not len(crop):
                continue
            roi = roi_of_crop(crop)
            name = str(roi.get('name', crop.kind))
            info = (f'ThermoSpectro: crop {name} of the camera {camera.attr("camera", "")}, '
                    f'window x {roi.get("x0", "?")}-{roi.get("x1", "?")}, y {roi.get("y0", "?")}-{roi.get("y1", "?")} '
                    f'pixel, {len(crop)} images, pixels in {camera.intensity_units or "?"}. {about_camera}')
            path, big = write_images(crop, f'camera_crop_{file_part(name)}', t0, temperature_of, stem, info, options,
                                     extras)
            crop_files[name] = path.name
            written.append((path, f'camera: the pixels of the crop {name}, axes T, C, Y, X ({len(crop)} frames'
                                  f'{BIGTIFF_WORDS if big else ""})'))

    if images is not None and len(images) and tif is not None:  # (which frame of the tif holds the image of each readout)
        rows, valid = matching_rows(images.time[:len(images)], at)
        names.append('tif frame')
        columns.append(WithGaps(np.where(valid, rows + 1, 0).astype(float), valid))
    if names:
        time_name, times = time_column(at, t0, options)
        path = stem.with_name(f'{stem.name}_camera.csv')
        write_csv(path, [time_name] + names, [times] + columns, options.delimiter)
        written.insert(0, (path, 'camera: one row per readout, with the time, the temperature, the exposure time and the '
                                 'ROIs which give a number'))
    return written, crop_files


class WithGaps:
    """A column of a csv file where some rows have nothing (empty cells): the whole image of a long log is saved only now
    and then"""

    def __init__(self, values: np.ndarray, valid: np.ndarray):
        self.values, self.valid = values, valid

    def __len__(self) -> int:
        return len(self.valid)

    def __getitem__(self, row: int):
        return float(self.values[row]) if self.valid[row] else ''


def matching_rows(time: np.ndarray, at: np.ndarray, tolerance: float = 1e-3) -> tuple[np.ndarray, np.ndarray]:
    """For each time of `at`, the index of the sample of `time` taken at that time, and whether there is one

    The groups of a unit are stamped with the time of the readout they come from: a sample which is not at the time of
    a readout (within `tolerance`, s) does not belong to it.
    """
    rows = nearest_index(time, at) if len(time) else np.zeros(len(at), dtype=int)
    valid = np.abs(time[rows] - at) <= tolerance if len(time) else np.zeros(len(at), dtype=bool)
    return rows, valid


def nearest_index(time: np.ndarray, at: np.ndarray) -> np.ndarray:
    """For each time of `at`, the index of the closest sample"""
    return nearest(time, np.arange(len(time), dtype=float), at).astype(int)


# ----------------------------------------------------------------------------------------------------------------------
# the command
# ----------------------------------------------------------------------------------------------------------------------
def describe(units: dict[int, UnitData], description: Optional[dict[str, str]] = None, n_notes: int = 0,
             status: str = '') -> str:
    lines = [f'  {status}'] if status else []
    said = [f'{label} {description[key]}' for label, key in (('experiment', 'experiment_title'), ('sample', 'sample'),
                                                             ('operator', 'operator')) if (description or {}).get(key)]
    if said:
        lines.append('  ' + ', '.join(said))
    if (description or {}).get('protocol'):
        lines.append(f'  a protocol of {len(description["protocol"])} characters')
    if n_notes:
        lines.append(f'  {n_notes} observation{"" if n_notes == 1 else "s"}')
    for number in sorted(units):
        unit = units[number]
        what = ', '.join(f'{kind} ({len(series)} rows)' for kind, series in unit.series.items())
        lines.append(f'  unit {number}: {unit.name} {unit.attr("camera" if unit.is_camera else "spectrometer", "")} '
                     f'with {unit.attr("thermostat", "") or "no thermostat"}: {what}')
        if 'ROIs' in unit.series and unit.value_units:
            lines.append(f'    the values of the ROIs are in {unit.value_units}')
    return '\n'.join(lines)


def export_file(path: Path, out: Optional[Path], options) -> list[Path]:
    """Convert one file, returns the files which were written"""
    path = Path(path)
    folder = Path(out) if out else path.parent
    folder.mkdir(parents=True, exist_ok=True)
    stem = folder / path.stem
    written: list[tuple[Path, str]] = []
    crop_files: dict[int, dict[str, str]] = {}
    with open_file(path, getattr(options, 'wait', 0.0)) as file:
        units = read_experiment(file, options.experiment)
        description = read_description(file)
        times, texts = read_notes(experiment_group(file, options.experiment)[0])
        status = file_status(file, units)[1]
        print(f'{path}:\n{describe(units, description, len(texts), status)}')
        if options.list:
            return []
        t0 = experiment_start(units)
        for number in sorted(units):
            unit = units[number]
            try:
                if unit.is_camera:
                    files, crops = export_camera(unit, units, t0, stem, options)
                    written += files
                    crop_files[number] = crops
                else:
                    written += export_values(unit, t0, stem, options)
                    written += export_spectra(unit, t0, stem, options)
                    files, crops = export_spectrum_crops(unit, t0, stem, options)
                    written += files
                    crop_files[number] = crops
            except ValueError as e:
                print(f'  {unit.name}: {e}', file=sys.stderr)
        for notes in export_notes(times, texts, t0, stem, options):
            written.append((notes, 'the observations noted during the experiment, with their time'))
        report = stem.with_name(f'{stem.name}_experiment.txt')
        written.append((report, 'this text: what was written about the experiment, the instruments and the units'))
        report.write_text(report_text(file, units, description, times, texts, t0, written, crop_files, status),
                          encoding='utf-8-sig')
    return [path for path, _ in written]


def positive(text: str) -> float:
    """A number of the command line which is more than 0"""
    value = float(text)
    if not value > 0:
        raise argparse.ArgumentTypeError(f'{text} is not more than 0')
    return value


def parser() -> argparse.ArgumentParser:
    arguments = argparse.ArgumentParser(
        prog='lab_export',
        description='Convert the h5 file of an experiment of the lab into csv files (the values of each spectrometer and '
                    'of the ROIs of the camera, the full spectra, the crops of the spectra), tif files (the images and '
                    'the crops of the camera, axes T, C, Y, X) and a text with the protocol and the observations.',
        epilog=__doc__.split('\n\n')[0])
    arguments.add_argument('files', nargs='+', type=Path, help='the h5 file(s) written by the extension')
    arguments.add_argument('-o', '--out', type=Path, help='the folder of the new files (default: the one of the h5 file)')
    arguments.add_argument('--experiment', type=int, default=-1, metavar='N',
                           help='the experiment of the file, 0 is the first (default: the last one)')
    arguments.add_argument('--list', action='store_true', help='only tell what the files hold')
    arguments.add_argument('--short-names', action='store_true',
                           help='name the columns of the ROIs "ROI 1 [A.U.]" instead of "ROI 1 (454.2-464.6 nm) '
                                '[A.U.]"')
    arguments.add_argument('--per-integration-time', action='store_true',
                           help='divide the values of the ROIs and the spectra which were saved as they were measured '
                                'by the integration time of each readout (A.U./ms)')
    arguments.add_argument('--wait', type=float, default=WAIT, metavar='SECONDS',
                           help='how long to wait for the file of a running experiment which is being created (its first '
                                f'seconds) or closed (its last moments), default {WAIT:g}; 0: do not wait')
    arguments.add_argument('--no-spectra', action='store_true', help='do not write the csv file of the full spectra')
    arguments.add_argument('--no-tif', action='store_true', help='do not write the tif files (the csv files only)')
    arguments.add_argument('--magnification', type=positive, metavar='M',
                           help='the magnification of the optics between the sample and the camera: a pixel of the tif is '
                                'the pitch of the sensor (times the binning) divided by this, in um (default 1: the '
                                'sensor plane)')
    arguments.add_argument('--pixel-size', type=positive, metavar='UM',
                           help='the size of a pixel of the images on the sample, in um, as measured (binning included): '
                                'the calibration of the tif files, instead of what the camera said')
    arguments.add_argument('--absolute-time', action='store_true', help='the time in epoch seconds, not from the start')
    arguments.add_argument('--program', action='store_true',
                           help='add the set-point, the cycle and the phase code of the temperature ramp')
    arguments.add_argument('--with-spectrometers', action='store_true',
                           help='in the csv of the camera, add the temperature and the ROIs of the spectrometers, at the '
                                'readouts of the camera (the one of the closest readout, or --interpolate)')
    arguments.add_argument('--interpolate', action='store_true',
                           help='with --with-spectrometers, interpolate the other detectors at the time of the readout '
                                'of the camera instead of taking the closest one')
    arguments.add_argument('--delimiter', default=',', help='the delimiter of the csv files (default: a comma)')
    return arguments


def expand(paths: Sequence[Path]) -> tuple[list[Path], bool]:
    """The files named on the command line: a name with a wildcard (``*.h5``) which is not a file is expanded here, as
    Windows does not do it

    Returns
    -------
    (files, complete): the files, and False if a name with a wildcard matched nothing
    """
    files, complete = [], True
    for path in paths:
        if not path.exists() and any(character in str(path) for character in '*?['):
            matches = sorted(glob.glob(str(path)))
            if not matches:
                print(f'{path}: no file of this name', file=sys.stderr)
                complete = False
            files += [Path(match) for match in matches]
        else:
            files.append(path)
    return files, complete


def main(argv: Optional[Sequence[str]] = None) -> int:
    options = parser().parse_args(argv)
    files, complete = expand(options.files)
    status = 0 if complete else 1
    for path in files:
        try:
            written = export_file(path, options.out, options)
        except (OSError, ValueError, KeyError) as e:
            print(f'{path}: {e}', file=sys.stderr)
            status = 1
            continue
        for file in written:
            print(f'  written {file}')
    return status


if __name__ == '__main__':
    sys.exit(main())
