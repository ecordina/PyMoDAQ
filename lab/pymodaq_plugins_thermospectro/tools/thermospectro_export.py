#!/usr/bin/env python
"""Convert the h5 file of a ThermoSpectro experiment into csv and tif files

    python thermospectro_export.py Dataset_20260101_000.h5

The file written by PyMoDAQ is made of one small array per kind of data. This script puts them side by side:

* for each **spectrometer** (``Unit1_...``, ``Unit2_...``): ``<file>_spectrometer1.csv``, one row per readout, with the
  columns ``time (s) | temperature (°C) | ROI 1 | ROI 2 | ... | full spectrum``;
* for the **camera** (``Unit3_...``): ``<file>_camera.tif``, the images stitched in one tif (an ImageJ hyperstack with the
  axes Time, Channel, Y, X: open it with Fiji / ImageJ, or ``tifffile.imread``), and ``<file>_camera.csv``, one row per
  image (the row number is the number of the image, the frame of the tif counted from 1 as ImageJ does) with the columns
  ``time (s) | temperature (°C) | spectrometer 1 ROI 1 | ... | camera ROI 1 | ...``. ImageJ cannot open files of more
  than 4 GB (BigTIFF): a longer series of images is split in ``<file>_camera_part01.tif``, ``..._part02.tif``..., and the
  csv files have the columns ``tif file`` and ``tif frame`` (from 1, in that file).

* what the user wrote about the experiment (the title, the sample, the operator, the protocol), as ``<file>_protocol.txt``,
  and the observations noted while it ran, one row each, as ``<file>_notes.csv`` with the columns
  ``time (s) | clock time | observation`` (the same zero of the time as the other files; the clock time is the one of
  the computer which converts the file).

What is not in the file (a spectrometer or a camera which was not used, the temperature or the full spectrum or the
full image which were not recorded, no observation, nothing written about the experiment) is left out. The full image is recorded only if *The full image* was ticked under
*Record to disk* of the camera.

The time is in seconds from the first measurement of the experiment, the same zero in all the files (``--absolute-time``
gives the epoch seconds instead). The spectrometers and the camera are read out independently: in the file of the camera,
the value of a spectrometer is the one of its readout closest in time (``--interpolate`` interpolates it linearly). A
value which is further than three readout intervals from its closest readout (the module did not answer for a while) is
left empty rather than repeated.

The full spectrum is one column with the intensities of the pixels separated by spaces; the wavelengths are in
``<file>_spectrometer1_wavelengths.csv``. ``--spectrum columns`` makes one column per wavelength instead (named by it),
``--spectrum none`` leaves the full spectrum out. In a long log the whole spectrum (image) is saved only every so often
(*Full spectrum every* / *Full image every* in the settings): there is one row per readout of the ROIs, and the cells of
the spectrum are empty at the readouts which have none. The images are in the tif, the csv of the camera has one row per
image, and ``<file>_camera_log.csv`` has one row per readout, with the number of the image (the row of the other csv, the
frame of the tif counted from 1) at the readouts which have one. The spectra are read from the file a few hundred rows at
a time, so that a log of days does not have to fit in memory.

Needs ``numpy``, ``h5py`` and, for the camera, ``tifffile`` (``python -m pip install tifffile``). It does not need
PyMoDAQ.
"""
from __future__ import annotations

import argparse
import ast
import csv
import datetime
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional, Sequence

import numpy as np

KINDS = ('Spectrum', 'Image', 'ROIs', 'Temperature', 'Program')
#: What the user wrote about the experiment: the attributes of the group RawData (and the application which made the file)
DESCRIPTION = ('experiment_title', 'sample', 'operator', 'protocol', 'application')
NOTES = 'Notes'  # the group of the observations, in the group of the experiment
TITLE = re.compile(r'^Unit(\d+)_(Spectrum|Image|ROIs|Temperature|Program)$')
IMAGEJ_TYPES = (np.uint8, np.uint16, np.float32)
CHANNEL_COLORS = ('red', 'green', 'blue')
#: ImageJ does not read BigTIFF: a tif holds at most this many bytes of pixels (a classic tif is limited to 4 GB)
TIF_PART_BYTES = 3_500_000_000
#: a value of another detector is left empty when it is further than this many readout intervals
MAX_GAP = 3.0


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

    def __len__(self) -> int:
        return min([len(self.time)] + [len(array) for array in self.arrays])

    def column(self, index: int, rows: Optional[int] = None) -> np.ndarray:
        return np.asarray(self.arrays[index][:rows if rows is not None else len(self)], dtype=float).reshape(
            len(self) if rows is None else rows, -1)[:, 0]


@dataclass
class UnitData:
    """The groups of a unit (1 and 2 are spectrometers, 3 is the camera)"""
    number: int
    series: dict = field(default_factory=dict)  # kind: Series

    @property
    def is_camera(self) -> bool:
        return 'Image' in self.series or any('camera' in s.attrs for s in self.series.values())

    @property
    def kind(self) -> str:
        return 'camera' if self.is_camera else 'spectrometer'

    @property
    def name(self) -> str:
        return 'camera' if self.is_camera else f'spectrometer {self.number}'

    def attr(self, key: str, default=''):
        for series in self.series.values():
            if key in series.attrs:
                return series.attrs[key]
        return default

    @property
    def temperature_units(self) -> str:
        return str(self.attr('temperature_units', ''))


def open_file(path: Path):
    import h5py
    try:
        return h5py.File(path, 'r')
    except OSError:  # a file which is still being written (SWMR)
        return h5py.File(path, 'r', swmr=True)


def read_series(title: str, kind: str, group) -> Series:
    """The arrays of a group of the file"""
    time = np.asarray(group['NavAxes']['Axis00'][:], dtype=float) if 'NavAxes' in group else None
    arrays, labels, axis = [], [], None
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
    if time is None:
        time = np.arange(len(arrays[0]), dtype=float) if arrays else np.zeros(0)
    attrs = {key: attribute(value) for key, value in group.attrs.items()}
    return Series(kind, title, time, arrays, labels, axis, attrs)


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
    """``ROI 1 (454.2-464.6 nm)`` -> ``ROI 1`` (only the last group in parentheses goes: ``Peak (main) (1-2 nm)`` is
    ``Peak (main)``)"""
    return re.sub(r'\s*\([^()]*\)\s*$', '', label)


def roi_columns(series: Series, short_names: bool) -> tuple[list[str], list[np.ndarray]]:
    names = [short(label) if short_names else label for label in series.labels]
    return names, [series.column(index) for index in range(len(series.arrays))]


def master_series(unit: UnitData) -> Series:
    """The group which gives the times of the rows of a unit"""
    # (what is recorded at each readout comes first: the spectrum and the image may be saved only now and then)
    for kind in ('ROIs', 'Temperature', 'Program', 'Spectrum', 'Image'):
        if kind in unit.series and len(unit.series[kind]):
            return unit.series[kind]
    raise ValueError(f'Nothing was recorded for the {unit.name}')


def unit_columns(unit: UnitData, at: np.ndarray, options) -> tuple[list[str], list[np.ndarray]]:
    """The temperature, the ROIs (and the program, if asked for) of a unit, at the times `at`"""
    names, columns = [], []

    def add(series: Series, column_names: list[str], values: list[np.ndarray]):
        for column_name, value in zip(column_names, values):
            names.append(column_name)
            columns.append(nearest(series.time[:len(value)], value, at, options.interpolate, MAX_GAP))

    if 'Temperature' in unit.series and len(unit.series['Temperature']):
        series = unit.series['Temperature']
        add(series, [temperature_name(unit)], [series.column(0)])
    if 'ROIs' in unit.series:
        series = unit.series['ROIs']
        add(series, *roi_columns(series, options.short_names))
    if options.program and 'Program' in unit.series:
        series = unit.series['Program']
        add(series, ['set-point', 'cycle', 'phase code'][:len(series.arrays)],
            [series.column(index) for index in range(len(series.arrays))])
    return names, columns


def temperature_name(unit: UnitData) -> str:
    return f'temperature ({unit.temperature_units})' if unit.temperature_units else 'temperature'


def time_column(at: np.ndarray, t0: float, options) -> tuple[str, list[str]]:
    """The time of each row as text with microseconds (epoch seconds have ten digits: the 9 significant digits of the
    other columns would round them to ten seconds)"""
    name, values = ('time (epoch s)', at) if options.absolute_time else ('time (s)', at - t0)
    return name, [f'{value:.6f}' for value in values]


def experiment_start(units: dict[int, UnitData]) -> float:
    return min(float(s.time[0]) for unit in units.values() for s in unit.series.values() if len(s.time))


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


def protocol_text(description: dict[str, str], file_name: str, started: str) -> str:
    """What the user wrote about the experiment, as a text ('' if nothing was written)"""
    said = {key: description.get(key, '').strip() for key in DESCRIPTION}
    if not any(said[key] for key in ('experiment_title', 'sample', 'operator', 'protocol')):
        return ''
    lines = [f'{label}: {said[key]}' for label, key in (('Experiment', 'experiment_title'), ('Sample', 'sample'),
                                                         ('Operator', 'operator'), ('Application', 'application'))
             if said[key]]
    lines += [f'Started: {started}'] if started else []
    lines.append(f'File: {file_name}')
    if said['protocol']:
        lines += ['', 'Protocol', '--------', said['protocol']]
    return '\n'.join(lines) + '\n'


def export_protocol(description: dict[str, str], file, stem: Path) -> list[Path]:
    """``<file>_protocol.txt``: what the user wrote about the experiment (nothing if nothing was written)"""
    text = protocol_text(description, Path(file.filename).name, started_at(file))
    if not text:
        return []
    path = stem.with_name(f'{stem.name}_protocol.txt')
    path.write_text(text, encoding='utf-8-sig')
    return [path]


# ----------------------------------------------------------------------------------------------------------------------
# a spectrometer: time | temperature | ROIs | full spectrum
# ----------------------------------------------------------------------------------------------------------------------
def export_spectrometer(unit: UnitData, t0: float, stem: Path, options) -> list[Path]:
    """``<file>_spectrometer1.csv``: time | temperature | ROIs | full spectrum"""
    master = master_series(unit)
    at = master.time[:len(master)]
    names, columns = unit_columns(unit, at, options)
    time_name, times = time_column(at, t0, options)
    header, data = [time_name] + names, [times] + columns
    written = []
    spectra = unit.series.get('Spectrum')
    if spectra is not None and options.spectrum != 'none' and len(spectra):
        wavelengths = spectra.axis
        if options.spectrum == 'columns':
            reader = SpectrumReader(spectra, 0, at)
            for pixel in range(spectra.arrays[0].shape[1]):
                header.append(f'{wavelengths[pixel]:.3f} nm' if wavelengths is not None else f'pixel {pixel}')
                data.append(SpectrumPixel(reader, pixel))
        else:
            for index in range(len(spectra.arrays)):
                header.append('full spectrum' if index == 0 else f'full spectrum {index + 1}')
                data.append(spectrum_cells(spectra, index, at))
            if wavelengths is not None:
                path = stem.with_name(f'{stem.name}_{unit.kind}{unit.number}_wavelengths.csv')
                write_csv(path, ['pixel', 'wavelength (nm)'], [np.arange(len(wavelengths)), wavelengths],
                          options.delimiter)
                written.append(path)
    path = stem.with_name(f'{stem.name}_{unit.kind}{unit.number}.csv')
    write_csv(path, header, data, options.delimiter)
    return [path] + written


class WithGaps:
    """A column of a csv file where some rows have nothing (empty cells): the whole spectrum of a long log is saved only
    now and then"""

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


class SpectrumReader:
    """The spectra of a unit at the times `at`, read from the file a block of rows at a time while the csv is written (the
    spectra of a log of days do not fit in memory)"""
    BLOCK = 256

    def __init__(self, spectra: Series, index: int, at: np.ndarray):
        self.dataset = spectra.arrays[index]
        self.rows, self.valid = matching_rows(spectra.time[:min(len(spectra), len(self.dataset))], at)
        self._start = self._stop = 0
        self._wanted = np.zeros(0, dtype=int)
        self._block = np.zeros((0, 0))

    def __len__(self) -> int:
        return len(self.valid)

    def spectrum(self, row: int) -> Optional[np.ndarray]:
        """The spectrum of a row of the csv file, None if none was saved at that readout"""
        if not self.valid[row]:
            return None
        if not self._start <= row < self._stop:
            stop = min(row + self.BLOCK, len(self.rows))
            self._wanted = np.unique(self.rows[row:stop][self.valid[row:stop]])
            self._block = np.asarray(self.dataset[self._wanted], dtype=float)
            self._start, self._stop = row, stop
        return self._block[int(np.searchsorted(self._wanted, self.rows[row]))]


class SpectrumCells:
    """A column of the csv file: the whole spectrum of each row, one text of intensities separated by spaces (an empty
    text where no spectrum was saved: in a long log it is saved every so often)"""

    def __init__(self, reader: SpectrumReader):
        self.reader = reader

    def __len__(self) -> int:
        return len(self.reader)

    def __getitem__(self, row: int) -> str:
        spectrum = self.reader.spectrum(row)
        return '' if spectrum is None else ' '.join(f'{value:.6g}' for value in spectrum)


class SpectrumPixel:
    """A column of the csv file: the intensity of one pixel of the spectra (empty where no spectrum was saved)"""

    def __init__(self, reader: SpectrumReader, pixel: int):
        self.reader, self.pixel = reader, pixel

    def __len__(self) -> int:
        return len(self.reader)

    def __getitem__(self, row: int) -> str:
        spectrum = self.reader.spectrum(row)
        return '' if spectrum is None else f'{float(spectrum[self.pixel]):.9g}'


def spectrum_cells(spectra: Series, index: int, at: np.ndarray) -> SpectrumCells:
    """The spectra as the cells of a column of the csv file, one for each time of `at`"""
    return SpectrumCells(SpectrumReader(spectra, index, at))


# ----------------------------------------------------------------------------------------------------------------------
# the camera: a tif and a csv
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


def tif_name(stem: Path, part: int, n_parts: int) -> Path:
    """``<file>_camera.tif``, or ``<file>_camera_part01.tif``... when the images are in several files"""
    return stem.with_name(f'{stem.name}_camera.tif' if n_parts == 1 else f'{stem.name}_camera_part{part + 1:02d}.tif')


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
    metadata = {'axes': 'TCYX', 'finterval': step, 'Info': info, 'Labels': labels}
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


def camera_table(camera: UnitData, units: dict[int, UnitData], at: np.ndarray, t0: float, options
                 ) -> tuple[list[str], list, Optional[np.ndarray]]:
    """The columns of the csv files of the camera at the times `at`: time | temperature | spectrometers | camera"""
    time_name, times = time_column(at, t0, options)
    own_names, own_columns = unit_columns(camera, at, options)
    spectrometers = [units[number] for number in sorted(units) if not units[number].is_camera]

    # the time, the temperature (the one of the camera if it has one, else the one of a spectrometer), the
    # spectrometers, the camera
    names, columns = [time_name], [times]
    temperature = None
    for name, column in zip(own_names, own_columns):
        if name.startswith('temperature'):
            names.append(name)
            columns.append(temperature := column)
    if temperature is None:
        for spectrometer in spectrometers:
            series = spectrometer.series.get('Temperature')
            if series is not None and len(series):
                names.append(temperature_name(spectrometer))
                columns.append(temperature := nearest(series.time[:len(series)], series.column(0), at,
                                                      options.interpolate, MAX_GAP))
                break
    for spectrometer in spectrometers:
        other_names, other_columns = unit_columns(spectrometer, at, options)
        for name, column in zip(other_names, other_columns):
            if not name.startswith('temperature'):
                names.append(f'spectrometer {spectrometer.number} {name}')
                columns.append(column)
    for name, column in zip(own_names, own_columns):
        if not name.startswith('temperature'):
            names.append(f'camera {name}')
            columns.append(column)
    return names, columns, temperature


def export_camera(camera: UnitData, units: dict[int, UnitData], t0: float, stem: Path, options) -> list[Path]:
    """``<file>_camera.tif`` (the images; ``_part01.tif``... when there are too many for ImageJ), ``<file>_camera.csv``
    (one row per image) and, when the images were saved only now and then, ``<file>_camera_log.csv`` (one row per
    readout)"""
    images = camera.series.get('Image')
    master = images if images is not None and len(images) else master_series(camera)
    at = master.time[:len(master)]  # one row per image (per readout of the ROIs if the images were not recorded)
    names, columns, temperature = camera_table(camera, units, at, t0, options)
    written = []

    parts, tifs = [], []  # the frames of each tif, and its name
    if images is None:
        print('  the images of the camera were not recorded (tick "The full image" under "Record to disk" of the '
              'camera): no tif file', file=sys.stderr)
    elif len(images) and not options.no_tif:
        dtype = imagej_dtype(images.arrays)
        parts = tif_parts(images, dtype)
        tifs = [tif_name(stem, number, len(parts)) for number in range(len(parts))]
    split = len(parts) > 1
    if split:  # (which file, and which frame of it, holds each image)
        per_part = len(parts[0])
        names += ['tif file', 'tif frame']
        columns += [[tifs[row // per_part].name for row in range(len(at))],
                    [str(row % per_part + 1) for row in range(len(at))]]
    path = stem.with_name(f'{stem.name}_camera.csv')
    write_csv(path, names, columns, options.delimiter)
    written.append(path)

    if parts:
        info = (f'ThermoSpectro: camera {camera.attr("camera", "")}, thermostat {camera.attr("thermostat", "")}, '
                f'exposure {camera.attr("integration_time_ms", "")} ms, readout {camera.attr("readout_time_s", "")} s, '
                f'{len(images)} images. The rows of the csv file of the same name are the images.')
        for part, tif in zip(parts, tifs):
            write_tif(tif, images, at - t0, temperature, info, part, dtype)
            written.append(tif)

    readouts = camera.series.get('ROIs') or camera.series.get('Temperature')
    if images is not None and len(images) and readouts is not None and len(readouts) > len(images):
        # the images were saved every so often: the log of every readout is in a file of its own, with the number of
        # the image (the row of the other csv, the frame of the tif counted from 1) at the readouts which have one
        log_at = readouts.time[:len(readouts)]
        names, columns, _ = camera_table(camera, units, log_at, t0, options)
        rows, valid = matching_rows(images.time[:len(images)], log_at)
        names.append('image')
        columns.append(WithGaps(rows.astype(float) + 1, valid))
        if split:
            names += ['tif file', 'tif frame']
            columns += [[tifs[row // per_part].name if there else '' for row, there in zip(rows, valid)],
                        [str(row % per_part + 1) if there else '' for row, there in zip(rows, valid)]]
        path = stem.with_name(f'{stem.name}_camera_log.csv')
        write_csv(path, names, columns, options.delimiter)
        written.append(path)
    return written


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
    return '\n'.join(lines)


def export_file(path: Path, out: Optional[Path], options) -> list[Path]:
    """Convert one file, returns the files which were written"""
    path = Path(path)
    folder = Path(out) if out else path.parent
    folder.mkdir(parents=True, exist_ok=True)
    stem = folder / path.stem
    written = []
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
                    written += export_camera(unit, units, t0, stem, options)
                else:
                    written += export_spectrometer(unit, t0, stem, options)
            except ValueError as e:
                print(f'  {unit.name}: {e}', file=sys.stderr)
        written += export_notes(times, texts, t0, stem, options)
        written += export_protocol(description, file, stem)
    return written


def parser() -> argparse.ArgumentParser:
    arguments = argparse.ArgumentParser(
        description='Convert the h5 file of a ThermoSpectro experiment into csv files (one per spectrometer), a '
                    'tif with its csv (the camera), the observations (csv) and the protocol (text).',
        epilog=__doc__.split('\n\n')[0])
    arguments.add_argument('files', nargs='+', type=Path, help='the h5 file(s) written by the extension')
    arguments.add_argument('-o', '--out', type=Path, help='the folder of the new files (default: the one of the h5 file)')
    arguments.add_argument('--experiment', type=int, default=-1, metavar='N',
                           help='the experiment of the file, 0 is the first (default: the last one)')
    arguments.add_argument('--spectrum', choices=('column', 'columns', 'none'), default='column',
                           help='the full spectrum: one column of intensities (default), one column per wavelength, or '
                                'left out')
    arguments.add_argument('--short-names', action='store_true',
                           help='name the columns of the ROIs "ROI 1" instead of "ROI 1 (454.2-464.6 nm)"')
    arguments.add_argument('--absolute-time', action='store_true', help='the time in epoch seconds, not from the start')
    arguments.add_argument('--interpolate', action='store_true',
                           help='in the csv of the camera, interpolate the other detectors at the time of the image '
                                'instead of taking the closest readout')
    arguments.add_argument('--program', action='store_true',
                           help='add the set-point, the cycle and the phase code of the temperature ramp')
    arguments.add_argument('--no-tif', action='store_true', help='do not write the tif file (the csv only)')
    arguments.add_argument('--delimiter', default=',', help='the delimiter of the csv files (default: a comma)')
    arguments.add_argument('--list', action='store_true', help='only tell what the files hold')
    return arguments


def main(argv: Optional[Sequence[str]] = None) -> int:
    options = parser().parse_args(argv)
    status = 0
    for path in options.files:
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
