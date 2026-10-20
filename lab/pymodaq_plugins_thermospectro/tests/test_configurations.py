"""The five applications of the lab: each one has the settings and the windows of its own instruments and nothing else"""
import pytest

from pymodaq.utils.gui_utils.loader_utils import create_extension

from conftest import release
from pymodaq_plugins_thermospectro import configurations
from pymodaq_plugins_thermospectro.configurations import CONFIGURATIONS, configuration_of_class
from pymodaq_plugins_thermospectro.unit_settings import END_INITIAL
from pymodaq_plugins_thermospectro.extensions import (camera_only, thermo_2spectro, thermo_camera,
                                                      thermo_camera_spectro, thermo_spectro, thermo_spectro_one)

pytestmark = pytest.mark.filterwarnings('ignore')

MODULES = {'thermo_2spectro': thermo_2spectro, 'thermo_spectro': thermo_spectro_one, 'camera': camera_only,
           'thermo_camera': thermo_camera, 'thermo_camera_spectro': thermo_camera_spectro}


class TestConfigurations:
    def test_the_five_commands(self):
        assert list(CONFIGURATIONS) == ['thermo_2spectro', 'thermo_spectro', 'camera', 'thermo_camera',
                                        'thermo_camera_spectro']

    @pytest.mark.parametrize('command, spectrometers, thermostats, camera, names', [
        ('thermo_2spectro', 2, 2, False, ['Thermo1', 'Thermo2', 'Spectro1', 'Spectro2']),
        ('thermo_spectro', 1, 1, False, ['Thermo1', 'Spectro1']),
        ('camera', 0, 0, True, ['Camera1']),
        ('thermo_camera', 0, 1, True, ['Thermo1', 'Camera1']),
        ('thermo_camera_spectro', 1, 1, True, ['Thermo1', 'Spectro1', 'Camera1']),
    ])
    def test_the_instruments_of_each(self, command, spectrometers, thermostats, camera, names):
        configuration = CONFIGURATIONS[command]

        assert (configuration.spectrometers, configuration.thermostats, configuration.camera) == (
            spectrometers, thermostats, camera)
        assert configuration.module_names == names

    def test_the_units_follow_the_instruments(self):
        for configuration in CONFIGURATIONS.values():
            wanted = list(configurations.SPECTROMETER_NUMBERS[:configuration.spectrometers])
            wanted += [configurations.CAMERA_NUMBER] if configuration.camera else []
            assert list(configuration.numbers) == wanted

    def test_each_extension_class_is_known(self):
        for command, configuration in CONFIGURATIONS.items():
            module = MODULES[command]
            assert module.CLASS_NAME == configuration.class_name
            assert getattr(module, module.CLASS_NAME).NUMBERS == configuration.numbers
            assert configuration_of_class(configuration.class_name) is configuration
        assert configuration_of_class('ThermoSpectro') is None

    @pytest.mark.parametrize('command, detectors, everything, hint', [
        ('thermo_2spectro', 'spectrometers', 'spectrometers and thermal controllers', 'spectrum'),
        ('thermo_spectro', 'spectrometer', 'spectrometer and thermal controller', 'spectrum'),
        ('camera', 'camera', 'camera', 'image'),
        ('thermo_camera', 'camera', 'camera and thermal controller', 'image'),
        ('thermo_camera_spectro', 'spectrometer and camera', 'spectrometer and camera and thermal controller', 'both'),
    ])
    def test_the_words_are_those_of_the_instruments_it_has(self, command, detectors, everything, hint):
        cls = getattr(MODULES[command], CONFIGURATIONS[command].class_name)

        assert cls._instrument_words(cls) == (detectors, everything)
        text = cls._roi_hint(cls)
        assert ('image' in text) == (hint in ('image', 'both')) and ('spectrum' in text) == (hint in ('spectrum', 'both'))
        assert ('Camera:' in text) == (hint == 'both')

    def test_the_base_class_is_not_an_extension_of_the_dashboard(self):
        assert not hasattr(thermo_spectro, 'EXTENSION_NAME') and not hasattr(thermo_spectro, 'CLASS_NAME')

    def test_pymodaq_finds_the_five_extensions(self):
        from pymodaq.extensions import ExtensionEnum
        from pymodaq.extensions.utils import get_extensions

        found = get_extensions()

        for configuration in CONFIGURATIONS.values():
            assert ExtensionEnum[configuration.class_name.upper()] in found
        assert 'THERMOSPECTRO' not in [key.name for key in found]


def dashboard_with(qtbot, names, camera='Camera1'):
    """A Dashboard with Mock modules of these names"""
    import qt_themes
    from pymodaq_utils.config import GlobalConfig
    from pymodaq.dashboard import create_load_dashboard
    from pymodaq.control_modules.enums import DAQTypesEnum
    from pymodaq.utils.managers.modules import ModuleType
    from pymodaq.utils.managers.modules.loader import PluginInfo

    config = GlobalConfig()
    qt_themes.set_theme(theme=config('gui', 'style', 'theme')[0], style=config('gui', 'style', 'style')[0])
    shared_ui, dashboard = create_load_dashboard()
    manager = dashboard.modules_manager
    infos = []
    for name in names:
        if name.startswith('Thermo'):
            infos.append(PluginInfo(id=manager.get_random_id(), name=name, class_name='Mock', type=ModuleType.Actuator,
                                    is_master=True, do_init=True, ui=None, daq_type=None, axis_name='Temp'))
        else:
            dim = DAQTypesEnum.DAQ2D if name.startswith('Camera') else DAQTypesEnum.DAQ1D
            infos.append(PluginInfo(id=manager.get_random_id(), name=name, class_name='Mock', type=ModuleType.Detector,
                                    is_master=True, do_init=True, ui=None, daq_type=dim))
    dashboard.module_creator.load_created_modules([[info] for info in infos])
    qtbot.waitUntil(lambda: len(manager.modules_all) == len(infos)
                    and all(m.initialized_state for m in manager.modules_all), timeout=60000)
    qtbot.wait(600)
    return shared_ui, dashboard


@pytest.mark.parametrize('command', list(CONFIGURATIONS))
def test_an_application_has_the_settings_and_the_windows_of_its_instruments_only(qtbot, monkeypatch, tmp_path, command):
    configuration = CONFIGURATIONS[command]
    shared_ui, dashboard = dashboard_with(qtbot, configuration.module_names)
    module = MODULES[command]
    monkeypatch.setattr(thermo_spectro, 'default_store_path', lambda: tmp_path / 'rois.json')
    shared_ui, extension = create_extension(dashboard, getattr(module, module.CLASS_NAME))
    extension.do_things_after_experiment_set('test')
    try:
        numbers = list(configuration.numbers)
        assert [unit.number for unit in extension.units] == numbers
        assert [child.name() for child in extension.settings.children() if child.name().startswith('unit')] == [
            f'unit{number}' for number in numbers]
        assert [view.number for view in extension.views] == numbers
        for number in (1, 2, 3):
            present = number in numbers
            assert (f'spectrum{number}' in extension.docks) is present and (f'rois{number}' in extension.docks) is present
            assert extension.has_action(f'show_spectrum{number}') is present
        assert all(unit.settings.enabled for unit in extension.units)  # (their instruments are there)
        assert [unit.settings.thermostat for unit in extension.units] == [
            ('Thermo1' if number == 3 else f'Thermo{number}') if configuration.thermostats else 'None'
            for number in numbers]
        for number in set((1, 2, 3)) - set(numbers):
            with pytest.raises(KeyError):
                extension.unit(number)
        # the buttons which end the ramps only exist where there is a thermal controller to ramp
        for name in ('end_top', 'end_bottom'):
            assert extension.has_action(name) is bool(configuration.thermostats)
        assert [unit.settings.end_at for unit in extension.units] == [END_INITIAL] * len(numbers)
        assert extension.settings.child('experiment', 'update_period').opts.get('visible', True) is bool(
            configuration.thermostats)
    finally:
        window = extension.mainwindow
        extension.quit_fun()
        dashboard.quit_fun()
        release(window)
        qtbot.wait(150)
