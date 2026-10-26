"""What the extension needs to know about a camera: its images, and the rectangular regions of interest (ROI) on them

The camera of the extension is a 2D detector of the Dashboard (a Thorlabs camera, or any other one). Each readout takes
one image. Each ROI gives, at each readout, either **a number**, an aggregation of its pixels (their mean, sum, median,
minimum, maximum or standard deviation: :data:`AGGREGATIONS`), followed in time like the ROIs of a spectrum, or **its
pixels**, when it is a **crop** (:data:`CROP`): they are saved as images, and there is nothing to plot. The edges of a ROI
are in pixels: ``x`` along the columns, ``y`` along the rows (the origin is the corner of the first pixel, which spans
from 0 to 1).

A camera which gives colors gives several 2D arrays, its **channels** (the image viewer of PyMoDAQ shows them as red,
green and blue, in this order). A ROI which gives a number is measured on the channels it is told to (all of them by
default): the aggregation is made on each one and the results are **averaged**, so that with green and blue and the
standard deviation a ROI gives ``(std(green) + std(blue)) / 2``.

Pure numpy and json, no Qt: it can be tested alone, see :mod:`.rois` for the ROIs of a spectrum.
"""
from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass, replace
from typing import Optional, Sequence

import numpy as np

from pymodaq_data import DataToExport
from pymodaq_data.data import DataDim, DataSource, DataWithAxes

# (the choice of what a ROI gives is the same for the ROIs of a spectrum: it is defined with them)
from pymodaq_plugins_thermospectro.rois import AGGREGATIONS, CROP, DEFAULT_AGGREGATION, MODES, aggregate  # noqa: F401

#: The number of ROIs a camera starts with
N_DEFAULT = 4
#: The image the default ROIs are made for (pixels): they are fitted to the real size when the first image comes
NOMINAL_SHAPE = (1000, 1000)
#: The side of the default ROIs is picked between these two fractions of the image
DEFAULT_SIDES = (0.12, 0.25)
UNITS = 'pixel'
#: The channels of an image, in the order of the viewer of PyMoDAQ (a fourth one and the next ones are numbers)
CHANNEL_NAMES = ('red', 'green', 'blue')
#: What can be written for a channel in the settings, besides its number counted from 1
CHANNEL_WORDS = {'r': 0, 'red': 0, 'g': 1, 'green': 1, 'b': 2, 'blue': 2}
ALL_CHANNELS = 'all'
#: What the user is told of the channels a ROI is measured on, wherever they are chosen
CHANNELS_RULE = ('If two channels or more are selected, the operation (mean, sum, median, min, max or std) is done on each of '
                 'them and the results are averaged: with green and blue selected and std, the value is the mean of '
                 'std(green) and std(blue). At least one channel stays selected. A crop keeps all the channels.')


def channel_name(index: int) -> str:
    """``red``, ``green``, ``blue``, then ``ch4``, ``ch5``... for the channel `index` (counted from 0)"""
    return CHANNEL_NAMES[index] if 0 <= index < len(CHANNEL_NAMES) else f'ch{index + 1}'


def channel_letter(index: int) -> str:
    """``R``, ``G``, ``B``, then ``4``, ``5``...: what is written on the box of the channel `index` (counted from 0)"""
    return 'RGB'[index] if 0 <= index < 3 else str(index + 1)


def selected_channels(channels: Optional[Sequence[int]], n: int) -> tuple[int, ...]:
    """The channels (counted from 0) of an image of `n` channels which a ROI is measured on

    All of them for None, and also for a choice which is none of the channels of this image (a ROI made for a color camera
    and given a gray one): the ROI is never left with nothing to measure.
    """
    if channels is not None:
        chosen = tuple(sorted({int(index) for index in channels if 0 <= int(index) < n}))
        if chosen:
            return chosen
    return tuple(range(max(int(n), 0)))


def fit_channels(channels: Optional[Sequence[int]], n: int) -> Optional[tuple[int, ...]]:
    """A choice of channels as it is for an image of `n` channels: None if it is all of them"""
    chosen = selected_channels(channels, n)
    return None if len(chosen) == n else chosen


def parse_channels(text) -> Optional[tuple[int, ...]]:
    """The channels written in the settings, counted from 0: ``all`` is None, else numbers counted from 1 (``2, 3``) and/or
    colors (``green blue``, ``G+B``); what is not understood is ignored, and a text with no channel in it is ``all``"""
    found = set()
    for token in re.findall(r'[A-Za-z]+|\d+', str(text)):
        index = int(token) - 1 if token.isdigit() else CHANNEL_WORDS.get(token.lower())
        if index is not None and index >= 0:
            found.add(index)
    return tuple(sorted(found)) if found else None


def format_channels(channels: Optional[Sequence[int]]) -> str:
    """The channels as they are written in the settings: ``all``, or their numbers counted from 1 (``2, 3``)"""
    if channels is None:
        return ALL_CHANNELS
    return ', '.join(str(int(index) + 1) for index in sorted(channels))


def channels_name(channels: Sequence[int]) -> str:
    """``green+blue``: what the channels are called, in a label"""
    return '+'.join(channel_name(int(index)) for index in sorted(channels))


def as_channels(image) -> list[np.ndarray]:
    """The 2D arrays of an image: the image itself if it is 2D, else its channels (a sequence of 2D arrays)

    Raises
    ------
    ValueError
        if there is no 2D array in it
    """
    if isinstance(image, np.ndarray) and image.ndim == 2:
        return [image]
    arrays = [np.asarray(array) for array in image]
    if not arrays or any(array.ndim != 2 for array in arrays):
        raise ValueError('A 2D image, or a sequence of 2D images (the channels), is needed')
    return arrays


def pixel_range(low: float, high: float, size: int) -> tuple[int, int]:
    """The pixels of a line of `size` pixels which a window ``low`` to ``high`` covers, as ``(first, stop)``

    A window narrower than a pixel still selects one, as long as it is inside the image; ``first == stop`` means that
    nothing is selected (the window is outside).
    """
    low, high = (low, high) if low <= high else (high, low)
    first = int(math.floor(low + 0.5))
    stop = max(int(math.floor(high + 0.5)), first + 1)
    first, stop = max(first, 0), min(stop, size)
    return (first, stop) if stop > first else (0, 0)


def window_ranges(shape: tuple[int, int], x0: float, x1: float, y0: float, y1: float
                  ) -> Optional[tuple[tuple[int, int], tuple[int, int]]]:
    """The rows and the columns of an image of this shape which a window covers, as ``((row0, row1), (column0, column1))``
    (stops excluded), None if the window is outside of the image"""
    columns = pixel_range(x0, x1, shape[1])
    rows = pixel_range(y0, y1, shape[0])
    if columns[1] <= columns[0] or rows[1] <= rows[0]:
        return None
    return rows, columns


def rect_pixels(image: np.ndarray, x0: float, x1: float, y0: float, y1: float) -> Optional[np.ndarray]:
    """The pixels of an image inside a window (a view of the image, not a copy)

    Parameters
    ----------
    image: ndarray
        2D, rows first (``image[y, x]``)
    x0, x1, y0, y1: float
        The window in pixels, each pair in any order

    Returns
    -------
    ndarray or None
        None if the window is outside of the image

    Raises
    ------
    ValueError
        if `image` is not 2D
    """
    image = np.asarray(image)
    if image.ndim != 2:
        raise ValueError(f'A 2D image is needed, not {image.ndim}D data')
    found = window_ranges(image.shape, x0, x1, y0, y1)
    if found is None:
        return None
    (row0, row1), (column0, column1) = found
    return image[row0:row1, column0:column1]


def rect_value(image, x0: float, x1: float, y0: float, y1: float, aggregation: str = DEFAULT_AGGREGATION,
               channels: Optional[Sequence[int]] = None) -> float:
    """An aggregation (the mean by default) of the pixels of an image inside a window

    With several channels the aggregation is made on each of the selected ones, and the results are averaged:
    ``(std(green) + std(blue)) / 2`` for the standard deviation and the green and blue channels.

    Parameters
    ----------
    image: ndarray or sequence of ndarray
        2D, rows first (``image[y, x]``), or the channels of a color image: a sequence of such arrays
    x0, x1, y0, y1: float
        The window in pixels, each pair in any order
    aggregation: str
        One of :data:`AGGREGATIONS`
    channels: sequence of int, optional
        The channels (counted from 0) to measure: all of them by default, see :func:`selected_channels`

    Returns
    -------
    float
        nan if the window is outside of the image

    Raises
    ------
    ValueError
        if `image` is not 2D (or a sequence of 2D arrays), or `aggregation` is unknown
    """
    if aggregation not in AGGREGATIONS:  # (a mistake, even for a window outside of the image)
        raise ValueError(f'{aggregation!r} is not a way to make a number of some pixels: {", ".join(AGGREGATIONS)}')
    arrays = as_channels(image)
    results = []
    for index in selected_channels(channels, len(arrays)):
        pixels = rect_pixels(arrays[index], x0, x1, y0, y1)
        if pixels is None:  # (the window is the same for all the channels)
            return float('nan')
        results.append(aggregate(pixels, aggregation))
    return float(np.mean(results))


@dataclass(frozen=True)
class RectRoi:
    """A rectangular window of the images of a camera

    Parameters
    ----------
    name: str
        What it is called in the plots and in the file
    x0, x1, y0, y1: float
        The edges of the window in pixels, ``x0 <= x1`` and ``y0 <= y1``
    record: bool
        Whether it is saved in the file (its value, or its pixels if it is a crop)
    plot: bool
        Whether its value is plotted (a crop has none)
    aggregation: str
        What it gives at each readout: one of :data:`AGGREGATIONS`, the number made of its pixels, or :data:`CROP`, its
        pixels
    channels: tuple of int, optional
        The channels of the image (counted from 0) the aggregation is made on, the results being averaged; None for all
        of them (a crop always keeps all of them)
    """
    name: str
    x0: float
    x1: float
    y0: float
    y1: float
    record: bool = True
    plot: bool = True
    aggregation: str = DEFAULT_AGGREGATION
    channels: Optional[tuple] = None

    @property
    def is_crop(self) -> bool:
        """It gives its pixels, not a number"""
        return self.aggregation == CROP

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    def problems(self) -> list[str]:
        """What is wrong with this ROI (empty if it is valid)"""
        if not all(math.isfinite(value) for value in (self.x0, self.x1, self.y0, self.y1)):
            return [f'{self.name}: the edges are not finite numbers']
        problems = [] if self.aggregation in MODES else [f'{self.name}: {self.aggregation!r} is not one of '
                                                          f'{", ".join(MODES)}']
        if self.channels is not None and (not self.channels or any(int(index) != index or index < 0
                                                                   for index in self.channels)):
            problems.append(f'{self.name}: the channels are numbers counted from 0, at least one')
        if self.x0 > self.x1:
            problems.append(f'{self.name}: it starts ({self.x0:g}) after it stops ({self.x1:g}) in x')
        if self.y0 > self.y1:
            problems.append(f'{self.name}: it starts ({self.y0:g}) after it stops ({self.y1:g}) in y')
        return problems

    def value(self, image) -> float:
        """The aggregation of the image in this window (nan if it is outside, and for a crop: it has no value)

        `image` is a 2D array, or the channels of a color image (a sequence of 2D arrays): the aggregation is then made on the
        channels of the ROI and averaged, see :func:`rect_value`.
        """
        if self.is_crop:
            return float('nan')
        return rect_value(image, self.x0, self.x1, self.y0, self.y1, self.aggregation, self.channels)

    def pixels(self, image: np.ndarray) -> Optional[np.ndarray]:
        """The pixels of the image in this window (None if it is outside)"""
        return rect_pixels(image, self.x0, self.x1, self.y0, self.y1)

    def label(self, units: str = UNITS, with_range: bool = False) -> str:
        """The name, followed by what it gives and the window if asked: ``ROI 1 sum (x 100-200, y 50-80 pixel)``, and the
        channels if it is not measured on all of them: ``ROI 1 std green+blue (x 100-200, y 50-80 pixel)``"""
        if not with_range:
            return self.name
        suffix = f' {units}' if units else ''
        which = f' {channels_name(self.channels)}' if self.channels is not None and not self.is_crop else ''
        return (f'{self.name} {self.aggregation}{which} '
                f'(x {self.x0:.6g}-{self.x1:.6g}, y {self.y0:.6g}-{self.y1:.6g}{suffix})')

    def scaled(self, factor_x: float, factor_y: float) -> 'RectRoi':
        """The same window on an image whose width and height are this many times the ones it was made for"""
        return replace(self, x0=round(self.x0 * factor_x), x1=round(self.x1 * factor_x),
                       y0=round(self.y0 * factor_y), y1=round(self.y1 * factor_y))

    def to_dict(self) -> dict:
        values = {'name': self.name, 'x0': float(self.x0), 'x1': float(self.x1), 'y0': float(self.y0),
                  'y1': float(self.y1), 'record': bool(self.record), 'plot': bool(self.plot),
                  'aggregation': self.aggregation}
        if self.channels is not None:  # (all of them is the default: nothing is said)
            values['channels'] = [int(index) for index in self.channels]
        return values

    @classmethod
    def from_dict(cls, values: dict) -> 'RectRoi':
        """Read a ROI as saved by :meth:`to_dict`

        Raises
        ------
        ValueError
            if what was saved is not a rectangular ROI
        """
        try:
            aggregation = values.get('aggregation', DEFAULT_AGGREGATION)  # (a file made before it existed has none)
            channels = values.get('channels')  # (neither has a file made before the channels could be chosen)
            try:
                channels = tuple(sorted({int(index) for index in channels if int(index) >= 0})) or None
            except (TypeError, ValueError):
                channels = None  # (not a list of channels: all of them)
            roi = cls(name=str(values['name']), x0=float(values['x0']), x1=float(values['x1']),
                      y0=float(values['y0']), y1=float(values['y1']), record=bool(values.get('record', True)),
                      plot=bool(values.get('plot', True)),
                      aggregation=aggregation if aggregation in MODES else DEFAULT_AGGREGATION, channels=channels)
        except (AttributeError, KeyError, TypeError, ValueError) as e:
            raise ValueError(f'Not a rectangular ROI: {values!r}') from e
        if roi.problems():
            raise ValueError('; '.join(roi.problems()))
        return roi


def default_rect_rois(unit: int = 3, n: int = N_DEFAULT, shape: tuple[int, int] = NOMINAL_SHAPE) -> list[RectRoi]:
    """The ROIs a camera starts with: `n` squares picked at random, one in each of the regions of a grid

    They never overlap, and they are always the same for a given unit: the generator is seeded with its number.

    Parameters
    ----------
    unit: int
        The number of the unit
    n: int
        How many ROIs
    shape: tuple of int
        (rows, columns) of the image they are made for
    """
    rng = random.Random(2000 + int(unit))
    rows, columns = shape
    n_columns = max(1, math.ceil(math.sqrt(n)))
    n_rows = max(1, math.ceil(n / n_columns))
    rois = []
    for index in range(n):
        row, column = divmod(index, n_columns)
        cell_x, cell_y = columns / n_columns, rows / n_rows
        side = rng.uniform(*DEFAULT_SIDES) * min(columns, rows)
        side = min(side, 0.9 * min(cell_x, cell_y))
        x0 = rng.uniform(column * cell_x + 0.05 * cell_x, (column + 1) * cell_x - side - 0.05 * cell_x)
        y0 = rng.uniform(row * cell_y + 0.05 * cell_y, (row + 1) * cell_y - side - 0.05 * cell_y)
        rois.append(RectRoi(f'ROI {index + 1}', round(x0), round(x0 + side), round(y0), round(y0 + side)))
    return rois


def fit_to_image(rois: list[RectRoi], shape: tuple[int, int]) -> list[RectRoi]:
    """The default ROIs of the nominal image, on an image of another size"""
    return [roi.scaled(shape[1] / NOMINAL_SHAPE[1], shape[0] / NOMINAL_SHAPE[0]) for roi in rois]


@dataclass
class Frame:
    """The last image received from a camera"""
    y: np.ndarray  # 2D, rows first: the same name as the intensities of a Spectrum, for what they have in common
    time: float  # when it arrived (time.monotonic())
    dwa: DataWithAxes
    units: str = UNITS
    axis: Optional[object] = None  # a camera has no axis to save along with its data: the dwa has its own

    @property
    def image(self) -> np.ndarray:
        return self.y

    @property
    def shape(self) -> tuple[int, int]:
        return self.y.shape

    @property
    def channels(self) -> list[np.ndarray]:
        """The 2D arrays of all the channels, as the camera gave them (a ROI takes its pixels from them before it computes
        anything: they are not converted)"""
        arrays = [np.asarray(array) for array in self.dwa.data]
        return [array for array in arrays if array.ndim == 2 and array.shape == self.y.shape] or [self.y]

    @property
    def n_channels(self) -> int:
        return len(self.channels)


def extract_frame(dte: DataToExport) -> Optional[tuple[DataWithAxes, np.ndarray]]:
    """The first raw 2D data of what a detector emitted, as (data, image)

    A camera which gives colors (several 2D arrays) is shown with its channels; the image returned is the first one (see
    :attr:`Frame.channels` for all of them: the ROIs are measured on the channels they are told to).
    """
    for dwa in dte:
        if dwa.dim == DataDim.Data2D and dwa.source == DataSource.raw and len(dwa.data) > 0:
            image = np.asarray(dwa.data[0], dtype=float)
            if image.ndim == 2 and image.size > 0:
                return dwa, image
    return None
