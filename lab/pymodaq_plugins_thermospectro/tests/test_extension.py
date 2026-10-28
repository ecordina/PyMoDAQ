"""The extension on a Dashboard with Mock modules: two Mock spectrometers and two Mock thermostats"""
import ast
import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import h5py
import numpy as np
import pytest

from pymodaq.utils.managers.modules import ModuleType

from pymodaq_plugins_thermospectro import unit as unit_module
from pymodaq_plugins_thermospectro.extensions import thermo_spectro
from pymodaq_plugins_thermospectro.ramp import PHASE_CODES, Phase
from pymodaq_plugins_thermospectro.rois import Roi, RoiStore, default_rois, window_mask
from pymodaq_plugins_thermospectro.unit import UnitState

pytestmark = pytest.mark.filterwarnings('ignore')

SLOW = dict(velocity=30.)  # 0.5 per second: the ramp from 0 to 3 takes 6 s


def spy_setpoints(ext):
    """Keep what is sent to each thermostat: {1: [...], 2: [...]}"""
    sent = {unit.number: [] for unit in ext.units}
    for unit in ext.units:
        original = unit.send_setpoint

        def spy(value, unit=unit, original=original):
            sent[unit.number].append(value)
            original(value)

        unit.send_setpoint = spy
    return sent


def set_ramp(ext, units=None, **values):
    for unit in units or ext.units:
        for name, value in values.items():
            unit.settings.child('ramp', name).setValue(value)


def run(qtbot, ext, **ramp):
    """Start an experiment and wait until it is going"""
    set_ramp(ext, **ramp)
    ext.start_run()
    qtbot.waitUntil(lambda: ext.run_active, timeout=20000)


def wait_for_end(qtbot, ext, timeout=40000):
    qtbot.waitUntil(lambda: not ext.run_active, timeout=timeout)
    qtbot.waitUntil(lambda: not ext._worker.is_saving, timeout=20000)  # the file is closed


def wait_for(qtbot, condition, timeout=10000):
    qtbot.waitUntil(condition, timeout=timeout)


def end_run(qtbot, ext):
    """Stop an experiment right away (a first stop starts the way back, a second one gives up on it)"""
    ext.answers['abort_return'] = True
    ext.stop_run()
    if ext.run_active:
        ext.stop_run()
    wait_for_end(qtbot, ext)


def ui_enabled(param) -> bool:
    """Whether the user can edit a parameter: what its widgets in the tree say (a group does not disable them)"""
    widgets = [getattr(item, 'widget', None) or getattr(item, 'button', None) for item in param.items]
    widgets = [widget for widget in widgets if widget is not None]
    assert widgets, f'{param.name()} is not shown in the tree'
    return all(widget.isEnabled() for widget in widgets)


def data_files(tmp_path) -> list[Path]:
    return sorted((tmp_path / 'data').rglob('*.h5'))


def h5_path(tmp_path) -> Path:
    files = data_files(tmp_path)
    assert len(files) == 1, files
    return files[0]


def _text(value):
    """An attribute as the text it holds (PyMoDAQ saves them as {"module": ..., "type": "str", "data": "'text'"})"""
    text = value.decode() if isinstance(value, bytes) else str(value)
    try:
        text = ast.literal_eval(json.loads(text)['data'])
    except (ValueError, KeyError, SyntaxError, TypeError):
        pass
    return text.decode() if isinstance(text, bytes) else text


class H5Experiment:
    """What an experiment of the extension saved"""

    def __init__(self, tmp_path, index=0, file=None):
        self.file = h5py.File(file or h5_path(tmp_path), 'r')
        self.group = self.file['RawData'][f'Datalogger{index:03d}']
        self.modules = {_text(group.attrs['TITLE']): group for name, group in self.group.items()
                        if isinstance(group, h5py.Group) and 'TITLE' in group.attrs and name != 'Notes'}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.file.close()

    @property
    def titles(self) -> list[str]:
        return sorted(self.modules)

    def time(self, title) -> np.ndarray:
        return self.modules[title]['NavAxes']['Axis00'][:]

    def zero_d(self, title, index=0) -> np.ndarray:
        return self.modules[title]['Data0D']['CH00'][f'EnlData{index:02d}'][:].ravel()

    def n_zero_d(self, title) -> int:
        return len(self.modules[title]['Data0D']['CH00'])

    def spectra(self, title) -> np.ndarray:
        return self.modules[title]['Data1D']['CH00']['EnlData00'][:]

    def wavelengths(self, title) -> np.ndarray:
        return self.modules[title]['Data1D']['CH00']['Axis00'][:]

    def label(self, title, index) -> str:
        return _text(self.modules[title]['Data0D']['CH00'][f'EnlData{index:02d}'].attrs['label'])

    def attribute(self, title, name):
        return _text(self.modules[title].attrs[name])


class TestModules:
    def test_the_modules_of_the_dashboard_are_selected(self, ext):
        assert [(u.settings.spectrometer, u.settings.thermostat) for u in ext.units[:2]] == [
            ('Spectro1', 'Thermo1'), ('Spectro2', 'Thermo2')]
        assert all(unit.settings.enabled for unit in ext.units[:2])

    def test_the_units_of_the_thermostat_are_used(self, ext):
        ramp = ext.unit(1).settings.child('ramp')

        assert ramp.child('final').opts['suffix'] == '°C'
        assert ramp.child('velocity').opts['suffix'] == '°C/min'

    def test_the_cycle_duration_is_shown(self, ext):
        assert ext.unit(1).settings.child('ramp', 'cycle_duration').value().startswith('0:04')  # 2 s up, 2 s back
        ext.unit(1).settings.child('ramp', 'final').setValue(0.)
        assert ext.unit(1).settings.child('ramp', 'cycle_duration').value().startswith('invalid')
        assert ext.unit(2).settings.child('ramp', 'cycle_duration').value().startswith('0:04')  # (the other unit)

    def test_modules_cannot_be_changed_while_looking(self, ext):
        ext.start_monitoring()

        for unit in ext.units[:2]:
            for name in ('enabled', 'spectrometer', 'thermostat'):
                assert not unit.settings.child(name).opts['enabled']
            assert unit.settings.child('integration_time').opts['enabled']  # this one can
        ext.stop_monitoring()
        assert ext.unit(1).settings.child('spectrometer').opts['enabled']

    def test_only_1d_detectors_are_offered_as_spectrometers(self, ext, monkeypatch):
        from types import SimpleNamespace
        from pymodaq.control_modules.enums import DAQTypesEnum
        manager = ext.modules_manager
        camera = SimpleNamespace(title='Camera', daq_type=DAQTypesEnum.DAQ2D)
        monkeypatch.setattr(manager, 'detectors_all', list(manager.detectors_all) + [camera])

        ext._offer_modules()

        limits = ext.unit(1).settings.child('spectrometer').opts['limits']
        assert 'Camera' not in limits and 'Spectro1' in limits and 'Spectro2' in limits

    def test_one_spectrometer_in_the_preset_is_one_unit(self, ext, monkeypatch):
        manager = ext.modules_manager
        monkeypatch.setattr(manager, 'detectors_all', list(manager.detectors_all)[:1])
        ext._modules_offered = False  # as when the extension starts

        ext._offer_modules()

        assert ext.unit(1).settings.enabled and not ext.unit(2).settings.enabled
        assert ext.unit(2).settings.spectrometer == 'None'
        assert ext.dock_visible('spectrum1') and ext.dock_visible('rois1')
        assert not ext.dock_visible('spectrum2') and not ext.dock_visible('rois2')

    def test_the_modules_loaded_after_the_extension_are_taken(self, ext, monkeypatch):
        """`dashboard -x preset -e ThermoSpectro` opens the extension while the Dashboard loads the preset"""
        manager = ext.modules_manager
        detectors, actuators = list(manager.detectors_all), list(manager.actuators_all)
        monkeypatch.setattr(manager, 'detectors_all', [])
        monkeypatch.setattr(manager, 'actuators_all', [])
        ext._modules_offered = False  # as when the extension starts
        ext._offer_modules()  # (nothing there yet)
        assert not ext.unit(1).settings.enabled and not ext.unit(2).settings.enabled

        monkeypatch.setattr(manager, 'detectors_all', detectors)
        monkeypatch.setattr(manager, 'actuators_all', actuators)
        ext.do_things_after_experiment_set('preset')  # (the preset is loaded)

        for number, unit in enumerate(ext.units[:2], start=1):
            assert unit.settings.enabled
            assert unit.settings.spectrometer == f'Spectro{number}' and unit.settings.thermostat == f'Thermo{number}'
            assert ext.dock_visible(f'spectrum{number}') and ext.dock_visible(f'rois{number}')

    def test_offering_the_modules_looks_for_each_module_once_and_never_for_none(self, ext, monkeypatch):
        """A list which has just got new limits has no value for a moment: PyMoDAQ would warn that no module is
        called None (in the log, where the user looks first)"""
        from pymodaq.utils.managers.modules import ModuleType
        manager = ext.modules_manager
        original = manager.get_mod_from_name
        asked = []

        def spy(name, mod=ModuleType.Detector):
            asked.append(name)
            return original(name, mod)

        monkeypatch.setattr(manager, 'get_mod_from_name', spy)
        ext._modules_offered = False  # as when the extension starts

        ext._offer_modules()

        assert sorted(asked) == ['Spectro1', 'Spectro2', 'Thermo1', 'Thermo1', 'Thermo2']  # (the camera has Thermo1 too)

    def test_the_choice_of_the_user_is_kept_when_another_preset_is_applied(self, ext):
        ext.unit(1).settings.child('spectrometer').setValue('Spectro2')
        ext.unit(2).settings.child('enabled').setValue(False)

        ext.do_things_after_experiment_set('another preset')

        assert ext.unit(1).settings.spectrometer == 'Spectro2'
        assert not ext.unit(2).settings.enabled

    def test_defaults_that_were_asked_for(self, dashboard, qtbot, monkeypatch, tmp_path):
        from pymodaq.utils.gui_utils.loader_utils import create_extension
        monkeypatch.setattr(thermo_spectro, 'default_store_path', lambda: tmp_path / 'fresh' / 'rois.json')
        shared_ui, fresh = create_extension(dashboard, thermo_spectro.ThermoSpectro)
        try:
            for unit in fresh.units[:2]:
                settings = unit.settings
                assert settings.integration_time == 500.  # ms
                assert settings.readout_time == 1.  # s
                assert settings.rois == default_rois(unit.number) and len(settings.rois) == 6
                assert settings.record_spectrum and settings.record_temperature
                assert all(roi.record and roi.plot for roi in settings.rois)
                assert settings.plot_temperature and settings.run_ramp
        finally:
            fresh.quit_fun()

    def test_the_two_spectrometers_do_not_start_with_the_same_rois(self, dashboard, monkeypatch, tmp_path):
        from pymodaq.utils.gui_utils.loader_utils import create_extension
        monkeypatch.setattr(thermo_spectro, 'default_store_path', lambda: tmp_path / 'fresh2' / 'rois.json')
        shared_ui, fresh = create_extension(dashboard, thermo_spectro.ThermoSpectro)
        try:
            assert fresh.unit(1).settings.rois != fresh.unit(2).settings.rois
        finally:
            fresh.quit_fun()

    @pytest.mark.parametrize('actuators, number, expected', [
        (['Thermo1', 'Thermo2'], 1, 'Thermo1'), (['Thermo1', 'Thermo2'], 2, 'Thermo2'),
        (['Thermo2', 'Thermo1'], 1, 'Thermo1'),  # (by the number, not by the order)
        (['Heater', 'Thermostat_A', 'Thermostat_B'], 1, 'Thermostat_A'),
        (['Heater', 'Thermostat_A', 'Thermostat_B'], 2, 'Thermostat_B'),
        (['Motor', 'Temperature'], 1, 'Temperature'), (['Temperature'], 2, 'None'), ([], 1, 'None'),
        (['Motor1', 'Motor2'], 2, 'Motor2'),  # (no name looks like a thermostat: any actuator)
        (['Therm10', 'Therm1'], 1, 'Therm1')])
    def test_the_guess_of_the_thermostat(self, actuators, number, expected):
        assert thermo_spectro.guess_thermostat(actuators, number) == expected


def test_the_help_of_the_extension(ext):
    assert ext.get_help_markdown().startswith('# ThermoSpectro')


class TestIntegrationTime:
    @pytest.fixture
    def calls(self, ext, monkeypatch):
        calls = []

        def fake(module, milliseconds):
            calls.append((module.title, milliseconds))
            return milliseconds

        monkeypatch.setattr(unit_module, 'set_integration_time', fake)
        return calls

    def test_500_ms_by_default_and_a_readout_every_second(self, ext, calls):
        for unit in ext.units[:2]:
            unit.settings.child('integration_time').setValue(500.)
            unit.settings.child('readout_time').setValue(1.)  # (the defaults, which the fixture made quicker)

        ext.start_monitoring()

        assert ext.monitoring
        assert sorted(calls) == [('Spectro1', 500.), ('Spectro2', 500.)]

    def test_each_spectrometer_gets_its_own_integration_time(self, ext, calls):
        ext.unit(1).settings.child('integration_time').setValue(60.)
        ext.unit(2).settings.child('integration_time').setValue(30.)

        ext.start_monitoring()

        assert sorted(calls) == [('Spectro1', 60.), ('Spectro2', 30.)]

    def test_it_is_also_set_when_an_experiment_starts(self, ext, calls, qtbot):
        run(qtbot, ext, **SLOW)

        assert len(calls) == 2  # (once: the experiment starts from the live view it makes)
        end_run(qtbot, ext)

    def test_a_change_during_the_live_view_is_applied_to_that_unit_only(self, ext, calls):
        ext.start_monitoring()
        calls.clear()

        ext.unit(2).settings.child('integration_time').setValue(40.)

        assert calls == [('Spectro2', 40.)]

    def test_it_can_be_changed_during_an_experiment_and_the_readout_time_cannot(self, ext, qtbot):
        run(qtbot, ext, **SLOW)

        assert all(ui_enabled(unit.settings.child('integration_time')) for unit in ext.units[:2])  # (it is saved)
        assert not any(ui_enabled(unit.settings.child('readout_time')) for unit in ext.units[:2])
        end_run(qtbot, ext)

    def test_a_change_during_an_experiment_is_set_before_the_next_readout(self, ext, calls, qtbot):
        run(qtbot, ext, **SLOW)
        calls.clear()

        ext.unit(2).settings.child('integration_time').setValue(40.)

        assert calls == []  # (not at once: a readout may be going on)
        wait_for(qtbot, lambda: calls == [('Spectro2', 40.)])
        end_run(qtbot, ext)

    def test_a_spectrometer_without_integration_time_is_not_an_error(self, ext, qtbot):
        ext.start_monitoring()  # (the Mock has none)

        wait_for(qtbot, lambda: all(unit.n_measurements > 0 for unit in ext.units[:2]))


class TestLiveView:
    def test_spectra_temperatures_and_rois_are_shown(self, ext, qtbot):
        ext.start_monitoring()

        wait_for(qtbot, lambda: all(unit.n_measurements >= 3 for unit in ext.units[:2]))
        qtbot.wait(400)  # (the display is refreshed four times a second)

        assert ext.monitoring and all(unit.state == UnitState.LIVE for unit in ext.units[:2])
        for view in ext.views[:2]:
            assert view.spectrum_plot.x_range is not None
            assert len(view.curves_plot.curves) == 6
            assert len(view.curves_plot.curves[0].getData()[0]) >= 3
            assert len(view.curves_plot.secondary_curves) == 2  # the temperature and its set-point
        assert all(unit.temperature is not None for unit in ext.units[:2])

    def test_the_modules_are_left_as_they_were(self, ext, qtbot):
        detectors = [ext.modules_manager.get_mod_from_name(f'Spectro{n}', mod=ModuleType.Detector) for n in (1, 2)]
        ext.start_monitoring()
        wait_for(qtbot, lambda: all(unit.n_measurements > 0 for unit in ext.units[:2]))

        ext.stop_monitoring()

        assert all(not detector.grab_state for detector in detectors)  # (they are read out one at a time)
        assert all(unit.state == UnitState.IDLE for unit in ext.units[:2])

    def test_a_grab_started_by_the_user_is_replaced_by_the_readouts(self, ext, qtbot):
        detector = ext.modules_manager.get_mod_from_name('Spectro1', mod=ModuleType.Detector)
        detector.manage_ui_actions('grab', 'setChecked', True)
        detector.grab_data(True)
        wait_for(qtbot, lambda: detector.grab_state)

        ext.start_monitoring()

        wait_for(qtbot, lambda: ext.unit(1).n_measurements >= 3)
        assert not detector.grab_state

    def test_the_two_units_have_their_own_readout_time(self, ext, qtbot):
        ext.unit(2).settings.child('readout_time').setValue(0.5)

        ext.start_monitoring()
        qtbot.wait(2500)

        first, second = ext.unit(1).n_measurements, ext.unit(2).n_measurements
        assert second >= 3
        assert first > 2 * second

    def test_a_roi_is_the_mean_of_its_window(self, ext, qtbot):
        measurements = []
        ext.unit(1).measurement.connect(measurements.append)
        ext.start_monitoring()
        wait_for(qtbot, lambda: len(measurements) >= 2)

        measurement = measurements[1]

        assert len(measurement.values) == 6
        for roi, value in zip(measurement.rois, measurement.values):
            mask = window_mask(measurement.spectrum.x, roi.start, roi.stop)
            assert mask.any()
            assert value == pytest.approx(measurement.spectrum.y[mask].mean())

    def test_the_rois_follow_the_edits_of_the_user(self, ext, qtbot):
        measurements = []
        ext.unit(1).measurement.connect(measurements.append)
        ext.start_monitoring()
        wait_for(qtbot, lambda: len(measurements) >= 2)

        ext.unit(1).settings.child('rois', 'roi00', 'start').setValue(100.)
        ext.unit(1).settings.child('rois', 'roi00', 'stop').setValue(120.)
        n = len(measurements)
        wait_for(qtbot, lambda: len(measurements) >= n + 2)

        assert (measurements[-1].rois[0].start, measurements[-1].rois[0].stop) == (100., 120.)
        assert len(ext.unit(1).series_rois) <= len(measurements) - n + 2  # (the history starts again)

    def test_nothing_starts_if_a_module_is_missing(self, ext):
        ext.unit(1).settings.child('spectrometer').setValue('None')

        assert not ext.start_monitoring()

        assert not ext.monitoring
        assert 'no spectrometer is selected' in ext.notifications[-1][1]
        assert not ext.is_action_checked('monitor')

    def test_an_unused_unit_is_left_alone(self, ext, qtbot):
        ext.unit(2).settings.child('enabled').setValue(False)
        detector = ext.modules_manager.get_mod_from_name('Spectro2', mod=ModuleType.Detector)

        ext.start_monitoring()
        wait_for(qtbot, lambda: ext.unit(1).n_measurements >= 3)

        assert ext.unit(2).state == UnitState.IDLE and ext.unit(2).n_measurements == 0
        assert not detector.grab_state

    def test_no_unit_no_live_view(self, ext):
        for unit in ext.units[:2]:
            unit.settings.child('enabled').setValue(False)

        assert not ext.start_monitoring()

        assert 'No spectrometer is used' in ext.notifications[-1][1]

    def test_two_units_cannot_use_the_same_spectrometer(self, ext):
        ext.unit(2).settings.child('spectrometer').setValue('Spectro1')

        assert not ext.start_monitoring()

        assert 'are the same module' in ext.notifications[-1][1]

    def test_two_units_can_share_a_thermostat_if_only_one_drives_it(self, ext, qtbot):
        ext.unit(2).settings.child('thermostat').setValue('Thermo1')
        assert not ext.start_monitoring()
        assert 'would both drive the thermostat Thermo1' in ext.notifications[-1][1]

        ext.unit(2).settings.child('ramp', 'run').setValue(False)

        assert ext.start_monitoring()
        wait_for(qtbot, lambda: all(unit.n_measurements > 0 for unit in ext.units[:2]))

    def test_the_units_of_the_axis_are_used_for_the_wavelengths(self, ext, qtbot):
        ext.start_monitoring()
        wait_for(qtbot, lambda: all(unit.spectrum is not None for unit in ext.units[:2]))

        assert [unit.axis_units for unit in ext.units[:2]] == ['nm', 'nm']

    def test_the_live_view_says_once_when_a_module_gives_nothing(self, ext, qtbot):
        unit = ext.unit(1)
        ext.start_monitoring()
        wait_for(qtbot, lambda: unit.spectrum is not None)
        unit._disconnect(unit.detector)  # (the spectra come back to nobody)
        unit.spectrum = None  # (as if nothing had come)
        messages = []
        ext.status_signal.connect(messages.append)
        unit._started_at -= 20.

        qtbot.wait(600)

        assert [m for m in messages if 'no 1D data' in m] == [f'{unit.title} has delivered no 1D data (a spectrum)']

    def test_the_edges_of_the_rois_are_in_the_units_of_the_spectrum(self, ext, qtbot):
        ext.start_monitoring()
        wait_for(qtbot, lambda: all(unit.spectrum is not None for unit in ext.units[:2]))
        assert ext.unit(1).settings.child('rois', 'roi00', 'start').opts['suffix'] == 'nm'

        ext.unit(1).axis_units_changed.emit('')  # (a spectrometer giving pixels)

        assert ext.unit(1).settings.child('rois', 'roi00', 'start').opts['suffix'] == ''
        assert ext.unit(2).settings.child('rois', 'roi00', 'start').opts['suffix'] == 'nm'
        key = ext.add_roi(1)
        assert ext.unit(1).settings.child('rois', key, 'start').opts['suffix'] == ''

    def test_the_live_view_can_be_started_from_the_toolbar(self, ext, qtbot):
        ext.get_action('monitor').trigger()

        assert ext.monitoring and ext.is_action_checked('monitor')
        ext.get_action('monitor').trigger()
        assert not ext.monitoring and not ext.is_action_checked('monitor')


class TestRois:
    def test_each_roi_is_a_numbered_window_on_the_spectrum(self, ext, qtbot):
        ext.start_monitoring()
        wait_for(qtbot, lambda: all(view.spectrum_plot.x_range for view in ext.views[:2]))

        for view in ext.views[:2]:
            assert view.spectrum_plot.roi_keys == [f'roi{i:02d}' for i in range(6)]
            assert view.spectrum_plot.roi('roi02') == pytest.approx((150., 160.))
            assert view.spectrum_plot._labels['roi02'].textItem.toPlainText() == '3'

    def test_the_window_follows_the_settings(self, ext):
        ext.unit(1).settings.child('rois', 'roi01', 'start').setValue(120.)
        ext.unit(1).settings.child('rois', 'roi01', 'stop').setValue(135.)

        assert ext.view(1).spectrum_plot.roi('roi01') == pytest.approx((120., 135.))
        assert ext.view(2).spectrum_plot.roi('roi01') == pytest.approx((100., 110.))  # (the other unit)

    def test_dragging_a_window_changes_the_settings(self, ext):
        region = ext.view(1).spectrum_plot._regions['roi00']

        region.setRegion((60., 80.))
        region.sigRegionChangeFinished.emit(region)

        roi = ext.unit(1).settings.roi('roi00')
        assert (roi.start, roi.stop) == pytest.approx((60., 80.))
        assert ext.unit(2).settings.roi('roi00').start == 50.

    def test_resizing_a_window_by_one_edge(self, ext):
        region = ext.view(2).spectrum_plot._regions['roi03']
        low, high = region.getRegion()

        region.setRegion((low, high + 25.))
        region.sigRegionChangeFinished.emit(region)

        roi = ext.unit(2).settings.roi('roi03')
        assert (roi.start, roi.stop) == pytest.approx((200., 235.))

    def test_a_double_click_on_a_spectrum_adds_a_roi_there(self, ext):
        ext.view(2).spectrum_plot.add_requested.emit(333.)

        assert len(ext.unit(2).settings.roi_keys) == 7 and len(ext.unit(1).settings.roi_keys) == 6
        roi = ext.unit(2).settings.rois[-1]
        assert roi.center == pytest.approx(333.) and roi.width == pytest.approx(10.)
        assert ext.view(2).spectrum_plot.roi_keys[-1] == 'roi06'
        assert len(ext.view(2).curves_plot.curves) == 7

    def test_the_add_button(self, ext):
        ext.unit(1).settings.child('rois', 'add_roi').activate()

        assert len(ext.unit(1).settings.roi_keys) == 7

    def test_the_buttons_still_work_after_settings_are_loaded(self, ext, tmp_path):
        from pymodaq_gui.parameter import ioxml
        file = tmp_path / 'settings.xml'
        ioxml.parameter_to_xml_file(ext.settings, str(file))

        ext.set_settings(file)
        ext.unit(1).settings.child('rois', 'add_roi').activate()
        ext.unit(2).settings.child('rois', 'reset_rois').activate()

        assert len(ext.unit(1).settings.roi_keys) == 7
        assert ext.unit(2).settings.rois == default_rois(2)
        assert ext.view(1).spectrum_plot.roi_keys[-1] == 'roi06'

    def test_the_rois_come_back_with_the_saved_settings(self, ext, tmp_path):
        from pymodaq_gui.parameter import ioxml
        ext.unit(1).settings.child('rois', 'roi00', 'name').setValue('peak')
        ext.unit(1).settings.child('rois', 'roi05').remove()
        file = tmp_path / 'settings.xml'
        ioxml.parameter_to_xml_file(ext.settings, str(file))
        ext.unit(1).settings.child('rois', 'roi00', 'name').setValue('changed')

        ext.set_settings(file)

        assert ext.unit(1).settings.roi('roi00').name == 'peak'
        assert len(ext.unit(1).settings.roi_keys) == 5
        assert ext.view(1).spectrum_plot.roi_keys == ext.unit(1).settings.roi_keys

    def test_removing_a_roi_removes_its_window_and_its_curve(self, ext):
        ext.unit(1).settings.child('rois', 'roi02').remove()

        assert ext.view(1).spectrum_plot.roi_keys == ['roi00', 'roi01', 'roi03', 'roi04', 'roi05']
        assert len(ext.view(1).curves_plot.curves) == 5
        assert ext.view(1).toggles.names[:5] == ['roi00', 'roi01', 'roi03', 'roi04', 'roi05']
        assert len(ext.unit(2).settings.roi_keys) == 6  # (the other unit is not touched)
        assert len(ext.view(2).spectrum_plot.roi_keys) == 6

    def test_removing_the_roi_of_the_second_unit(self, ext):
        ext.unit(2).settings.child('rois', 'roi00').remove()

        assert len(ext.unit(2).settings.roi_keys) == 5 and len(ext.unit(1).settings.roi_keys) == 6
        assert 'roi00' not in ext.view(2).spectrum_plot.roi_keys and 'roi00' in ext.view(1).spectrum_plot.roi_keys

    def test_the_default_rois_come_back(self, ext):
        ext.unit(1).settings.child('rois', 'reset_rois').activate()

        assert ext.unit(1).settings.rois == default_rois(1)
        assert ext.view(1).spectrum_plot.roi_keys == [f'roi{i:02d}' for i in range(6)]
        assert ext.unit(2).settings.rois[0].start == 50.  # (not the other unit)

    def test_a_roi_can_be_renamed(self, ext):
        ext.unit(1).settings.child('rois', 'roi00', 'name').setValue('peak')

        assert ext.unit(1).settings.child('rois', 'roi00').title() == 'peak'
        assert ext.view(1).toggles.texts[0] == '1  peak'
        assert ext.view(1).curves_plot.curves[0].name() == 'peak'

    def test_the_edits_reset_the_history_of_the_curves(self, ext, qtbot):
        ext.start_monitoring()
        wait_for(qtbot, lambda: ext.unit(1).n_measurements >= 4)

        ext.unit(1).settings.child('rois', 'roi00', 'stop').setValue(70.)

        assert len(ext.unit(1).series_rois) == 0
        assert len(ext.unit(2).series_rois) > 0


class TestRoisAreRemembered:
    """The ROIs are edited, and saved to be reloaded exactly the same for the next experiments"""

    def store(self, tmp_path) -> RoiStore:
        return RoiStore(tmp_path / 'remembered' / 'rois.json')

    def test_the_default_rois_are_remembered_from_the_start(self, ext, tmp_path):
        assert self.store(tmp_path).get(1) is not None  # (written when the extension started)

    def test_a_change_is_remembered_a_moment_later(self, ext, qtbot, tmp_path):
        ext.unit(1).settings.child('rois', 'roi00', 'start').setValue(12.5)

        wait_for(qtbot, lambda: self.store(tmp_path).get(1)[0].start == 12.5)

        assert self.store(tmp_path).get(2) != self.store(tmp_path).get(1)

    def test_the_rois_come_back_exactly_the_same_next_time(self, ext, dashboard, qtbot, tmp_path):
        from pymodaq.utils.gui_utils.loader_utils import create_extension
        settings = ext.unit(1).settings
        settings.child('rois', 'roi00', 'name').setValue('peak')
        settings.child('rois', 'roi00', 'start').setValue(12.3456789)
        settings.child('rois', 'roi00', 'stop').setValue(67.891)
        settings.child('rois', 'roi00', 'record').setValue(False)
        settings.child('rois', 'roi01', 'plot').setValue(False)
        settings.child('rois', 'roi04').remove()
        ext.view(2).spectrum_plot.add_requested.emit(333.3)
        ext.save_rois()
        expected = [ext.unit(1).settings.rois, ext.unit(2).settings.rois]

        shared_ui, fresh = create_extension(dashboard, thermo_spectro.ThermoSpectro)
        try:
            assert fresh.unit(1).settings.rois == expected[0]
            assert fresh.unit(2).settings.rois == expected[1]
            assert fresh.unit(1).settings.roi('roi00') == Roi('peak', 12.3456789, 67.891, record=False, plot=True)
            assert len(fresh.view(1).spectrum_plot.roi_keys) == len(ext.view(1).spectrum_plot.roi_keys) == 5
        finally:
            fresh.quit_fun()

    def test_closing_the_extension_remembers_what_was_just_changed(self, ext, tmp_path):
        ext.unit(2).settings.child('rois', 'roi03', 'stop').setValue(222.)

        assert ext._quit_fun()

        assert self.store(tmp_path).get(2)[3].stop == 222.

    def test_the_default_rois_can_be_restored_and_are_remembered(self, ext, qtbot, tmp_path):
        ext.unit(1).settings.child('rois', 'roi00', 'start').setValue(12.5)
        ext.save_rois()

        ext.unit(1).settings.child('rois', 'reset_rois').activate()
        ext.save_rois()

        assert self.store(tmp_path).get(1) == default_rois(1)

    def test_ten_edits_in_a_row_are_written_once(self, ext, qtbot, tmp_path, monkeypatch):
        ext.save_rois()  # (what the setting up of the test changed)
        writes = []
        original = ext._store.save
        monkeypatch.setattr(ext._store, 'save', lambda number, rois: (writes.append(number), original(number, rois)))

        for value in range(10):
            ext.unit(1).settings.child('rois', 'roi00', 'start').setValue(float(value))
        wait_for(qtbot, lambda: bool(writes))
        qtbot.wait(800)

        assert writes == [1]

    @pytest.mark.errors_expected
    def test_a_file_which_cannot_be_written_does_not_stop_anything(self, ext, qtbot, tmp_path, monkeypatch):
        def failing(number, rois):
            raise OSError('read-only')

        monkeypatch.setattr(ext._store, 'save', failing)
        ext.unit(1).settings.child('rois', 'roi00', 'start').setValue(12.5)

        ext.save_rois()

        assert ext.unit(1).settings.roi('roi00').start == 12.5  # (the ROI is there, it is just not remembered)
        assert not ext._roi_dirty

    def test_the_rois_cannot_be_edited_during_an_experiment(self, ext, qtbot):
        run(qtbot, ext, **SLOW)
        keys = ext.unit(1).settings.roi_keys
        starts = [ext.unit(1).settings.roi(key).start for key in keys]

        region = ext.view(1).spectrum_plot._regions['roi00']
        assert not region.movable
        region.setRegion((300., 310.))
        region.sigRegionChangeFinished.emit(region)
        ext.view(1).spectrum_plot.add_requested.emit(333.)
        ext.unit(1).settings.child('rois', 'add_roi').activate()
        ext.unit(1).settings.child('rois', 'reset_rois').activate()

        assert ext.unit(1).settings.roi_keys == keys
        assert [ext.unit(1).settings.roi(key).start for key in keys] == starts
        for name in ('start', 'stop', 'name', 'record'):
            assert not ui_enabled(ext.unit(1).settings.child('rois', 'roi00', name))
        assert ui_enabled(ext.unit(1).settings.child('rois', 'roi00', 'plot'))  # what is plotted can change
        with pytest.raises(RuntimeError, match='cannot be changed'):
            ext.add_roi(1)
        end_run(qtbot, ext)
        assert ui_enabled(ext.unit(1).settings.child('rois', 'roi00', 'start'))
        assert ext.view(1).spectrum_plot._regions['roi00'].movable


class TestRoiManager:
    """The ROI manager of each unit: a table where the ROIs are set up, moved, added and deleted"""

    def manager(self, ext, number=1):
        return ext.view(number).manager

    def test_each_unit_has_its_manager_with_its_rois(self, ext):
        for number in (1, 2):
            manager = self.manager(ext, number)
            assert manager.keys == ext.unit(number).settings.roi_keys == [f'roi{i:02d}' for i in range(6)]
            for key in manager.keys:
                roi, row = ext.unit(number).settings.roi(key), manager.row(key)
                assert (row.name.text(), row.start.value(), row.stop.value()) == (roi.name, roi.start, roi.stop)
                assert (row.record.isChecked(), row.plot.isChecked()) == (roi.record, roi.plot)

    def test_the_numbers_and_the_colors_are_those_of_the_windows_on_the_spectrum(self, ext):
        from pymodaq_plugins_thermospectro.widgets import wavelength_color
        row, roi = self.manager(ext).row('roi02'), ext.unit(1).settings.roi('roi02')

        assert row.badge.text() == '3' == ext.view(1).spectrum_plot._labels['roi02'].textItem.toPlainText()
        color = ext.view(1).spectrum_plot.region_color('roi02')  # (the color of the wavelength in the middle of the window)
        assert color == wavelength_color((roi.start + roi.stop) / 2.) and color in row.badge.styleSheet()

    def test_typing_the_edges_moves_the_window(self, ext):
        row = self.manager(ext).row('roi01')

        row.start.setValue(120.)
        row.stop.setValue(135.)

        roi = ext.unit(1).settings.roi('roi01')
        assert (roi.start, roi.stop) == (120., 135.)
        assert ext.view(1).spectrum_plot.roi('roi01') == pytest.approx((120., 135.))
        assert ext.unit(2).settings.roi('roi01').start == 100.  # (not the other unit)
        assert self.manager(ext, 2).row('roi01').start.value() == 100.

    def test_the_unit_two_has_its_own_manager(self, ext):
        self.manager(ext, 2).row('roi03').stop.setValue(250.)

        assert ext.unit(2).settings.roi('roi03').stop == 250.
        assert ext.unit(1).settings.roi('roi03').stop == 210.

    def test_an_edge_typed_past_the_other_one_moves_the_window(self, ext):
        self.manager(ext).row('roi00').start.setValue(75.)  # (the window is 50-60)

        roi = ext.unit(1).settings.roi('roi00')
        assert (roi.start, roi.stop) == (75., 85.)
        assert self.manager(ext).row('roi00').stop.value() == 85.

    def test_the_other_edge_is_kept_as_it_is_in_the_settings(self, ext):
        ext.unit(1).settings.child('rois', 'roi00', 'start').setValue(50.123456)

        self.manager(ext).row('roi00').stop.setValue(61.)

        assert ext.unit(1).settings.roi('roi00').start == 50.123456

    def test_dragging_a_window_moves_the_numbers_as_it_goes(self, ext):
        region = ext.view(1).spectrum_plot._regions['roi00']
        row = self.manager(ext).row('roi00')

        region.lines[0].setValue(60.)  # (what the mouse does all along a drag: the region is not finished)
        region.lines[1].setValue(80.)

        assert (row.start.value(), row.stop.value()) == (60., 80.)
        assert ext.unit(1).settings.roi('roi00').start == 50.  # (the settings follow when the mouse is released)
        region.sigRegionChangeFinished.emit(region)
        assert (ext.unit(1).settings.roi('roi00').start, ext.unit(1).settings.roi('roi00').stop) == (60., 80.)
        assert (row.start.value(), row.stop.value()) == (60., 80.)

    def test_what_is_changed_in_the_settings_shows_in_the_manager(self, ext):
        settings = ext.unit(1).settings
        row = self.manager(ext).row('roi02')

        settings.child('rois', 'roi02', 'name').setValue('peak')
        settings.child('rois', 'roi02', 'start').setValue(151.)
        settings.child('rois', 'roi02', 'record').setValue(False)
        settings.child('rois', 'roi02', 'plot').setValue(False)

        assert (row.name.text(), row.start.value(), row.stop.value()) == ('peak', 151., 160.)
        assert not row.record.isChecked() and not row.plot.isChecked()

    def test_renaming_a_roi_in_the_manager(self, ext):
        row = self.manager(ext).row('roi00')

        row.name.setText('peak')
        row.name.editingFinished.emit()

        assert ext.unit(1).settings.roi('roi00').name == 'peak'
        assert ext.unit(1).settings.child('rois', 'roi00').title() == 'peak'
        assert ext.view(1).toggles.texts[0] == '1  peak'
        assert ext.view(1).curves_plot.curves[0].name() == 'peak'

    def test_the_boxes_record_and_plot_a_roi(self, ext):
        manager = self.manager(ext)

        manager.row('roi01').record.setChecked(False)
        manager.row('roi01').plot.setChecked(False)

        assert not ext.unit(1).settings.roi('roi01').record and not ext.unit(1).settings.roi('roi01').plot
        assert not ext.view(1).toggles.button('roi01').isChecked()
        assert len(ext.view(1).curves_plot.curves) == 5
        assert ext.unit(2).settings.roi('roi01').record  # (not the other unit)

    def test_the_plot_buttons_and_the_boxes_go_together(self, ext):
        ext.view(1).toggles.button('roi03').click()

        assert not self.manager(ext).row('roi03').plot.isChecked()
        ext.view(1).toggles.button('roi03').click()
        assert self.manager(ext).row('roi03').plot.isChecked()

    def test_adding_a_roi(self, ext, qtbot):
        manager = self.manager(ext)

        manager.add_button.click()

        assert len(ext.unit(1).settings.roi_keys) == 7 and len(ext.unit(2).settings.roi_keys) == 6
        assert manager.keys == ext.unit(1).settings.roi_keys and manager.row('roi06').badge.text() == '7'
        assert ext.view(1).spectrum_plot.roi_keys[-1] == 'roi06' and len(ext.view(1).curves_plot.curves) == 7
        assert manager.row('roi06').name.selectedText() == 'ROI 7'  # (ready to be named)
        assert len(self.manager(ext, 2).keys) == 6

    def test_the_new_roi_is_in_the_middle_of_the_spectrum(self, ext, qtbot):
        ext.start_monitoring()
        wait_for(qtbot, lambda: all(view.spectrum_plot.x_range for view in ext.views[:2]))
        low, high = ext.view(1).spectrum_plot.x_range

        self.manager(ext).add_button.click()

        assert ext.unit(1).settings.rois[-1].center == pytest.approx((low + high) / 2., abs=1.)

    def test_deleting_a_roi(self, ext):
        manager = self.manager(ext)

        manager.row('roi02').delete.click()

        assert ext.unit(1).settings.roi_keys == ['roi00', 'roi01', 'roi03', 'roi04', 'roi05']
        assert manager.keys == ext.unit(1).settings.roi_keys
        assert ext.view(1).spectrum_plot.roi_keys == manager.keys and len(ext.view(1).curves_plot.curves) == 5
        assert ext.view(1).toggles.names[:5] == manager.keys
        assert manager.row('roi03').badge.text() == '3'  # (the numbers follow)
        assert len(ext.unit(2).settings.roi_keys) == 6 and len(self.manager(ext, 2).keys) == 6

    def test_a_roi_can_be_deleted_from_the_menu_of_its_window(self, ext):
        ext.view(2).spectrum_plot.delete_requested.emit('roi00')

        assert 'roi00' not in ext.unit(2).settings.roi_keys and 'roi00' not in self.manager(ext, 2).keys
        assert 'roi00' in ext.unit(1).settings.roi_keys

    def test_a_roi_can_be_added_from_the_menu_of_the_spectrum(self, ext):
        ext.view(1).spectrum_plot.add_requested.emit(333.)

        assert len(self.manager(ext).keys) == 7 and self.manager(ext).row('roi06').start.value() == 328.

    def test_every_roi_can_be_deleted_and_new_ones_added(self, ext):
        manager = self.manager(ext)
        for key in list(manager.keys):
            manager.row(key).delete.click()

        assert ext.unit(1).settings.roi_keys == [] and manager.keys == [] and ext.view(1).spectrum_plot.roi_keys == []
        manager.add_button.click()
        assert len(manager.keys) == 1 and ext.unit(1).settings.roi('roi00').name == 'ROI 1'

    def test_the_default_rois_come_back_with_the_reset_button(self, ext):
        manager = self.manager(ext)
        manager.row('roi00').delete.click()
        manager.row('roi01').start.setValue(300.)

        manager.reset_button.click()

        assert ext.unit(1).settings.rois == default_rois(1)
        assert manager.keys == [f'roi{i:02d}' for i in range(6)]
        assert manager.row('roi00').name.text() == 'ROI 1'
        assert ext.unit(2).settings.rois[0].start == 50.  # (not the other unit)

    def test_the_edges_are_in_the_units_of_the_spectrum(self, ext):
        assert self.manager(ext).header_text('start') == 'From (nm)'

        ext.unit(1).axis_units_changed.emit('')  # (a spectrometer giving pixels)

        assert self.manager(ext).header_text('start') == 'From'
        assert self.manager(ext, 2).header_text('start') == 'From (nm)'

    def test_a_window_out_of_the_spectrum_is_flagged(self, ext, qtbot):
        ext.start_monitoring()
        wait_for(qtbot, lambda: all(view.spectrum_plot.x_range for view in ext.views[:2]))
        high = ext.view(1).spectrum_plot.x_range[1]

        ext.unit(1).settings.child('rois', 'roi00', 'start').setValue(high + 10.)
        ext.unit(1).settings.child('rois', 'roi00', 'stop').setValue(high + 20.)

        row = self.manager(ext).row('roi00')
        assert 'outside' in row.start.toolTip() and row.start.styleSheet() != ''
        assert self.manager(ext).row('roi01').start.styleSheet() == ''

    def test_what_is_set_in_the_manager_is_remembered(self, ext, tmp_path):
        row = self.manager(ext).row('roi04')
        row.start.setValue(222.)
        row.name.setText('line')
        row.name.editingFinished.emit()
        self.manager(ext).row('roi05').delete.click()
        ext.save_rois()

        remembered = RoiStore(tmp_path / 'remembered' / 'rois.json').get(1)

        assert len(remembered) == 5 and remembered[4].name == 'line' and remembered[4].start == 222.

    def test_the_manager_cannot_change_the_rois_during_an_experiment(self, ext, qtbot):
        run(qtbot, ext, **SLOW)
        manager = self.manager(ext)
        keys, starts = ext.unit(1).settings.roi_keys, [r.start for r in ext.unit(1).settings.rois]

        assert not manager.add_button.isEnabled() and not manager.reset_button.isEnabled()
        row = manager.row('roi00')
        assert not any(w.isEnabled() for w in (row.name, row.start, row.stop, row.record, row.delete))
        assert row.plot.isEnabled()  # (what is plotted can change)
        # what would still get through if the widgets were not disabled
        ext._on_delete_requested(1, 'roi00')
        ext._on_roi_field_edited(1, 'roi01', 'name', 'other')
        ext._on_add_clicked(1)
        ext._on_reset_requested(1)
        ext._on_roi_edited(1, 'roi02', 10., 20.)
        assert ext.unit(1).settings.roi_keys == keys and [r.start for r in ext.unit(1).settings.rois] == starts
        assert ext.unit(1).settings.roi('roi01').name == 'ROI 2'
        row.plot.setChecked(False)
        assert not ext.unit(1).settings.roi('roi00').plot

        end_run(qtbot, ext)

        assert manager.add_button.isEnabled() and row.start.isEnabled() and row.delete.isEnabled()

    def test_there_is_one_manager_window_for_all_the_spectrometers(self, ext):
        container = ext.docks['settings'].container()

        assert ext.docks['roimanager'].container() is container  # (a tab next to the settings and the saving)
        assert ext.is_action_checked('show_roimanager') and ext.docks['roimanager'].title() == 'ROI manager'
        boxes = [ext.view(number).manager_box for number in (1, 2)]
        assert [box.title() for box in boxes] == ['Spectrometer 1: Spectro1', 'Spectrometer 2: Spectro2']
        assert not any(box.isHidden() for box in boxes)
        assert boxes[0].parentWidget() is boxes[1].parentWidget()  # (in the same window)

    def test_a_unit_which_is_not_used_has_no_part_in_the_manager(self, ext):
        ext.unit(2).settings.child('enabled').setValue(False)

        assert ext.view(2).manager_box.isHidden() and not ext.view(1).manager_box.isHidden()
        assert ext.dock_visible('roimanager')

        ext.unit(2).settings.child('enabled').setValue(True)

        assert not ext.view(2).manager_box.isHidden()

    def test_the_manager_can_be_closed_and_opened_again(self, ext, qtbot):
        ext.set_dock_visible('roimanager', False)
        assert not ext.dock_visible('roimanager') and not ext.is_action_checked('show_roimanager')

        ext.get_action('show_roimanager').trigger()
        qtbot.wait(50)

        container = ext.docks['settings'].container()
        assert ext.dock_visible('roimanager') and ext.docks['roimanager'].container() is container
        assert container.stack.indexOf(ext.docks['roimanager']) > container.stack.indexOf(ext.docks['saving'])
        assert ext.view(1).manager.keys == ext.unit(1).settings.roi_keys  # (the ROIs are still there)

    def test_the_settings_tree_does_not_show_the_rois_unless_asked(self, ext):
        assert not ext.unit(1).settings.child('rois').opts['expanded']

    def test_the_manager_follows_settings_loaded_from_a_file(self, ext, tmp_path):
        from pymodaq_gui.parameter import ioxml
        self.manager(ext).row('roi00').name.setText('peak')
        self.manager(ext).row('roi00').name.editingFinished.emit()
        self.manager(ext).row('roi05').delete.click()
        file = tmp_path / 'settings.xml'
        ioxml.parameter_to_xml_file(ext.settings, str(file))
        self.manager(ext).row('roi00').start.setValue(77.)
        self.manager(ext).add_button.click()
        self.manager(ext).add_button.click()

        ext.set_settings(file)

        manager = self.manager(ext)
        assert manager.keys == ext.unit(1).settings.roi_keys == [f'roi{i:02d}' for i in range(5)]
        assert manager.row('roi00').name.text() == 'peak' and manager.row('roi00').start.value() == 50.
        manager.add_button.click()  # (and it still works)
        assert len(manager.keys) == 6 and len(ext.unit(1).settings.roi_keys) == 6

    def test_the_rois_which_are_recorded_are_those_of_the_manager(self, ext, qtbot, tmp_path):
        manager = self.manager(ext)
        manager.row('roi00').start.setValue(40.)
        manager.row('roi00').stop.setValue(55.)
        manager.row('roi01').delete.click()
        manager.row('roi02').record.setChecked(False)
        manager.add_button.click()
        manager.row('roi06').start.setValue(30.)
        manager.row('roi06').stop.setValue(36.)

        run(qtbot, ext)
        wait_for_end(qtbot, ext)

        with H5Experiment(tmp_path) as saved:
            labels = [saved.label('Unit1_ROIs', index) for index in range(saved.n_zero_d('Unit1_ROIs'))]
            assert labels == ['ROI 1 mean (40-55 nm)', 'ROI 4 mean (200-210 nm)', 'ROI 5 mean (250-260 nm)',
                              'ROI 6 mean (300-310 nm)', 'ROI 7 mean (30-36 nm)']
            assert saved.n_zero_d('Unit2_ROIs') == 6  # (the other unit is not touched)

    def test_the_proportions_of_the_windows(self, ext, qtbot):
        ext.mainwindow.show()
        qtbot.waitExposed(ext.mainwindow)
        top = ext.dockarea.topContainer

        ext._set_layout()

        sizes = top.sizes()
        assert len(sizes) == 3 and not ext._layout_timer.isActive()
        assert [round(size / sum(sizes), 2) for size in sizes] == pytest.approx([0.38, 0.31, 0.31], abs=0.02)

    def test_the_proportions_are_not_set_when_no_unit_is_used(self, ext, qtbot):
        for unit in ext.units[:2]:
            unit.settings.child('enabled').setValue(False)
        ext.mainwindow.show()
        qtbot.waitExposed(ext.mainwindow)

        ext._set_layout()  # (only the tabs are left: there is nothing to share the window between)

        assert not ext._layout_timer.isActive()

    def test_the_old_buttons_of_the_settings_still_work(self, ext):
        ext.unit(1).settings.child('rois', 'add_roi').activate()
        ext.unit(1).settings.child('rois', 'roi00').remove()

        assert self.manager(ext).keys == ext.unit(1).settings.roi_keys and len(self.manager(ext).keys) == 6


class TestPlots:
    KEYS = ['settings', 'saving', 'roimanager', 'spectrum1', 'rois1', 'spectrum2', 'rois2']

    def test_every_window_is_open_with_an_entry_in_the_plots_menu(self, ext):
        for key in self.KEYS:
            assert ext.dock_visible(key)
            assert ext.is_action_checked(f'show_{key}')
        assert set(ext.get_menu('plots').actions()) >= {ext.get_action(f'show_{key}') for key in self.KEYS}

    @pytest.mark.parametrize('key', ['spectrum1', 'rois1', 'spectrum2', 'rois2', 'settings', 'saving', 'roimanager'])
    def test_a_window_can_be_closed_and_opened_again(self, ext, qtbot, key):
        ext.set_dock_visible(key, False)

        assert not ext.dock_visible(key) and not ext.is_action_checked(f'show_{key}')

        ext.set_dock_visible(key, True)
        qtbot.wait(50)

        assert ext.dock_visible(key) and ext.is_action_checked(f'show_{key}')

    @pytest.mark.parametrize('key', ['spectrum1', 'rois2', 'settings', 'saving', 'roimanager'])
    def test_a_reopened_window_has_its_title_bar_and_its_close_button(self, ext, qtbot, key):
        ext.mainwindow.show()
        qtbot.waitExposed(ext.mainwindow)
        assert ext.docks[key].label.isVisible()

        ext.set_dock_visible(key, False)
        ext.set_dock_visible(key, True)
        qtbot.wait(100)

        assert ext.docks[key].label.isVisible()
        assert ext.docks[key].label.isClosable()
        ext.docks[key].close()  # (it can be closed again)
        assert not ext.dock_visible(key)

    def test_the_menu_does_it_too(self, ext):
        ext.get_action('show_rois2').trigger()  # unchecks
        assert not ext.dock_visible('rois2')

        ext.get_action('show_rois2').trigger()

        assert ext.dock_visible('rois2')

    def test_the_close_button_of_a_window_is_followed_by_the_menu(self, ext):
        ext.docks['spectrum1'].close()  # (what its close button does)

        assert not ext.is_action_checked('show_spectrum1')
        assert not ext.dock_visible('spectrum1')
        ext.get_action('show_spectrum1').trigger()
        assert ext.dock_visible('spectrum1') and ext.is_action_checked('show_spectrum1')

    def test_all_the_windows_can_be_closed_and_opened_again(self, ext):
        for key in self.KEYS:
            ext.set_dock_visible(key, False)
        assert not any(ext.dock_visible(key) for key in self.KEYS)

        for key in self.KEYS:
            ext.set_dock_visible(key, True)

        assert all(ext.dock_visible(key) for key in self.KEYS)
        ext.set_dock_visible('rois1', False)
        ext.set_dock_visible('rois1', False)  # (twice: nothing happens)
        ext.set_dock_visible('rois1', True)
        ext.set_dock_visible('rois1', True)
        assert ext.dock_visible('rois1')

    def test_a_reopened_plot_is_where_it_was(self, ext):
        ext.set_dock_visible('rois1', False)
        ext.set_dock_visible('rois1', True)

        # below its spectrum, in the same column
        assert ext.docks['rois1'].container() is ext.docks['spectrum1'].container()

    def test_the_windows_of_the_units_come_back_in_their_order(self, ext, qtbot):
        ext.mainwindow.show()
        qtbot.waitExposed(ext.mainwindow)
        for key in self.KEYS:
            ext.set_dock_visible(key, False)
        for key in self.KEYS:
            ext.set_dock_visible(key, True)
        qtbot.wait(100)

        x = {key: ext.docks[key].mapTo(ext.mainwindow, ext.docks[key].rect().topLeft()).x() for key in self.KEYS}
        assert x['settings'] < x['spectrum1'] < x['spectrum2']
        assert x['settings'] < x['rois1'] < x['rois2']
        assert abs(x['spectrum1'] - x['rois1']) < 5 and abs(x['spectrum2'] - x['rois2']) < 5  # (one above the other)
        y = {key: ext.docks[key].mapTo(ext.mainwindow, ext.docks[key].rect().topLeft()).y() for key in self.KEYS}
        assert y['spectrum1'] < y['rois1'] and y['spectrum2'] < y['rois2']

    def test_a_reopened_plot_shows_what_was_measured_meanwhile(self, ext, qtbot):
        ext.start_monitoring()
        wait_for(qtbot, lambda: ext.unit(1).n_measurements >= 2)
        ext.set_dock_visible('rois1', False)
        n = ext.unit(1).n_measurements
        wait_for(qtbot, lambda: ext.unit(1).n_measurements >= n + 4)

        ext.set_dock_visible('rois1', True)
        qtbot.wait(500)

        assert len(ext.view(1).curves_plot.curves[0].getData()[0]) >= n + 4

    def test_the_plots_can_be_closed_during_an_experiment_which_goes_on(self, ext, qtbot, tmp_path):
        run(qtbot, ext, **SLOW)
        for key in ('spectrum1', 'rois1', 'spectrum2', 'rois2'):
            ext.set_dock_visible(key, False)
        qtbot.wait(800)

        assert ext.run_active
        for key in ('spectrum1', 'rois1', 'spectrum2', 'rois2'):
            ext.set_dock_visible(key, True)
        n = ext.unit(1).n_measurements
        wait_for(qtbot, lambda: ext.unit(1).n_measurements >= n + 3)
        end_run(qtbot, ext)
        with H5Experiment(tmp_path) as saved:  # (the recording did not notice)
            assert saved.time('Unit1_ROIs').size >= 10

    def test_a_unit_which_is_not_used_shows_no_plot(self, ext):
        ext.unit(2).settings.child('enabled').setValue(False)

        assert not ext.dock_visible('spectrum2') and not ext.dock_visible('rois2')
        assert ext.dock_visible('spectrum1') and ext.dock_visible('rois1')

        ext.unit(2).settings.child('enabled').setValue(True)

        assert ext.dock_visible('spectrum2') and ext.dock_visible('rois2')

    def test_the_plots_of_a_closed_unit_can_still_be_opened_by_hand(self, ext):
        ext.unit(2).settings.child('enabled').setValue(False)

        ext.set_dock_visible('rois2', True)

        assert ext.dock_visible('rois2')

    def test_a_closed_window_is_not_refreshed_for_nothing(self, ext, qtbot, monkeypatch):
        ext.set_dock_visible('spectrum1', False)
        ext.set_dock_visible('rois1', False)
        calls = []
        monkeypatch.setattr(ext.view(1), 'refresh', lambda span=0.: calls.append(span))

        qtbot.wait(700)

        assert calls == []


class TestPlotButtons:
    def test_there_is_a_button_per_roi_and_one_for_the_temperature(self, ext):
        toggles = ext.view(1).toggles

        assert toggles.names == [f'roi{i:02d}' for i in range(6)] + ['temperature']
        assert toggles.texts == [f'{i + 1}  ROI {i + 1}' for i in range(6)] + ['T']
        assert all(toggles.button(name).isChecked() for name in toggles.names)

    def test_there_is_no_temperature_button_without_thermostat(self, ext):
        ext.unit(1).settings.child('thermostat').setValue('None')

        assert ext.view(1).toggles.names[-1] == 'roi05'
        assert ext.view(2).toggles.names[-1] == 'temperature'
        assert not ext.view(1).curves_plot.secondary_curves

    def test_a_roi_can_be_hidden_with_its_button(self, ext):
        ext.view(1).toggles.button('roi02').click()

        assert not ext.unit(1).settings.roi('roi02').plot
        assert len(ext.view(1).curves_plot.curves) == 5
        assert ext.view(2).toggles.button('roi02').isChecked()  # (the other unit)
        assert len(ext.view(2).curves_plot.curves) == 6

    def test_or_with_its_setting(self, ext):
        ext.unit(2).settings.child('rois', 'roi04', 'plot').setValue(False)

        assert not ext.view(2).toggles.button('roi04').isChecked()
        assert len(ext.view(2).curves_plot.curves) == 5

    def test_a_hidden_roi_comes_back(self, ext):
        ext.view(1).toggles.button('roi02').click()
        ext.view(1).toggles.button('roi02').click()

        assert ext.unit(1).settings.roi('roi02').plot
        assert len(ext.view(1).curves_plot.curves) == 6

    def test_the_colors_do_not_change_when_a_roi_is_hidden(self, ext):
        before = [curve.opts['pen'].color().name() for curve in ext.view(1).curves_plot.curves]

        ext.view(1).toggles.button('roi01').click()

        after = [curve.opts['pen'].color().name() for curve in ext.view(1).curves_plot.curves]
        assert after == before[:1] + before[2:]

    def test_the_temperature_is_on_its_own_scale(self, ext, qtbot):
        ext.start_monitoring()
        wait_for(qtbot, lambda: ext.unit(1).n_measurements >= 4)
        qtbot.wait(500)
        plot = ext.view(1).curves_plot

        assert plot.has_secondary_scale
        names = [item[1].text for item in plot.legend.items]
        assert names[-2:] == ['temperature', 'set-point']
        assert plot.getPlotItem().getAxis('right').labelText == 'temperature'
        assert len(plot.secondary_curves[0].getData()[0]) >= 4

    def test_the_temperature_can_be_hidden_with_its_button_or_its_setting(self, ext):
        ext.view(1).toggles.button('temperature').click()

        assert not ext.unit(1).settings.plot_temperature
        assert not ext.view(1).curves_plot.secondary_curves
        assert not ext.view(1).curves_plot.has_secondary_scale
        assert ext.view(2).curves_plot.has_secondary_scale  # (the other unit)

        ext.unit(1).settings.child('plot_temperature').setValue(True)

        assert ext.view(1).toggles.button('temperature').isChecked()
        assert ext.view(1).curves_plot.has_secondary_scale

    def test_hiding_does_not_restart_the_plot_nor_stop_the_recording(self, ext, qtbot):
        ext.start_monitoring()
        wait_for(qtbot, lambda: ext.unit(1).n_measurements >= 3)
        n = len(ext.unit(1).series_rois)

        ext.view(1).toggles.button('roi00').click()

        assert len(ext.unit(1).series_rois) >= n  # (the history is still there: it is only not drawn)
        before = ext.unit(1).n_measurements
        wait_for(qtbot, lambda: ext.unit(1).n_measurements > before)

    def test_the_buttons_work_during_an_experiment(self, ext, qtbot):
        run(qtbot, ext, **SLOW)

        ext.view(1).toggles.button('roi00').click()
        ext.view(1).toggles.button('temperature').click()

        assert not ext.unit(1).settings.roi('roi00').plot
        assert not ext.unit(1).settings.plot_temperature
        assert ext.run_active
        end_run(qtbot, ext)

    def test_a_hidden_roi_is_recorded_all_the_same(self, ext, qtbot, tmp_path):
        ext.view(1).toggles.button('roi00').click()
        run(qtbot, ext)
        wait_for_end(qtbot, ext)

        with H5Experiment(tmp_path) as saved:
            assert saved.n_zero_d('Unit1_ROIs') == 6
            assert saved.zero_d('Unit1_ROIs', 0).size > 5


class TestRampSettings:
    def test_each_unit_has_its_own_ramp(self, ext):
        ext.unit(1).settings.child('ramp', 'final').setValue(40.)

        assert ext.unit(1).settings.child('ramp', 'final').value() == 40.
        assert ext.unit(2).settings.child('ramp', 'final').value() == 3.
        assert ext.unit(1).settings.program().final == 40. and ext.unit(2).settings.program().final == 3.

    def test_the_duration_is_computed_from_the_velocity(self, ext):
        ramp = ext.unit(1).settings.child('ramp')

        assert ramp['duration'] == pytest.approx(3. / 90.)  # minutes: 2 s
        assert ramp.child('duration').opts['readonly']

        ramp['velocity'] = 1.5

        assert ramp['duration'] == pytest.approx(2.)

    def test_the_other_way_to_define_it(self, ext):
        ramp = ext.unit(1).settings.child('ramp')

        ramp['mode'] = 'initial, final, duration'
        ramp['duration'] = 1.5

        assert ramp['velocity'] == pytest.approx(2.)
        assert ramp.child('velocity').opts['readonly'] and not ramp.child('duration').opts['readonly']

    def test_and_the_last_one(self, ext):
        ramp = ext.unit(1).settings.child('ramp')

        ramp['mode'] = 'initial, velocity, duration'
        ramp['velocity'] = 2.
        ramp['duration'] = 5.

        assert ramp['final'] == pytest.approx(10.)

    def test_a_way_back_in_a_given_time(self, ext):
        ramp = ext.unit(1).settings.child('ramp')

        ramp['back_mode'] = 'duration'
        ramp['back_duration'] = 0.5  # minutes

        assert ramp['back_velocity'] == pytest.approx(6.)  # 3 degrees in 30 s
        assert ext.unit(1).settings.program().duration_back == pytest.approx(30.)

    def test_the_current_temperature_can_become_the_initial_one(self, ext, qtbot):
        ext.start_monitoring()
        wait_for(qtbot, lambda: ext.unit(1).temperature is not None)

        ext.unit(1).settings.child('ramp', 'take_current').activate()

        assert ext.unit(1).settings.child('ramp', 'initial').value() == pytest.approx(
            round(ext.unit(1).temperature.value, 4))

    def test_the_current_temperature_is_not_known_before_the_live_view(self, ext):
        ext.unit(1).settings.child('ramp', 'take_current').activate()

        assert 'not known yet' in ext.notifications[-1][1]
        assert ext.unit(1).settings.child('ramp', 'initial').value() == 0.


UNIT_GROUPS = ('Spectrum', 'ROIs', 'Temperature', 'Program')


class TestRun:
    def test_the_ramps_are_followed_and_everything_is_saved(self, ext, qtbot, tmp_path):
        sent = spy_setpoints(ext)
        run(qtbot, ext)
        wait_for_end(qtbot, ext)

        for number in (1, 2):
            assert 2.4 < max(sent[number]) <= 3.0 + 1e-9  # (the top, or the last set-point before it)
            assert sent[number][-1] == 0.
        with H5Experiment(tmp_path) as saved:
            assert saved.titles == sorted(f'Unit{n}_{what}' for n in (1, 2) for what in UNIT_GROUPS)
            for number in (1, 2):
                n = saved.time(f'Unit{number}_Spectrum').size
                assert n >= 15
                assert saved.spectra(f'Unit{number}_Spectrum').shape[0] == n
                assert saved.n_zero_d(f'Unit{number}_ROIs') == 6
                assert all(saved.zero_d(f'Unit{number}_ROIs', index).size == n for index in range(6))
                assert saved.zero_d(f'Unit{number}_Temperature').size == n
                assert saved.zero_d(f'Unit{number}_Program', 0).size == n

    def test_the_arrays_of_a_unit_are_aligned_in_time(self, ext, qtbot, tmp_path):
        run(qtbot, ext)
        wait_for_end(qtbot, ext)

        with H5Experiment(tmp_path) as saved:
            for number in (1, 2):
                times = [saved.time(f'Unit{number}_{what}') for what in UNIT_GROUPS]
                assert all(np.array_equal(times[0], other) for other in times[1:])
                assert np.all(np.diff(times[0]) > 0)
                assert 0.08 < np.median(np.diff(times[0])) < 0.5  # one readout every 0.1 s
                assert abs(times[0][0] - time.time()) < 120  # epoch seconds

    def test_each_roi_saved_is_the_mean_of_the_saved_spectrum_in_its_window(self, ext, qtbot, tmp_path):
        run(qtbot, ext)
        wait_for_end(qtbot, ext)

        with H5Experiment(tmp_path) as saved:
            for number in (1, 2):
                wavelengths = saved.wavelengths(f'Unit{number}_Spectrum')
                spectra = saved.spectra(f'Unit{number}_Spectrum')
                for index, roi in enumerate(ext.unit(number).settings.rois):
                    mask = window_mask(wavelengths, roi.start, roi.stop)
                    assert mask.any()
                    assert saved.zero_d(f'Unit{number}_ROIs', index) == pytest.approx(spectra[:, mask].mean(axis=1))

    def test_the_labels_of_the_rois_say_where_they_are(self, ext, qtbot, tmp_path):
        run(qtbot, ext)
        wait_for_end(qtbot, ext)

        with H5Experiment(tmp_path) as saved:
            assert saved.label('Unit1_ROIs', 0) == 'ROI 1 mean (50-60 nm)'
            assert saved.label('Unit2_ROIs', 5) == 'ROI 6 mean (300-310 nm)'

    def test_the_program_is_saved_with_the_set_point_the_cycle_and_the_phase(self, ext, qtbot, tmp_path):
        run(qtbot, ext)
        wait_for_end(qtbot, ext)

        with H5Experiment(tmp_path) as saved:
            setpoint, cycle, phase = (saved.zero_d('Unit1_Program', index) for index in range(3))
            codes = json.loads(saved.attribute('Unit1_Program', 'phase_codes'))
        assert 2.4 < setpoint.max() <= 3.0 + 1e-9 and setpoint.min() >= 0.
        assert set(cycle) == {1.}
        assert {PHASE_CODES[Phase.RAMP], PHASE_CODES[Phase.BACK]} <= set(phase)
        assert codes['ramp'] == PHASE_CODES[Phase.RAMP] and codes['back'] == PHASE_CODES[Phase.BACK]

    def test_the_measured_temperature_is_saved(self, ext, qtbot, tmp_path):
        run(qtbot, ext)
        wait_for_end(qtbot, ext)

        with H5Experiment(tmp_path) as saved:
            for number in (1, 2):
                temperature = saved.zero_d(f'Unit{number}_Temperature')
                assert temperature.max() > 1.0  # the Mock thermostat follows the ramp
                assert abs(temperature[0]) < 1.0
                assert not np.isnan(temperature).any()

    def test_what_a_reader_needs_to_know_is_saved_with_the_groups(self, ext, qtbot, tmp_path):
        run(qtbot, ext)
        wait_for_end(qtbot, ext)

        with H5Experiment(tmp_path) as saved:
            assert saved.attribute('Unit1_ROIs', 'integration_time_ms') == 20.
            assert saved.attribute('Unit1_ROIs', 'readout_time_s') == 0.1
            assert saved.attribute('Unit2_Spectrum', 'spectrometer') == 'Spectro2'
            assert saved.attribute('Unit2_Temperature', 'thermostat') == 'Thermo2'
            rois = json.loads(saved.attribute('Unit1_ROIs', 'rois'))
            assert [roi['name'] for roi in rois] == [f'ROI {i}' for i in range(1, 7)]
            assert rois[2]['start'] == 150. and rois[2]['stop'] == 160.
            settings = saved.attribute('Unit1_ROIs', 'settings')  # (the settings of the unit, as XML)
            assert 'ROI 3' in str(settings) and 'readout_time' in str(settings)

    def test_each_experiment_has_its_own_file(self, ext, qtbot, tmp_path):
        run(qtbot, ext)
        wait_for_end(qtbot, ext)
        run(qtbot, ext)
        wait_for_end(qtbot, ext)

        files = data_files(tmp_path)
        assert len(files) == 2
        for file in files:
            with H5Experiment(tmp_path, file=file) as saved:
                assert 'Unit1_ROIs' in saved.titles and saved.time('Unit1_ROIs').size >= 10

    def test_the_experiment_ends_by_itself_when_the_ramps_are_over(self, ext, qtbot):
        run(qtbot, ext)

        wait_for_end(qtbot, ext)

        assert not ext.run_active and ext.notifications == []
        assert all(unit.state == UnitState.IDLE for unit in ext.units[:2])  # (the live view of the experiment is over)
        assert ext.get_action('start').isEnabled() and not ext.get_action('stop').isEnabled()
        assert ext.unit(1).settings.child('status', 'phase').value() == 'idle'

    def test_an_experiment_started_from_the_live_view_goes_back_to_it(self, ext, qtbot):
        ext.start_monitoring()
        wait_for(qtbot, lambda: all(unit.ready for unit in ext.units[:2]))

        run(qtbot, ext)
        wait_for_end(qtbot, ext)

        assert ext.monitoring and all(unit.state == UnitState.LIVE for unit in ext.units[:2])
        ext.stop_monitoring()

    def test_different_ramps_for_each_thermostat(self, ext, qtbot, tmp_path):
        sent = spy_setpoints(ext)
        set_ramp(ext, [ext.unit(2)], final=1.5, velocity=45., n_cycles=2)  # 2 s up and 2 s back, twice: 8 s
        run(qtbot, ext)  # unit 1: 3 degrees at 1.5 per second, once: 4 s

        wait_for_end(qtbot, ext, timeout=60000)

        assert 2.4 < max(sent[1]) <= 3.0 + 1e-9 and 1.2 < max(sent[2]) <= 1.5 + 1e-9
        rises = sum(1 for before, after in zip(sent[2], sent[2][1:]) if before < 0.75 <= after)
        assert rises == 2
        assert sum(1 for before, after in zip(sent[1], sent[1][1:]) if before < 1.5 <= after) == 1
        with H5Experiment(tmp_path) as saved:
            for number in (1, 2):  # the experiment lasts as long as the longest ramp: both are recorded till then
                times = saved.time(f'Unit{number}_ROIs')
                assert times[-1] - times[0] > 6.5

    def test_a_unit_without_a_ramp_is_only_recorded(self, ext, qtbot, tmp_path):
        sent = spy_setpoints(ext)
        ext.unit(2).settings.child('ramp', 'run').setValue(False)
        run(qtbot, ext)

        wait_for_end(qtbot, ext)

        assert sent[2] == [] and len(sent[1]) > 5
        with H5Experiment(tmp_path) as saved:
            assert 'Unit2_Program' not in saved.titles and 'Unit2_Temperature' in saved.titles
            assert saved.time('Unit2_ROIs').size >= 15

    def test_without_any_ramp_the_experiment_goes_on_until_it_is_stopped(self, ext, qtbot, tmp_path):
        sent = spy_setpoints(ext)
        for unit in ext.units[:2]:
            unit.settings.child('ramp', 'run').setValue(False)
        run(qtbot, ext)
        qtbot.wait(1500)

        assert ext.run_active
        ext.stop_run()  # (nothing to bring back)
        wait_for_end(qtbot, ext)

        assert sent == {1: [], 2: [], 3: []}
        with H5Experiment(tmp_path) as saved:
            assert saved.time('Unit1_ROIs').size >= 8
            assert 'Unit1_Program' not in saved.titles

    def test_one_unit_only(self, ext, qtbot, tmp_path):
        sent = spy_setpoints(ext)
        ext.unit(2).settings.child('enabled').setValue(False)
        run(qtbot, ext)

        wait_for_end(qtbot, ext)

        assert sent[2] == []
        with H5Experiment(tmp_path) as saved:
            assert saved.titles == sorted(f'Unit1_{what}' for what in UNIT_GROUPS)
        assert ext.unit(2).n_measurements == 0

    def test_the_status_is_shown_during_the_experiment(self, ext, qtbot):
        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: ext.unit(1).ramp_state.setpoint > 0.3)
        ext._on_ramp_tick()

        text = ext._status_message_label.text()
        assert 'Spectrometer 1: cycle 1/1 ramp' in text and 'Spectrometer 2: cycle 1/1 ramp' in text
        assert 'set-point' in text and 'measured' in text and '°C' in text
        assert ext.unit(2).settings.child('status', 'phase').value() == 'ramp'
        assert ext.settings['experiment', 'elapsed'] != ''
        end_run(qtbot, ext)

    def test_the_buttons_follow_the_experiment(self, ext, qtbot):
        assert ext.get_action('start').isEnabled()
        assert not ext.get_action('stop').isEnabled() and not ext.get_action('pause').isEnabled()

        run(qtbot, ext, **SLOW)

        assert not ext.get_action('start').isEnabled()
        assert ext.get_action('stop').isEnabled() and ext.get_action('pause').isEnabled()
        assert not ext.get_action('monitor').isEnabled()
        end_run(qtbot, ext)
        assert ext.get_action('start').isEnabled() and ext.get_action('monitor').isEnabled()
        assert not ext.get_action('stop').isEnabled() and not ext.get_action('pause').isEnabled()

    def test_what_cannot_start_is_said(self, ext):
        ext.unit(1).settings.child('ramp', 'final').setValue(0.)  # as the initial value

        ext.start_run()

        assert not ext.run_active
        title, text, error = ext.notifications[-1]
        assert title == 'Cannot start' and 'Spectrometer 1' in text and 'ramp is not valid' in text
        assert ext.get_action('start').isEnabled()

    def test_a_readout_time_shorter_than_the_integration_time_is_refused(self, ext):
        ext.unit(2).settings.child('integration_time').setValue(500.)
        ext.unit(2).settings.child('readout_time').setValue(0.2)

        ext.start_run()

        assert 'readout time (0.2 s) must be longer than the integration time (500 ms)' in ext.notifications[-1][1]
        assert not ext.run_active and not ext.monitoring

    def test_the_ramp_starts_from_the_temperature_the_thermostat_has(self, ext, qtbot):
        sent = spy_setpoints(ext)
        set_ramp(ext, initial=2.0, final=3.0, velocity=60.)  # 1 degree in a minute... in 1 s: 60 per minute
        run(qtbot, ext)
        wait_for(qtbot, lambda: len(sent[1]) > 3)

        assert ext.unit(1).ramp_state.cycle in (0, 1)
        assert sent[1][0] <= 0.5  # it went from where the thermostat was (0) up to the initial value first
        assert max(sent[1]) <= 3.0 + 1e-9
        end_run(qtbot, ext)


class TestWhatIsRecorded:
    """Each kind of data is recorded, or not, independently of the others"""

    def test_each_kind_of_data_can_be_left_out_independently(self, ext, qtbot, tmp_path):
        one, two = ext.units[:2]
        one.settings.child('record', 'spectrum').setValue(False)
        for key in ('roi01', 'roi03'):
            one.settings.child('rois', key, 'record').setValue(False)
        two.settings.child('record', 'temperature').setValue(False)
        run(qtbot, ext)

        wait_for_end(qtbot, ext)

        with H5Experiment(tmp_path) as saved:
            assert saved.titles == ['Unit1_Program', 'Unit1_ROIs', 'Unit1_Temperature', 'Unit2_ROIs', 'Unit2_Spectrum']
            assert saved.n_zero_d('Unit1_ROIs') == 4 and saved.n_zero_d('Unit2_ROIs') == 6
            assert [saved.label('Unit1_ROIs', index) for index in range(4)] == [
                'ROI 1 mean (50-60 nm)', 'ROI 3 mean (150-160 nm)', 'ROI 5 mean (250-260 nm)', 'ROI 6 mean (300-310 nm)']

    def test_only_the_temperature_of_a_unit(self, ext, qtbot, tmp_path):
        ext.unit(2).settings.child('enabled').setValue(False)
        one = ext.unit(1).settings
        one.child('record', 'spectrum').setValue(False)
        for key in one.roi_keys:
            one.child('rois', key, 'record').setValue(False)
        run(qtbot, ext)

        wait_for_end(qtbot, ext)

        with H5Experiment(tmp_path) as saved:
            assert saved.titles == ['Unit1_Program', 'Unit1_Temperature']

    def test_only_the_spectra(self, ext, qtbot, tmp_path):
        for unit in ext.units[:2]:
            unit.settings.child('record', 'temperature').setValue(False)
            for key in unit.settings.roi_keys:
                unit.settings.child('rois', key, 'record').setValue(False)
        run(qtbot, ext)

        wait_for_end(qtbot, ext)

        with H5Experiment(tmp_path) as saved:
            assert saved.titles == ['Unit1_Spectrum', 'Unit2_Spectrum']

    def test_the_thermostats_are_driven_whatever_is_recorded(self, ext, qtbot):
        sent = spy_setpoints(ext)
        for unit in ext.units[:2]:
            unit.settings.child('record', 'temperature').setValue(False)
        run(qtbot, ext)

        wait_for_end(qtbot, ext)

        assert 2.4 < max(sent[1]) <= 3.0 + 1e-9 and sent[1][-1] == 0.

    def test_when_nothing_is_recorded_there_is_no_file(self, ext, qtbot, tmp_path):
        for unit in ext.units[:2]:
            unit.settings.child('record', 'spectrum').setValue(False)
            unit.settings.child('record', 'temperature').setValue(False)
            for key in unit.settings.roi_keys:
                unit.settings.child('rois', key, 'record').setValue(False)
        messages = []
        ext.status_signal.connect(messages.append)

        run(qtbot, ext)
        wait_for_end(qtbot, ext)

        assert data_files(tmp_path) == []
        assert any('Nothing is recorded' in message for message in messages)

    def test_nothing_is_saved_when_logging_is_off(self, ext, qtbot, tmp_path):
        ext.set_action_checked('log', False)

        run(qtbot, ext)
        wait_for_end(qtbot, ext)

        assert data_files(tmp_path) == []

    def test_the_log_button_is_locked_during_an_experiment(self, ext, qtbot):
        run(qtbot, ext, **SLOW)

        assert not ext.get_action('log').isEnabled()
        end_run(qtbot, ext)
        assert ext.get_action('log').isEnabled()

    def test_the_record_boxes_cannot_be_changed_during_an_experiment(self, ext, qtbot):
        run(qtbot, ext, **SLOW)

        for unit in ext.units[:2]:
            assert not ui_enabled(unit.settings.child('record', 'spectrum'))
            assert not ui_enabled(unit.settings.child('record', 'temperature'))
            assert not ui_enabled(unit.settings.child('rois', 'roi00', 'record'))
        end_run(qtbot, ext)
        assert ui_enabled(ext.unit(1).settings.child('record', 'spectrum'))


class TestSwmr:
    READER = textwrap.dedent('''
        import h5py, json, sys
        file = h5py.File(sys.argv[1], 'r', swmr=True)
        sizes = {}
        def visit(name, obj):
            if isinstance(obj, h5py.Dataset) and name.endswith('EnlData00'):
                obj.refresh()
                sizes[name] = obj.shape[0]
        file['RawData/Datalogger000'].visititems(visit)
        print(json.dumps(sizes))
    ''')

    def read_from_another_process(self, path) -> dict:
        result = subprocess.run([sys.executable, '-c', self.READER, str(path)], capture_output=True, text=True,
                                timeout=120)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    def test_the_file_can_be_read_by_another_program_while_the_experiment_runs(self, ext, qtbot, tmp_path):
        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: ext._module_and_data_saver.swmr_started, timeout=20000)
        path = h5_path(tmp_path)

        first = self.read_from_another_process(path)
        qtbot.wait(1500)
        second = self.read_from_another_process(path)

        assert len(first) == 4 + 2 + 2  # (spectrum, temperature, set-point of two units, and the 6 ROIs each: 1 per group)
        assert all(size > 0 for size in first.values())
        assert sum(second.values()) > sum(first.values())  # it grows while it is read
        end_run(qtbot, ext)

    def test_the_file_is_written_for_swmr_whatever_the_backend_of_the_saver(self, ext, qtbot, tmp_path):
        pytest.importorskip('tables')
        ext.h5_manager._init_h5_saver().settings['backend', 'backend_type'] = 'tables'

        run(qtbot, ext)
        wait_for_end(qtbot, ext)

        with h5py.File(h5_path(tmp_path), 'r') as f:
            assert _text(f.attrs['swmr_compatible']) in (True, 'True')

    def test_swmr_ends_with_the_experiment_and_the_file_is_complete(self, ext, qtbot, tmp_path):
        run(qtbot, ext)
        wait_for_end(qtbot, ext)

        with h5py.File(h5_path(tmp_path), 'r') as f:  # (an ordinary reader: no swmr=True)
            assert not f.attrs['swmr_active']
            group = f['RawData/Datalogger000']
            for node in group.values():
                if isinstance(node, h5py.Group) and 'Data0D' in node:
                    for array in node['Data0D']['CH00'].values():
                        assert _text(array.attrs['shape']) == array.shape

    def test_the_experiment_can_be_loaded_with_the_pymodaq_tools(self, ext, qtbot, tmp_path):
        from pymodaq_data.h5modules.data_saving import DataLoader
        run(qtbot, ext)
        wait_for_end(qtbot, ext)

        path = h5_path(tmp_path)
        with H5Experiment(tmp_path) as saved:
            node = [name for name, group in saved.group.items()
                    if isinstance(group, h5py.Group) and _text(group.attrs['TITLE']) == 'Unit2_Spectrum'][0]
            n = saved.time('Unit2_Spectrum').size
        data = DataLoader(path).load_data(f'/RawData/Datalogger000/{node}/Data1D/CH00/EnlData00', load_all=True)

        assert data.shape[0] == n and [axis.label for axis in data.axes] == ['photon wavelength', 'time'] \
            or [axis.label for axis in data.axes][-1] == 'time'


class TestInitializeTheModules:
    @staticmethod
    def not_initialized(ext, delay=300, fail=()):
        """The modules of the units look not initialized, and initializing one takes `delay` ms (then it says how it
        went, as a module does with init_signal; the titles in `fail` do not succeed). Returns what was asked, in
        which order and with which modules already initialized (a DAQ_Viewer cannot be closed through the Dashboard in
        PyMoDAQ 5.3: this pretends)"""
        from qtpy import QtCore
        modules = [ext.unit(1).detector, ext.unit(1).thermostat, ext.unit(2).detector, ext.unit(2).thermostat]
        asked = []
        for module in modules:
            module._controller_and_thread.initialized = False

            def initialize(do_init=True, module=module):
                asked.append((module.title, sorted(other.title for other in modules if other.initialized_state),
                              time.monotonic()))

                def finish():
                    ok = module.title not in fail
                    module._controller_and_thread.initialized = ok
                    module.init_signal.emit(ok)

                QtCore.QTimer.singleShot(delay, finish)

            module.init_hardware_ui = initialize
        return modules, asked

    def test_the_button_is_called_connect(self, ext):
        action = ext.get_action('initialize')

        assert action.text() == 'Connect the modules'
        assert 'initialize' in action.toolTip() and 'preset' in action.toolTip()

    def test_the_live_view_asks_to_connect_the_modules_first(self, ext):
        modules, asked = self.not_initialized(ext)

        assert not ext.start_monitoring()

        title, text, error = ext.notifications[-1]
        assert title == 'Cannot start' and 'detector Spectro1 is not initialized' in text
        assert 'Click "Connect the modules"' in text
        assert asked == []  # (nothing was connected behind the back of the user)
        for module in modules:
            module._controller_and_thread.initialized = True  # (so that the Dashboard can close it)

    def test_an_experiment_asks_to_connect_the_modules_first_too(self, ext):
        modules, asked = self.not_initialized(ext)

        ext.start_run()

        assert not ext.run_active and asked == []
        assert 'Click "Connect the modules"' in ext.notifications[-1][1]
        for module in modules:
            module._controller_and_thread.initialized = True

    def test_no_hint_when_the_problem_is_another_one(self, ext):
        ext.unit(1).settings.child('readout_time').setValue(0.05)
        ext.unit(1).settings.child('integration_time').setValue(500.)

        assert not ext.start_monitoring()

        assert 'readout time' in ext.notifications[-1][1] and 'Connect' not in ext.notifications[-1][1]

    def test_the_units_of_the_temperature_are_shown_once_the_thermostats_are_connected(self, ext, qtbot):
        modules, asked = self.not_initialized(ext, delay=100)
        for unit in ext.units[:2]:
            unit.settings.set_units('')  # (what the unit knew while the thermostat was not connected)
        assert ext.unit(1).settings.child('ramp', 'initial').opts['suffix'] == ''

        ext.initialize_modules()

        wait_for(qtbot, lambda: not ext._init_timer.isActive(), timeout=30000)
        for unit in ext.units[:2]:
            assert unit.settings.child('ramp', 'initial').opts['suffix'] == '°C'
            assert unit.settings.child('ramp', 'velocity').opts['suffix'] == '°C/min'

    def test_nothing_to_do_when_everything_is_initialized(self, ext, qtbot):
        calls = []
        for unit in ext.units[:2]:
            for module in (unit.detector, unit.thermostat):
                module.init_hardware_ui = lambda do_init=True, m=module: calls.append(m.title)

        ext.initialize_modules()
        qtbot.wait(500)

        assert calls == [] and not ext._init_timer.isActive()

    def test_the_modules_which_are_not_are_initialized_one_after_the_other(self, ext, qtbot):
        modules, asked = self.not_initialized(ext)

        ext.initialize_modules()

        wait_for(qtbot, lambda: all(module.initialized_state for module in modules), timeout=30000)
        assert [title for title, _, _ in asked] == ['Spectro1', 'Thermo1', 'Spectro2', 'Thermo2']  # (the units in order)
        assert [done for _, done, _ in asked] == [[], ['Spectro1'], ['Spectro1', 'Thermo1'],
                                                  ['Spectro1', 'Spectro2', 'Thermo1']]  # one at a time
        qtbot.waitUntil(lambda: not ext._init_timer.isActive(), timeout=5000)

    def test_a_module_which_never_initializes_does_not_hold_the_others_up(self, ext, qtbot):
        modules, asked = self.not_initialized(ext, delay=300)
        modules[0].init_hardware_ui = lambda do_init=True: asked.append(('Spectro1', [], 0.))  # (it never says anything)
        ext.initialize_modules()
        wait_for(qtbot, lambda: [title for title, _, _ in asked] == ['Spectro1'])

        ext._init_deadline = time.monotonic() - 1.  # (the minute it is given is over)

        wait_for(qtbot, lambda: all(module.initialized_state for module in modules[1:]), timeout=30000)
        assert not modules[0].initialized_state
        modules[0]._controller_and_thread.initialized = True  # (so that the Dashboard can close it)

    @pytest.mark.errors_expected
    def test_a_module_which_fails_does_not_hold_the_next_one_up_and_is_told_at_the_end(self, ext, qtbot):
        modules, asked = self.not_initialized(ext, delay=200, fail=('Spectro1',))

        ext.initialize_modules()

        wait_for(qtbot, lambda: not ext._init_timer.isActive(), timeout=30000)
        assert [title for title, _, _ in asked] == ['Spectro1', 'Thermo1', 'Spectro2', 'Thermo2']
        assert asked[1][2] - asked[0][2] < 5.  # (not the minute it waits for a module which says nothing)
        assert [module.initialized_state for module in modules] == [False, True, True, True]
        title, text, error = ext.notifications[-1]
        assert title == 'Initialization' and 'Spectro1 could not be initialized' in text and error
        assert 'Thermo1' not in text and 'Spectro2' not in text
        modules[0]._controller_and_thread.initialized = True  # (so that the Dashboard can close it)

    def test_nothing_is_said_when_everything_was_initialized(self, ext, qtbot):
        modules, asked = self.not_initialized(ext, delay=100)

        ext.initialize_modules()

        wait_for(qtbot, lambda: not ext._init_timer.isActive(), timeout=30000)
        assert all(module.initialized_state for module in modules) and ext.notifications == []

    def test_asking_again_while_it_goes_on_starts_over_without_asking_twice_at_once(self, ext, qtbot):
        modules, asked = self.not_initialized(ext, delay=300)
        ext.initialize_modules()
        wait_for(qtbot, lambda: len(asked) >= 1)

        ext.initialize_modules()
        wait_for(qtbot, lambda: not ext._init_timer.isActive(), timeout=30000)

        assert all(module.initialized_state for module in modules)
        assert max(len(done) for _, done, _ in asked) <= 3

    def test_a_unit_which_is_not_used_is_left_alone(self, ext, qtbot):
        ext.unit(2).settings.child('enabled').setValue(False)
        modules, asked = self.not_initialized(ext)

        ext.initialize_modules()

        wait_for(qtbot, lambda: all(module.initialized_state for module in modules[:2]), timeout=30000)
        qtbot.wait(500)
        assert [title for title, _, _ in asked] == ['Spectro1', 'Thermo1']
        for module in modules[2:]:
            module._controller_and_thread.initialized = True  # (so that the Dashboard can close them)

    def test_the_toolbar_button_initializes_a_thermostat_for_real(self, ext, qtbot):
        module = ext.unit(1).thermostat
        module.init_hardware_ui(False)
        wait_for(qtbot, lambda: not module.initialized_state)

        ext.get_action('initialize').trigger()

        wait_for(qtbot, lambda: module.initialized_state, timeout=30000)


class TestStart:
    def test_the_experiment_waits_until_every_module_has_delivered(self, ext, qtbot, tmp_path, monkeypatch):
        # the Mock thermostat of the second unit does not answer at first: nothing starts without its temperature
        thermostat = ext.modules_manager.get_mod_from_name('Thermo2', mod=ModuleType.Actuator)
        original = thermostat.get_actuator_value
        monkeypatch.setattr(thermostat, 'get_actuator_value', lambda *args, **kwargs: None)

        ext.start_run()
        qtbot.wait(800)

        assert ext._pending_start and not ext.run_active
        assert 'Thermo2' in ext._status_message_label.text()
        assert ext.get_action('stop').isEnabled() and not ext.get_action('start').isEnabled()

        monkeypatch.setattr(thermostat, 'get_actuator_value', original)
        qtbot.waitUntil(lambda: ext.run_active, timeout=20000)
        wait_for_end(qtbot, ext)
        with H5Experiment(tmp_path) as saved:  # (no unit starts to be recorded before the other)
            assert saved.time('Unit1_ROIs')[0] == pytest.approx(saved.time('Unit2_ROIs')[0], abs=0.5)

    def test_it_gives_up_and_says_who_is_missing(self, ext, qtbot, monkeypatch):
        thermostat = ext.modules_manager.get_mod_from_name('Thermo1', mod=ModuleType.Actuator)
        monkeypatch.setattr(thermostat, 'get_actuator_value', lambda *args, **kwargs: None)
        ext.start_run()
        assert ext._pending_start

        ext._check_pending_start()

        assert not ext._pending_start and not ext.run_active and not ext.monitoring
        title, text, error = ext.notifications[-1]
        assert 'Nothing came from' in text and 'Thermo1' in text and error
        assert ext.get_action('start').isEnabled() and not ext.get_action('stop').isEnabled()

    def test_stop_while_waiting(self, ext, qtbot, monkeypatch):
        thermostat = ext.modules_manager.get_mod_from_name('Thermo1', mod=ModuleType.Actuator)
        monkeypatch.setattr(thermostat, 'get_actuator_value', lambda *args, **kwargs: None)
        monkeypatch.setattr(ext, '_begin_run', lambda: pytest.fail('it should not start'))
        ext.start_run()
        assert ext._pending_start

        ext.stop_run()

        assert not ext._pending_start and not ext._start_timer.isActive()
        assert not ext.monitoring and ext.get_action('start').isEnabled()

    def test_the_wait_of_a_cancelled_start_cannot_cancel_the_next_one(self, ext, monkeypatch):
        thermostat = ext.modules_manager.get_mod_from_name('Thermo1', mod=ModuleType.Actuator)
        monkeypatch.setattr(thermostat, 'get_actuator_value', lambda *args, **kwargs: None)
        ext.start_run()
        assert ext._pending_start and ext._start_timer.isActive()
        ext.stop_run()
        assert not ext._pending_start and not ext._start_timer.isActive()

        ext.start_run()

        assert ext._pending_start and ext._start_timer.isActive()
        ext.stop_run()

    def test_starting_twice_does_nothing(self, ext, qtbot):
        run(qtbot, ext, **SLOW)
        started = ext._origin

        ext.start_run()

        assert ext._origin == started and ext.run_active
        end_run(qtbot, ext)


class TestStop:
    def test_the_thermostats_go_back_to_their_initial_value_before_the_experiment_ends(self, ext, qtbot, tmp_path):
        sent = spy_setpoints(ext)
        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: all(unit.ramp_state.setpoint > 0.6 for unit in ext.units[:2]))
        n = {number: len(values) for number, values in sent.items()}

        ext.stop_run()

        assert ext.run_active
        assert all(unit.ramp_state.phase == Phase.RETURN for unit in ext.units[:2])
        wait_for_end(qtbot, ext)
        for number in (1, 2):
            assert sent[number][-1] == 0.
            back = sent[number][n[number]:]  # what was sent from the Stop on
            assert len(back) > 3
            assert all(a >= b - 1e-9 for a, b in zip(back, back[1:]))  # it only goes down
            assert max(sent[number]) < 3.0  # the top of the ramp was not reached
        with H5Experiment(tmp_path) as saved:
            for number in (1, 2):
                assert PHASE_CODES[Phase.RETURN] in saved.zero_d(f'Unit{number}_Program', 2)

    def test_the_return_takes_as_long_as_the_ramp_says(self, ext, qtbot):
        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: ext.unit(1).ramp_state.setpoint > 1.0)
        start = time.perf_counter()
        expected = ext.unit(1).ramp_state.setpoint / 0.5

        ext.stop_run()
        qtbot.waitUntil(lambda: not ext.run_active, timeout=30000)

        elapsed = time.perf_counter() - start
        assert elapsed >= expected - 0.3  # never faster than the ramp: this is what protects the sample
        assert elapsed < expected + 10.  # (and not much later, whatever the load of the machine)
        qtbot.waitUntil(lambda: not ext._worker.is_saving, timeout=20000)

    def test_each_thermostat_returns_at_the_speed_of_its_own_ramp(self, ext, qtbot):
        set_ramp(ext, [ext.unit(1)], velocity=30.)  # 0.5 per second
        set_ramp(ext, [ext.unit(2)], velocity=60., final=2.)  # 1 per second
        run(qtbot, ext)
        wait_for(qtbot, lambda: ext.unit(1).ramp_state.setpoint > 1.0 and ext.unit(2).ramp_state.setpoint > 1.0)
        top = {number: ext.unit(number).ramp_state.setpoint for number in (1, 2)}
        start = time.perf_counter()

        ext.stop_run()
        wait_for(qtbot, lambda: ext.unit(2).program_over, timeout=30000)
        second_took = time.perf_counter() - start

        assert second_took < top[2] / 1.0 + 3.0
        assert not ext.unit(1).program_over or top[1] / 0.5 < second_took + 0.5  # the first one is slower
        end_run(qtbot, ext)

    def test_stopping_twice_asks_before_leaving_the_thermostats_where_they_are(self, ext, qtbot):
        sent = spy_setpoints(ext)
        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: ext.unit(1).ramp_state.setpoint > 1.0)
        ext.stop_run()
        ext.answers['abort_return'] = False

        ext.stop_run()

        assert ext.run_active and all(unit.ramp_state.phase == Phase.RETURN for unit in ext.units[:2])

        ext.answers['abort_return'] = True
        ext.stop_run()
        qtbot.waitUntil(lambda: not ext.run_active, timeout=5000)
        assert sent[1][-1] > 0.1 and sent[2][-1] > 0.1  # left where they were
        qtbot.waitUntil(lambda: not ext._worker.is_saving, timeout=20000)

    def test_stop_while_paused(self, ext, qtbot):
        sent = spy_setpoints(ext)
        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: ext.unit(1).ramp_state.setpoint > 0.5)
        ext.pause_run(True)
        frozen = ext.unit(1).ramp_state.setpoint

        ext.stop_run()

        assert ext.unit(1).ramp_state.phase == Phase.RETURN and not ext.unit(1).ramp_state.paused
        assert ext.unit(1).ramp_state.setpoint == pytest.approx(frozen, abs=0.2)
        wait_for_end(qtbot, ext)
        assert sent[1][-1] == 0. and sent[2][-1] == 0.

    def test_stop_without_an_experiment_does_nothing(self, ext):
        ext.stop_run()

        assert not ext.run_active and ext.notifications == []

    def test_a_unit_which_is_already_back_does_not_hold_the_others_up(self, ext, qtbot):
        sent = spy_setpoints(ext)
        ext.unit(2).settings.child('ramp', 'run').setValue(False)  # (no thermostat to bring back)
        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: ext.unit(1).ramp_state.setpoint > 0.5)

        ext.stop_run()
        wait_for_end(qtbot, ext)

        assert sent[1][-1] == 0. and sent[2] == []


class TestPause:
    def test_the_set_points_are_frozen_and_go_on_afterwards(self, ext, qtbot):
        sent = spy_setpoints(ext)
        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: all(unit.ramp_state.setpoint > 0.4 for unit in ext.units[:2]))

        ext.pause_run(True)
        n = {number: len(values) for number, values in sent.items()}
        frozen = {unit.number: unit.ramp_state.setpoint for unit in ext.units[:2]}
        qtbot.wait(1000)

        for unit in ext.units[:2]:
            assert unit.ramp_state.paused
            assert len(sent[unit.number]) == n[unit.number]  # nothing is sent to the thermostat
            assert unit.ramp_state.setpoint == frozen[unit.number]
            assert unit.settings.child('status', 'phase').value() == 'ramp (paused)'

        ext.pause_run(False)
        qtbot.waitUntil(lambda: all(unit.ramp_state.setpoint > frozen[unit.number] + 0.2 for unit in ext.units[:2]),
                        timeout=5000)
        assert not ext.unit(1).ramp_state.paused
        end_run(qtbot, ext)

    def test_the_recording_goes_on_during_the_pause(self, ext, qtbot, tmp_path):
        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: ext.unit(1).ramp_state.setpoint > 0.3)
        ext.pause_run(True)
        qtbot.wait(1500)
        ext.pause_run(False)
        end_run(qtbot, ext)

        with H5Experiment(tmp_path) as saved:
            for number in (1, 2):
                setpoint = saved.zero_d(f'Unit{number}_Program', 0)
                assert np.isclose(np.diff(setpoint), 0.).sum() >= 5  # about 15 samples while the set-point did not move

    def test_the_pause_button(self, ext, qtbot):
        run(qtbot, ext, **SLOW)

        ext.get_action('pause').trigger()
        assert ext.unit(1).ramp_state.paused and ext.unit(2).ramp_state.paused
        ext.get_action('pause').trigger()
        assert not ext.unit(1).ramp_state.paused
        end_run(qtbot, ext)

    def test_pausing_without_an_experiment_does_nothing(self, ext):
        ext.pause_run(True)

        assert not ext.run_active


@pytest.mark.errors_expected  # (a failure is logged as an error)
class TestFailures:
    """What goes wrong with the modules of one unit is handled for that unit: its thermostat goes back"""

    def test_a_unit_whose_spectrometer_stops_brings_its_thermostat_back_while_the_other_goes_on(self, ext, qtbot):
        sent = spy_setpoints(ext)
        ext.settings['experiment', 'watchdog'] = 1.0
        for unit in ext.units[:2]:
            unit.settings.child('readout_time').setValue(0.1)
            unit.settings.child('restart_after').setValue(0)  # (never restarted: the time decides, see test_lost_instruments)
        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: ext.unit(2).ramp_state.setpoint > 0.5)
        detector = ext.unit(1).detector
        detector.command_hardware.disconnect()  # the spectrometer of the first unit stops answering

        qtbot.waitUntil(lambda: ext.unit(1).returning, timeout=15000)

        title, text, error = ext.notifications[-1]
        assert error and 'No spectrum from Spectro1' in text and 'brought back' in text
        assert ext.unit(1).failed and not ext.unit(2).returning and not ext.unit(2).failed
        assert ext.run_active
        wait_for(qtbot, lambda: ext.unit(1).program_over, timeout=30000)
        assert sent[1][-1] == 0.
        end_run(qtbot, ext)

    def test_a_thermostat_which_stops_answering_is_not_trusted_to_return(self, ext, qtbot, monkeypatch):
        ext.settings['experiment', 'watchdog'] = 1.0
        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: ext.unit(1).ramp_state.setpoint > 0.5)
        unit = ext.unit(1)
        unit._disconnect(unit.thermostat)  # only its temperature stops coming
        unit.temperature.time -= 5.

        qtbot.waitUntil(lambda: unit.returning, timeout=15000)

        assert 'No temperature from Thermo1' in ext.notifications[-1][1]
        assert 'nothing says that it gets it' in ext.notifications[-1][1]
        assert not ext.unit(2).returning
        end_run(qtbot, ext)

    def test_a_closed_spectrometer_during_an_experiment(self, ext, qtbot):
        sent = spy_setpoints(ext)
        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: ext.unit(2).ramp_state.setpoint > 0.5)

        ext.unit(2).detector.init_signal.emit(False)

        assert ext.unit(2).returning and 'Spectro2 was closed' in ext.notifications[-1][1]
        assert not ext.unit(1).returning
        wait_for(qtbot, lambda: ext.unit(2).program_over, timeout=30000)
        assert sent[2][-1] == 0.
        end_run(qtbot, ext)

    def test_a_lost_thermostat_cannot_be_brought_back_and_the_experiment_goes_on(self, ext, qtbot):
        sent = spy_setpoints(ext)
        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: ext.unit(1).ramp_state.setpoint > 0.5)

        ext.unit(1).thermostat.init_signal.emit(False)

        title, text, error = ext.notifications[-1]
        assert error and 'cannot be returned to its initial temperature' in text
        assert f'{sent[1][-1]:.4g}' in text  # where it was left
        assert ext.unit(1).program_over and not ext.unit(2).program_over
        assert ext.run_active  # (the other unit is still running its ramp)
        end_run(qtbot, ext)

    def test_a_lost_thermostat_does_not_stop_the_recording_of_its_spectrometer(self, ext, qtbot, tmp_path):
        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: ext.unit(1).ramp_state.setpoint > 0.3)
        ext.unit(1).thermostat.init_signal.emit(False)
        n = ext.unit(1).n_measurements

        wait_for(qtbot, lambda: ext.unit(1).n_measurements >= n + 5)
        end_run(qtbot, ext)

        with H5Experiment(tmp_path) as saved:
            temperature = saved.zero_d('Unit1_Temperature')
            assert np.isnan(temperature[-1]) and not np.isnan(temperature[0])

    def test_when_nothing_can_be_read_any_more_the_experiment_ends(self, ext, qtbot):
        for unit in ext.units[:2]:
            unit.settings.child('ramp', 'run').setValue(False)  # (nothing else to wait for)
        run(qtbot, ext)

        ext.unit(1).detector.init_signal.emit(False)
        assert ext.run_active  # (the second unit still reads)
        ext.unit(2).detector.init_signal.emit(False)

        qtbot.waitUntil(lambda: not ext.run_active, timeout=10000)
        qtbot.waitUntil(lambda: not ext._worker.is_saving, timeout=20000)

    def test_no_watchdog_when_it_is_off(self, ext, qtbot):
        ext.settings['experiment', 'watchdog'] = 0.
        run(qtbot, ext, **SLOW)
        for unit in ext.units[:2]:
            unit.spectrum.time -= 1000.
            unit.temperature.time -= 1000.
        qtbot.wait(1500)

        assert all(unit.ramp_state.phase == Phase.RAMP for unit in ext.units[:2])
        assert ext.notifications == []
        end_run(qtbot, ext)

    def test_a_module_which_is_closed_during_the_live_view(self, ext):
        ext.start_monitoring()

        ext.unit(1).detector.init_signal.emit(False)

        assert 'Spectro1 was closed' in ext.notifications[-1][1]
        assert ext.unit(1).failed and ext.monitoring  # (the other unit is still looked at)
        assert not ext.unit(2).failed

    def test_the_watchdog_is_longer_than_a_few_readouts(self, ext):
        ext.settings['experiment', 'watchdog'] = 30.
        unit = ext.unit(1)

        unit.settings.child('integration_time').setValue(500.)
        unit.settings.child('readout_time').setValue(1.)
        assert ext._watchdog_limit(unit) == 30.
        unit.settings.child('integration_time').setValue(60000.)
        unit.settings.child('readout_time').setValue(70.)
        assert ext._watchdog_limit(unit) == 3. * 130. + 5.
        ext.settings['experiment', 'watchdog'] = 0.
        assert ext._watchdog_limit(unit) == 0.


class TestClosing:
    def test_closing_while_idle(self, ext):
        assert ext._quit_fun()

    def test_closing_during_the_live_view_stops_it(self, ext, qtbot):
        ext.start_monitoring()
        wait_for(qtbot, lambda: all(unit.n_measurements > 0 for unit in ext.units[:2]))

        assert ext._quit_fun()

        assert not ext.monitoring and all(unit.state == UnitState.IDLE for unit in ext.units[:2])

    def test_closing_during_an_experiment_can_set_the_initial_temperatures_at_once(self, ext, qtbot, tmp_path):
        sent = spy_setpoints(ext)
        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: all(unit.ramp_state.setpoint > 0.5 for unit in ext.units[:2]))
        wait_for(qtbot, lambda: all(unit.n_measurements > 4 for unit in ext.units[:2]))  # (a loaded computer reads slowly)
        ext.answers['quit'] = 'jump'

        assert ext._quit_fun()

        assert not ext.run_active
        assert sent[1][-1] == 0. and sent[2][-1] == 0.
        assert not ext._worker.is_saving  # the file was written and closed
        with H5Experiment(tmp_path) as saved:
            assert saved.zero_d('Unit1_Program', 0).size > 3

    def test_or_bring_them_back_gradually(self, ext, qtbot):
        sent = spy_setpoints(ext)
        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: all(unit.ramp_state.setpoint > 0.8 for unit in ext.units[:2]))
        ext.answers['quit'] = 'return'

        assert not ext._quit_fun()  # the extension stays open

        assert all(unit.returning for unit in ext.units[:2])
        wait_for_end(qtbot, ext)
        assert sent[1][-1] == 0. and sent[2][-1] == 0.
        assert ext._quit_fun()  # and can be closed once it is done

    def test_or_not_close(self, ext, qtbot):
        run(qtbot, ext, **SLOW)
        ext.answers['quit'] = 'cancel'

        assert not ext._quit_fun()

        assert ext.run_active and not any(unit.returning for unit in ext.units[:2])
        end_run(qtbot, ext)

    def test_quitting_from_the_dashboard_sets_the_initial_temperatures_without_asking(self, ext, qtbot, monkeypatch):
        sent = spy_setpoints(ext)
        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: all(unit.ramp_state.setpoint > 0.5 for unit in ext.units[:2]))
        monkeypatch.setattr(ext, '_ask_quit_choice', lambda: pytest.fail('it should not ask'))

        def quit_fun(self):  # as DashBoard.quit_fun, which ignores the answer and closes the modules afterwards
            return ext._quit_fun()

        assert quit_fun(ext.dashboard)

        assert not ext.run_active
        assert sent[1][-1] == 0. and sent[2][-1] == 0.
        assert not ext._worker.is_saving

    def test_the_quit_dialog_proposes_the_safe_answer_first(self, ext, monkeypatch):
        from qtpy import QtWidgets
        seen = []
        monkeypatch.setattr(QtWidgets.QMessageBox, 'exec', lambda box: seen.append(box.defaultButton().text()) or 0)

        thermo_spectro.ThermoSpectro._ask_quit_choice(ext)

        assert seen == ['Set them now and close']

    def test_the_rois_are_remembered_when_the_extension_closes(self, ext, tmp_path):
        ext.unit(1).settings.child('rois', 'roi00', 'start').setValue(33.)

        assert ext._quit_fun()

        assert RoiStore(tmp_path / 'remembered' / 'rois.json').get(1)[0].start == 33.


class TestWhatTheDesignMustSurvive:
    """Each of these was a way to leave a thermostat hot, or an experiment wedged"""

    def test_the_ramp_cannot_be_edited_during_an_experiment(self, ext, qtbot):
        names = ('run', 'mode', 'initial', 'take_current', 'hold_final', 'back_mode', 'hold_initial', 'n_cycles')
        assert all(ui_enabled(unit.settings.child('ramp', name)) for unit in ext.units[:2] for name in names)

        run(qtbot, ext, **SLOW)

        assert not any(ui_enabled(unit.settings.child('ramp', name)) for unit in ext.units[:2] for name in names)
        end_run(qtbot, ext)
        assert all(ui_enabled(unit.settings.child('ramp', name)) for unit in ext.units[:2] for name in names)

    def test_the_settings_and_the_file_cannot_be_replaced_during_an_experiment(self, ext, qtbot):
        from pymodaq_gui.managers.h5manager import FileAction
        run(qtbot, ext, **SLOW)

        assert not ext._settings_tree.get_action('load_settings').isEnabled()
        assert not ext._settings_tree.get_action('update_settings').isEnabled()
        assert not ext.h5_manager.get_action(FileAction.NEW_FILE).isEnabled()
        panel = ext.saving_panel  # (the window stays usable, to write observations: the file and the experiment are locked)
        assert ext.docks['saving'].isEnabled()
        assert not any(widget.isEnabled() for widget in (panel.folder, panel.browse, panel.name, panel.add_date,
                                                         panel.date_folders, panel.title, panel.sample, panel.operator))
        end_run(qtbot, ext)
        assert ext._settings_tree.get_action('load_settings').isEnabled()
        assert ext.h5_manager.get_action(FileAction.NEW_FILE).isEnabled()
        assert all(widget.isEnabled() for widget in (panel.folder, panel.browse, panel.name, panel.add_date,
                                                     panel.date_folders, panel.title, panel.sample, panel.operator))

    def test_the_locks_survive_a_replaced_settings_tree(self, ext, qtbot, tmp_path):
        from pymodaq_gui.parameter import ioxml
        run(qtbot, ext, **SLOW)
        file = tmp_path / 'settings.xml'
        ioxml.parameter_to_xml_file(ext.settings, str(file))

        ext.set_settings(file)

        assert not ui_enabled(ext.unit(1).settings.child('ramp', 'n_cycles'))
        assert not ui_enabled(ext.unit(2).settings.child('rois', 'roi00', 'start'))
        end_run(qtbot, ext)

    def test_the_units_follow_a_replaced_settings_tree(self, ext, tmp_path):
        from pymodaq_gui.parameter import ioxml
        ext.unit(1).settings.child('ramp', 'final').setValue(12.)
        file = tmp_path / 'settings.xml'
        ioxml.parameter_to_xml_file(ext.settings, str(file))
        ext.unit(1).settings.child('ramp', 'final').setValue(7.)

        ext.set_settings(file)

        assert ext.unit(1).settings.param is ext.settings.child('unit1')
        assert ext.unit(1).settings.child('ramp', 'final').value() == 12.
        assert ext.unit(1).settings.program().final == 12.
        ext.unit(1).settings.child('ramp', 'velocity').setValue(4.)  # (the tree is followed: the fields are computed)
        assert ext.unit(1).settings.child('ramp', 'duration').value() == pytest.approx(12. / 4.)

    def test_the_modules_in_use_are_not_swapped_during_an_experiment(self, ext, qtbot):
        run(qtbot, ext, **SLOW)
        thermostat = ext.unit(1).thermostat

        ext.unit(1).settings.child('thermostat').setValue('None')

        assert ext.unit(1).thermostat is thermostat and ext.run_active
        ext.unit(1).settings.child('thermostat').setValue('Thermo1')
        end_run(qtbot, ext)

    @pytest.mark.errors_expected
    def test_a_failure_while_ending_does_not_wedge_the_experiment(self, ext, qtbot, monkeypatch):
        sent = spy_setpoints(ext)

        def failing(*args):
            raise RuntimeError('boom')

        monkeypatch.setattr(ext._worker, 'drain', failing)
        run(qtbot, ext)

        qtbot.waitUntil(lambda: not ext.run_active, timeout=40000)

        assert not ext._finishing
        assert sent[1][-1] == 0. and sent[2][-1] == 0.  # the initial temperatures go before anything which can fail
        assert ext.get_action('start').isEnabled() and not ext.get_action('stop').isEnabled()
        assert ui_enabled(ext.unit(1).settings.child('ramp', 'n_cycles'))
        qtbot.waitUntil(lambda: not ext._worker.is_saving, timeout=20000)

    @pytest.mark.errors_expected
    def test_a_file_which_cannot_be_closed_does_not_keep_the_interface_locked(self, ext, qtbot, monkeypatch):
        original = ext._worker.stop

        def failing(message=None):
            raise OSError('disk full')

        monkeypatch.setattr(ext._worker, 'stop', failing)
        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: ext.unit(1).ramp_state.setpoint > 0.3)

        ext._finish_run('end', abort=True)

        assert not ext.run_active
        assert ext.get_action('start').isEnabled()
        assert ui_enabled(ext.unit(1).settings.child('ramp', 'n_cycles'))
        assert 'could not be closed properly' in ext.notifications[-1][1]
        original('cleanup')  # (the saver thread is still there)
        qtbot.waitUntil(lambda: not ext._worker.is_saving, timeout=20000)

    def test_a_saver_which_stops_answering_is_said_and_does_not_hang(self, ext, qtbot, monkeypatch):
        monkeypatch.setattr(ext._worker, 'drain', lambda *args: False)
        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: ext.unit(1).ramp_state.setpoint > 0.3)

        ext._finish_run('end', abort=True)

        assert not ext.run_active
        assert 'The saver stopped answering' in ext.notifications[-1][1]
        qtbot.waitUntil(lambda: not ext._worker.is_saving, timeout=20000)

    @pytest.mark.errors_expected
    def test_the_wait_for_the_saver_gives_up_only_when_nothing_is_written_any_more(self, ext, monkeypatch):
        worker = ext._worker
        monkeypatch.setattr(worker, 'saver_worker', type('W', (), {'_n_jobs_done': 5})())
        worker.thread_manager.n_jobs['SaverWorker'] = 5
        assert worker.drain(stall=0.1)  # everything was written

        worker.thread_manager.n_jobs['SaverWorker'] = 9
        start = time.perf_counter()
        assert not worker.drain(stall=0.3)  # four are never written
        assert 0.3 <= time.perf_counter() - start < 3.

    def test_numbers_loaded_from_a_file_cannot_stop_the_timers(self, ext):
        ext.unit(1).settings.child('readout_time').setValue(0.)
        ext.settings.child('experiment', 'update_period').setValue(0.)

        assert ext.unit(1).settings.readout_time >= 0.05
        assert ext._update_period() >= 0.05

    def test_a_temperature_which_is_not_a_number_is_ignored(self, ext):
        from pymodaq.utils.data import DataActuator
        unit = ext.unit(1)
        unit._on_temperature(DataActuator('Thermo1', data=float('nan'), units='°C'))
        assert unit.temperature is None

        unit._on_temperature(DataActuator('Thermo1', data=21.5, units='°C'))
        assert unit.temperature.value == 21.5
        unit._on_temperature(DataActuator('Thermo1', data=float('inf'), units='°C'))
        assert unit.temperature.value == 21.5

    def test_the_saver_never_uses_the_thread_unsafe_append_of_data_to_export(self, ext, qtbot, monkeypatch):
        """PyMoDAQ's DataToExport.append is shared between threads: used in the saver thread it made a DAQ_Viewer lose
        its data (found with this extension: a unit stopped being read out)"""
        import threading
        from pymodaq_data import DataToExport
        callers = []
        original = DataToExport.__dict__['append']

        def spy(self, *args):
            callers.append(threading.current_thread())
            original.__get__(self, type(self))
            return original(*args)

        monkeypatch.setattr(DataToExport, 'append', spy)
        run(qtbot, ext)
        wait_for_end(qtbot, ext)

        assert callers  # (the viewers use it)
        assert all(caller is threading.main_thread() for caller in callers)

    def test_the_experiment_survives_a_spectrum_which_never_came_back(self, ext, qtbot, tmp_path):
        """The detector lost the answer to a snap (it happens): the unit asks again instead of waiting for ever"""
        unit = ext.unit(1)
        unit.pending_timeout = lambda: 0.5
        run(qtbot, ext, **SLOW)
        detector = unit.detector
        # the spectra come back to nobody: as if the answers were lost (not the initialization signal too: the unit
        # closes and initializes the detector again after two missed readouts, and has to hear that it is done)
        detector.grab_done_signal.disconnect(unit._on_spectrum)

        wait_for(qtbot, lambda: unit.n_lost >= 2, timeout=15000)
        detector.grab_done_signal.connect(unit._on_spectrum)
        n = unit.n_measurements

        wait_for(qtbot, lambda: unit.n_measurements >= n + 3, timeout=15000)  # it is read out again
        assert ext.run_active
        end_run(qtbot, ext)


class TestTheRealDialogs:
    """The fixture replaces the dialogs: here the real ones, with the answers given by the test"""

    def test_a_notification_does_not_block_and_is_in_the_status_bar(self, ext, qtbot):
        messages = []
        ext.status_signal.connect(messages.append)

        thermo_spectro.ThermoSpectro._notify(ext, 'A title', 'Something happened')

        assert messages == ['Something happened']
        assert len(ext._boxes) == 1 and not ext._boxes[0].isModal()
        ext._boxes[0].close()
        qtbot.waitUntil(lambda: ext._boxes == [], timeout=3000)

    @pytest.mark.errors_expected
    def test_an_error_notification_is_logged_as_an_error(self, ext, caplog):
        with caplog.at_level('ERROR'):
            thermo_spectro.ThermoSpectro._notify(ext, 'Problem', 'It broke', error=True)

        assert any('It broke' in record.getMessage() for record in caplog.records)
        ext._boxes[0].close()

    @pytest.mark.parametrize('answer, expected', [('Yes', True), ('No', False)])
    def test_the_question_when_stopping_twice_says_where_the_thermostats_are(self, ext, qtbot, monkeypatch, answer,
                                                                             expected):
        from qtpy import QtWidgets
        asked = []
        button = getattr(QtWidgets.QMessageBox.StandardButton, answer)
        monkeypatch.setattr(QtWidgets.QMessageBox, 'question',
                            staticmethod(lambda parent, title, text, buttons, default: asked.append(text) or button))
        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: ext.unit(1).ramp_state.setpoint > 0.5)
        ext.stop_run()

        assert thermo_spectro.ThermoSpectro._ask_abort_return(ext) is expected

        assert 'Spectro1: ' in asked[0] and 'Spectro2: ' in asked[0] and '°C' in asked[0]
        end_run(qtbot, ext)

    @pytest.mark.parametrize('clicked, expected', [('Set them now and close', 'jump'),
                                                   ('Bring them back gradually', 'return'), (None, 'cancel')])
    def test_the_three_answers_of_the_quit_dialog(self, ext, monkeypatch, clicked, expected):
        from qtpy import QtWidgets

        def execute(box):
            box.target = (next(b for b in box.buttons() if b.text() == clicked) if clicked else box.escapeButton())

        monkeypatch.setattr(QtWidgets.QMessageBox, 'exec', execute)
        monkeypatch.setattr(QtWidgets.QMessageBox, 'clickedButton', lambda box: box.target)

        assert thermo_spectro.ThermoSpectro._ask_quit_choice(ext) == expected


class TestRareCases:
    @pytest.mark.errors_expected
    def test_an_experiment_which_cannot_start_leaves_everything_as_it_was(self, ext, qtbot, monkeypatch):
        sent = spy_setpoints(ext)

        class Broken:
            @staticmethod
            def from_unit(unit):
                raise RuntimeError('no recording')

        monkeypatch.setattr(thermo_spectro, 'UnitRecording', Broken)

        ext.start_run()
        wait_for(qtbot, lambda: any('could not start' in text for _, text, _ in ext.notifications), timeout=20000)

        title, text, error = ext.notifications[-1]
        assert error and 'no recording' in text
        assert not ext.run_active and not ext._pending_start and not ext.monitoring
        assert ext.get_action('start').isEnabled() and not ext.get_action('stop').isEnabled()
        assert ui_enabled(ext.unit(1).settings.child('ramp', 'n_cycles'))
        assert sent == {1: [], 2: [], 3: []}  # (no thermostat was touched)
        assert all(unit.state == UnitState.IDLE for unit in ext.units[:2])

    @pytest.mark.errors_expected
    def test_data_which_could_not_be_saved_is_said_at_the_end(self, ext, qtbot, monkeypatch):
        from pymodaq_plugins_thermospectro.saving import ThermoSpectroSaver
        original = ThermoSpectroSaver.add_data

        def failing(self, dte, **kwargs):
            if dte.name == 'Unit1_ROIs':
                raise OSError('disk full')
            return original(self, dte, **kwargs)

        monkeypatch.setattr(ThermoSpectroSaver, 'add_data', failing)
        run(qtbot, ext)

        wait_for_end(qtbot, ext)

        assert any('data could not be saved' in text for _, text, _ in ext.notifications)

    def test_a_thermostat_which_is_not_initialized_any_more_is_said_at_the_end(self, ext, qtbot):
        run(qtbot, ext)
        ext.unit(1).thermostat._controller_and_thread.initialized = False  # (without a signal)

        wait_for_end(qtbot, ext)

        assert any('Thermo1 is not initialized any more' in text for _, text, _ in ext.notifications)
        ext.unit(1).thermostat._controller_and_thread.initialized = True

    def test_the_rois_can_be_removed_by_the_program(self, ext):
        ext.remove_roi(1, 'roi01')

        assert 'roi01' not in ext.unit(1).settings.roi_keys
        assert len(ext.view(1).curves_plot.curves) == 5

    def test_the_rois_cannot_be_removed_during_an_experiment(self, ext, qtbot):
        run(qtbot, ext, **SLOW)

        with pytest.raises(RuntimeError, match='cannot be changed'):
            ext.remove_roi(1, 'roi01')
        end_run(qtbot, ext)

    def test_the_live_view_cannot_be_stopped_during_an_experiment(self, ext, qtbot):
        run(qtbot, ext, **SLOW)

        with pytest.raises(RuntimeError, match='stop it first'):
            ext.stop_monitoring()
        end_run(qtbot, ext)

    def test_a_cancelled_start_goes_back_to_the_live_view_if_it_was_on(self, ext, qtbot, monkeypatch):
        ext.start_monitoring()
        wait_for(qtbot, lambda: all(unit.ready for unit in ext.units[:2]))
        monkeypatch.setattr(ext, '_missing_data', lambda: ['Thermo1'])  # (it waits)
        ext.start_run()
        assert ext._pending_start

        ext.stop_run()

        assert ext.monitoring and ext._status_message_label.text() == 'Live'
        assert ext.get_action('start').isEnabled()

    def test_closing_the_extension_while_an_experiment_waits_to_start(self, ext, monkeypatch):
        monkeypatch.setattr(ext, '_missing_data', lambda: ['Thermo1'])
        ext.start_run()
        assert ext._pending_start

        assert ext._quit_fun()

        assert not ext._pending_start and not ext.monitoring

    def test_a_live_view_which_cannot_start_does_not_start_the_experiment(self, ext, monkeypatch):
        monkeypatch.setattr(ext, 'start_monitoring', lambda: False)

        ext.start_run()

        assert not ext._pending_start and not ext.run_active and not ext._monitoring_for_run

    def test_a_missing_button_is_only_a_warning(self, ext, caplog):
        ext.unit(1).settings.param.child('rois').removeChild(ext.unit(1).settings.child('rois', 'add_roi'))

        with caplog.at_level('WARNING'):
            ext._connect_buttons()

        assert any('no rois/add_roi button' in record.getMessage() for record in caplog.records)
