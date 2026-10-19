import importlib
from pathlib import Path

from pymodaq_utils.logger import set_logger

logger = set_logger('move_plugins', add_to_console=False)

path = Path(__file__)  # (PyMoDAQ finds the plugins of the package from ``daq_move_plugins.path.parent``)

for module in path.parent.iterdir():
    try:
        if '__init__' not in str(module):
            importlib.import_module('.' + module.stem, __package__)
    except Exception as e:
        logger.warning(f"{module.stem} plugin couldn't be loaded due to some missing packages or errors: {e}")
