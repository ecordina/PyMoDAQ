#!/usr/bin/env python
"""Convert the h5 file of an experiment of the lab into csv, tif and text files

    python thermospectro_export.py Pomme_20261022_153012.h5

It is the command ``lab_export`` of the package (``lab_export FILE.h5``, see ``lab_apps``), which is the file
``src/pymodaq_plugins_thermospectro/export.py``: this script runs that file, wherever the package is installed or not
(that file needs only numpy, h5py and, for the images, tifffile, not PyMoDAQ: it can be copied alone to any computer and run
with ``python export.py FILE.h5``).
"""
import importlib.util
import sys
from pathlib import Path

IMPLEMENTATION = Path(__file__).resolve().parents[1] / 'src' / 'pymodaq_plugins_thermospectro' / 'export.py'


def load():
    """The module ``export.py`` of the package, loaded from its file (the package itself is not imported)"""
    spec = importlib.util.spec_from_file_location('thermospectro_export_implementation', IMPLEMENTATION)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # (the dataclasses of the module look it up)
    spec.loader.exec_module(module)
    return module


if __name__ == '__main__':
    sys.exit(load().main())
else:  # (imported: the names of the module are the ones of this one)
    globals().update({name: value for name, value in vars(load()).items() if not name.startswith('__')})
