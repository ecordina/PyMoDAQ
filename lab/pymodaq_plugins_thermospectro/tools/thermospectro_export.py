#!/usr/bin/env python
"""Convert the h5 file of a ThermoSpectro experiment into csv and tif files

    python thermospectro_export.py Dataset_20260101_000.h5

The file written by PyMoDAQ is made of one small array per kind of data. This script puts them side by side:

* for each **spectrometer** (``Unit1_...``, ``Unit2_...``): ``<file>_spectrometer1.csv``, one row per readout, with the
  columns ``time (s) | temperature (°C) | ROI 1 | ROI 2 | ... | full spectrum``;
* for the **camera** (``Unit3_...``): ``<file>_camera.tif``, the images stitched in one tif (an ImageJ hyperstack with the
  axes Time, Channel, Y, X: open it with Fiji / ImageJ, or ``tifffile.imread``), and ``<file>_camera.csv``, one row per
  image (the row number is the number of the image in the tif) with the columns
  ``time (s) | temperature (°C) | spectrometer 1 ROI 1 | ... | camera ROI 1 | ...``.

What is not in the file (a spectrometer or a camera which was not used, the temperature or the full spectrum or the
full image which were not recorded) is left out. The full image is recorded only if *The full image* was ticked under
*Record to disk* of the camera.

The time is in seconds from the first measurement of the experiment, the same zero in all the files (``--absolute-time``
gives the epoch seconds instead). The spectrometers and the camera are read out independently: in the file of the camera,
the value of a spectrometer is the one of its readout closest in time (``--interpolate`` interpolates it linearly).

The full spectrum is one column with the intensities of the pixels separated by spaces; the wavelengths are in
``<file>_spectrometer1_wavelengths.csv``. ``--spectrum columns`` makes one column per wavelength instead (named by it),
``--spectrum none`` leaves the full spectrum out.

Needs ``numpy``, ``h5py`` and, for the camera, ``tifffile`` (``python -m pip install tifffile``). It does not need
PyMoDAQ.
"""
from __future__ import annotations

import argparse
import ast
import csv
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional, Sequence

import numpy as np

KINDS = ('Spectrum', 'Image', 'ROIs', 'Temperature', 'Program')
TITLE = re.compile(r'^Unit(\d+)_(Spectrum|Image|ROIs|Temperature|Program)$')
IMAGEJ_TYPES = (np.uint8, np.uint16, np.float32)
CHANNEL_COLORS = ('red', 'green', 'blue')


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


def read_experiment(file, index: int = -1) -> dict[int, UnitData]:
    """The units of an experiment (`index` -1: the last one of the file)"""
    root = file['RawData']
    names = sorted((name for name in root if name.startswith('Datalogger')), key=natural)
    if not names:
        raise ValueError('There is no experiment of ThermoSpectro in this file (no Datalogger group)')
    name = names[index] if index < 0 else f'Datalogger{index:03d}'
    if name not in root:
        raise ValueError(f'There is no experiment {index} in this file: it has {", ".join(names)}')
    units: dict[int, UnitData] = {}
    for group in root[name].values():
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
def nearest(time: np.ndarray, values: np.ndarray, at: np.ndarray, interpolate: bool = False) -> np.ndarray:
    """The values at the times `at`: those of the closest sample, or interpolated"""
    if len(time) == 0:
        return np.full(len(at), np.nan)
    if interpolate and len(time) > 1:
        return np.interp(at, time, values)
    right = np.clip(np.searchsorted(time, at), 1, len(time) - 1) if len(time) > 1 else np.zeros(len(at), int)
    left = np.maximum(right - 1, 0)
    pick = np.where(np.abs(time[right] - at) < np.abs(time[left] - at), right, left)
    return values[pick]


def short(label: str) -> str:
    """``ROI 1 (454.2-464.6 nm)`` -> ``ROI 1``"""
    return re.sub(r'\s*\(.*\)\s*$', '', label)


def roi_columns(series: Series, short_names: bool) -> tuple[list[str], list[np.ndarray]]:
    names = [short(label) if short_names else label for label in series.labels]
    return names, [series.column(index) for index in range(len(series.arrays))]


def master_series(unit: UnitData) -> Series:
    """The group which gives the times of the rows of a unit"""
    for kind in ('ROIs', 'Spectrum', 'Image', 'Temperature', 'Program'):
        if kind in unit.series and len(unit.series[kind]):
            return unit.series[kind]
    raise ValueError(f'Nothing was recorded for the {unit.name}')


def unit_columns(unit: UnitData, at: np.ndarray, options) -> tuple[list[str], list[np.ndarray]]:
    """The temperature, the ROIs (and the program, if asked for) of a unit, at the times `at`"""
    names, columns = [], []

    def add(series: Series, column_names: list[str], values: list[np.ndarray]):
        for column_name, value in zip(column_names, values):
            names.append(column_name)
            columns.append(nearest(series.time[:len(value)], value, at, options.interpolate))

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


def time_column(at: np.ndarray, t0: float, options) -> tuple[str, np.ndarray]:
    return ('time (epoch s)', at) if options.absolute_time else ('time (s)', at - t0)


def experiment_start(units: dict[int, UnitData]) -> float:
    return min(float(s.time[0]) for unit in units.values() for s in unit.series.values() if len(s.time))


def write_csv(path: Path, header: Sequence[str], columns: Sequence, delimiter: str = ','):
    """A csv file with one row per sample (the columns are arrays or lists of text), readable by Excel (utf-8 with
    a signature, for the degree sign)"""
    rows = min(len(column) for column in columns)
    with open(path, 'w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.writer(stream, delimiter=delimiter)
        writer.writerow(header)
        for row in range(rows):
            writer.writerow([column[row] if isinstance(column[row], str) else f'{float(column[row]):.9g}'
                             for column in columns])


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
            spectrum = np.asarray(spectra.arrays[0][:len(spectra)], dtype=float)
            rows = nearest_index(spectra.time[:len(spectrum)], at)
            for pixel in range(spectrum.shape[1]):
                header.append(f'{wavelengths[pixel]:.3f} nm' if wavelengths is not None else f'pixel {pixel}')
                data.append(spectrum[rows, pixel])
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


def nearest_index(time: np.ndarray, at: np.ndarray) -> np.ndarray:
    """For each time of `at`, the index of the closest sample"""
    return nearest(time, np.arange(len(time), dtype=float), at).astype(int)


def spectrum_cells(spectra: Series, index: int, at: np.ndarray) -> list[str]:
    """The spectra, one text of intensities separated by spaces for each time of `at`"""
    data = np.asarray(spectra.arrays[index][:len(spectra)], dtype=float)
    rows = nearest_index(spectra.time[:len(data)], at)
    return [' '.join(f'{value:.6g}' for value in data[row]) for row in rows]


# ----------------------------------------------------------------------------------------------------------------------
# the camera: a tif and a csv
# ----------------------------------------------------------------------------------------------------------------------
def imagej_dtype(arrays) -> np.dtype:
    """The type of the pixels in the tif: ImageJ reads 8 and 16 bits integers and 32 bits floats"""
    dtype = arrays[0].dtype
    if dtype in IMAGEJ_TYPES:
        return np.dtype(dtype)
    if np.issubdtype(dtype, np.integer):
        low = min(float(np.min(array[:])) for array in arrays if len(array))
        high = max(float(np.max(array[:])) for array in arrays if len(array))
        if low >= 0 and high <= 65535:
            return np.dtype(np.uint16)
    return np.dtype(np.float32)


def channel_labels(n_channels: int) -> list[str]:
    return ['R', 'G', 'B'][:n_channels] if n_channels == 3 else [f'channel {index + 1}' for index in range(n_channels)]


def write_tif(path: Path, images: Series, times: np.ndarray, temperature: Optional[np.ndarray], info: str):
    """The images in an ImageJ hyperstack: axes Time, Channel, Y (rows), X (columns)"""
    try:
        import tifffile
    except ImportError:
        raise SystemExit('The tif files need tifffile: python -m pip install tifffile') from None
    n_frames, n_channels = len(images), len(images.arrays)
    shape = tuple(images.arrays[0].shape[1:])
    if len(shape) != 2:
        raise ValueError(f'{images.title} holds {len(shape)}D data, not images')
    dtype = imagej_dtype(images.arrays)
    step = float(np.median(np.diff(times))) if n_frames > 1 else 0.0
    labels = []
    for frame in range(n_frames):
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
        for frame in range(n_frames):
            for array in images.arrays:
                yield np.asarray(array[frame]).astype(dtype, copy=False)

    size = n_frames * n_channels * int(np.prod(shape)) * dtype.itemsize
    with tifffile.TiffWriter(path, imagej=True, bigtiff=size > 3_800_000_000) as writer:
        writer.write(pages(), shape=(n_frames, n_channels) + shape, dtype=dtype, metadata=metadata)


def export_camera(camera: UnitData, units: dict[int, UnitData], t0: float, stem: Path, options) -> list[Path]:
    """``<file>_camera.tif`` (the images) and ``<file>_camera.csv`` (one row per image)"""
    images = camera.series.get('Image')
    master = images if images is not None and len(images) else master_series(camera)
    at = master.time[:len(master)]  # one row per image (per readout of the ROIs if the images were not recorded)
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
                                                      options.interpolate))
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
    path = stem.with_name(f'{stem.name}_camera.csv')
    write_csv(path, names, columns, options.delimiter)
    written = [path]

    if images is None:
        print('  the images of the camera were not recorded (tick "The full image" under "Record to disk" of the '
              'camera): no tif file', file=sys.stderr)
    elif len(images) and not options.no_tif:
        info = (f'ThermoSpectro: camera {camera.attr("camera", "")}, thermostat {camera.attr("thermostat", "")}, '
                f'exposure {camera.attr("integration_time_ms", "")} ms, readout {camera.attr("readout_time_s", "")} s, '
                f'{len(images)} images. The rows of the csv file of the same name are the images.')
        path = stem.with_name(f'{stem.name}_camera.tif')
        write_tif(path, images, at - t0, temperature, info)
        written.append(path)
    return written


# ----------------------------------------------------------------------------------------------------------------------
# the command
# ----------------------------------------------------------------------------------------------------------------------
def describe(units: dict[int, UnitData]) -> str:
    lines = []
    for number in sorted(units):
        unit = units[number]
        what = ', '.join(f'{kind} ({len(series)} rows)' for kind, series in unit.series.items())
        lines.append(f'  unit {number}: {unit.name} {unit.attr("camera" if unit.is_camera else "spectrometer", "")} '
                     f'with {unit.attr("thermostat", "no thermostat")}: {what}')
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
        print(f'{path}:\n{describe(units)}')
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
    return written


def parser() -> argparse.ArgumentParser:
    arguments = argparse.ArgumentParser(
        description='Convert the h5 file of a ThermoSpectro experiment into csv files (one per spectrometer) and a '
                    'tif with its csv (the camera).', epilog=__doc__.split('\n\n')[0])
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
