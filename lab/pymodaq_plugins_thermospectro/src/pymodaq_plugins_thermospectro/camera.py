"""What the extension needs to know about a camera: its images, and the rectangular regions of interest (ROI) on them

The camera of the extension is a 2D detector of the Dashboard (a Thorlabs camera, or any other one). Each readout takes
one image, and the value of each ROI is the mean of its pixels, followed in time like the ROIs of a spectrum. The edges
of a ROI are in pixels: ``x`` along the columns, ``y`` along the rows (the origin is the corner of the first pixel,
which spans from 0 to 1).

Pure numpy and json, no Qt: it can be tested alone, see :mod:`.rois` for the ROIs of a spectrum.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, replace
from typing import Optional

import numpy as np

from pymodaq_data import DataToExport
from pymodaq_data.data import DataDim, DataSource, DataWithAxes

#: The number of ROIs a camera starts with
N_DEFAULT = 4
#: The image the default ROIs are made for (pixels): they are fitted to the real size when the first image comes
NOMINAL_SHAPE = (1000, 1000)
#: The side of the default ROIs is picked between these two fractions of the image
DEFAULT_SIDES = (0.12, 0.25)
UNITS = 'pixel'


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


def rect_value(image: np.ndarray, x0: float, x1: float, y0: float, y1: float) -> float:
    """The mean of the pixels of an image inside a window

    Parameters
    ----------
    image: ndarray
        2D, rows first (``image[y, x]``)
    x0, x1, y0, y1: float
        The window in pixels, each pair in any order

    Returns
    -------
    float
        nan if the window is outside of the image

    Raises
    ------
    ValueError
        if `image` is not 2D
    """
    image = np.asarray(image)
    if image.ndim != 2:
        raise ValueError(f'A 2D image is needed, not {image.ndim}D data')
    columns = pixel_range(x0, x1, image.shape[1])
    rows = pixel_range(y0, y1, image.shape[0])
    if columns[1] <= columns[0] or rows[1] <= rows[0]:
        return float('nan')
    return float(np.mean(image[rows[0]:rows[1], columns[0]:columns[1]], dtype=float))


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
        Whether its value is saved in the file
    plot: bool
        Whether its value is plotted
    """
    name: str
    x0: float
    x1: float
    y0: float
    y1: float
    record: bool = True
    plot: bool = True

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
        problems = []
        if self.x0 > self.x1:
            problems.append(f'{self.name}: it starts ({self.x0:g}) after it stops ({self.x1:g}) in x')
        if self.y0 > self.y1:
            problems.append(f'{self.name}: it starts ({self.y0:g}) after it stops ({self.y1:g}) in y')
        return problems

    def value(self, image: np.ndarray) -> float:
        """The mean of the image in this window (nan if it is outside)"""
        return rect_value(image, self.x0, self.x1, self.y0, self.y1)

    def label(self, units: str = UNITS, with_range: bool = False) -> str:
        """The name, followed by the window if asked: ``ROI 1 (x 100-200, y 50-80 pixel)``"""
        if not with_range:
            return self.name
        suffix = f' {units}' if units else ''
        return f'{self.name} (x {self.x0:.6g}-{self.x1:.6g}, y {self.y0:.6g}-{self.y1:.6g}{suffix})'

    def scaled(self, factor_x: float, factor_y: float) -> 'RectRoi':
        """The same window on an image whose width and height are this many times the ones it was made for"""
        return replace(self, x0=round(self.x0 * factor_x), x1=round(self.x1 * factor_x),
                       y0=round(self.y0 * factor_y), y1=round(self.y1 * factor_y))

    def to_dict(self) -> dict:
        return {'name': self.name, 'x0': float(self.x0), 'x1': float(self.x1), 'y0': float(self.y0),
                'y1': float(self.y1), 'record': bool(self.record), 'plot': bool(self.plot)}

    @classmethod
    def from_dict(cls, values: dict) -> 'RectRoi':
        """Read a ROI as saved by :meth:`to_dict`

        Raises
        ------
        ValueError
            if what was saved is not a rectangular ROI
        """
        try:
            roi = cls(name=str(values['name']), x0=float(values['x0']), x1=float(values['x1']),
                      y0=float(values['y0']), y1=float(values['y1']), record=bool(values.get('record', True)),
                      plot=bool(values.get('plot', True)))
        except (KeyError, TypeError, ValueError) as e:
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


def extract_frame(dte: DataToExport) -> Optional[tuple[DataWithAxes, np.ndarray]]:
    """The first raw 2D data of what a detector emitted, as (data, image)

    A camera which gives colors (several 2D arrays) is shown and measured through its first channel.
    """
    for dwa in dte:
        if dwa.dim == DataDim.Data2D and dwa.source == DataSource.raw and len(dwa.data) > 0:
            image = np.asarray(dwa.data[0], dtype=float)
            if image.ndim == 2 and image.size > 0:
                return dwa, image
    return None
