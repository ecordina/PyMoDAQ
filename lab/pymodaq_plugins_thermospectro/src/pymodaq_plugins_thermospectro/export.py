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
  the same kind of tif. ImageJ cannot open files of more than 4 GB (BigTIFF): a longer series is split in
  ``..._part01.tif``, ``..._part02.tif``... and the csv file has the columns ``tif file`` and ``tif frame``;
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional, Sequence

import numpy as np

TITLE = re.compile(r'^Unit(\d+)_(Spectrum|Image|ROIs|Integration|Temperature|Program|Crop\d+)$')
#: What the user wrote about the experiment: the attributes of the group RawData (and the application which made the file)
DESCRIPTION = ('experiment_title', 'sample', 'operator', 'protocol', 'application')
NOTES = 'Notes'  # the group of the observations, in the group of the experiment
IMAGEJ_TYPES = (np.uint8, np.uint16, np.float32)
CHANNEL_COLORS = ('red', 'green', 'blue')
#: ImageJ does not read BigTIFF: a tif holds at most this many bytes of pixels (a classic tif is limited to 4 GB)
TIF_PART_BYTES = 3_500_000_000
#: a value of another detector is left empty when it is further than this many readout intervals
MAX_GAP = 3.0
#: The integration time is in ms in the file
INTEGRATION_UNITS = 'ms'
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


def open_file(path: Path):
    import h5py
    try:
        return h5py.File(path, 'r')
    except OSError:  # a file which is still being written (SWMR)
        return h5py.File(path, 'r', swmr=True)


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


def roi_lines(unit: UnitData, files: dict[str, str]) -> list[str]:
    """What each ROI of a unit is, one line each (`files`: the tif file of each crop, by the name of the ROI)"""
    series = unit.series.get('ROIs')
    lines = []
    try:
        recorded = json.loads(series.attrs['rois']) if series is not None and 'rois' in series.attrs else []
    except (TypeError, ValueError):
        recorded = []
    for roi in recorded:
        name = roi.get('name', '')
        what = AGGREGATION_WORDS.get(roi.get('aggregation', 'mean'), 'mean')
        if 'x0' in roi:
            lines.append(f'    {name}: {what} of the pixels in x {roi["x0"]:g}-{roi["x1"]:g}, y {roi["y0"]:g}-{roi["y1"]:g} '
                         f'pixel')
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
                t0: float, written: Sequence[tuple[Path, str]], crop_files: dict[int, dict[str, str]]) -> str:
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


def tif_parts(images: Series, dtype: np.dtype) -> list[range]:
    """The frames of each tif file: ImageJ reads files of up to 4 GB (not BigTIFF), so a longer series is split"""
    n_frames, n_channels = len(images), len(images.arrays)
    frame_bytes = max(1, n_channels * int(np.prod(images.arrays[0].shape[1:])) * dtype.itemsize)
    per_part = max(1, TIF_PART_BYTES // frame_bytes)
    return [range(first, min(first + per_part, n_frames)) for first in range(0, n_frames, per_part)]


def tif_name(stem: Path, what: str, part: int, n_parts: int) -> Path:
    """``<file>_camera.tif``, or ``<file>_camera_part01.tif``... when the images are in several files (`what` is
    ``camera``, or ``camera_crop_<name>`` for a crop)"""
    return stem.with_name(f'{stem.name}_{what}.tif' if n_parts == 1 else f'{stem.name}_{what}_part{part + 1:02d}.tif')


def write_tif(path: Path, images: Series, times: np.ndarray, temperature: Optional[np.ndarray], info: str,
              frames: Optional[range] = None, dtype: Optional[np.dtype] = None):
    """The images (the frames `frames`, all by default) in an ImageJ hyperstack: axes Time, Channel, Y (rows), X (columns)"""
    try:
        import tifffile
    except ImportError:
        raise SystemExit('The tif files need tifffile: python -m pip install tifffile') from None
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
            labels.append(f'{text}, {channel_labels(n_channels)[channel]}')
    metadata = {'axes': 'TCYX', 'finterval': step, 'Info': info, 'Labels': labels, 'unit': 'pixel'}
    if n_channels == 3:  # shown as the colors of an image
        metadata['mode'] = 'composite'
        ramp = np.arange(256, dtype=np.uint8)
        metadata['LUTs'] = [np.stack([ramp if color == channel else np.zeros(256, np.uint8) for color in range(3)])
                            for channel in range(3)]

    def pages() -> Iterator[np.ndarray]:
        for frame in frames:
            for array in images.arrays:
                yield np.asarray(array[frame]).astype(dtype, copy=False)

    with tifffile.TiffWriter(path, imagej=True, bigtiff=False) as writer:
        writer.write(pages(), shape=(n_frames, n_channels) + shape, dtype=dtype, metadata=metadata)


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


def write_images(series: Series, what: str, at_zero: float, temperature_of, stem: Path, info: str, options
                 ) -> tuple[list[Path], list[range]]:
    """The tif file(s) of a series of images: the paths and the frames of each"""
    dtype = imagej_dtype(series.arrays)
    parts = tif_parts(series, dtype)
    paths = [tif_name(stem, what, number, len(parts)) for number in range(len(parts))]
    times = series.time[:len(series)]
    temperature = temperature_of(times)
    for part, path in zip(parts, paths):
        write_tif(path, series, times - at_zero, temperature, info, part, dtype)
    return paths, parts


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

    tifs, parts, split = [], [], False
    if images is None and not camera.crops:
        print('  the images of the camera were not recorded (tick "The full image" under "Record to disk" of the '
              'camera, or make a ROI a crop): no tif file', file=sys.stderr)
    if images is not None and len(images) and not options.no_tif:
        info = (f'ThermoSpectro: camera {camera.attr("camera", "")}, thermostat {camera.attr("thermostat", "")}, '
                f'exposure {camera.attr("integration_time_ms", "")} ms (at the start), readout '
                f'{camera.attr("readout_time_s", "")} s, {len(images)} images, pixels in {camera.intensity_units or "?"}. '
                f'The frames of the tif are the images of the csv file of the same name.')
        tifs, parts = write_images(images, 'camera', t0, temperature_of, stem, info, options)
        split = len(parts) > 1
        for path, part in zip(tifs, parts):
            written.append((path, f'camera: the full images, axes T, C, Y, X ({len(part)} frames)'))

    crop_files: dict[str, str] = {}
    if not options.no_tif:
        for crop in camera.crops:
            if not len(crop):
                continue
            roi = roi_of_crop(crop)
            name = str(roi.get('name', crop.kind))
            info = (f'ThermoSpectro: crop {name} of the camera {camera.attr("camera", "")}, '
                    f'window x {roi.get("x0", "?")}-{roi.get("x1", "?")}, y {roi.get("y0", "?")}-{roi.get("y1", "?")} '
                    f'pixel, {len(crop)} images, pixels in {camera.intensity_units or "?"}')
            paths, crop_parts = write_images(crop, f'camera_crop_{file_part(name)}', t0, temperature_of, stem, info,
                                             options)
            crop_files[name] = paths[0].name if len(paths) == 1 else f'{paths[0].name} ... {paths[-1].name}'
            for path, part in zip(paths, crop_parts):
                written.append((path, f'camera: the pixels of the crop {name}, axes T, C, Y, X ({len(part)} frames)'))

    if images is not None and len(images) and tifs:  # (which frame of which tif holds the image of each readout)
        rows, valid = matching_rows(images.time[:len(images)], at)
        per_part = len(parts[0])
        if split:
            names.append('tif file')
            columns.append([tifs[row // per_part].name if there else '' for row, there in zip(rows, valid)])
        names.append('tif frame')
        columns.append(WithGaps(np.where(valid, rows % per_part + 1, 0).astype(float), valid))
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
def describe(units: dict[int, UnitData], description: Optional[dict[str, str]] = None, n_notes: int = 0) -> str:
    lines = []
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
    with open_file(path) as file:
        units = read_experiment(file, options.experiment)
        description = read_description(file)
        times, texts = read_notes(experiment_group(file, options.experiment)[0])
        print(f'{path}:\n{describe(units, description, len(texts))}')
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
        report.write_text(report_text(file, units, description, times, texts, t0, written, crop_files),
                          encoding='utf-8-sig')
    return [path for path, _ in written]


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
    arguments.add_argument('--no-spectra', action='store_true', help='do not write the csv file of the full spectra')
    arguments.add_argument('--no-tif', action='store_true', help='do not write the tif files (the csv files only)')
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
