"""lab/install_lab.py: what it would install, without installing anything"""
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'install_lab.py'
HERE = SCRIPT.parent
SPEC = importlib.util.spec_from_file_location('install_lab', SCRIPT)
install_lab = importlib.util.module_from_spec(SPEC)
sys.modules['install_lab'] = install_lab
SPEC.loader.exec_module(install_lab)


def names(steps):
    return [' '.join(step[3:]) for step in steps]  # (after "python -m pip")


class TestCommands:
    def test_the_three_plugins_are_installed_editable(self, tmp_path):
        steps = install_lab.commands(root=tmp_path, have_pymodaq=True, have_qt=True)

        assert names(steps) == [f'install -e {tmp_path / "pymodaq_plugins_oceaninsight"}',
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
        assert steps[1].startswith('install -e ') and 'oceaninsight' in steps[1]

    def test_a_binding_is_found_by_the_name_of_its_module(self, monkeypatch):
        monkeypatch.setattr(install_lab, 'installed', lambda module: module == 'PySide6')

        assert install_lab.has_qt_binding() is True

        monkeypatch.setattr(install_lab, 'installed', lambda module: False)

        assert install_lab.has_qt_binding() is False

    def test_the_camera_gets_the_headless_opencv_on_linux_and_macos(self, tmp_path):
        """opencv-python (required by the Thorlabs plugin) ships a Qt 5 there, which stops PyQt6 from starting"""
        for platform in ('linux', 'darwin'):
            steps = names(install_lab.commands(camera=True, root=tmp_path, have_pymodaq=True, have_qt=True,
                                               platform=platform))

            assert steps[0] == 'install pymodaq_plugins_thorlabs tifffile'
            assert steps[-2:] == ['uninstall -y opencv-python', 'install opencv-python-headless']  # (after the plugins)
            assert steps[-3].startswith('install -e ') and len(steps) == 1 + 3 + 2

    def test_not_on_windows_nor_without_the_camera(self, tmp_path):
        windows = names(install_lab.commands(camera=True, root=tmp_path, have_pymodaq=True, have_qt=True,
                                             platform='win32'))
        without = names(install_lab.commands(root=tmp_path, have_pymodaq=True, have_qt=True, platform='linux'))

        assert not any('opencv' in step for step in windows + without) and len(windows) == 1 + 3 and len(without) == 3

    def test_the_usb_packages_alone_do_not_touch_opencv(self, tmp_path):
        steps = names(install_lab.commands(usb=True, root=tmp_path, have_pymodaq=True, have_qt=True, platform='linux'))

        assert not any('opencv' in step for step in steps)

    def test_the_plugins_of_the_repository_are_all_there(self):
        for plugin in install_lab.PLUGINS:
            assert (SCRIPT.parent / plugin).is_dir()


class TestCore:
    """--core: PyMoDAQ from the packages folder of the repository, not from PyPI"""

    def test_the_five_packages_in_the_order_of_their_dependencies(self, tmp_path):
        steps = install_lab.core_steps(repository=tmp_path)

        packages = tmp_path / 'packages'
        assert names(steps) == [f'install -e {packages / "pymodaq_utils"}', f'install -e {packages / "pymodaq_data"}',
                                f'install -e {packages / "pymodaq_scripting"}',
                                f'install -e {packages / "pymodaq_gui"}[pyqt6]', f'install -e {packages / "pymodaq"}[pyqt6]']

    def test_another_qt_binding_is_an_extra_of_the_two_packages_which_have_it(self, tmp_path):
        steps = install_lab.core_steps('pyside6', repository=tmp_path)

        assert [step[-1].endswith('[pyside6]') for step in steps] == [False, False, False, True, True]

    def test_the_extras_exist(self):
        import toml
        for name in install_lab.CORE_WITH_QT:
            project = toml.load(install_lab.REPOSITORY / 'packages' / name / 'pyproject.toml')['project']
            assert set(install_lab.QT_BINDINGS) <= set(project['optional-dependencies']), name

    def test_the_core_comes_before_everything_else_and_replaces_the_pypi_installation(self, tmp_path):
        steps = names(install_lab.commands(camera=True, root=tmp_path, repository=tmp_path, core=True,
                                           have_pymodaq=False, have_qt=False, platform='win32'))

        assert len(steps) == 5 + 1 + 3
        assert [step.split(' ')[1] for step in steps[:5]] == ['-e'] * 5 and 'pymodaq_utils' in steps[0]
        assert steps[5] == 'install pymodaq_plugins_thorlabs tifffile' and steps[6].startswith('install -e ')
        assert not any('>=5.3' in step or step == 'install PyQt6' for step in steps)

    def test_without_core_nothing_of_the_repository_is_installed(self, tmp_path):
        steps = names(install_lab.commands(root=tmp_path, repository=tmp_path, have_pymodaq=False))

        assert not any(str(tmp_path / 'packages') in step for step in steps)

    def test_the_version_is_given_and_not_looked_for_in_git(self):
        environment = install_lab.core_environment()

        assert environment['SETUPTOOLS_SCM_PRETEND_VERSION'] == f'{install_lab.CORE_VERSION}+lab'
        assert 'PATH' in environment or os_environment_is_empty()

    def test_that_version_satisfies_what_the_packages_ask_of_each_other(self):
        """pip would replace an editable package by one from PyPI if the version made up here were too old"""
        import toml
        from packaging.requirements import Requirement
        from packaging.version import Version

        version = Version(f'{install_lab.CORE_VERSION}+lab')
        projects = [toml.load(install_lab.REPOSITORY / 'packages' / name / 'pyproject.toml')['project']
                    for name in install_lab.CORE_PACKAGES]
        projects += [toml.load(HERE / plugin / 'pyproject.toml')['project'] for plugin in install_lab.PLUGINS
                     if (HERE / plugin / 'pyproject.toml').exists()]
        checked = 0
        for project in projects:
            for dependency in project.get('dependencies', []):
                requirement = Requirement(dependency)
                if requirement.name in install_lab.CORE_PACKAGES:
                    assert requirement.specifier.contains(version, prereleases=True), (project['name'], dependency)
                    checked += 1
        assert checked >= 8
        assert Requirement(f'pymodaq{install_lab.PYMODAQ_VERSIONS}').specifier.contains(version)

    def test_main_gives_the_version_to_the_core_steps_only(self, monkeypatch, capsys):
        calls = []

        class Result:
            returncode = 0

        monkeypatch.setattr(install_lab.subprocess, 'run', lambda step, **kwargs: calls.append((step, kwargs)) or Result())

        assert install_lab.main(['--core']) == 0

        core = install_lab.core_steps()
        assert [step for step, _ in calls[:5]] == core and len(calls) == 5 + 3
        assert all(kwargs['env']['SETUPTOOLS_SCM_PRETEND_VERSION'] == f'{install_lab.CORE_VERSION}+lab'
                   for _, kwargs in calls[:5])
        assert all(kwargs == {} for _, kwargs in calls[5:])  # (the plugins have their own versions)

    def test_a_dry_run_says_the_version(self, capsys, monkeypatch):
        monkeypatch.setattr(install_lab.subprocess, 'run', lambda *args, **kwargs: pytest.fail('nothing is installed'))

        assert install_lab.main(['--core', '--dry-run']) == 0

        told = capsys.readouterr().out
        assert told.count(f'(version {install_lab.CORE_VERSION}+lab)') == 5 and 'pymodaq_plugins_thermospectro' in told

    def test_core_and_no_pymodaq_contradict_each_other(self, capsys):
        assert install_lab.main(['--core', '--no-pymodaq', '--dry-run']) == 2

        assert 'choose one' in capsys.readouterr().err


class TestAll:
    """--all: everything the lab uses, in one command"""

    @staticmethod
    def dry_run(capsys, *arguments):
        assert install_lab.main([*arguments, '--dry-run']) == 0
        return [line.strip() for line in capsys.readouterr().out.splitlines()[1:]]  # (after the line of the environment)

    def test_it_is_the_core_the_camera_and_the_usb_packages(self, capsys):
        everything = self.dry_run(capsys, '--all')

        assert everything == self.dry_run(capsys, '--core', '--camera', '--usb')

    def test_it_installs_the_core_the_extras_then_the_three_plugins_in_that_order(self, capsys):
        steps = self.dry_run(capsys, '--all')

        assert len(steps) == 5 + 1 + 3 + (0 if sys.platform.startswith('win') else 2)  # (the headless opencv at the end)
        assert all(f'-e {install_lab.REPOSITORY / "packages"}' in step for step in steps[:5])
        assert steps[5].endswith('install pymodaq_plugins_thorlabs tifffile libusb-package pyusb')
        assert [plugin in step for plugin, step in zip(install_lab.PLUGINS, steps[6:])] == [True] * 3
        assert not any('>=5.3' in step or step.endswith('install PyQt6') for step in steps)  # (not from PyPI)

    def test_another_qt_binding_goes_with_it(self, capsys):
        steps = self.dry_run(capsys, '--all', '--qt', 'pyside6')

        assert 'pymodaq_gui[pyside6]' in steps[3] and 'packages/pymodaq[pyside6]' in steps[4].replace('\\', '/')
        assert sum('[pyside6]' in step for step in steps) == 2

    def test_a_real_run_gives_the_version_to_the_core_steps_only(self, monkeypatch):
        calls = []

        class Result:
            returncode = 0

        monkeypatch.setattr(install_lab.subprocess, 'run', lambda step, **kwargs: calls.append((step, kwargs)) or Result())

        assert install_lab.main(['--all']) == 0

        assert len(calls) == 9 + (0 if sys.platform.startswith('win') else 2)
        assert [step for step, _ in calls[:5]] == install_lab.core_steps()
        assert all('env' in kwargs for _, kwargs in calls[:5]) and all(kwargs == {} for _, kwargs in calls[5:])

    def test_it_contradicts_no_pymodaq(self, capsys):
        assert install_lab.main(['--all', '--no-pymodaq', '--dry-run']) == 2

        assert '--all installs PyMoDAQ' in capsys.readouterr().err

    def test_it_is_in_the_help(self, capsys):
        with pytest.raises(SystemExit) as stop:
            install_lab.main(['--help'])

        told = capsys.readouterr().out
        assert stop.value.code == 0 and '--all' in told and 'everything' in told


def os_environment_is_empty() -> bool:
    import os
    return not os.environ


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
        applications = [name for name in scripts if name not in ('lab_apps', 'lab_export')]
        assert list(install_lab.APPLICATIONS) == applications  # (what the hint says is what the package installs)
        assert all(command in told for command in scripts) and 'qnw_tc_check' in told
        assert 'thermospectro ' not in told and 'Preset' not in told and 'qnw_tc_start' not in told

    def test_help(self, capsys):
        with pytest.raises(SystemExit) as stop:
            install_lab.main(['--help'])

        assert stop.value.code == 0 and '--camera' in capsys.readouterr().out
