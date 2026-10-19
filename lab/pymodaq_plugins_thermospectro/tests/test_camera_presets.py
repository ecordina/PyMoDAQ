"""The presets with a camera, loaded into a Dashboard as the user would: the extension takes the right modules"""
import pytest

from pymodaq.utils.gui_utils.loader_utils import create_extension

from conftest import release
from pymodaq_plugins_thermospectro import launcher
from pymodaq_plugins_thermospectro.extensions import thermo_spectro

pytestmark = pytest.mark.filterwarnings('ignore')


@pytest.fixture
def empty_dashboard(qtbot):
    import qt_themes
    from pymodaq_utils.config import GlobalConfig
    from pymodaq.dashboard import create_load_dashboard

    config = GlobalConfig()
    qt_themes.set_theme(theme=config('gui', 'style', 'theme')[0], style=config('gui', 'style', 'style')[0])
    shared_ui, dashboard = create_load_dashboard()
    yield dashboard
    dashboard.quit_fun()
    release(shared_ui.mainwindow)
    qtbot.wait(150)


def load_preset(qtbot, dashboard, name, n_modules):
    dashboard.experiment_manager.execute_entry(launcher.PRESETS_FOLDER / f'{name}.xml')
    qtbot.waitUntil(lambda: len(dashboard.modules_manager.modules_all) == n_modules, timeout=60000)
    qtbot.wait(600)


@pytest.mark.parametrize('preset, n_modules, expected', [
    ('ThermoCameraMock', 2, [(False, 'None', 'Thermo1'), (False, 'None', 'None'), (True, 'Camera1', 'Thermo1')]),
    ('ThermoCameraSpectroMock', 3, [(True, 'Spectro1', 'Thermo1'), (False, 'None', 'None'),
                                    (True, 'Camera1', 'Thermo1')]),
])
def test_the_extension_takes_the_modules_of_the_preset(empty_dashboard, qtbot, monkeypatch, tmp_path, preset, n_modules,
                                                       expected):
    load_preset(qtbot, empty_dashboard, preset, n_modules)
    monkeypatch.setattr(thermo_spectro, 'default_store_path', lambda: tmp_path / 'rois.json')
    shared_ui, extension = create_extension(empty_dashboard, thermo_spectro.ThermoSpectro)
    extension.do_things_after_experiment_set(preset)
    try:
        assert [(unit.settings.enabled, unit.settings.spectrometer, unit.settings.thermostat)
                for unit in extension.units] == expected
        camera = extension.unit(3).settings
        assert camera.run_ramp is (preset == 'ThermoCameraMock')  # (the camera does not drive what a spectrometer drives)
        assert extension.dock_visible('spectrum3') and extension.dock_visible('rois3')
        assert extension.dock_visible('spectrum1') is (preset == 'ThermoCameraSpectroMock')
    finally:
        window = extension.mainwindow
        extension.quit_fun()
        release(window)
