from pymodaq_utils.config import GlobalConfig
from pymodaq_utils.utils import get_version, PackageNotFoundError

config = GlobalConfig()  # (PyMoDAQ registers the Config of utils.py in it: calling Config() here is deprecated)
try:
    __version__ = get_version(__package__)
except PackageNotFoundError:
    __version__ = '0.0.0dev'
