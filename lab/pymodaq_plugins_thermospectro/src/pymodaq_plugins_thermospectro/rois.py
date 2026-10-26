"""Regions of interest (ROI) of a spectrum: a window of wavelengths, followed in time as a number made of its pixels (their
mean, sum, median, min, max or standard deviation: :data:`AGGREGATIONS`), or saved as it is, a **crop** of the spectrum
(:data:`CROP`)

Pure numpy and json, no Qt and no PyMoDAQ, so that it can be tested alone. The ROIs of a camera (:mod:`.camera`) give the same
choice.

Each spectrometer starts with :data:`N_DEFAULT` ROIs picked at random (the same ones every time: the random generator
is seeded with the number of the spectrometer), and what the user changes is remembered in a json file
(:class:`RoiStore`) so that the next experiment starts with exactly the same ROIs.
"""
from __future__ import annotations

import json
import math
import os
import random
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Optional, Sequence

import numpy as np

#: The number of ROIs a spectrometer starts with
N_DEFAULT = 6
#: Where the default ROIs are picked (nm): the visible and near infrared, where the usual spectrometers measure
NOMINAL_RANGE = (450.0, 850.0)
#: The width of the default ROIs is picked between these two values (nm)
DEFAULT_WIDTHS = (4.0, 12.0)
#: What a ROI can give from its pixels: a number...
AGGREGATIONS = ('mean', 'sum', 'median', 'min', 'max', 'std')
#: ... or the pixels themselves (a crop: the part of the spectrum, or of the image, which the ROI covers)
CROP = 'crop'
MODES = AGGREGATIONS + (CROP,)
DEFAULT_AGGREGATION = 'mean'


def aggregate(pixels: np.ndarray, aggregation: str = DEFAULT_AGGREGATION) -> float:
    """One number out of some pixels: their ``mean``, ``sum``, ``median``, ``min``, ``max`` or ``std`` (the standard
    deviation of the pixels, not of an estimate: ``numpy.std``). The sums and the means are made in floats: a sum of
    16 bits pixels does not overflow

    Raises
    ------
    ValueError
        if `aggregation` is not one of :data:`AGGREGATIONS`
    """
    if aggregation == 'mean':
        return float(np.mean(pixels, dtype=float))
    if aggregation == 'sum':
        return float(np.sum(pixels, dtype=float))
    if aggregation == 'median':
        return float(np.median(pixels))
    if aggregation == 'min':
        return float(np.min(pixels))
    if aggregation == 'max':
        return float(np.max(pixels))
    if aggregation == 'std':
        return float(np.std(pixels, dtype=float))
    raise ValueError(f'{aggregation!r} is not a way to make a number of some pixels: {", ".join(AGGREGATIONS)}')


def window_mask(axis: np.ndarray, start: float, stop: float) -> np.ndarray:
    """The pixels of a spectrum inside a window

    Parameters
    ----------
    axis: ndarray
        The wavelength (or any other x value) of each pixel, in any order
    start, stop: float
        The window, in the unit of `axis`, in any order

    Returns
    -------
    ndarray of bool
        The pixels inside the window. A window narrower than the pixels still selects the nearest one, as long as it
        is not farther than a pixel outside of the spectrum (nothing is selected then).
    """
    axis = np.asarray(axis, dtype=float)
    low, high = (start, stop) if start <= stop else (stop, start)
    mask = (axis >= low) & (axis <= high)
    if mask.any() or axis.size == 0:
        return mask
    finite = np.isfinite(axis)
    if not finite.any():
        return mask
    first, last = axis[finite].min(), axis[finite].max()
    pitch = (last - first) / max(axis.size - 1, 1)
    center = (low + high) / 2.
    if center < first - pitch or center > last + pitch:
        return mask
    mask[np.argmin(np.where(finite, np.abs(axis - center), np.inf))] = True
    return mask


def roi_value(axis: np.ndarray, spectrum: np.ndarray, start: float, stop: float,
              aggregation: str = DEFAULT_AGGREGATION) -> float:
    """An aggregation (the mean by default) of the pixels of a spectrum inside a window

    Parameters
    ----------
    axis, spectrum: ndarray
        The x values of the pixels and their intensity
    start, stop: float
        The window, see :func:`window_mask`
    aggregation: str
        One of :data:`AGGREGATIONS`

    Returns
    -------
    float
        nan if the window is outside of the spectrum

    Raises
    ------
    ValueError
        if the spectrum and its axis do not have the same number of pixels, or `aggregation` is unknown
    """
    axis = np.asarray(axis, dtype=float)
    spectrum = np.asarray(spectrum, dtype=float)
    if axis.shape != spectrum.shape:
        raise ValueError(f'The spectrum has {spectrum.size} pixels but its axis has {axis.size}')
    if aggregation not in AGGREGATIONS:  # (a mistake, even for a window outside of the spectrum)
        raise ValueError(f'{aggregation!r} is not a way to make a number of some pixels: {", ".join(AGGREGATIONS)}')
    mask = window_mask(axis, start, stop)
    if not mask.any():
        return float('nan')
    return aggregate(spectrum[mask], aggregation)


@dataclass(frozen=True)
class Roi:
    """A window of the spectrum of a spectrometer

    Parameters
    ----------
    name: str
        What it is called in the plots and in the file
    start, stop: float
        The edges of the window, in the unit of the spectrum axis (nm), ``start <= stop``
    record: bool
        Whether it is saved in the file (its value, or the part of the spectrum if it is a crop)
    plot: bool
        Whether its value is plotted (a crop has none)
    aggregation: str
        What it gives at each measurement: one of :data:`AGGREGATIONS`, the number made of its pixels, or :data:`CROP`, the
        part of the spectrum its window covers
    """
    name: str
    start: float
    stop: float
    record: bool = True
    plot: bool = True
    aggregation: str = DEFAULT_AGGREGATION

    @property
    def is_crop(self) -> bool:
        """It gives its pixels, not a number"""
        return self.aggregation == CROP

    @property
    def center(self) -> float:
        return (self.start + self.stop) / 2.

    @property
    def width(self) -> float:
        return self.stop - self.start

    def problems(self) -> list[str]:
        """What is wrong with this ROI (empty if it is valid)"""
        if self.aggregation not in MODES:
            return [f'{self.name}: {self.aggregation!r} is not one of {", ".join(MODES)}']
        if not (math.isfinite(self.start) and math.isfinite(self.stop)):
            return [f'{self.name}: the edges are not finite numbers']
        if self.start > self.stop:
            return [f'{self.name}: it starts ({self.start:g}) after it stops ({self.stop:g})']
        return []

    def value(self, axis: np.ndarray, spectrum: np.ndarray) -> float:
        """The aggregation of the spectrum in this window (nan if it is outside, and for a crop: it has no value)"""
        if self.is_crop:
            return float('nan')
        return roi_value(axis, spectrum, self.start, self.stop, self.aggregation)

    def mask(self, axis: np.ndarray) -> np.ndarray:
        """The pixels of a spectrum with this x axis which are in this window (see :func:`window_mask`)"""
        return window_mask(axis, self.start, self.stop)

    def label(self, units: str = 'nm', with_range: bool = False) -> str:
        """The name, followed by what it gives and the window if asked: ``ROI 1 sum (450-455 nm)``"""
        if not with_range:
            return self.name
        suffix = f' {units}' if units else ''
        return f'{self.name} {self.aggregation} ({self.start:.6g}-{self.stop:.6g}{suffix})'

    def to_dict(self) -> dict:
        return {'name': self.name, 'start': float(self.start), 'stop': float(self.stop),
                'record': bool(self.record), 'plot': bool(self.plot), 'aggregation': self.aggregation}

    @classmethod
    def from_dict(cls, values: dict) -> 'Roi':
        """Read a ROI as saved by :meth:`to_dict`

        Raises
        ------
        ValueError
            if what was saved is not a ROI
        """
        try:
            aggregation = values.get('aggregation', DEFAULT_AGGREGATION)  # (a file made before it existed has none)
            roi = cls(name=str(values['name']), start=float(values['start']), stop=float(values['stop']),
                      record=bool(values.get('record', True)), plot=bool(values.get('plot', True)),
                      aggregation=aggregation if aggregation in MODES else DEFAULT_AGGREGATION)
        except (AttributeError, KeyError, TypeError, ValueError) as e:
            raise ValueError(f'Not a ROI: {values!r}') from e
        if roi.problems():
            raise ValueError('; '.join(roi.problems()))
        return roi


def default_rois(unit: int = 1, n: int = N_DEFAULT, x_range: tuple[float, float] = NOMINAL_RANGE) -> list[Roi]:
    """The ROIs a spectrometer starts with: `n` windows picked at random, one in each n-th of the range

    They never overlap, and they are always the same for a given unit: the generator is seeded with its number.

    Parameters
    ----------
    unit: int
        The number of the spectrometer (1, 2): each one gets other windows
    n: int
        How many ROIs
    x_range: tuple of float
        Where to pick them
    """
    rng = random.Random(1000 + int(unit))
    low, high = x_range
    edges = np.linspace(low, high, n + 1)
    rois = []
    for index in range(n):
        width = round(rng.uniform(*DEFAULT_WIDTHS), 1)
        margin = width / 2. + 1.
        center = rng.uniform(edges[index] + margin, edges[index + 1] - margin)
        start = round(center - width / 2., 1)
        rois.append(Roi(f'ROI {index + 1}', start, round(start + width, 1)))
    return rois


def default_store_path() -> Path:
    """Where the ROIs are remembered: in the PyMoDAQ folder of the user (``~/.pymodaq/thermospectro/rois.json``)"""
    from pymodaq_utils.config import get_set_config_dir
    return get_set_config_dir('thermospectro', user=True) / 'rois.json'


class RoiStore:
    """The ROIs of each spectrometer, kept in a json file between two experiments

    The file is read each time (nothing is cached) and replaced as a whole when it is written, so it never holds half
    of a write. A file which cannot be read is not an error: it is kept as ``rois.json.bad`` and the spectrometers
    start with the default ROIs.

    Parameters
    ----------
    path: Path
        The json file, created when the first ROIs are saved
    """
    VERSION = 1

    def __init__(self, path: Path):
        self.path = Path(path)

    def _read(self) -> dict:
        try:
            text = self.path.read_text(encoding='utf-8')
        except FileNotFoundError:
            return {}
        except OSError:
            return {}
        try:
            content = json.loads(text)
            units = content['units']
            if not isinstance(units, dict):
                raise TypeError('units')
        except (ValueError, KeyError, TypeError):
            try:
                os.replace(self.path, self.path.with_name(self.path.name + '.bad'))
            except OSError:
                pass
            return {}
        return units

    def get(self, unit: int, roi_class=Roi) -> Optional[list]:
        """The ROIs saved for a spectrometer, None if there are none (or they cannot be read)

        `roi_class` is the kind of ROI of the unit (:class:`Roi`, or the :class:`~.camera.RectRoi` of a camera): ROIs
        of another kind are not ROIs of this unit.
        """
        entry = self._read().get(f'unit{unit}')
        if not isinstance(entry, dict) or not isinstance(entry.get('rois'), list):
            return None
        try:
            return [roi_class.from_dict(item) for item in entry['rois']]
        except (ValueError, TypeError):
            return None

    def save(self, unit: int, rois: Iterable[Roi]):
        """Remember the ROIs of a spectrometer (the ones of the other spectrometers are kept)"""
        units = self._read()
        units[f'unit{unit}'] = {'rois': [roi.to_dict() for roi in rois]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + '.tmp')
        temporary.write_text(json.dumps({'version': self.VERSION, 'units': units}, indent=2), encoding='utf-8')
        os.replace(temporary, self.path)

    def load(self, unit: int, roi_class=Roi, defaults=None) -> list:
        """The ROIs of a spectrometer: the saved ones, else the default ones (which are then saved, so that they
        are the same next time). A camera gives its `roi_class` and the function making its `defaults`"""
        rois = self.get(unit, roi_class)
        if rois is None:
            rois = (defaults or default_rois)(unit)
            try:
                self.save(unit, rois)
            except OSError:
                pass  # the ROIs are not remembered, but the experiment does not depend on it
        return rois


def unique_names(rois: Sequence[Roi]) -> list[str]:
    """The names of the ROIs, each followed by its number if the name is already taken by an earlier ROI"""
    seen: dict[str, int] = {}
    names = []
    for roi in rois:
        count = seen.get(roi.name, 0)
        seen[roi.name] = count + 1
        names.append(roi.name if count == 0 else f'{roi.name} ({count + 1})')
    return names


def renamed(rois: Sequence[Roi]) -> list[Roi]:
    """The ROIs with unique names"""
    return [replace(roi, name=name) for roi, name in zip(rois, unique_names(rois))]
