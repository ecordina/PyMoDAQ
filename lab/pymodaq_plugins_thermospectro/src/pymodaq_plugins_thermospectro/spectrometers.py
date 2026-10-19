"""What the extension needs to know about the spectrometers, without depending on a particular plugin"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Optional

import numpy as np

from pymodaq_data import DataToExport, Q_, Unit
from pymodaq_data.data import Axis, DataDim, DataSource, DataWithAxes
from pymodaq_gui.parameter import Parameter

#: The names under which the plugins declare their integration time, always in ms
INTEGRATION_NAMES = ('integration', 'integration_time', 'exposure_time')


def _walk(param: Parameter) -> Iterator[Parameter]:
    for child in param.children():
        yield child
        yield from _walk(child)


def find_integration_params(module) -> list[Parameter]:
    """The settings of a detector module holding its integration time (there is one per spectrometer in a module
    driving several of them)"""
    return [param for param in _walk(module.settings)
            if param.name() in INTEGRATION_NAMES and param.type() in ('float', 'int')]


def _limits(param: Parameter) -> tuple[Optional[float], Optional[float]]:
    limits = param.opts.get('limits')
    if isinstance(limits, (list, tuple, np.ndarray)) and len(limits) == 2:
        return limits[0], limits[1]
    return param.opts.get('min'), param.opts.get('max')


def set_integration_time(module, milliseconds: float) -> Optional[float]:
    """Set the integration time of a spectrometer module, keeping it within what the instrument allows

    Returns
    -------
    float or None
        The integration time that was set (ms), None if the plugin of the module has no such setting
    """
    params = find_integration_params(module)
    if not params:
        return None
    applied = None
    for param in params:
        low, high = _limits(param)
        value = float(milliseconds)
        if low is not None:
            value = max(value, float(low))
        if high is not None:
            value = min(value, float(high))
        if param.type() == 'int':
            value = int(round(value))
        param.setValue(value)
        applied = float(param.value()) if applied is None else applied
    return applied


def wavelength_axis(values: np.ndarray, units: str) -> tuple[np.ndarray, str]:
    """The values and the units the channels are chosen on: nm when the axis is a length, as it is otherwise"""
    values = np.asarray(values, dtype=float)
    if units and units != 'nm':
        try:
            if Unit(units).is_compatible_with(Unit('nm')):
                return Q_(values, units).m_as('nm'), 'nm'
        except Exception:  # not a unit pint knows: the axis is used as it is
            pass
    return values, units


@dataclass
class Spectrum:
    """The last spectrum received from a spectrometer"""
    axis: Axis  # as the plugin gave it: saved with the data
    y: np.ndarray
    time: float  # when it arrived (time.monotonic())
    dwa: DataWithAxes
    x: np.ndarray  # what the ROIs and the plot use: nm when the axis is a length, else the axis as it is
    units: str


@dataclass
class Reading:
    """The last temperature received from a thermostat"""
    value: float
    time: float  # when it arrived (time.monotonic())


def extract_spectrum(dte: DataToExport, known_axis: Optional[Axis] = None
                     ) -> Optional[tuple[DataWithAxes, np.ndarray, Axis]]:
    """The first raw 1D data of what a detector emitted, as (data, intensities, x axis)

    A plugin may send the axis only when it changes (the viewers remember it): when there is none in the data,
    the `known_axis` is used if it fits, else the pixel number.
    """
    for dwa in dte:
        if dwa.dim == DataDim.Data1D and dwa.source == DataSource.raw and len(dwa.data) > 0:
            y = np.asarray(dwa.data[0], dtype=float).ravel()
            axes = [axis for axis in dwa.axes if axis.index == 0]  # (get_axis_from_index warns if there is none)
            for axis in axes + [known_axis]:
                if axis is not None and axis.size == y.size:
                    return dwa, y, axis
            return dwa, y, Axis(label='pixel', units='', data=np.arange(y.size, dtype=float), index=0)
    return None
