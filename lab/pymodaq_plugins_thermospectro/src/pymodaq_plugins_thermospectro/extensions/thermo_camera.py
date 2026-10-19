"""A camera and its thermal controller (command ``thermo_camera``)

One of the five applications of the lab, see :mod:`pymodaq_plugins_thermospectro.configurations`: the settings and the
windows of its instruments, and nothing else. It is started by its command, which creates and connects its instruments.
"""
from pymodaq_plugins_thermospectro.extensions.thermo_spectro import ThermoSpectro
from pymodaq_plugins_thermospectro.unit_settings import CONTROL_HOLD

EXTENSION_NAME = 'Thermo + camera'  # the name displayed in the extension list of the dashboard
CLASS_NAME = 'ThermoCamera'


class ThermoCamera(ThermoSpectro):
    """A camera and its thermal controller (command ``thermo_camera``)"""
    CONTROL = CONTROL_HOLD  # (what is wanted most of the time: set a temperature and log)
    NUMBERS = (3,)
