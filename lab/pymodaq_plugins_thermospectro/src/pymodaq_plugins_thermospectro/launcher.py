# -*- coding: utf-8 -*-
"""The ``thermospectro`` command: the Dashboard with a preset and the ThermoSpectro extension, in one go

``thermospectro`` does the same as ``dashboard -x ThermoSpectroSeabreeze -e ThermoSpectro``, after having put the preset
in the experiments folder of PyMoDAQ if it is not there yet.
"""
import argparse
import shutil
import sys
from pathlib import Path
from typing import Optional, Sequence

DEFAULT_PRESET = 'ThermoSpectroSeabreeze'
EXTENSION = 'ThermoSpectro'  # (the name of the extension class: what `dashboard -e` is given)
PRESETS_FOLDER = Path(__file__).parent / 'resources' / 'presets'


def bundled_presets() -> list[str]:
    """The names of the presets which come with the package"""
    return sorted(path.stem for path in PRESETS_FOLDER.glob('*.xml'))


def experiments_folder() -> Path:
    """The folder where the Dashboard looks for its presets (it calls them experiments)"""
    from pymodaq.utils.config import get_set_experiment_path
    return get_set_experiment_path()


def install_presets() -> list[str]:
    """Copy the presets of the package which are not in the experiments folder yet, so that the Dashboard lists them

    A preset which is already there is never replaced: it may have been edited.

    Returns
    -------
    The names of the presets which were copied
    """
    folder = experiments_folder()
    copied = []
    for name in bundled_presets():
        if not (folder / f'{name}.xml').exists():
            shutil.copyfile(PRESETS_FOLDER / f'{name}.xml', folder / f'{name}.xml')
            copied.append(name)
    return copied


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        prog='thermospectro',
        description=f'Start the PyMoDAQ Dashboard with a preset (default: {DEFAULT_PRESET}) and open the '
                    f'{EXTENSION} extension. The presets which come with the package are copied to the experiments '
                    f'folder of PyMoDAQ if they are not there yet: {", ".join(bundled_presets())}.')
    parser.add_argument('preset', nargs='?', default=DEFAULT_PRESET, metavar='PRESET',
                        help='name of a preset of the experiments folder of PyMoDAQ')
    name = parser.parse_args(argv).preset
    if name.lower().endswith('.xml'):
        name = name[:-4]
    if Path(name).name != name:
        parser.error('give the name of a preset of the experiments folder of PyMoDAQ, not a path')
    folder = experiments_folder()
    path = folder / f'{name}.xml'
    try:
        copied, problem = install_presets(), None
    except OSError as e:
        copied, problem = [], e
    if not path.exists():
        if problem is not None and name in bundled_presets():
            parser.exit(1, f'thermospectro: the presets could not be copied to the experiments folder of PyMoDAQ '
                           f'({problem}). Copy the files of {PRESETS_FOLDER} to {folder} by hand.\n')
        available = sorted(other.stem for other in folder.glob('*.xml'))
        parser.exit(2, f'thermospectro: there is no preset {name} in {folder}. The presets there are: '
                       f'{", ".join(available)}.\n')
    if problem is not None:
        print(f'thermospectro: the presets of the package could not be copied to {folder} ({problem})',
              file=sys.stderr)
    if copied:
        print(f'thermospectro: copied {", ".join(copied)} to {folder}: they are in the menu '
              f'Tools > Experiment > Load Experiments of the Dashboard')

    from pymodaq.dashboard import main as dashboard_main  # (it registers the extensions, which takes a moment)
    sys.argv = ['thermospectro', '-x', name, '-e', EXTENSION]
    dashboard_main()


if __name__ == '__main__':
    main()
