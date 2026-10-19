import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from pymodaq_plugins_thermospectro import launcher
from pymodaq_plugins_thermospectro.extensions import thermo_spectro

SPECTRO_PRESETS = ['ThermoSpectroMock', 'ThermoSpectroSeabreeze', 'ThermoSpectroSingleMock',
                   'ThermoSpectroSingleSeabreeze']
CAMERA_PRESETS = ['ThermoCameraMock', 'ThermoCameraSpectroMock', 'ThermoCameraThorlabsTSI',
                  'ThermoCameraSpectroThorlabsTSI', 'ThermoCameraThorlabsUC480', 'ThermoCameraSpectroThorlabsUC480',
                  'ThermoZeluxMockThermo', 'ThermoZeluxMockSpectroMockThermo']
PRESETS = sorted(SPECTRO_PRESETS + CAMERA_PRESETS)


@pytest.fixture
def folder(tmp_path, monkeypatch):
    """An experiments folder of PyMoDAQ: the real one is not touched"""
    path = tmp_path / 'experiments'
    path.mkdir()
    monkeypatch.setattr(launcher, 'experiments_folder', lambda: path)
    return path


@pytest.fixture
def dashboard_calls(monkeypatch):
    """The Dashboard is not started: what it is given on its command line is recorded"""
    import pymodaq.dashboard
    calls = []
    monkeypatch.setattr(pymodaq.dashboard, 'main', lambda: calls.append(list(sys.argv)))
    monkeypatch.setattr(sys, 'argv', list(sys.argv))  # (the launcher replaces it: it is put back afterwards)
    return calls


def modules(preset: str, kind: str) -> list[tuple[str, str, str]]:
    """(name, plugin, init) of the detectors or the actuators of a preset"""
    root = ET.parse(launcher.PRESETS_FOLDER / f'{preset}.xml').getroot()
    return [(child.find('name').text, child.find('info/type').text, child.find('info/init').text)
            for child in root.find(kind)]


def dimensions(preset: str) -> list[str]:
    """The dimension of each detector of a preset"""
    root = ET.parse(launcher.PRESETS_FOLDER / f'{preset}.xml').getroot()
    return [child.find('info/dim').text for child in root.find('detector')]


def first_module(preset: str, kind: str) -> bytes:
    """The whole settings of the first detector or actuator of a preset"""
    root = ET.parse(launcher.PRESETS_FOLDER / f'{preset}.xml').getroot()
    return ET.tostring(root.find(kind)[0])


def refuse(*args):
    raise PermissionError('Permission denied')


class TestPresets:
    def test_the_presets_of_the_package(self):
        assert launcher.bundled_presets() == PRESETS
        assert launcher.DEFAULT_PRESET in launcher.bundled_presets()

    def test_two_mock_spectrometers_and_two_thermostats(self):
        assert [m[:2] for m in modules('ThermoSpectroMock', 'detector')] == [('Spectro1', 'Mock'), ('Spectro2', 'Mock')]
        assert [m[:2] for m in modules('ThermoSpectroMock', 'actuator')] == [('Thermo1', 'Mock'), ('Thermo2', 'Mock')]

    def test_two_seabreeze_spectrometers_and_two_thermostats(self):
        assert [m[:2] for m in modules('ThermoSpectroSeabreeze', 'detector')] == [('Spectro1', 'Seabreeze'),
                                                                                  ('Spectro2', 'Seabreeze')]
        assert [m[:2] for m in modules('ThermoSpectroSeabreeze', 'actuator')] == [('Thermo1', 'Mock'),
                                                                                  ('Thermo2', 'Mock')]

    def test_a_camera_and_a_thermostat(self):
        assert modules('ThermoCameraMock', 'detector') == [('Camera1', 'Mock', '0')]
        assert [m[:2] for m in modules('ThermoCameraMock', 'actuator')] == [('Thermo1', 'Mock')]
        assert dimensions('ThermoCameraMock') == ['DAQ2D']

    def test_a_camera_a_spectrometer_and_one_thermostat(self):
        assert [m[:2] for m in modules('ThermoCameraSpectroMock', 'detector')] == [('Spectro1', 'Mock'),
                                                                                   ('Camera1', 'Mock')]
        assert [m[:2] for m in modules('ThermoCameraSpectroMock', 'actuator')] == [('Thermo1', 'Mock')]
        assert dimensions('ThermoCameraSpectroMock') == ['DAQ1D', 'DAQ2D']

    @pytest.mark.parametrize('plugin', ['Thorlabs_TSI', 'UC480'])
    def test_thorlabs_cameras_with_the_thermostat_of_quantum_northwest(self, plugin):
        suffix = 'TSI' if plugin == 'Thorlabs_TSI' else 'UC480'

        assert [m[:2] for m in modules(f'ThermoCameraThorlabs{suffix}', 'detector')] == [('Camera1', plugin)]
        assert [m[:2] for m in modules(f'ThermoCameraSpectroThorlabs{suffix}', 'detector')] == [
            ('Spectro1', 'Seabreeze'), ('Camera1', plugin)]
        for preset in (f'ThermoCameraThorlabs{suffix}', f'ThermoCameraSpectroThorlabs{suffix}'):
            assert [m[:2] for m in modules(preset, 'actuator')] == [('Thermo1', 'QNW_TC')]
        assert dimensions(f'ThermoCameraSpectroThorlabs{suffix}') == ['DAQ1D', 'DAQ2D']

    def test_the_zelux_camera_with_mock_thermostat_and_spectrometer(self):
        """The presets of lab/windows/zelux.bat and zeluxspec.bat: a Thorlabs Zelux camera, the rest is simulated"""
        assert [m[:2] for m in modules('ThermoZeluxMockThermo', 'detector')] == [('Camera1', 'Thorlabs_TSI')]
        assert [m[:2] for m in modules('ThermoZeluxMockThermo', 'actuator')] == [('Thermo1', 'Mock')]
        assert [m[:2] for m in modules('ThermoZeluxMockSpectroMockThermo', 'detector')] == [
            ('Spectro1', 'Mock'), ('Camera1', 'Thorlabs_TSI')]
        assert [m[:2] for m in modules('ThermoZeluxMockSpectroMockThermo', 'actuator')] == [('Thermo1', 'Mock')]
        assert dimensions('ThermoZeluxMockSpectroMockThermo') == ['DAQ1D', 'DAQ2D']

    @pytest.mark.parametrize('preset', PRESETS)
    def test_nothing_is_initialized_when_a_preset_opens(self, preset):
        """The user connects the modules (the button of the extension) when the spectrometers are plugged in"""
        for kind in ('detector', 'actuator'):
            assert all(init == '0' for _, _, init in modules(preset, kind))

    def test_the_camera_presets_are_made_like_the_others(self):
        """The module of a camera preset is written as the one of the other presets: same groups, same settings"""
        def shape(element):
            return element.tag, element.attrib.get('type'), [shape(child) for child in element]

        mock = ET.parse(launcher.PRESETS_FOLDER / 'ThermoSpectroSingleMock.xml').getroot()
        for preset in CAMERA_PRESETS:
            root = ET.parse(launcher.PRESETS_FOLDER / f'{preset}.xml').getroot()
            assert shape(root.find('actuator/actuator00')) == shape(mock.find('actuator/actuator00'))
            assert shape(root.find('detector/detector00')) == shape(mock.find('detector/detector00'))

    @pytest.mark.parametrize('plugin', ['Mock', 'Seabreeze'])
    def test_the_single_presets_are_the_first_unit_of_the_others(self, plugin):
        single, both = f'ThermoSpectroSingle{plugin}', f'ThermoSpectro{plugin}'

        assert modules(single, 'detector') == [('Spectro1', plugin, '0')]
        assert modules(single, 'actuator') == [('Thermo1', 'Mock', '0')]
        for kind in ('detector', 'actuator'):
            assert first_module(single, kind) == first_module(both, kind)

    def test_the_thermostats_are_temperature_axes(self):
        for preset in launcher.bundled_presets():
            root = ET.parse(launcher.PRESETS_FOLDER / f'{preset}.xml').getroot()
            axes = [child.find('controller/axis').text for child in root.find('actuator')]
            assert set(axes) <= {"str('Temp')", "str('Temperature')"} and 1 <= len(axes) <= 2  # (Mock, QNW_TC)

    def test_the_modules_of_a_preset_have_their_own_controller(self):
        for preset in launcher.bundled_presets():
            root = ET.parse(launcher.PRESETS_FOLDER / f'{preset}.xml').getroot()
            ids = [child.find('controller/controller_ID').text for kind in ('actuator', 'detector')
                   for child in root.find(kind)]
            assert len(set(ids)) == len(ids)  # (they are all masters: two modules must not share a controller)


class TestInstallation:
    def test_the_presets_which_are_missing_are_copied(self, folder):
        assert launcher.install_presets() == PRESETS

        for name in PRESETS:
            assert (folder / f'{name}.xml').read_bytes() == (launcher.PRESETS_FOLDER / f'{name}.xml').read_bytes()

    def test_a_preset_which_is_there_is_not_replaced(self, folder):
        (folder / 'ThermoSpectroMock.xml').write_text('edited by the user')

        assert launcher.install_presets() == [name for name in PRESETS if name != 'ThermoSpectroMock']

        assert (folder / 'ThermoSpectroMock.xml').read_text() == 'edited by the user'
        assert launcher.install_presets() == []  # (nothing left to copy)

    def test_the_other_presets_are_left_alone(self, folder):
        (folder / 'mine.xml').write_text('x')

        launcher.install_presets()

        assert (folder / 'mine.xml').read_text() == 'x'
        assert sorted(path.stem for path in folder.iterdir()) == sorted(PRESETS + ['mine'])


class TestCommand:
    def test_the_default_preset_and_the_extension(self, folder, dashboard_calls):
        launcher.main([])

        assert dashboard_calls == [['thermospectro', '-x', 'ThermoSpectroSeabreeze', '-e', 'ThermoSpectro']]

    def test_both_presets_are_in_the_dashboard_whichever_one_is_started(self, folder, dashboard_calls, capsys):
        launcher.main(['ThermoSpectroMock'])

        assert dashboard_calls == [['thermospectro', '-x', 'ThermoSpectroMock', '-e', 'ThermoSpectro']]
        assert sorted(path.stem for path in folder.iterdir()) == PRESETS
        told = capsys.readouterr().out
        assert 'ThermoSpectroMock, ThermoSpectroSeabreeze' in told and 'Load Experiments' in told

    def test_nothing_is_told_when_nothing_is_copied(self, folder, dashboard_calls, capsys):
        launcher.install_presets()

        launcher.main([])

        assert capsys.readouterr().out == ''

    def test_the_extension_of_the_file_may_be_given(self, folder, dashboard_calls):
        launcher.main(['ThermoSpectroMock.xml'])

        assert dashboard_calls[0][2] == 'ThermoSpectroMock'

    def test_a_preset_of_the_user(self, folder, dashboard_calls):
        (folder / 'mine.xml').write_text('x')

        launcher.main(['mine'])

        assert dashboard_calls == [['thermospectro', '-x', 'mine', '-e', 'ThermoSpectro']]
        assert (folder / 'ThermoSpectroMock.xml').exists()  # (and the ones of the package are there too)

    def test_an_unknown_preset_is_told(self, folder, dashboard_calls, capsys):
        (folder / 'other.xml').write_text('x')

        with pytest.raises(SystemExit) as info:
            launcher.main(['nothing'])

        assert info.value.code == 2
        error = capsys.readouterr().err
        assert 'nothing' in error and 'other' in error and 'ThermoSpectroMock' in error and str(folder) in error
        assert dashboard_calls == []

    def test_a_folder_which_cannot_be_written_is_told(self, folder, dashboard_calls, capsys, monkeypatch):
        monkeypatch.setattr(launcher.shutil, 'copyfile', refuse)

        with pytest.raises(SystemExit) as info:
            launcher.main([])

        assert info.value.code == 1
        error = capsys.readouterr().err
        assert 'Permission denied' in error and str(launcher.PRESETS_FOLDER) in error and str(folder) in error
        assert dashboard_calls == []

    def test_a_folder_which_cannot_be_written_does_not_stop_a_preset_which_is_there(self, folder, dashboard_calls,
                                                                                    capsys, monkeypatch):
        (folder / 'ThermoSpectroSeabreeze.xml').write_text('x')  # (the Mock one is missing, and cannot be copied)
        monkeypatch.setattr(launcher.shutil, 'copyfile', refuse)

        launcher.main([])

        assert dashboard_calls == [['thermospectro', '-x', 'ThermoSpectroSeabreeze', '-e', 'ThermoSpectro']]
        assert 'Permission denied' in capsys.readouterr().err

    def test_an_unknown_preset_in_a_folder_which_cannot_be_written_is_not_a_copy_problem(self, folder,
                                                                                         dashboard_calls, capsys,
                                                                                         monkeypatch):
        monkeypatch.setattr(launcher.shutil, 'copyfile', refuse)

        with pytest.raises(SystemExit) as info:
            launcher.main(['nothing'])

        assert info.value.code == 2
        assert 'there is no preset nothing' in capsys.readouterr().err

    def test_a_path_is_refused(self, folder, dashboard_calls, capsys):
        with pytest.raises(SystemExit) as info:
            launcher.main([str(folder / 'ThermoSpectroMock.xml')])

        assert info.value.code == 2
        assert 'not a path' in capsys.readouterr().err
        assert dashboard_calls == []

    def test_the_help_lists_the_presets(self, capsys):
        with pytest.raises(SystemExit) as info:
            launcher.main(['--help'])

        assert info.value.code == 0
        assert 'ThermoSpectroMock' in capsys.readouterr().out

    def test_the_dashboard_will_find_the_extension(self):
        # `dashboard -e NAME` looks for NAME.upper() among the names of the extensions, which are the upper-cased
        # classes of the modules of the 'extensions' folders of the plugins
        assert launcher.EXTENSION.upper() == thermo_spectro.CLASS_NAME.upper()
        assert hasattr(thermo_spectro, thermo_spectro.CLASS_NAME)

    def test_the_command_is_declared_by_the_package(self):
        import toml
        project = toml.load(Path(__file__).parents[1] / 'pyproject.toml')

        assert project['project']['scripts'] == {'thermospectro': 'pymodaq_plugins_thermospectro.launcher:main'}
