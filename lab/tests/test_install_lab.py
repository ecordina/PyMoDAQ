"""lab/install_lab.py: what it would install, without installing anything"""
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'install_lab.py'
SPEC = importlib.util.spec_from_file_location('install_lab', SCRIPT)
install_lab = importlib.util.module_from_spec(SPEC)
sys.modules['install_lab'] = install_lab
SPEC.loader.exec_module(install_lab)


def names(steps):
    return [' '.join(step[3:]) for step in steps]  # (after "python -m pip")


class TestCommands:
    def test_the_three_plugins_are_installed_editable(self, tmp_path):
        steps = install_lab.commands(root=tmp_path, have_pymodaq=True, have_qt=True)

        assert names(steps) == [f'install --no-build-isolation -e {tmp_path / "pymodaq_plugins_oceaninsight"}',
                                f'install -e {tmp_path / "pymodaq_plugins_quantumnorthwest"}',
                                f'install -e {tmp_path / "pymodaq_plugins_thermospectro"}']
        assert all(step[:3] == [sys.executable, '-m', 'pip'] for step in steps)

    def test_pymodaq_is_installed_first_with_a_qt_binding_if_it_is_missing(self, tmp_path):
        steps = install_lab.commands(root=tmp_path, have_pymodaq=False)

        assert names(steps)[0] == 'install pymodaq[pyqt6]>=5.3,<5.4' and len(steps) == 4  # (PyMoDAQ alone has no Qt)

    def test_another_qt_binding_can_be_chosen(self, tmp_path):
        steps = install_lab.commands(root=tmp_path, have_pymodaq=False, qt='pyside6')

        assert names(steps)[0] == 'install pymodaq[pyside6]>=5.3,<5.4'

    def test_a_qt_binding_is_added_to_a_pymodaq_which_has_none(self, tmp_path):
        steps = install_lab.commands(root=tmp_path, have_pymodaq=True, have_qt=False)

        assert names(steps)[0] == 'install PyQt6' and len(steps) == 4

    def test_a_pymodaq_with_its_qt_binding_is_left_alone(self, tmp_path):
        steps = install_lab.commands(root=tmp_path, have_pymodaq=True, have_qt=True)

        assert len(steps) == 3 and not any('PyQt6' in text or 'pymodaq' in text.split(' -e ')[0] for text in names(steps))

    def test_pymodaq_is_left_alone_if_it_is_not_wanted(self, tmp_path):
        steps = install_lab.commands(pymodaq=False, root=tmp_path, have_pymodaq=False, have_qt=False)

        assert len(steps) == 3 and not any('pymodaq[' in text or 'PyQt6' in text for text in names(steps))

    def test_the_camera_and_the_usb_packages_come_before_the_plugins(self, tmp_path):
        steps = names(install_lab.commands(camera=True, usb=True, root=tmp_path, have_pymodaq=True, have_qt=True))

        assert steps[0] == 'install pymodaq_plugins_thorlabs tifffile libusb-package pyusb'
        assert steps[1].startswith('install --no-build-isolation -e')

    def test_a_binding_is_found_by_the_name_of_its_module(self, monkeypatch):
        monkeypatch.setattr(install_lab, 'installed', lambda module: module == 'PySide6')

        assert install_lab.has_qt_binding() is True

        monkeypatch.setattr(install_lab, 'installed', lambda module: False)

        assert install_lab.has_qt_binding() is False

    def test_the_plugins_of_the_repository_are_all_there(self):
        for plugin in install_lab.PLUGINS:
            assert (SCRIPT.parent / plugin).is_dir()


class TestMain:
    def test_a_dry_run_installs_nothing(self, capsys, monkeypatch):
        called = []
        monkeypatch.setattr(install_lab.subprocess, 'run', lambda *args, **kwargs: called.append(args))

        assert install_lab.main(['--dry-run', '--no-pymodaq']) == 0

        assert called == [] and 'pymodaq_plugins_thermospectro' in capsys.readouterr().out

    def test_a_failing_step_stops_the_installation(self, monkeypatch, capsys):
        results = []

        class Result:
            returncode = 3

        monkeypatch.setattr(install_lab.subprocess, 'run', lambda step: results.append(step) or Result())

        assert install_lab.main(['--no-pymodaq']) == 3

        assert len(results) == 1 and 'Failed' in capsys.readouterr().err

    def test_the_hint_at_the_end_names_the_commands_of_the_package(self, monkeypatch, capsys):
        import toml

        class Result:
            returncode = 0

        monkeypatch.setattr(install_lab.subprocess, 'run', lambda step: Result())

        assert install_lab.main(['--no-pymodaq']) == 0

        told = capsys.readouterr().out
        scripts = toml.load(SCRIPT.parent / 'pymodaq_plugins_thermospectro' / 'pyproject.toml')['project']['scripts']
        assert list(install_lab.APPLICATIONS) == list(scripts)  # (what the hint says is what the package installs)
        assert all(command in told for command in scripts) and 'qnw_tc_check' in told
        assert 'thermospectro ' not in told and 'Preset' not in told and 'qnw_tc_start' not in told

    def test_help(self, capsys):
        with pytest.raises(SystemExit) as stop:
            install_lab.main(['--help'])

        assert stop.value.code == 0 and '--camera' in capsys.readouterr().out
