from pathlib import Path
from pymodaq_utils.config import GlobalConfig
from pymodaq_utils.logger import set_logger  # to be imported by other modules.

config = GlobalConfig()  # (PyMoDAQ registers the Config of utils.py in it: calling Config() here is deprecated)


with open(str(Path(__file__).parent.joinpath('resources/VERSION')), 'r') as fvers:
    __version__ = fvers.read().strip()
