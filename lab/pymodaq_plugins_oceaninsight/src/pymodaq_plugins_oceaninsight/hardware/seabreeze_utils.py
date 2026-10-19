# -*- coding: utf-8 -*-
"""
Helpers of the Seabreeze viewer plugin that do not need any spectrometer to be connected:

* the python-seabreeze backends (cseabreeze, pyseabreeze) and the way to open a spectrometer with the one chosen for it
* the handling of the electric dark pixels of the detector
"""
import importlib
import re
import threading
from typing import Iterable, Iterator, Optional

import numpy as np

#: the two backends of python-seabreeze
BACKENDS = ('pyseabreeze', 'cseabreeze')
#: the backend of a spectrometer unless the Backend list of its module says otherwise
DEFAULT_BACKEND = 'pyseabreeze'
#: the entry of the Device list that lets the plugin choose the spectrometer, see :func:`find_device`
AUTO = 'Auto'

_BACKEND_HINTS = {
    'cseabreeze': "It is the compiled extension coming with the seabreeze package: reinstall seabreeze or "
                  "choose the 'pyseabreeze' backend.",
    'pyseabreeze': "It needs the pyusb package (pip install pyusb) and the libusb library: install them or "
                   "choose the 'cseabreeze' backend.",
}


# ---------------------------------------------------------------------------------------------------------------------
# Names of the backends
# ---------------------------------------------------------------------------------------------------------------------

def check_backend(name) -> str:
    """ Check the name of a backend

    Parameters
    ----------
    name: str
        'cseabreeze' (compiled libseabreeze) or 'pyseabreeze' (pure python, needs pyusb)

    Returns
    -------
    str: the name of the backend, in lower case and without surrounding spaces

    Raises
    ------
    ValueError
        if name is not the name of a backend
    """
    backend = str(name).strip().lower()
    if backend not in BACKENDS:
        raise ValueError(f"Unknown seabreeze backend '{name}', it should be one of: {', '.join(BACKENDS)}")
    return backend


# ---------------------------------------------------------------------------------------------------------------------
# Using a backend
#
# python-seabreeze selects its backend for the whole python process (seabreeze.use) before the first spectrometer is
# used. Here the backend is chosen for each spectrometer when it is initialized, so the backends are used directly:
# nothing depends on the process wide selection of python-seabreeze.
# ---------------------------------------------------------------------------------------------------------------------

def load_backend(name: str):
    """ Import a python-seabreeze backend

    Parameters
    ----------
    name: str
        the name of the backend, see :data:`BACKENDS`

    Returns
    -------
    module: seabreeze.cseabreeze or seabreeze.pyseabreeze

    Raises
    ------
    ImportError
        if the backend cannot be imported (pyseabreeze without the pyusb package for instance), with the reason and
        what to do about it
    """
    name = check_backend(name)
    try:
        return importlib.import_module(f'seabreeze.{name}')
    except ImportError as e:
        raise ImportError(f"The seabreeze backend '{name}' cannot be loaded: {e}. {_BACKEND_HINTS[name]}") from e


_apis = {}
_spectrometer_classes = {}
_lock = threading.Lock()


def get_api(name: str):
    """ Get the SeaBreezeAPI of a backend

    It is created once for the whole process: creating the API of pyseabreeze resets all the Ocean Optics USB devices,
    which would break the spectrometers that are open.
    """
    name = check_backend(name)
    with _lock:
        if name not in _apis:
            _apis[name] = load_backend(name).SeaBreezeAPI()
        return _apis[name]


def list_devices(name: str) -> list:
    """ List the spectrometers seen by a backend, sorted by serial number

    The backend briefly opens them to read their serial number. The order does not depend on the order in which the
    USB bus gives them, so the first spectrometer is always the same one as long as the same ones are connected.
    """
    return sorted_devices(get_api(name).list_devices())


def spectrometer_class(name: str):
    """ Get the seabreeze Spectrometer class that uses a given backend

    Parameters
    ----------
    name: str
        the name of the backend, see :data:`BACKENDS`

    Returns
    -------
    A subclass of seabreeze.spectrometers.Spectrometer whose devices are the ones of the backend, whatever the backend
    python-seabreeze selected for the process

    Raises
    ------
    ImportError
        if the backend cannot be loaded or if seabreeze is too old: it can be chosen for each spectrometer from the
        version 2.9.0
    """
    name = check_backend(name)
    with _lock:
        if name not in _spectrometer_classes:
            from seabreeze.spectrometers import Spectrometer
            if '_backend' not in vars(Spectrometer):  # (not getattr: it would make seabreeze select its backend)
                raise ImportError("The seabreeze backend can be chosen for each spectrometer from seabreeze 2.9.0, "
                                  "please update it: pip install -U seabreeze")
            _spectrometer_classes[name] = type(f'Spectrometer_{name}', (Spectrometer,),
                                               {'_backend': load_backend(name)})
        return _spectrometer_classes[name]


_REPR_SERIAL = re.compile(r'^<SeaBreezeDevice [^:>]*:([^>]*)>$')


def device_serial(device) -> Optional[str]:
    """ Get the serial number of an item of the Device list

    Parameters
    ----------
    device: SeaBreezeDevice or str
        a device, or its representation as it is saved in the presets (<SeaBreezeDevice FLAMES:FLMS12345>)

    Returns
    -------
    str: the serial number, None if there is none (nothing is selected or the serial number is not known)
    """
    serial = getattr(device, 'serial_number', None)
    if serial is None and isinstance(device, str):
        match = _REPR_SERIAL.match(device.strip())
        serial = match.group(1) if match else None
    serial = '' if serial is None else str(serial).strip()
    return serial if serial not in ('', '?') else None


def sorted_devices(devices: Iterable) -> list:
    """ Put the devices in a stable order, by serial number (the ones without serial number last)"""
    return sorted(devices, key=lambda device: (device_serial(device) is None, device_serial(device) or '',
                                               str(device)))


_ENDING_NUMBER = re.compile(r'(\d+)\s*$')


def module_number(title) -> Optional[int]:
    """ The number ending the name of a module ('Spectro 2' -> 2), None if there is none or if it is 0"""
    match = _ENDING_NUMBER.search(str(title or ''))
    number = int(match.group(1)) if match else 0
    return number if number >= 1 else None


def find_device(devices: list, serial: Optional[str], backend: str, number: Optional[int] = None):
    """ Find the spectrometer to open among the devices seen by a backend

    Parameters
    ----------
    devices: list of SeaBreezeDevice
        the devices seen by the backend, sorted (see :func:`list_devices`)
    serial: str or None
        the serial number of the spectrometer to open. None to let the plugin choose (Auto): the one whose rank in
        the list is the `number` if there is such a number and this spectrometer is not open yet, else the first one
        that is not open yet
    backend: str
        the name of the backend, for the error messages
    number: int, optional
        the number of the module (Spectro 1, Spectro 2...) when it has one: the spectrometer number 1 is the first of
        the list, the number 2 the second one...

    Raises
    ------
    RuntimeError
        if there is no such spectrometer
    """
    hint = "Connect it, or choose the other backend in the Backend list."
    if serial is not None:
        for device in devices:
            if str(device.serial_number) == serial:
                return device
        raise RuntimeError(f"No spectrometer with the serial number {serial} was found by the '{backend}' seabreeze "
                           f"backend. {hint}")
    if number is not None and 1 <= number <= len(devices) and not devices[number - 1].is_open:
        return devices[number - 1]
    for device in devices:
        if not device.is_open:
            return device
    if devices:
        raise RuntimeError(f"All the spectrometers found by the '{backend}' seabreeze backend are already open.")
    raise RuntimeError(f"No spectrometer was found by the '{backend}' seabreeze backend. {hint}")


class BackendInUseError(RuntimeError):
    """ The other backend is in use by a spectrometer of this process"""


_open_spectrometers = {}  # the number of open spectrometers of each backend


def _reserve_backend(backend: str):
    with _lock:
        for other, count in _open_spectrometers.items():
            if count > 0 and other != backend:
                raise BackendInUseError(
                    f"The '{other}' seabreeze backend is already used by a spectrometer of this PyMoDAQ session, "
                    f"the two backends cannot be mixed (pyseabreeze resets the USB devices). Use '{other}' for all "
                    f"the spectrometers, or close the ones using it first.")
        _open_spectrometers[backend] = _open_spectrometers.get(backend, 0) + 1


def release_backend(backend: str):
    """ Tell that a spectrometer opened with :func:`open_spectrometer` is closed (or has not been opened)"""
    with _lock:
        _open_spectrometers[backend] = max(0, _open_spectrometers.get(backend, 0) - 1)


def open_spectrometer(backend: str, selected_device=None, module_title: Optional[str] = None,
                      seen: Optional[list] = None):
    """ Open a spectrometer with a given backend

    The spectrometers are listed by the backend when this is called (nothing is looked for before: the user connects the
    spectrometer when he wants to), so a spectrometer which is plugged in at any time is found. :func:`release_backend`
    has to be called when the spectrometer is closed.

    Parameters
    ----------
    backend: str
        the name of the backend, see :data:`BACKENDS`
    selected_device: SeaBreezeDevice or str, optional
        an item of the Device list, only its serial number is used. If it is not given, if it has no serial number or
        if it is :data:`AUTO`, the plugin chooses: the spectrometers being sorted by serial number, the one whose
        rank is the number ending the name of the module (Spectro 1 opens the first one, Spectro 2 the second
        one...), else the first one that is not open yet
    module_title: str, optional
        the name of the module (Spectro 1, Spectro 2...)
    seen: list, optional
        it is filled with the spectrometers the backend listed, sorted by serial number, to show them in the Device list
        (even if the one asked for was not found)

    Returns
    -------
    An open seabreeze Spectrometer that uses the backend

    Raises
    ------
    BackendInUseError
        if the other backend is used by an open spectrometer
    ImportError
        if the backend cannot be loaded
    RuntimeError
        if there is no such spectrometer
    """
    backend = check_backend(backend)
    spectrometer = spectrometer_class(backend)
    serial = device_serial(selected_device)
    _reserve_backend(backend)
    try:
        devices = list_devices(backend)
        if seen is not None:
            seen[:] = devices
        return spectrometer(find_device(devices, serial, backend, module_number(module_title)))
    except BaseException:
        release_backend(backend)
        raise


# ---------------------------------------------------------------------------------------------------------------------
# Dark pixels
# ---------------------------------------------------------------------------------------------------------------------

def _flatten(indices: Iterable) -> Iterator[int]:
    """ pyseabreeze returns the dark pixel indices of some models (USB4000, NIRQuest) wrapped in a tuple"""
    for index in indices:
        if isinstance(index, (list, tuple, np.ndarray)):
            yield from _flatten(index)
        else:
            yield int(index)


def active_pixel_slice(n_pixels: int, dark_indices: Iterable) -> slice:
    """ Get the slice of the pixels of a spectrum that are kept once the dark pixels are removed

    The electric dark pixels (see seabreeze ``get_electric_dark_pixel_indices``) are at the beginning of the
    detector, at its end, at both, or there is none (STS, HDX, FX... return an empty list). What is cropped is
    everything up to the last dark pixel of the first half of the detector, and everything from the first dark pixel
    of its second half. This also removes the unused pixels next to the dark ones, for instance the padding after
    the dark pixels at the end of the detector of the Maya2000Pro.

    Parameters
    ----------
    n_pixels: int
        the number of pixels of the spectrum
    dark_indices: iterable of int
        the indices of the electric dark pixels

    Returns
    -------
    slice: to be applied on the wavelengths and on the intensities. All the pixels are kept (slice(0, n_pixels)) if
    there is no dark pixel
    """
    dark = sorted({index for index in _flatten(dark_indices) if 0 <= index < n_pixels})
    middle = n_pixels / 2
    leading = [index for index in dark if index < middle]
    trailing = [index for index in dark if index >= middle]
    start = leading[-1] + 1 if leading else 0
    stop = trailing[0] if trailing else n_pixels
    if start >= stop:  # no pixel left: the dark pixels are not where they are expected to be, keep everything
        return slice(0, n_pixels)
    return slice(start, stop)
