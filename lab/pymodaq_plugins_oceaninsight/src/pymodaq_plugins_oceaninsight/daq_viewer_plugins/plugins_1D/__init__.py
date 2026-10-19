import importlib
from pathlib import Path
from ... import set_logger
logger = set_logger('viewer1D_plugins', add_to_console=False)

for path in Path(__file__).parent.iterdir():
    try:
        if '__init__' not in str(path):
            importlib.import_module('.' + path.stem, __package__)
    except ImportError as e:  # an optional library (or runtime) which is not installed: not a problem
        logger.info("{:} plugin is not available: {:}".format(path.stem, str(e)))
    except Exception as e:
        logger.warning("{:} plugin couldn't be loaded due to some missing packages or errors: {:}".format(path.stem, str(e)))
        pass

