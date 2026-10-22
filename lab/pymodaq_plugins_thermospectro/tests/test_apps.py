"""``lab_apps``: the names of the applications of the lab"""
import subprocess
import sys
from pathlib import Path

import pytest

from pymodaq_plugins_thermospectro import apps
from pymodaq_plugins_thermospectro.configurations import CONFIGURATIONS

PYPROJECT = Path(__file__).resolve().parents[1] / 'pyproject.toml'


def scripts() -> dict:
    import toml
    return toml.load(PYPROJECT)['project']['scripts']


class TestTheList:
    def test_it_names_the_five_applications_in_order(self, capsys):
        assert apps.main([]) == 0

        told = capsys.readouterr().out
        positions = [told.index(f'\n  {command} ') for command in CONFIGURATIONS]
        assert list(CONFIGURATIONS) == ['thermo_2spectro', 'thermo_spectro', 'camera', 'thermo_camera',
                                        'thermo_camera_spectro'] and positions == sorted(positions)

    def test_it_says_what_each_one_has(self, capsys):
        apps.main([])

        lines = {line.split()[0]: line for line in capsys.readouterr().out.splitlines()
                 if line.startswith('  ') and line.split() and line.split()[0] in CONFIGURATIONS}
        assert '2 spectrometers + 2 thermal controllers' in lines['thermo_2spectro']
        assert '1 spectrometer + 1 thermal controller' in lines['thermo_spectro']
        assert '1 camera' in lines['camera'] and 'thermal' not in lines['camera'] and 'spectrometer' not in lines['camera']
        assert '1 camera + 1 thermal controller' in lines['thermo_camera']
        assert '1 camera + 1 spectrometer + 1 thermal controller' in lines['thermo_camera_spectro']

    def test_it_names_the_other_commands_and_the_options(self, capsys):
        apps.main([])

        told = capsys.readouterr().out
        flat = ' '.join(told.split())  # (the paragraphs are wrapped to the width of the terminal)
        assert 'lab_apps' in told and 'qnw_tc_check COM3 --trace' in told and 'lab_export FILE.h5' in told
        assert '--mock' in flat and '--port COM6' in flat and 'nothing is acquired until you press Start' in flat

    def test_it_fits_a_small_terminal(self):
        assert max(len(line) for line in apps.describe(width=70).splitlines()) <= 80
        assert max(len(line) for line in apps.describe(width=100).splitlines()) <= 100

    def test_the_names_alone_are_one_per_line(self, capsys):
        assert apps.main(['--names']) == 0

        assert capsys.readouterr().out.split() == list(CONFIGURATIONS)

    def test_the_commands_of_the_package_are_the_applications_and_these_two(self):
        assert [name for name in scripts() if name not in ('lab_apps', 'lab_export')] == list(CONFIGURATIONS)
        assert scripts()['lab_apps'] == 'pymodaq_plugins_thermospectro.apps:main'
        assert scripts()['lab_export'] == 'pymodaq_plugins_thermospectro.export:main'

    def test_help_and_a_wrong_option(self, capsys):
        with pytest.raises(SystemExit) as stop:
            apps.main(['--help'])
        assert stop.value.code == 0 and '--names' in capsys.readouterr().out
        with pytest.raises(SystemExit) as stop:
            apps.main(['--nonsense'])
        assert stop.value.code == 2

    def test_the_converter_is_a_command_which_can_be_started(self):
        from pymodaq_plugins_thermospectro import export

        module, function = scripts()['lab_export'].split(':')
        assert module == export.__name__ and callable(getattr(export, function))
        assert apps.OTHER_COMMANDS[1][0].startswith('lab_export')


def test_it_runs_as_a_module_and_needs_no_qt():
    """``python -m pymodaq_plugins_thermospectro.apps`` works without the command being installed, and instantly"""
    code = ('import runpy, sys\n'
            'try:\n'
            '    runpy.run_module("pymodaq_plugins_thermospectro.apps", run_name="__main__")\n'
            'except SystemExit as stop:\n'
            '    print("exit", stop.code)\n'
            'print("qt imported:", any(name.split(".")[0] in ("qtpy", "PyQt6", "PyQt5", "PySide6") for name in sys.modules))\n')
    done = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=120)

    assert done.returncode == 0, done.stderr
    assert 'thermo_camera_spectro' in done.stdout and 'exit 0' in done.stdout and 'qt imported: False' in done.stdout
