#!/usr/bin/env python
"""Install (or update) the lab plugins of this repository in the current Python environment

    python lab/install_lab.py                 # the three plugins, in editable mode
    python lab/install_lab.py --camera --usb  # and what a Thorlabs camera and the pyseabreeze spectrometers need
    python lab/install_lab.py --dry-run       # only show what would be done

The plugins are installed *editable*: the environment uses the files of this folder, so a ``git pull`` is enough to
update a machine (restart PyMoDAQ afterwards). Run it again when a plugin has new dependencies.

The plugins (folders next to this script):

* ``pymodaq_plugins_thermospectro``: the five applications which log spectrometers, a camera and thermal controllers
  (held at a temperature or ramped) and the commands which start them (``thermo_2spectro``, ``thermo_spectro``,
  ``camera``, ``thermo_camera``, ``thermo_camera_spectro``);
* ``pymodaq_plugins_quantumnorthwest``: the actuator ``QNW_TC`` (Quantum Northwest TC 125 / TC 1 / TC 1B) and ``qnw_tc_check``;
* ``pymodaq_plugins_oceaninsight``: the Seabreeze spectrometers (pyseabreeze by default, several spectrometers at once).

PyMoDAQ itself (release 5.3, with a Qt binding: PyQt6 by default, ``--qt`` chooses another) is installed first if the
environment does not have it, unless ``--no-pymodaq`` is given; a Qt binding is added if the environment has PyMoDAQ but no
Qt binding. The pins of the environments of the two computers of the lab are in ``lab/environments``.
"""
from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence

HERE = Path(__file__).resolve().parent
PLUGINS = ('pymodaq_plugins_oceaninsight', 'pymodaq_plugins_quantumnorthwest', 'pymodaq_plugins_thermospectro')
#: the plugins which are built with setup.py (hatchling and its plugins are not needed, but setuptools is)
NO_ISOLATION = ('pymodaq_plugins_oceaninsight',)
CAMERA_PACKAGES = ('pymodaq_plugins_thorlabs', 'tifffile')
USB_PACKAGES = ('libusb-package', 'pyusb')
MINIMUM_PYTHON = (3, 10)
#: the Qt bindings PyMoDAQ can use: the name of the extra of pymodaq, the python module which says it is installed
QT_BINDINGS = {'pyqt6': 'PyQt6', 'pyside6': 'PySide6', 'pyqt5': 'PyQt5'}
PYMODAQ_VERSIONS = '>=5.3,<5.4'
#: the commands of pymodaq_plugins_thermospectro: each one looks at what is connected and starts its own application
APPLICATIONS = ('thermo_2spectro', 'thermo_spectro', 'camera', 'thermo_camera', 'thermo_camera_spectro')
FINAL_HINT = ('\nDone. Start PyMoDAQ again. The command of each application looks for the instruments by itself:\n'
              f'  {" | ".join(APPLICATIONS)}\n'
              '  thermo_camera_spectro --mock               (no hardware)\n'
              '  qnw_tc_check COM3 --trace                  (the thermostat)\n'
              '  python lab/pymodaq_plugins_thermospectro/tools/thermospectro_export.py FILE.h5   (csv and tif)')


def installed(module: str) -> bool:
    """Whether a python module can be imported in this environment (nothing is imported)"""
    return importlib.util.find_spec(module) is not None


def pip(*arguments: str) -> list[str]:
    return [sys.executable, '-m', 'pip', *arguments]


def has_qt_binding() -> bool:
    """Whether one of the Qt bindings PyMoDAQ can use is installed in this environment"""
    return any(installed(module) for module in QT_BINDINGS.values())


def commands(camera: bool = False, usb: bool = False, pymodaq: bool = True, root: Path = HERE,
             have_pymodaq: Optional[bool] = None, qt: str = 'pyqt6', have_qt: Optional[bool] = None) -> list[list[str]]:
    """The commands which install what is asked for, in the order they must run"""
    steps = []
    if pymodaq and not (installed('pymodaq') if have_pymodaq is None else have_pymodaq):
        steps.append(pip('install', f'pymodaq[{qt}]{PYMODAQ_VERSIONS}'))  # (PyMoDAQ has no Qt binding without the extra)
    elif pymodaq and not (has_qt_binding() if have_qt is None else have_qt):
        steps.append(pip('install', QT_BINDINGS[qt]))
    extras = []
    if camera:
        extras += CAMERA_PACKAGES
    if usb:
        extras += USB_PACKAGES
    if extras:
        steps.append(pip('install', *extras))
    for plugin in PLUGINS:
        arguments = ['install', '-e', str(root / plugin)]
        if plugin in NO_ISOLATION:
            arguments.insert(1, '--no-build-isolation')
        steps.append(pip(*arguments))
    return steps


def parse(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    arguments = argparse.ArgumentParser(description=__doc__.split('\n\n')[0], epilog=__doc__.split('\n\n', 1)[1],
                                        formatter_class=argparse.RawDescriptionHelpFormatter)
    arguments.add_argument('--camera', action='store_true',
                           help='also install pymodaq_plugins_thorlabs (the Thorlabs cameras) and tifffile (the tif '
                                'files of thermospectro_export.py)')
    arguments.add_argument('--usb', action='store_true',
                           help='also install libusb-package and pyusb (the pyseabreeze spectrometers on Windows)')
    arguments.add_argument('--no-pymodaq', action='store_true',
                           help='do not install PyMoDAQ (and a Qt binding) if it is missing')
    arguments.add_argument('--qt', choices=sorted(QT_BINDINGS), default='pyqt6',
                           help='the Qt binding to install if there is none (default: pyqt6, the one of the lab)')
    arguments.add_argument('--dry-run', action='store_true', help='only show the commands')
    return arguments.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse(argv)
    if sys.version_info < MINIMUM_PYTHON:
        print(f'Python {MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]} or newer is needed, this is {sys.version.split()[0]}',
              file=sys.stderr)
        return 1
    missing = [plugin for plugin in PLUGINS if not (HERE / plugin).is_dir()]
    if missing:
        print(f'Not found next to this script: {", ".join(missing)}', file=sys.stderr)
        return 1
    steps = commands(args.camera, args.usb, not args.no_pymodaq, qt=args.qt)
    print(f'Environment: {sys.executable}')
    for step in steps:
        print('  ' + ' '.join(step))
        if args.dry_run:
            continue
        result = subprocess.run(step)
        if result.returncode != 0:
            print(f'Failed: {" ".join(step)}', file=sys.stderr)
            return result.returncode
    if not args.dry_run:
        print(FINAL_HINT)
    return 0


if __name__ == '__main__':
    sys.exit(main())
