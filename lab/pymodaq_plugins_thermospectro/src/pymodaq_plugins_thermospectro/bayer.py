"""The Bayer mosaic of a color camera, as raw counts: how to read its colors without changing a value

A color sensor puts a red, green or blue filter in front of each pixel. The driver can give the raw mosaic (one channel,
the counts of the ADC) or its own conversion (interpolation, white balance and color matrices, which change the values).
The extension asks for the mosaic and, when a color image is wanted, :func:`debayer` interpolates the missing colors of
each pixel from its neighbours **without any matrix or gain**: the pixels which were measured keep their count, the others
are the mean of the neighbours of that color. Pure numpy, no Qt.
"""
from __future__ import annotations

import numpy as np

from pymodaq_data.data import DataRaw, DataWithAxes

#: (row, column) of a red pixel in the first 2x2 block, by the name the driver (pylablib) gives to the phase of the sensor
#: (its spelling "green_left_or_red" is a typo of the driver: both are known)
PHASE_OFFSETS = {'red': (0, 0), 'blue': (1, 1), 'green_left_of_red': (0, 1), 'green_left_of_blue': (1, 0),
                 'green_left_or_red': (0, 1), 'green_left_or_blue': (1, 0)}
LABELS = ['Red', 'Green', 'Blue']
BAYER = 'bayer'
RGB = 'rgb'  # (interpolated)
RGB_MEASURED = 'rgb_measured'  # (not interpolated: nan where a color was not measured)
#: What the file says about the colors which were interpolated, and about those which were not
NO_INTERPOLATION = 'none'
BILINEAR = ('bilinear: each color a pixel did not measure is the mean of the pixels of that color among its 8 '
            'neighbours (2 or 4 of them; fewer at the edges of the image), the pixels which measured a color keep '
            'their count; no white balance, no color matrix, nothing clipped')


def phase_offset(phase) -> tuple[int, int]:
    """(row, column) of a red pixel for the phase the driver reports (red at the origin if it is not known)"""
    return PHASE_OFFSETS.get(str(phase).lower().replace(' ', '_'), (0, 0))


def masks(shape: tuple[int, int], offset: tuple[int, int]) -> np.ndarray:
    """Which pixels are measured through a red, a green and a blue filter: bool array ``(3, rows, columns)``"""
    rows, columns = np.indices(shape)
    r0, c0 = offset
    red = ((rows - r0) % 2 == 0) & ((columns - c0) % 2 == 0)
    blue = ((rows - r0) % 2 == 1) & ((columns - c0) % 2 == 1)
    return np.stack([red, ~(red | blue), blue])


def _box(array: np.ndarray) -> np.ndarray:
    """Sum of each pixel and its 8 neighbours (zero outside of the image)"""
    padded = np.pad(array, 1)
    rows, columns = array.shape
    return sum(padded[i:i + rows, j:j + columns] for i in range(3) for j in range(3))


def debayer(mosaic, offset: tuple[int, int] = (0, 0), interpolate: bool = True) -> np.ndarray:
    """The mosaic as a color image ``(3, rows, columns)`` (red, green, blue) in float32, with no change of the counts

    A pixel keeps the count it measured in its own color; its other colors are the mean of the measured neighbours of that
    color (bilinear interpolation). Nothing is scaled, multiplied or clipped.

    Without interpolation (`interpolate` False) the other colors of a pixel are **nan**: each channel only holds the pixels
    which measured that color, at their place in the image (the image has as many pixels as the mosaic, a red one for
    each 4 of them, a green one for each 2, a blue one for each 4).
    """
    mosaic = np.asarray(mosaic, dtype=np.float32)
    if mosaic.ndim != 2:
        raise ValueError('The Bayer mosaic is a 2D image')
    found = masks(mosaic.shape, offset)
    if not interpolate:
        out = np.full((3,) + mosaic.shape, np.nan, dtype=np.float32)
        for index in range(3):
            out[index][found[index]] = mosaic[found[index]]
        return out
    out = np.empty((3,) + mosaic.shape, dtype=np.float32)
    for index in range(3):
        mask = found[index].astype(np.float32)
        weight = _box(mask)
        mean = _box(mosaic * mask) / np.maximum(weight, 1)
        out[index] = np.where(found[index], mosaic, mean)
    return out


def rgb_data(dwa: DataWithAxes, offset: tuple[int, int], interpolate: bool = True) -> DataWithAxes:
    """The mosaic of `dwa` as a color image (channels red, green, blue, labeled), on the same axes (see :func:`debayer`)"""
    colors = debayer(dwa.data[0], offset, interpolate)
    return DataRaw(dwa.name, data=[colors[0], colors[1], colors[2]], axes=list(dwa.axes), labels=list(LABELS),
                   units=dwa.units, source=dwa.source)
