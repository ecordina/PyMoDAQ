#!/usr/bin/env python
"""Install (or update) the lab plugins of this repository in the current Python environment

    python lab/install_lab.py                 # the three plugins, in editable mode
    python lab/install_lab.py --camera --usb  # and what a Thorlabs camera and the pyseabreeze spectrometers need
    python lab/install_lab.py --core          # and PyMoDAQ itself from this repository (with its fixes), not from PyPI
    python lab/install_lab.py --all           # everything: the three plugins, --core, --camera and --usb
    python lab/install_lab.py --dry-run       # only show what would be done (with any of the above)

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

``--core`` installs PyMoDAQ from the ``packages`` folder of this repository (the five packages, editable) instead of from
PyPI: the repository is the release 5.3.8 of PyMoDAQ with the fixes the lab needs (see ``lab/README.md``), so this is how
a computer gets them. An existing installation of PyMoDAQ is replaced. The packages are given the version
``5.3.8+lab``, so that no git tag is needed (a clone of this repository has none).

``--all`` is ``--core --camera --usb``: everything the lab uses, in one command (PyMoDAQ, the Qt binding, the camera and the
USB packages, the three plugins). It is the one to give on a computer which has nothing yet; ``--dry-run`` shows it first.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence

HERE = Path(__file__).resolve().parent
REPOSITORY = HERE.parent
PLUGINS = ('pymodaq_plugins_oceaninsight', 'pymodaq_plugins_quantumnorthwest', 'pymodaq_plugins_thermospectro')
CAMERA_PACKAGES = ('pymodaq_plugins_thorlabs', 'tifffile')
USB_PACKAGES = ('libusb-package', 'pyusb')
MINIMUM_PYTHON = (3, 10)
#: the Qt bindings PyMoDAQ can use: the name of the extra of pymodaq, the python module which says it is installed
QT_BINDINGS = {'pyqt6': 'PyQt6', 'pyside6': 'PySide6', 'pyqt5': 'PyQt5'}
PYMODAQ_VERSIONS = '>=5.3,<5.4'
#: the five packages of PyMoDAQ in the folder ``packages`` of the repository, in the order they must be installed in
CORE_PACKAGES = ('pymodaq_utils', 'pymodaq_data', 'pymodaq_scripting', 'pymodaq_gui', 'pymodaq')
CORE_WITH_QT = ('pymodaq_gui', 'pymodaq')  # (the packages which have the extra which brings a Qt binding)
#: the release of PyMoDAQ the repository is based on: the packages are installed as CORE_VERSION+lab
CORE_VERSION = '5.3.8'
#: the commands of pymodaq_plugins_thermospectro: each one looks at what is connected and starts its own application
APPLICATIONS = ('thermo_2spectro', 'thermo_spectro', 'camera', 'thermo_camera', 'thermo_camera_spectro')
FINAL_HINT = ('\nDone. Start PyMoDAQ again. The command of each application looks for the instruments by itself:\n'
              f'  {" | ".join(APPLICATIONS)}      (lab_apps: their names and what each one is for)\n'
              '  thermo_camera_spectro --mock               (no hardware)\n'
              '  qnw_tc_check COM3 --trace                  (the thermostat)\n'
              '  lab_export FILE.h5                         (the h5 file of an experiment as csv, tif and text files)')


def installed(module: str) -> bool:
    """Whether a python module can be imported in this environment (nothing is imported)"""
    return importlib.util.find_spec(module) is not None


def pip(*arguments: str) -> list[str]:
    return [sys.executable, '-m', 'pip', *arguments]


def has_qt_binding() -> bool:
    """Whether one of the Qt bindings PyMoDAQ can use is installed in this environment"""
    return any(installed(module) for module in QT_BINDINGS.values())


def core_steps(qt: str = 'pyqt6', repository: Path = REPOSITORY) -> list[list[str]]:
    """The commands which install PyMoDAQ from the ``packages`` folder of the repository (editable)"""
    steps = []
    for name in CORE_PACKAGES:
        target = str(repository / 'packages' / name) + (f'[{qt}]' if name in CORE_WITH_QT else '')
        steps.append(pip('install', '-e', target))
    return steps


def core_environment() -> dict:
    """The environment of the commands of :func:`core_steps`: the version of the packages is given, not looked for in the
    tags of git (hatch-vcs: setuptools_scm obeys this variable), which the clone of a fork does not have"""
    return {**os.environ, 'SETUPTOOLS_SCM_PRETEND_VERSION': f'{CORE_VERSION}+lab'}


def headless_opencv_steps() -> list[list[str]]:
    """The commands which put ``opencv-python-headless`` in place of ``opencv-python``

    ``pymodaq_plugins_thorlabs`` requires ``opencv-python``. On Linux and macOS that wheel ships a Qt 5 and, when it is
    imported (PyMoDAQ imports it with the plugins, before the window is made), points Qt to it: PyQt6 then cannot start
    (*Could not load the Qt platform plugin "xcb" in ".../cv2/qt/plugins"*), for every PyMoDAQ command. The headless
    wheel is the same ``cv2`` without the GUI. Both cannot be installed together (they share the folder ``cv2``): the
    first one goes, then the other comes. ``pip check`` then says that the plugin wants ``opencv-python``: it is the one
    thing it says, and it is not a problem.
    """
    return [pip('uninstall', '-y', 'opencv-python'), pip('install', 'opencv-python-headless')]


def commands(camera: bool = False, usb: bool = False, pymodaq: bool = True, root: Path = HERE,
             have_pymodaq: Optional[bool] = None, qt: str = 'pyqt6', have_qt: Optional[bool] = None,
             core: bool = False, repository: Path = REPOSITORY, platform: Optional[str] = None) -> list[list[str]]:
    """The commands which install what is asked for, in the order they must run

    With `core`, PyMoDAQ is the one of the repository (:func:`core_steps`), whatever `pymodaq` says. With `camera`, on
    a system which is not Windows (`platform`, ``sys.platform`` by default), the last commands give the computer the
    headless OpenCV (:func:`headless_opencv_steps`).
    """
    platform = sys.platform if platform is None else platform
    steps = []
    if core:
        steps.extend(core_steps(qt, repository))
    elif pymodaq and not (installed('pymodaq') if have_pymodaq is None else have_pymodaq):
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
        steps.append(pip('install', '-e', str(root / plugin)))
    if camera and not platform.startswith('win'):
        steps.extend(headless_opencv_steps())  # (after everything: nothing may bring opencv-python back before it)
    return steps


def parse(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    arguments = argparse.ArgumentParser(description=__doc__.split('\n\n')[0], epilog=__doc__.split('\n\n', 1)[1],
                                        formatter_class=argparse.RawDescriptionHelpFormatter)
    arguments.add_argument('--all', action='store_true',
                           help='install everything: the same as --core --camera --usb (PyMoDAQ from this repository, '
                                'the Thorlabs camera packages and the USB packages), then the three plugins')
    arguments.add_argument('--camera', action='store_true',
                           help='also install pymodaq_plugins_thorlabs (the Thorlabs cameras) and tifffile (the tif '
                                'files of lab_export)')
    arguments.add_argument('--usb', action='store_true',
                           help='also install libusb-package and pyusb (the pyseabreeze spectrometers on Windows)')
    arguments.add_argument('--no-pymodaq', action='store_true',
                           help='do not install PyMoDAQ (and a Qt binding) if it is missing')
    arguments.add_argument('--core', action='store_true',
                           help='install PyMoDAQ from the packages folder of this repository (editable, with the fixes '
                                f'of the lab, version {CORE_VERSION}+lab) instead of PyPI: it replaces any installed '
                                'PyMoDAQ')
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
    if args.all:
        args.core = args.camera = args.usb = True
    if args.core and args.no_pymodaq:
        print(f'{"--all" if args.all else "--core"} installs PyMoDAQ, --no-pymodaq does not: choose one', file=sys.stderr)
        return 2
    if args.core and not (REPOSITORY / 'packages' / 'pymodaq').is_dir():
        print(f'--core needs the packages folder of the repository, not found in {REPOSITORY}', file=sys.stderr)
        return 1
    steps = commands(args.camera, args.usb, not args.no_pymodaq, qt=args.qt, core=args.core)
    core = core_steps(args.qt) if args.core else []
    print(f'Environment: {sys.executable}')
    for step in steps:
        print('  ' + ' '.join(step) + (f'    (version {CORE_VERSION}+lab)' if step in core else ''))
        if args.dry_run:
            continue
        result = subprocess.run(step, env=core_environment()) if step in core else subprocess.run(step)
        if result.returncode != 0:
            print(f'Failed: {" ".join(step)}', file=sys.stderr)
            return result.returncode
    if not args.dry_run:
        print(FINAL_HINT)
    return 0


if __name__ == '__main__':
    sys.exit(main())
