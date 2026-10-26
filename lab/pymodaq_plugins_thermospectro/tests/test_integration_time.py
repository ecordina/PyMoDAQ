"""The integration time: it is saved with each measurement, it can be changed while an experiment runs, the values can be
divided by it, and the units are kept"""
import math

import h5py
import numpy as np
import pytest

from pymodaq_gui.parameter import Parameter

from pymodaq_plugins_thermospectro import saving
from pymodaq_plugins_thermospectro.rois import Roi, default_rois
from pymodaq_plugins_thermospectro.saving import INTEGRATION, UnitRecording, build_dtes, pint_safe
from pymodaq_plugins_thermospectro.unit import UnitState
from pymodaq_plugins_thermospectro.unit_settings import (DEFAULT_INTENSITY_UNITS, UnitSettings, unit_definition)

from test_saving import Experiment, attribute, h5saver, make_measurement, make_unit, titles  # noqa: F401  (a fixture)
from test_unit import Bench, bench  # noqa: F401  (bench is a fixture)

pytestmark = pytest.mark.filterwarnings('ignore')

INTEGRATION_PATH = ('detector_settings', 'integration')


def integration_times(bench: Bench) -> list[float]:
    return [measurement.integration_time for measurement in bench.measurements]


class TestWhatEachMeasurementIsMadeWith:
    def test_a_measurement_says_the_integration_time_of_the_instrument(self, bench):
        bench.run()
        bench.wait_for(3)

        assert set(integration_times(bench)) == {20.}
        assert bench.detector.settings[INTEGRATION_PATH] == 20.

    def test_what_the_instrument_accepts_is_what_is_recorded(self, bench):
        bench.settings.child('integration_time').setValue(1.)  # (the instrument does not go below 3.8 ms)

        bench.run()
        bench.wait_for(2)

        assert set(integration_times(bench)) == {3.8}

    def test_an_instrument_without_such_a_setting_has_the_time_of_the_settings(self, qtbot):
        bench = Bench(qtbot, detector_kwargs=dict(integration=False))
        try:
            bench.run()
            bench.wait_for(2)

            assert set(integration_times(bench)) == {20.}
        finally:
            bench.unit._release()


class TestChangedDuringTheExperiment:
    def test_the_next_readouts_are_made_with_the_new_time(self, bench):
        bench.run()
        bench.wait_for(2)

        bench.settings.child('integration_time').setValue(40.)
        assert bench.detector.settings[INTEGRATION_PATH] == 20.  # (during an experiment: set before the next readout)
        done = len(bench.measurements)
        bench.wait_for(done + 4)

        times = integration_times(bench)
        assert times[0] == 20. and times[-1] == 40.
        assert set(times) == {20., 40.}
        assert times == sorted(times)  # (it changes once, between two readouts)
        assert bench.detector.settings[INTEGRATION_PATH] == 40.

    def test_it_can_be_changed_again(self, bench):
        bench.run()
        bench.wait_for(2)
        for wanted in (30., 25.):
            bench.settings.child('integration_time').setValue(wanted)
            bench.wait_for(len(bench.measurements) + 3)

        assert integration_times(bench)[-1] == 25. and {20., 30., 25.} <= set(integration_times(bench))

    def test_a_time_which_is_not_shorter_than_the_readout_time_is_refused(self, bench):
        bench.run()
        bench.wait_for(2)

        bench.settings.child('integration_time').setValue(500.)  # (the readout time is 0.1 s)
        bench.wait_for(len(bench.measurements) + 3)

        assert set(integration_times(bench)) == {20.}
        assert bench.settings.integration_time == 20.  # (the setting went back to what the instrument is set to)
        assert any('not shorter than the readout time' in notice for notice in bench.notices)
        assert bench.detector.settings[INTEGRATION_PATH] == 20.

    def test_in_the_live_view_it_is_set_at_once(self, bench):
        bench.unit.start_live(0.)
        bench.qtbot.waitUntil(lambda: bench.unit.ready, timeout=10000)

        bench.settings.child('integration_time').setValue(50.)
        bench.unit.integration_changed()  # (what the extension does when the setting changes)

        assert bench.detector.settings[INTEGRATION_PATH] == 50.
        assert bench.unit.integration_time_now() == 50.

    def test_during_the_experiment_the_extension_does_not_set_it_at_once(self, bench):
        bench.run()
        bench.wait_for(1)
        bench.settings.child('integration_time').setValue(40.)

        bench.unit.integration_changed()

        assert bench.unit.state == UnitState.RUNNING and bench.detector.settings[INTEGRATION_PATH] == 20.


class TestDividedByTheIntegrationTime:
    def test_the_values_are_per_ms(self, bench):
        bench.settings.child('per_integration').setValue(True)
        bench.run()
        bench.wait_for(3)

        for measurement in bench.measurements:
            raw = [roi.value(measurement.spectrum.x, measurement.spectrum.y) for roi in measurement.rois]
            assert measurement.values == pytest.approx([value / 20. for value in raw])

    def test_the_curves_follow(self, bench):
        bench.settings.child('per_integration').setValue(True)
        bench.run()
        bench.wait_for(3)

        assert bench.unit.series_rois.column(0)[-1] == pytest.approx(bench.measurements[-1].values[0])

    def test_without_the_option_the_values_are_as_measured(self, bench):
        bench.run()
        bench.wait_for(2)

        measurement = bench.measurements[-1]
        raw = [roi.value(measurement.spectrum.x, measurement.spectrum.y) for roi in measurement.rois]
        assert measurement.values == pytest.approx(raw)

    def test_each_value_is_divided_by_the_time_of_its_readout(self, bench):
        bench.settings.child('per_integration').setValue(True)
        bench.run()
        bench.wait_for(2)
        bench.settings.child('integration_time').setValue(40.)
        bench.wait_for(len(bench.measurements) + 4)

        last = bench.measurements[-1]
        raw = [roi.value(last.spectrum.x, last.spectrum.y) for roi in last.rois]
        assert last.integration_time == 40. and last.values == pytest.approx([value / 40. for value in raw])
        first = bench.measurements[0]
        assert first.values == pytest.approx([roi.value(first.spectrum.x, first.spectrum.y) / 20. for roi in first.rois])

    def test_a_time_of_zero_gives_no_value_and_it_is_said_once(self, qtbot, caplog):
        bench = Bench(qtbot, detector_kwargs=dict(integration=False))  # (nothing limits the time of its settings)
        try:
            bench.settings.child('per_integration').setValue(True)
            bench.settings.child('integration_time').setValue(0.)
            bench.run()
            bench.wait_for(3)

            assert all(math.isnan(value) for measurement in bench.measurements for value in measurement.values)
            assert len([r for r in caplog.records if 'cannot be divided' in r.getMessage()]) == 1
        finally:
            bench.unit._release()


class TestUnitsOfTheValues:
    @pytest.fixture
    def settings(self, qapp):
        return UnitSettings(Parameter.create(**unit_definition(1, default_rois(1))), 1)

    def test_arbitrary_units_by_default(self, settings):
        assert settings.intensity_units == DEFAULT_INTENSITY_UNITS == 'A.U.' and settings.value_units == 'A.U.'

    def test_per_ms_once_divided(self, settings):
        settings.child('per_integration').setValue(True)

        assert settings.value_units == 'A.U./ms'
        settings.child('intensity_units').setValue('counts')
        assert settings.value_units == 'counts/ms'

    def test_the_unit_is_the_one_of_the_user(self, settings):
        settings.child('intensity_units').setValue('  W/m2  ')
        assert settings.intensity_units == 'W/m2' and settings.value_units == 'W/m2'

        settings.child('per_integration').setValue(True)
        assert settings.value_units == 'W/m2/ms'

    def test_without_a_unit_it_is_still_per_ms(self, settings):
        settings.child('intensity_units').setValue('')
        settings.child('per_integration').setValue(True)

        assert settings.value_units == '1/ms'

    def test_the_unit_of_a_unit_is_shown(self, bench):
        bench.settings.child('per_integration').setValue(True)

        assert bench.unit.value_units == 'A.U./ms'

    def test_both_are_classified_as_units(self, settings):
        assert settings.classify(settings.child('intensity_units')) == ('units', 'intensity_units')
        assert settings.classify(settings.child('per_integration')) == ('units', 'per_integration')

    def test_settings_loaded_from_an_older_file_have_defaults(self, settings):
        settings.param.child('per_integration').remove()
        settings.param.child('intensity_units').remove()
        settings.param.child('record', 'integration_time').remove()

        assert settings.intensity_units == 'A.U.' and not settings.per_integration
        assert settings.record_integration and settings.value_units == 'A.U.'


class TestTheIntegrationTimeInTheFile:
    def test_it_is_recorded_by_default_in_a_group_of_its_own(self, qapp):
        unit = make_unit(integration=True)

        recording = UnitRecording.from_unit(unit)
        dtes = build_dtes(recording, make_measurement(unit, 3, integration=250.))

        assert 'Unit1_Integration' in recording.titles
        integration = [dte for dte in dtes if dte.name == 'Unit1_Integration'][0][0]
        assert float(integration.data[0][0]) == 250. and integration.labels == ['integration time']
        assert str(integration.units) == 'ms'

    def test_it_is_recorded_by_default_in_the_settings_too(self, qapp):
        settings = UnitSettings(Parameter.create(**unit_definition(1, default_rois(1))), 1)

        assert settings.record_integration  # (unlike the full image, it costs nothing)

    def test_it_can_be_left_out(self, qapp):
        unit = make_unit(integration=False)

        assert 'Unit1_Integration' not in UnitRecording.from_unit(unit).titles

    def test_a_unit_which_only_records_the_integration_time_is_saved(self, qapp):
        unit = make_unit(integration=True)
        unit.settings.child('record', 'spectrum').setValue(False)
        unit.settings.child('record', 'temperature').setValue(False)
        unit.settings.set_rois([])

        recording = UnitRecording.from_unit(unit)

        assert recording.titles == ['Unit1_Integration'] and recording.is_saved

    def test_it_follows_the_changes_in_the_file(self, h5saver, qapp):
        unit = make_unit(integration=True)
        experiment = Experiment(h5saver, [unit])
        for index in range(6):
            experiment.write(unit, index, integration=20. if index < 3 else 40.)
        experiment.finish()

        with h5py.File(experiment.path, 'r') as f:
            group = titles(f['RawData/Datalogger000'])['Unit1_Integration']
            assert group['Data0D/CH00/EnlData00'][:, 0].tolist() == [20., 20., 20., 40., 40., 40.]
            assert attribute(group, 'integration_units') == 'ms'
            assert group['NavAxes/Axis00'][:].tolist() == [1000. + index for index in range(6)]  # (the time of the data)

    def test_swmr_starts_with_it(self, h5saver, qapp):
        unit = make_unit(integration=True)
        experiment = Experiment(h5saver, [unit])

        for index in range(2):
            experiment.write(unit, index, integration=20.)

        assert experiment.saver.swmr_started
        experiment.finish()


class TestUnitsInTheFile:
    def test_the_values_of_the_rois_have_their_units(self, qapp):
        unit = make_unit(rois=[Roi('a', 500., 510.)], units='V')

        rois = [dte for dte in build_dtes(UnitRecording.from_unit(unit), make_measurement(unit, 1))
                if dte.name == 'Unit1_ROIs'][0][0]

        assert str(rois.units) == 'V'

    def test_per_ms_when_they_were_divided(self, qapp):
        unit = make_unit(rois=[Roi('a', 500., 510.)], per_integration=True, units='counts')

        rois = [dte for dte in build_dtes(UnitRecording.from_unit(unit), make_measurement(unit, 1))
                if dte.name == 'Unit1_ROIs'][0][0]

        assert str(rois.units) == 'counts/ms'

    def test_arbitrary_units_are_no_unit_for_the_data_and_a_rate_when_divided(self, qapp):
        undivided = make_unit(rois=[Roi('a', 500., 510.)])
        divided = make_unit(rois=[Roi('a', 500., 510.)], per_integration=True)

        before = [dte for dte in build_dtes(UnitRecording.from_unit(undivided), make_measurement(undivided, 1))
                  if dte.name == 'Unit1_ROIs'][0][0]
        after = [dte for dte in build_dtes(UnitRecording.from_unit(divided), make_measurement(divided, 1))
                 if dte.name == 'Unit1_ROIs'][0][0]

        assert str(before.units) == '' and str(after.units) == '1/ms'

    def test_the_groups_name_the_arbitrary_units(self, h5saver, qapp):
        unit = make_unit(per_integration=True, integration=True)  # (the default unit)
        experiment = Experiment(h5saver, [unit])
        experiment.write(unit, 0, integration=20.)
        experiment.finish()

        with h5py.File(experiment.path, 'r') as f:
            groups = titles(f['RawData/Datalogger000'])
            assert attribute(groups['Unit1_ROIs'], 'values_units') == 'A.U./ms'
            assert attribute(groups['Unit1_ROIs'], 'intensity_units') == 'A.U.'
            assert attribute(groups['Unit1_Spectrum'], 'values_units') == 'A.U.'  # (the spectra are the other option)

    def test_the_full_spectrum_has_the_unit_of_the_user_if_the_plugin_gives_none(self, qapp):
        unit = make_unit(units='counts')

        spectrum = build_dtes(UnitRecording.from_unit(unit), make_measurement(unit, 0))[0][0]

        assert str(spectrum.units) == 'count' or str(spectrum.units) == 'counts'
        assert np.array_equal(spectrum.data[0], make_measurement(unit, 0).spectrum.y)  # (the numbers are not converted)

    def test_the_unit_the_plugin_gives_is_kept(self, qapp):
        unit = make_unit(units='counts')
        measurement = make_measurement(unit, 0)
        measurement.spectrum.dwa.force_units('V')

        spectrum = build_dtes(UnitRecording.from_unit(unit), measurement)[0][0]

        assert str(spectrum.units) == 'V'

    def test_a_unit_pint_does_not_know_is_dropped_in_the_data_but_kept_in_the_attributes(self, h5saver, qapp):
        unit = make_unit(units='photons per ms')
        experiment = Experiment(h5saver, [unit])
        experiment.write(unit, 0)
        experiment.finish()

        with h5py.File(experiment.path, 'r') as f:
            rois = titles(f['RawData/Datalogger000'])['Unit1_ROIs']
            assert attribute(rois, 'intensity_units') == 'photons per ms'
            assert attribute(rois, 'values_units') == 'photons per ms'

    def test_what_the_groups_say_of_their_units(self, h5saver, qapp):
        unit = make_unit(units='counts', per_integration=True, integration=True)
        experiment = Experiment(h5saver, [unit])
        experiment.write(unit, 0, integration=20.)
        experiment.finish()

        with h5py.File(experiment.path, 'r') as f:
            groups = titles(f['RawData/Datalogger000'])
            rois = groups['Unit1_ROIs']
            assert attribute(rois, 'values_units') == 'counts/ms' and attribute(rois, 'intensity_units') == 'counts'
            assert attribute(rois, 'per_integration') in (True, np.True_)
            assert attribute(groups['Unit1_Spectrum'], 'intensity_units') == 'counts'
            assert attribute(groups['Unit1_Temperature'], 'temperature_units') == '°C'

    @pytest.mark.parametrize('text, safe', [('counts', 'counts'), ('counts/ms', 'counts/ms'), ('V', 'V'), ('', ''),
                                            ('photons per ms', ''), ('no such unit', ''), ('a.u.', ''), ('A.U.', ''),
                                            ('au', ''), ('arbitrary units', ''), (' V ', 'V'),
                                            ('A.U./ms', '1/ms'), ('a.u./ms', '1/ms'), ('arbitrary units/ms', '1/ms'),
                                            ('no such unit/ms', '')])
    def test_pint_safe(self, text, safe):
        assert pint_safe(text) == safe

    def test_the_group_of_the_integration_time_is_named_so(self):
        assert saving.group_title(1, INTEGRATION) == 'Unit1_Integration'
