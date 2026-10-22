import gc
import logging
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))  # (the stand-ins of the modules: fakes.py)

# the tests build real Qt widgets: no display needed
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


def release(window):
    """Let go of a window: without it each test leaves a Dashboard behind and the memory grows to GB"""
    from qtpy import QtWidgets
    window.deleteLater()
    for _ in range(3):
        QtWidgets.QApplication.processEvents()
    gc.collect()


@pytest.fixture
def dashboard(qtbot, tmp_path, request):
    """A Dashboard with two Mock thermostats (the temperature axis of the Mock actuator) and two Mock spectrometers

    With ``@pytest.mark.parametrize('dashboard', ['camera'], indirect=True)`` it has a Mock camera (2D detector) too:
    *Camera1*. With ``'camera_only'`` it has *Thermo1* and *Camera1* and nothing else.
    """
    import qt_themes
    from pymodaq_utils.config import GlobalConfig
    from pymodaq.dashboard import create_load_dashboard
    from pymodaq.control_modules.enums import DAQTypesEnum
    from pymodaq.utils.managers.modules import ModuleType
    from pymodaq.utils.managers.modules.loader import PluginInfo

    config = GlobalConfig()
    qt_themes.set_theme(theme=config('gui', 'style', 'theme')[0], style=config('gui', 'style', 'style')[0])
    shared_ui, dashboard = create_load_dashboard()
    # (no qtbot.addWidget: pytest-qt would close the window before the fixtures are finalized, and the
    # extension needs it to quit properly)
    manager = dashboard.modules_manager
    camera_only = getattr(request, 'param', None) == 'camera_only'
    infos = [PluginInfo(id=manager.get_random_id(), name=name, class_name='Mock', type=ModuleType.Actuator,
                        is_master=True, do_init=True, ui=None, daq_type=None, axis_name='Temp')
             for name in (('Thermo1',) if camera_only else ('Thermo1', 'Thermo2'))]
    infos += [PluginInfo(id=manager.get_random_id(), name=name, class_name='Mock', type=ModuleType.Detector,
                         is_master=True, do_init=True, ui=None, daq_type=DAQTypesEnum.DAQ1D)
              for name in (() if camera_only else ('Spectro1', 'Spectro2'))]
    if getattr(request, 'param', None) in ('camera', 'camera_only'):
        infos.append(PluginInfo(id=manager.get_random_id(), name='Camera1', class_name='Mock',
                                type=ModuleType.Detector, is_master=True, do_init=True, ui=None,
                                daq_type=DAQTypesEnum.DAQ2D))
    dashboard.module_creator.load_created_modules([[info] for info in infos])
    qtbot.waitUntil(lambda: len(manager.modules_all) == len(infos)
                    and all(m.initialized_state for m in manager.modules_all), timeout=60000)
    # The threads of the modules go on setting up (and deleting objects) a moment after they say they are initialized.
    # A thread deleting a sip object holds a Qt mutex and waits for the GIL, which the GUI thread holds while it waits
    # for a Qt mutex to build a big settings tree: a deadlock, rare, which this wait makes much rarer
    qtbot.wait(600)
    yield dashboard
    dashboard.quit_fun()
    release(shared_ui.mainwindow)
    qtbot.wait(150)  # (the threads of the modules delete their objects: let them finish before the next test)


@pytest.fixture
def ext(dashboard, qtbot, tmp_path, monkeypatch):
    """The extension on that Dashboard. Dialogs are replaced: what the user would be told is in ext.notifications

    The ROIs and the settings of the Saving window are remembered in the folder of the test, and the files are made in its
    ``data`` folder. The experiments are quick: a readout every 0.1 s, a ramp from 0 to 3 (the Mock thermostat starts at
    0) in 2 s, one cycle.
    """
    from pymodaq.utils.gui_utils.loader_utils import create_extension
    from pymodaq_plugins_thermospectro.extensions import thermo_spectro
    from pymodaq_plugins_thermospectro.camera import RectRoi
    from pymodaq_plugins_thermospectro.rois import Roi
    from pymodaq_plugins_thermospectro.unit_settings import END_LOG, CameraSettings

    monkeypatch.setattr(thermo_spectro, 'default_store_path', lambda: tmp_path / 'remembered' / 'rois.json')
    shared_ui, extension = create_extension(dashboard, thermo_spectro.ThermoSpectro)
    extension.do_things_after_experiment_set('test')  # as the experiment manager does when a preset is applied
    extension.notifications = []
    extension.answers = {'abort_return': False, 'quit': 'jump'}
    monkeypatch.setattr(extension, '_notify',
                        lambda title, text, error=False: extension.notifications.append((title, text, error)))
    monkeypatch.setattr(extension, '_ask_abort_return', lambda: extension.answers['abort_return'])
    monkeypatch.setattr(extension, '_ask_quit_choice', lambda: extension.answers['quit'])
    extension.saving_panel.set_values({'folder': str(tmp_path / 'data')})
    for unit in extension.units:
        settings = unit.settings
        settings.child('integration_time').setValue(20.)  # (the defaults, 500 ms and 1 s, make slow tests)
        settings.child('readout_time').setValue(0.1)
        if isinstance(settings, CameraSettings):  # (inside the Mock image)
            settings.set_rois([RectRoi(f'ROI {i}', 10. * i, 10. * i + 8., 5. * i, 5. * i + 8.) for i in range(1, 4)])
        else:
            settings.set_rois([Roi(f'ROI {i}', 50. * i, 50. * i + 10.) for i in range(1, 7)])  # (inside the Mock spectrum)
        extension._rois_changed(unit, reset=True)
        ramp = settings.child('ramp')
        ramp['initial'] = 0.0
        ramp['final'] = 3.0
        ramp['velocity'] = 90.0  # 1.5 per second: 2 s
        ramp['n_cycles'] = 1
        ramp['end_at'] = END_LOG  # (the log ends with the ramps: the default, which goes on, has its own tests)
    extension.settings['experiment', 'update_period'] = 0.1
    extension.settings['experiment', 'watchdog'] = 0.0
    yield extension
    if extension.run_active:
        extension._finish_run('test over', abort=True)
        qtbot.waitUntil(lambda: not extension._worker.is_saving, timeout=20000)
    if extension.monitoring:
        extension.stop_monitoring()
    window = extension.mainwindow
    extension.quit_fun()
    release(window)
    qtbot.wait(150)  # (the saver thread deletes its objects: let it finish before the next test builds a big tree)


@pytest.fixture(autouse=True)
def saving_settings_in_tmp(tmp_path, monkeypatch):
    """What is typed in the Saving window is remembered in the folder of the test, never in the one of the user"""
    from pymodaq_plugins_thermospectro.extensions import thermo_spectro
    monkeypatch.setattr(thermo_spectro, 'default_saving_path', lambda: tmp_path / 'remembered' / 'saving.json')


@pytest.fixture(autouse=True)
def no_error_logged(request):
    """An error logged by the package is a bug (the slots log what they raise instead of aborting the application):
    the test fails, unless it is marked with ``@pytest.mark.errors_expected``"""
    records = []

    class Collector(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.ERROR and 'pymodaq_plugins_thermospectro' in (record.pathname or ''):
                records.append(record)

    handler = Collector()
    logging.getLogger().addHandler(handler)
    yield
    logging.getLogger().removeHandler(handler)
    if request.node.get_closest_marker('errors_expected') is None:
        assert not records, 'Errors were logged: ' + '; '.join(f'{r.name}: {r.getMessage()}' for r in records)


def pytest_configure(config):
    config.addinivalue_line('markers', 'errors_expected: the test makes the package log an error on purpose')
