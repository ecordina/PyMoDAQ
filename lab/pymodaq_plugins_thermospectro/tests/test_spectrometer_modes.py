"""The ROIs of a spectrometer give what the ones of a camera give: a number made of their pixels (mean, sum, median, min, max,
standard deviation) or, as a crop, the part of the spectrum in their window. The full spectra and the crops can be divided by
the integration time before they are saved"""
import json
import math
import time

import h5py
import numpy as np
import pytest
from qtpy import QtCore

from pymodaq_gui.parameter import Parameter

from pymodaq_plugins_thermospectro import camera, export
from pymodaq_plugins_thermospectro.roi_manager import HINT, RoiManager, RoiRow
from pymodaq_plugins_thermospectro.rois import (AGGREGATIONS, CROP, DEFAULT_AGGREGATION, MODES, Roi, RoiStore, aggregate,
                                                default_rois, roi_value, window_mask)
from pymodaq_plugins_thermospectro.saving import UnitRecording, build_dtes, crop_title, divided_by, per_ms
from pymodaq_plugins_thermospectro.unit import UnitState
from pymodaq_plugins_thermospectro.unit_settings import UnitSettings, unit_definition
from pymodaq_plugins_thermospectro.views import UnitView
from pymodaq_plugins_thermospectro.widgets import SpectrumPlot

from test_export import read_csv, run_export, write_experiment
from test_saving import Experiment, X, attribute, h5saver, make_measurement, make_unit, titles  # noqa: F401
from test_unit import Bench

pytestmark = pytest.mark.filterwarnings('ignore')

AXIS = np.linspace(400., 900., 1001)  # (0.5 nm between two pixels)
SPECTRUM = AXIS - 400.  # (the intensity of a pixel is its wavelength minus 400 nm)


class TestTheSameChoiceAsThePixelsOfAnImage:
    def test_the_choice_is_the_same_object(self):
        assert camera.AGGREGATIONS is AGGREGATIONS and camera.CROP == CROP and camera.MODES is MODES
        assert camera.aggregate is aggregate and camera.DEFAULT_AGGREGATION == DEFAULT_AGGREGATION == 'mean'

    @pytest.mark.parametrize('aggregation', AGGREGATIONS)
    def test_the_value_of_a_window_is_the_aggregation_of_its_pixels(self, aggregation):
        mask = window_mask(AXIS, 500., 510.)

        value = roi_value(AXIS, SPECTRUM, 500., 510., aggregation)

        assert mask.sum() == 21 and value == pytest.approx(aggregate(SPECTRUM[mask], aggregation))

    def test_the_mean_is_what_it_was(self):
        assert roi_value(AXIS, SPECTRUM, 545., 555.) == pytest.approx(150.)

    def test_a_window_outside_has_no_value_whatever_the_aggregation(self):
        assert all(math.isnan(roi_value(AXIS, SPECTRUM, 1500., 1510., aggregation)) for aggregation in AGGREGATIONS)

    def test_an_unknown_aggregation_is_a_mistake_even_outside_of_the_spectrum(self):
        with pytest.raises(ValueError, match='mean, sum, median, min, max, std'):
            roi_value(AXIS, SPECTRUM, 1500., 1510., 'mode')

    def test_a_crop_is_not_a_way_to_make_a_number(self):
        with pytest.raises(ValueError):
            roi_value(AXIS, SPECTRUM, 500., 510., CROP)


class TestARoiOfASpectrum:
    def test_it_gives_its_mean_unless_told_otherwise(self):
        roi = Roi('a', 545., 555.)

        assert roi.aggregation == 'mean' and not roi.is_crop
        assert roi.value(AXIS, SPECTRUM) == pytest.approx(150.)

    @pytest.mark.parametrize('aggregation', AGGREGATIONS)
    def test_its_value_is_the_aggregation(self, aggregation):
        roi = Roi('a', 545., 555., aggregation=aggregation)

        assert roi.value(AXIS, SPECTRUM) == pytest.approx(aggregate(SPECTRUM[roi.mask(AXIS)], aggregation))

    def test_a_crop_has_no_value_but_a_window(self):
        roi = Roi('a', 545., 555., aggregation=CROP)

        assert roi.is_crop and math.isnan(roi.value(AXIS, SPECTRUM))
        assert AXIS[roi.mask(AXIS)].tolist() == [545. + 0.5 * k for k in range(21)]

    def test_the_label_says_what_it_gives(self):
        assert Roi('a', 545., 555.).label('nm', with_range=True) == 'a mean (545-555 nm)'
        assert Roi('a', 545., 555., aggregation='std').label('nm', with_range=True) == 'a std (545-555 nm)'
        assert Roi('a', 545., 555., aggregation=CROP).label('nm', with_range=True) == 'a crop (545-555 nm)'
        assert Roi('a', 545., 555., aggregation='sum').label() == 'a'  # (the plots have the name)

    def test_it_is_saved_and_read_back(self):
        roi = Roi('a', 545., 555., record=False, plot=False, aggregation='median')

        assert roi.to_dict()['aggregation'] == 'median' and Roi.from_dict(roi.to_dict()) == roi

    def test_a_roi_saved_before_it_existed_is_a_mean(self):
        assert Roi.from_dict({'name': 'a', 'start': 1, 'stop': 2}).aggregation == 'mean'

    def test_an_unknown_aggregation_in_a_file_is_a_mean(self):
        assert Roi.from_dict({'name': 'a', 'start': 1, 'stop': 2, 'aggregation': 'mode'}).aggregation == 'mean'

    @pytest.mark.parametrize('what', ['text', None, 3, [1, 2]])
    def test_what_is_not_a_roi_is_a_value_error(self, what):
        with pytest.raises(ValueError, match='Not a ROI'):
            Roi.from_dict(what)

    def test_it_is_not_valid_with_an_unknown_aggregation(self):
        assert 'is not one of' in Roi('a', 1., 2., aggregation='mode').problems()[0]

    def test_the_store_remembers_it(self, tmp_path):
        store = RoiStore(tmp_path / 'rois.json')
        rois = [Roi('a', 450., 460.), Roi('b', 500., 510., aggregation='sum'), Roi('c', 600., 620., aggregation=CROP)]

        store.save(1, rois)

        assert store.get(1) == rois

    def test_a_store_made_before_gives_means(self, tmp_path):
        (tmp_path / 'rois.json').write_text('{"units": {"unit1": {"rois": [{"name": "a", "start": 1, "stop": 2}]}}}')

        assert [roi.aggregation for roi in RoiStore(tmp_path / 'rois.json').get(1)] == ['mean']


class TestSettingsOfASpectrometer:
    @pytest.fixture
    def settings(self, qapp):
        settings = UnitSettings(Parameter.create(**unit_definition(1, default_rois(1))), 1)
        settings.set_rois([Roi('a', 450., 460.), Roi('b', 500., 510., aggregation='sum'),
                           Roi('c', 600., 620., aggregation=CROP)])
        return settings

    def test_each_roi_has_a_value_to_choose(self, settings):
        group = settings.child('rois', 'roi00')

        assert group.child('aggregation').type() == 'list' and group['aggregation'] == 'mean'
        assert list(group.child('aggregation').opts['limits']) == list(MODES)
        assert [settings.roi(key).aggregation for key in settings.roi_keys] == ['mean', 'sum', 'crop']

    def test_the_value_can_be_changed(self, settings):
        settings.child('rois', 'roi00', 'aggregation').setValue('max')

        assert settings.roi('roi00').aggregation == 'max'

    def test_a_tree_from_an_older_file_has_means(self, settings):
        settings.child('rois', 'roi01', 'aggregation').remove()

        assert settings.roi('roi01').aggregation == 'mean'

    def test_a_value_which_is_not_one_is_a_mean(self, settings):
        settings.child('rois', 'roi00', 'aggregation').setOpts(limits=['mean', 'sum', 'nonsense'])
        settings.child('rois', 'roi00', 'aggregation').setValue('nonsense')

        assert settings.roi('roi00').aggregation == 'mean'

    def test_a_change_is_classified(self, settings):
        assert settings.classify(settings.child('rois', 'roi01', 'aggregation')) == ('roi_aggregation', 'roi01')

    def test_a_new_roi_is_a_mean(self, settings):
        assert settings.roi(settings.add_roi(settings.new_roi(700.))).aggregation == 'mean'

    def test_the_values_are_locked_during_an_experiment_and_the_plot_boxes_are_not(self, settings):
        settings.lock(True)

        assert not settings.child('rois', 'roi00', 'aggregation').opts['enabled']
        assert settings.child('rois', 'roi00', 'plot').opts['enabled']
        assert not settings.child('spectrum_per_integration').opts['enabled']
        assert not settings.child('record', 'crop_interval').opts['enabled']

    def test_the_crops_are_saved_every_so_often(self, settings):
        assert settings.crop_interval == 0.0 and settings.child('record', 'crop_interval').title() == 'Crops every:'
        assert [child.name() for child in settings.child('record').children()] == [
            'spectrum', 'spectrum_interval', 'temperature', 'integration_time', 'crop_interval']

        settings.child('record', 'crop_interval').setValue(30.)

        assert settings.crop_interval == 30.

    def test_a_tree_from_an_older_file_saves_the_crops_at_each_measurement(self, settings):
        settings.child('record', 'crop_interval').remove()

        assert settings.crop_interval == 0.0

    def test_the_spectra_are_not_divided_unless_asked(self, settings):
        assert not settings.spectrum_per_integration and settings.spectrum_units == 'counts'
        assert settings.child('spectrum_per_integration').title() == 'Spectra per ms of integration:'

    def test_the_spectra_are_per_ms_once_divided(self, settings):
        settings.child('spectrum_per_integration').setValue(True)

        assert settings.spectrum_per_integration and settings.spectrum_units == 'counts/ms'
        assert settings.value_units == 'counts'  # (the values of the ROIs are another option)

    def test_the_two_options_are_independent(self, settings):
        settings.child('per_integration').setValue(True)
        settings.child('intensity_units').setValue('V')

        assert settings.value_units == 'V/ms' and settings.spectrum_units == 'V'
        settings.child('spectrum_per_integration').setValue(True)
        assert settings.spectrum_units == 'V/ms'

    def test_without_a_unit_it_is_still_per_ms(self, settings):
        settings.child('intensity_units').setValue('')
        settings.child('spectrum_per_integration').setValue(True)

        assert settings.spectrum_units == '1/ms'

    def test_it_is_classified_as_units(self, settings):
        assert settings.classify(settings.child('spectrum_per_integration')) == ('units', 'spectrum_per_integration')

    def test_a_tree_from_an_older_file_does_not_divide_the_spectra(self, settings):
        settings.param.child('spectrum_per_integration').remove()

        assert not settings.spectrum_per_integration and settings.spectrum_units == 'counts'


@pytest.fixture
def spectrometer_bench(qtbot):
    bench = Bench(qtbot)
    bench.settings.set_rois([Roi('Mean', 545., 555.), Roi('Sum', 545., 555., aggregation='sum'),
                             Roi('Window', 600., 620., aggregation=CROP)])
    yield bench
    if bench.unit.state != UnitState.IDLE:
        bench.unit._release()


class TestMeasuredByTheUnit:
    def test_each_roi_gives_what_it_is_told_to(self, spectrometer_bench):
        spectrometer_bench.run()
        spectrometer_bench.wait_for(2)

        values = spectrometer_bench.measurements[-1].values
        assert values[0] == pytest.approx(150.) and values[1] == pytest.approx(150. * 21)
        assert math.isnan(values[2])  # (the crop has no value)

    def test_the_curves_have_a_column_for_each_roi(self, spectrometer_bench):
        spectrometer_bench.run()
        spectrometer_bench.wait_for(2)

        assert spectrometer_bench.unit.series_rois.n_columns == 3

    def test_the_values_per_ms_do_not_depend_on_the_spectra_option(self, spectrometer_bench):
        spectrometer_bench.settings.child('per_integration').setValue(True)
        spectrometer_bench.run()
        spectrometer_bench.wait_for(2)

        values = spectrometer_bench.measurements[-1].values
        assert values[0] == pytest.approx(150. / 20.) and values[1] == pytest.approx(150. * 21 / 20.)
        assert spectrometer_bench.unit.value_units == 'counts/ms'

    def test_the_crops_are_due_at_each_measurement_unless_told_otherwise(self, spectrometer_bench):
        spectrometer_bench.unit.start_live(time.time())
        spectrometer_bench.wait_for(3)

        assert all(measurement.crop_due for measurement in spectrometer_bench.measurements)

    def test_the_crops_every_so_often(self, spectrometer_bench):
        spectrometer_bench.settings.child('record', 'crop_interval').setValue(0.35)
        spectrometer_bench.unit.start_live(time.time())
        spectrometer_bench.wait_for(8)

        due = [measurement.crop_due for measurement in spectrometer_bench.measurements]
        assert due[0] and not all(due) and sum(due) >= 2
        assert due[1] is False  # (the readout time is 0.1 s)

    def test_the_first_readout_of_an_experiment_has_its_crops(self, spectrometer_bench):
        spectrometer_bench.settings.child('record', 'crop_interval').setValue(3600.)
        spectrometer_bench.unit.start_live(time.time())
        spectrometer_bench.wait_for(2)
        assert [measurement.crop_due for measurement in spectrometer_bench.measurements[:2]] == [True, False]
        n = len(spectrometer_bench.measurements)

        spectrometer_bench.unit.begin_run(time.time(), 0.0)  # (the live view is on: the experiment starts the cadence again)
        spectrometer_bench.wait_for(n + 2)

        assert [measurement.crop_due for measurement in spectrometer_bench.measurements[n:n + 2]] == [True, False]


class TestCropsInTheFile:
    @pytest.fixture
    def unit(self, qapp):
        rois = [Roi('Mean', 545., 555.), Roi('Window', 500., 510., aggregation=CROP),
                Roi('Other', 600., 603., aggregation=CROP), Roi('Skipped', 700., 710., record=False, aggregation=CROP)]
        return make_unit(rois=rois, integration=True)

    def test_the_crops_are_groups_of_their_own(self, unit):
        recording = UnitRecording.from_unit(unit)

        assert recording.titles == ['Unit1_Spectrum', 'Unit1_ROIs', 'Unit1_Crop1', 'Unit1_Crop2', 'Unit1_Integration',
                                    'Unit1_Temperature', 'Unit1_Program']
        assert [roi.name for roi in recording.crops] == ['Window', 'Other']  # (not the one which is not recorded)
        assert [roi.name for roi in recording.rois] == ['Mean'] and recording.roi_indices == [0]
        assert crop_title(1, 2) == 'Unit1_Crop2'

    def test_the_part_of_the_spectrum_of_each_crop_is_built_with_its_wavelengths(self, unit):
        measurement = make_measurement(unit, 2, integration=50.)

        dtes = {dte.name: dte for dte in build_dtes(UnitRecording.from_unit(unit), measurement)}

        window = dtes['Unit1_Crop1'][0]
        assert window.origin == 'Spectro1' and window.name == 'Window'
        assert window.data[0].tolist() == [100. + 0.5 * k + 2. for k in range(21)]  # (x - 400 + readout)
        axis = window.get_axis_from_index(0)[0]
        assert axis.label == 'wavelength' and axis.units == 'nm'
        assert axis.get_data().tolist() == [500. + 0.5 * k for k in range(21)]
        assert dtes['Unit1_Crop2'][0].data[0].size == 7

    def test_a_crop_is_only_built_when_it_is_due(self, unit):
        measurement = make_measurement(unit, 0, integration=50.)
        measurement.crop_due = False

        names = [dte.name for dte in build_dtes(UnitRecording.from_unit(unit), measurement)]

        assert 'Unit1_Crop1' not in names and 'Unit1_ROIs' in names and 'Unit1_Spectrum' in names

    def test_the_labels_of_the_values_say_what_they_are(self, unit):
        rois = [dte for dte in build_dtes(UnitRecording.from_unit(unit), make_measurement(unit, 1, integration=50.))
                if dte.name == 'Unit1_ROIs'][0][0]

        assert rois.labels == ['Mean mean (545-555 nm)'] and len(rois.data) == 1

    def test_the_spectra_are_as_they_come_by_default(self, unit):
        measurement = make_measurement(unit, 3, integration=50.)

        spectrum = build_dtes(UnitRecording.from_unit(unit), measurement)[0][0]

        assert np.array_equal(spectrum.data[0], X - 400. + 3) and str(spectrum.units) == 'counts'

    def test_a_crop_outside_of_the_spectrum_stops_the_experiment_from_starting(self, unit):
        unit.spectrum = make_measurement(unit, 0).spectrum
        unit.settings.set_rois([Roi('Far', 1500., 1600., aggregation=CROP)])

        with pytest.raises(ValueError, match=r'the crop Far \(1500-1600 nm\) is outside of the spectrum \(400-900 nm\)'):
            UnitRecording.from_unit(unit)

    def test_a_crop_inside_of_the_spectrum_is_fine(self, unit):
        unit.spectrum = make_measurement(unit, 0).spectrum
        unit.settings.set_rois([Roi('In', 500., 510., aggregation=CROP)])

        assert len(UnitRecording.from_unit(unit).crops) == 1

    def test_without_a_spectrum_there_is_nothing_to_check(self, unit):
        unit.spectrum = None
        unit.settings.set_rois([Roi('Far', 1500., 1600., aggregation=CROP)])

        assert len(UnitRecording.from_unit(unit).crops) == 1

    def test_what_the_file_holds(self, h5saver, unit):
        experiment = Experiment(h5saver, [unit])
        for index in range(4):
            experiment.write(unit, index, integration=50.)
        assert experiment.saver.swmr_started  # (every group, the crops too, had its first data)
        experiment.finish()

        with h5py.File(experiment.path, 'r') as f:
            groups = titles(f['RawData/Datalogger000'])
            window = groups['Unit1_Crop1']
            assert window['Data1D/CH00/EnlData00'].shape == (4, 21)
            assert window['Data1D/CH00/Axis00'][:].tolist() == [500. + 0.5 * k for k in range(21)]
            assert groups['Unit1_Crop2']['Data1D/CH00/EnlData00'].shape == (4, 7)
            assert window['NavAxes/Axis00'][:].tolist() == [1000., 1001., 1002., 1003.]
            roi = json.loads(attribute(window, 'roi'))
            assert roi == {'name': 'Window', 'start': 500.0, 'stop': 510.0, 'aggregation': 'crop'}
            assert attribute(window, 'spectrometer') == 'Spectro1' and attribute(window, 'intensity_units') == 'counts'
            assert [json.loads(attribute(groups['Unit1_ROIs'], 'rois'))[0]['aggregation']] == ['mean']


class TestDividedByTheIntegrationTime:
    def test_the_full_spectrum_is_divided_and_its_units_say_so(self, qapp):
        unit = make_unit(spectrum_per_integration=True)
        measurement = make_measurement(unit, 3, integration=50.)

        spectrum = build_dtes(UnitRecording.from_unit(unit), measurement)[0][0]

        assert np.allclose(spectrum.data[0], (X - 400. + 3) / 50.) and str(spectrum.units) == 'counts/ms'
        assert np.array_equal(measurement.spectrum.dwa.data[0], X - 400. + 3)  # (what was measured is not touched)

    def test_so_is_the_part_of_the_spectrum_of_a_crop(self, qapp):
        unit = make_unit(rois=[Roi('Window', 500., 510., aggregation=CROP)], spectrum_per_integration=True)

        window = [dte for dte in build_dtes(UnitRecording.from_unit(unit), make_measurement(unit, 2, integration=50.))
                  if dte.name == 'Unit1_Crop1'][0][0]

        assert np.allclose(window.data[0], [(100. + 0.5 * k + 2.) / 50. for k in range(21)])
        assert str(window.units) == 'counts/ms'

    def test_the_wavelengths_of_a_crop_are_not_divided(self, qapp):
        unit = make_unit(rois=[Roi('Window', 500., 510., aggregation=CROP)], spectrum_per_integration=True)

        window = [dte for dte in build_dtes(UnitRecording.from_unit(unit), make_measurement(unit, 2, integration=50.))
                  if dte.name == 'Unit1_Crop1'][0][0]

        assert window.get_axis_from_index(0)[0].get_data().tolist() == [500. + 0.5 * k for k in range(21)]

    def test_each_spectrum_is_divided_by_the_time_of_its_readout(self, qapp):
        unit = make_unit(spectrum_per_integration=True)
        recording = UnitRecording.from_unit(unit)

        first = build_dtes(recording, make_measurement(unit, 0, integration=20.))[0][0]
        second = build_dtes(recording, make_measurement(unit, 0, integration=40.))[0][0]

        assert np.allclose(first.data[0], 2 * second.data[0])

    def test_without_an_integration_time_the_arrays_are_nan_and_nothing_is_invented(self, qapp):
        unit = make_unit(rois=[Roi('Window', 500., 510., aggregation=CROP)], spectrum_per_integration=True)
        measurement = make_measurement(unit, 0)  # (its integration time is nan)

        dtes = {dte.name: dte for dte in build_dtes(UnitRecording.from_unit(unit), measurement)}

        assert np.isnan(dtes['Unit1_Spectrum'][0].data[0]).all() and np.isnan(dtes['Unit1_Crop1'][0].data[0]).all()
        assert dtes['Unit1_Spectrum'][0].data[0].shape == (1001,) and dtes['Unit1_Crop1'][0].data[0].shape == (21,)

    def test_a_time_of_zero_is_not_a_time(self):
        assert np.isnan(divided_by(np.ones(3), 0.)).all() and np.isnan(divided_by(np.ones(3), -5.)).all()
        assert divided_by(np.array([2., 4.]), 2.).tolist() == [1., 2.]

    def test_the_units_the_plugin_gives_are_kept_and_divided(self, qapp):
        unit = make_unit(spectrum_per_integration=True)
        measurement = make_measurement(unit, 0, integration=50.)
        measurement.spectrum.dwa.force_units('V')

        spectrum = build_dtes(UnitRecording.from_unit(unit), measurement)[0][0]

        assert str(spectrum.units) == 'V/ms'

    def test_the_values_of_the_rois_are_not_touched_by_the_option_of_the_spectra(self, qapp):
        unit = make_unit(rois=[Roi('a', 500., 510.)], spectrum_per_integration=True)

        rois = [dte for dte in build_dtes(UnitRecording.from_unit(unit), make_measurement(unit, 1, integration=50.))
                if dte.name == 'Unit1_ROIs'][0][0]

        assert float(rois.data[0][0]) == 100. and str(rois.units) == 'counts'  # (the value was given, not divided here)

    def test_per_ms(self):
        assert per_ms('counts') == 'counts/ms' and per_ms('') == '1/ms'

    def test_what_each_group_says_of_its_units(self, h5saver, qapp):
        unit = make_unit(rois=[Roi('a', 500., 510.), Roi('Window', 520., 530., aggregation=CROP)], per_integration=False,
                         spectrum_per_integration=True, integration=True)
        experiment = Experiment(h5saver, [unit])
        experiment.write(unit, 0, integration=20.)
        experiment.finish()

        with h5py.File(experiment.path, 'r') as f:
            groups = titles(f['RawData/Datalogger000'])
            for title in ('Unit1_Spectrum', 'Unit1_Crop1'):
                assert attribute(groups[title], 'values_units') == 'counts/ms'
                assert attribute(groups[title], 'per_integration') in (True, np.True_)
            assert attribute(groups['Unit1_ROIs'], 'values_units') == 'counts'  # (the values: the other option)
            assert attribute(groups['Unit1_ROIs'], 'per_integration') in (False, np.False_)
            spectrum = groups['Unit1_Spectrum']['Data1D/CH00/EnlData00'][0]
            assert np.allclose(spectrum, (X - 400.) / 20.)


class TestTheManagerOfASpectrometer:
    @pytest.fixture
    def manager(self, qtbot):
        widget = RoiManager()
        qtbot.addWidget(widget)
        widget.show()
        rois = [Roi('A', 450., 460.), Roi('B', 500., 510., aggregation='sum'), Roi('C', 600., 610., aggregation=CROP)]
        widget.set_rois(['roi00', 'roi01', 'roi02'], rois, ['#1f77b4', '#ff7f0e', '#2ca02c'], 'nm', (400., 900.), 0.5)
        return widget

    def test_a_column_says_what_each_roi_gives(self, manager):
        assert manager.header_text('aggregation') == 'Value'
        assert [manager.row(key).aggregation.currentText() for key in manager.keys] == ['mean', 'sum', 'crop']
        row = manager.row('roi00')
        assert [row.aggregation.itemText(i) for i in range(row.aggregation.count())] == list(MODES)

    def test_choosing_one_says_so(self, manager, qtbot):
        row = manager.row('roi00')

        with qtbot.waitSignal(manager.aggregation_edited) as signal:
            row.aggregation.setCurrentIndex(MODES.index('median'))
            row.aggregation.activated.emit(MODES.index('median'))  # (a click on an item emits it)

        assert signal.args == ['roi00', 'median']

    def test_showing_a_roi_says_nothing(self, manager, qtbot):
        with qtbot.assertNotEmitted(manager.aggregation_edited):
            manager.set_rois(['roi00'], [Roi('A', 450., 460., aggregation='max')], ['#1f77b4'], 'nm', (400., 900.), 0.5)

        assert manager.row('roi00').aggregation.currentText() == 'max'

    def test_a_crop_has_nothing_to_plot(self, manager):
        crop = manager.row('roi02')

        assert not crop.plot.isChecked() and not crop.plot.isEnabled() and 'no value to plot' in crop.plot.toolTip()
        assert manager.row('roi00').plot.isEnabled()

    def test_it_can_be_plotted_again_once_it_gives_a_number(self, manager):
        manager.set_rois(['roi02'], [Roi('C', 600., 610., plot=True)], ['#2ca02c'], 'nm', (400., 900.), 0.5)

        assert manager.row('roi02').plot.isEnabled() and manager.row('roi02').plot.isChecked()

    def test_locked_during_an_experiment(self, manager):
        manager.set_locked(True)

        assert not manager.row('roi00').aggregation.isEnabled()
        manager.set_locked(False)
        assert manager.row('roi00').aggregation.isEnabled()

    def test_the_hint_tells_about_the_value(self):
        assert 'crop' in HINT and 'median' in HINT and 'part of the spectrum' in HINT

    def test_the_tips_are_there(self, manager):
        assert 'crop' in RoiRow.VALUE_TIP and manager.row('roi00').aggregation.toolTip() == RoiRow.VALUE_TIP


class TestTheWindowsOnTheSpectrum:
    @pytest.fixture
    def plot(self, qtbot):
        widget = SpectrumPlot('Spectro1')
        qtbot.addWidget(widget)
        widget.resize(400, 200)
        widget.show()
        widget.set_spectrum(AXIS, SPECTRUM, 'nm', 'wavelength')
        return widget

    def test_a_crop_is_dashed(self, plot):
        plot.set_roi('roi00', 500., 510., '#1f77b4', '1', crop=True)
        plot.set_roi('roi01', 600., 610., '#1f77b4', '2')

        assert all(line.pen.style() == QtCore.Qt.PenStyle.DashLine for line in plot._regions['roi00'].lines)
        assert all(line.pen.style() == QtCore.Qt.PenStyle.SolidLine for line in plot._regions['roi01'].lines)

    def test_a_window_changes_its_style_with_its_value(self, plot):
        plot.set_roi('roi00', 500., 510., '#1f77b4', '1')

        plot.set_roi('roi00', 500., 510., '#1f77b4', '1', crop=True)
        assert all(line.pen.style() == QtCore.Qt.PenStyle.DashLine for line in plot._regions['roi00'].lines)

        plot.set_roi('roi00', 500., 510., '#1f77b4', '1')
        assert all(line.pen.style() == QtCore.Qt.PenStyle.SolidLine for line in plot._regions['roi00'].lines)

    def test_a_crop_dragged_to_another_wavelength_stays_dashed(self, plot):
        plot.set_roi('roi00', 500., 510., '#1f77b4', '1', crop=True)

        plot.set_roi_color('roi00', '#ff0000')

        lines = plot._regions['roi00'].lines
        assert all(line.pen.style() == QtCore.Qt.PenStyle.DashLine for line in lines)
        assert plot.region_color('roi00') == '#ff0000'

    def test_a_removed_window_is_forgotten(self, plot):
        plot.set_roi('roi00', 500., 510., '#1f77b4', '1', crop=True)

        plot.remove_roi('roi00')
        plot.set_roi('roi00', 500., 510., '#1f77b4', '1')

        assert all(line.pen.style() == QtCore.Qt.PenStyle.SolidLine for line in plot._regions['roi00'].lines)


class TestTheViewOfASpectrometer:
    @pytest.fixture
    def view(self, qtbot, spectrometer_bench):
        view = UnitView(spectrometer_bench.unit)
        qtbot.addWidget(view.spectrum_plot)
        view.update_definitions()
        return view

    def test_a_crop_has_no_curve_and_no_button(self, view):
        assert [curve.name() for curve in view.curves_plot.curves] == ['Mean (mean)', 'Sum (sum)']
        assert view.toggles.names == ['roi00', 'roi01', 'temperature']  # (not roi02, the crop)

    def test_the_window_of_a_crop_is_dashed_and_the_manager_shows_its_value(self, view):
        assert all(line.pen.style() == QtCore.Qt.PenStyle.DashLine for line in view.spectrum_plot._regions['roi02'].lines)
        assert view.manager.row('roi02').aggregation.currentText() == 'crop'

    def test_the_legend_has_the_names_when_all_curves_are_the_same(self, view, spectrometer_bench):
        spectrometer_bench.settings.child('rois', 'roi01', 'aggregation').setValue('mean')

        view.update_definitions()

        assert [curve.name() for curve in view.curves_plot.curves] == ['Mean', 'Sum']
        assert view.value_label(spectrometer_bench.settings.rois[:2])[0] == 'ROIs: mean of the pixels'

    @pytest.mark.parametrize('aggregation, text', [
        ('mean', 'mean'), ('sum', 'sum'), ('median', 'median'), ('min', 'minimum'), ('max', 'maximum'),
        ('std', 'standard deviation')])
    def test_the_axis_says_what_the_curves_are(self, view, spectrometer_bench, aggregation, text):
        spectrometer_bench.settings.set_rois([Roi('A', 545., 555., aggregation=aggregation)])

        view.update_definitions()

        assert view.curves_plot.getPlotItem().getAxis('left').labelText == f'ROIs: {text} of the pixels'

    def test_the_axis_has_the_units_of_the_values(self, view, spectrometer_bench):
        assert view.curves_plot.getPlotItem().getAxis('left').labelUnits == 'counts'

        spectrometer_bench.settings.child('per_integration').setValue(True)
        view.update_definitions()

        assert view.curves_plot.getPlotItem().getAxis('left').labelUnits == 'counts/ms'

    def test_a_choice_in_the_manager_goes_to_the_settings_through_the_view(self, view, qtbot):
        with qtbot.waitSignal(view.roi_field_edited) as signal:
            view.manager.row('roi00').aggregation.setCurrentIndex(MODES.index('max'))
            view.manager.row('roi00').aggregation.activated.emit(MODES.index('max'))

        assert signal.args == [1, 'roi00', 'aggregation', 'max']

    def test_the_curves_of_the_crops_are_not_drawn_and_the_others_are(self, view, spectrometer_bench):
        spectrometer_bench.run()
        spectrometer_bench.qtbot.waitUntil(lambda: len(spectrometer_bench.unit.series_rois) >= 3, timeout=10000)  # (the
        # readouts of the live view before the start are not in the curves)

        view.refresh()

        assert len(view.curves_plot.curves) == 2 and len(view.curves_plot.curves[1].getData()[0]) >= 3


class TestExportOfASpectrometer:
    ROIS = [Roi('Mean', 500., 510.), Roi('Sum', 500., 510., aggregation='sum'),
            Roi('Max', 500., 510., aggregation='max'), Roi('Window', 520., 530., aggregation=CROP)]

    def test_each_roi_says_what_it_gives_and_a_crop_has_no_column(self, h5saver, qapp, tmp_path):
        path = write_experiment(h5saver, camera=False, spectrometer_rois=self.ROIS)

        out = run_export(path, tmp_path, '--no-spectra')

        header, _ = read_csv(out / f'{path.stem}_spectrometer1.csv')
        assert header == ['time (s)', 'temperature (°C)', 'Mean mean (500-510 nm) [counts]',
                          'Sum sum (500-510 nm) [counts]', 'Max max (500-510 nm) [counts]']

    def test_the_crop_is_a_csv_of_its_own_with_the_wavelengths_of_its_window(self, h5saver, qapp, tmp_path):
        path = write_experiment(h5saver, camera=False, spectrometer_rois=self.ROIS, integration=lambda index: 50.)

        out = run_export(path, tmp_path)

        header, rows = read_csv(out / f'{path.stem}_spectrometer1_crop_Window.csv')
        assert header[:4] == ['time (s)', 'temperature (°C)', 'integration time (ms)', '520.000 nm']
        assert header[-1] == '530.000 nm' and len(header) == 3 + 21 and len(rows) == 6
        assert [row[0] for row in rows] == pytest.approx([0., 1., 2., 3., 4., 5.])
        assert [row[1] for row in rows] == pytest.approx([20., 21., 22., 23., 24., 25.])
        assert rows[3][3] == pytest.approx(120. + 3.) and rows[3][-1] == pytest.approx(130. + 3.)  # (x - 400 + readout)

    def test_a_spectrometer_with_a_crop_is_still_a_spectrometer(self, h5saver, qapp, tmp_path):
        path = write_experiment(h5saver, camera=False, spectrometer_rois=self.ROIS)

        with h5py.File(path, 'r') as file:
            unit = export.read_experiment(file)[1]

            assert unit.kind == 'spectrometer' and not unit.is_camera and len(unit.crops) == 1

    def test_the_names_of_the_files(self, h5saver, qapp, tmp_path):
        path = write_experiment(h5saver, camera=False, spectrometer_rois=self.ROIS)

        out = run_export(path, tmp_path)

        assert sorted(file.name for file in out.iterdir()) == sorted(
            f'{path.stem}{suffix}' for suffix in ('_spectrometer1.csv', '_spectrometer1_spectra.csv',
                                                  '_spectrometer1_crop_Window.csv', '_experiment.txt'))

    def test_the_report_says_what_each_roi_is(self, h5saver, qapp, tmp_path):
        path = write_experiment(h5saver, camera=False, spectrometer_rois=self.ROIS)

        out = run_export(path, tmp_path)

        text = (out / f'{path.stem}_experiment.txt').read_text(encoding='utf-8-sig')
        assert 'Mean: mean of the pixels in 500-510 nm' in text and 'Sum: sum of the pixels in 500-510 nm' in text
        assert 'Max: maximum of the pixels in 500-510 nm' in text
        assert f'Window: crop of the spectrum in 520-530 nm -> {path.stem}_spectrometer1_crop_Window.csv' in text

    def test_the_crops_follow_the_spectra_option(self, h5saver, qapp, tmp_path):
        path = write_experiment(h5saver, camera=False, spectrometer_rois=self.ROIS)

        out = run_export(path, tmp_path, '--no-spectra')

        assert (out / f'{path.stem}_spectrometer1_crop_Window.csv').exists()  # (--no-spectra is about the full spectra)

    def test_the_name_of_a_crop_makes_a_name_of_file(self, h5saver, qapp, tmp_path):
        path = write_experiment(h5saver, camera=False,
                                spectrometer_rois=[Roi('Peak / left: 1', 520., 530., aggregation=CROP)])

        out = run_export(path, tmp_path)

        assert (out / f'{path.stem}_spectrometer1_crop_Peak_left_1.csv').exists()

    def test_a_crop_can_be_divided_here(self, h5saver, qapp, tmp_path):
        path = write_experiment(h5saver, camera=False, spectrometer_rois=self.ROIS, integration=lambda index: 100. * (index + 1))

        out = run_export(path, tmp_path, '--per-integration-time')

        _, rows = read_csv(out / f'{path.stem}_spectrometer1_crop_Window.csv')
        assert rows[3][3] == pytest.approx((120. + 3.) / 400.) and rows[0][3] == pytest.approx(120. / 100.)


class TestSpectraDividedInTheApp:
    ROIS = [Roi('Window', 520., 530., aggregation=CROP)]

    def test_the_spectra_and_the_crops_are_as_the_file_has_them(self, h5saver, qapp, tmp_path):
        path = write_experiment(h5saver, camera=False, spectrometer_rois=self.ROIS, integration=lambda index: 50.,
                                spectrum_per_integration=True)

        out = run_export(path, tmp_path)

        _, spectra = read_csv(out / f'{path.stem}_spectrometer1_spectra.csv')
        _, crops = read_csv(out / f'{path.stem}_spectrometer1_crop_Window.csv')
        assert spectra[3][4] == pytest.approx((0.5 + 3.) / 50.)  # (the second pixel: x - 400 + readout, per ms)
        assert crops[3][4] == pytest.approx((120. + 0.5 + 3.) / 50.)

    def test_they_are_not_divided_twice(self, h5saver, qapp, tmp_path, capsys):
        path = write_experiment(h5saver, camera=False, spectrometer_rois=self.ROIS, integration=lambda index: 50.,
                                spectrum_per_integration=True)

        out = run_export(path, tmp_path, '--per-integration-time')

        _, spectra = read_csv(out / f'{path.stem}_spectrometer1_spectra.csv')
        _, crops = read_csv(out / f'{path.stem}_spectrometer1_crop_Window.csv')
        assert spectra[3][4] == pytest.approx((0.5 + 3.) / 50.) and crops[3][4] == pytest.approx((120. + 0.5 + 3.) / 50.)
        told = capsys.readouterr().err
        assert 'the full spectra were divided by the integration time when they were measured (counts/ms)' in told
        assert 'the part of the spectrum of the crop Window were divided' in told

    def test_the_report_and_the_units_say_it(self, h5saver, qapp, tmp_path):
        path = write_experiment(h5saver, camera=False, spectrometer_rois=self.ROIS, integration=lambda index: 50.,
                                spectrum_per_integration=True)

        out = run_export(path, tmp_path)

        text = (out / f'{path.stem}_experiment.txt').read_text(encoding='utf-8-sig')
        assert 'the full spectra were divided by the integration time in the extension (counts/ms)' in text
        assert 'intensity (spectrometer 1): counts; values of the ROIs: counts' not in text  # (no ROIs which are numbers)
        assert 'full spectra: counts/ms' in text and 'crops: counts/ms' in text

    def test_the_values_of_the_rois_are_the_other_option(self, h5saver, qapp, tmp_path):
        path = write_experiment(h5saver, camera=False, integration=lambda index: 50., spectrum_per_integration=True)

        out = run_export(path, tmp_path)

        header, _ = read_csv(out / f'{path.stem}_spectrometer1.csv')
        assert header[3] == 'A mean (500-510 nm) [counts]'  # (not divided: only the spectra were)
        text = (out / f'{path.stem}_experiment.txt').read_text(encoding='utf-8-sig')
        assert 'values of the ROIs: counts; full spectra: counts/ms' in text

    def test_a_file_made_before_the_spectra_could_be_divided_is_not_said_to_be(self, h5saver, qapp, tmp_path):
        path = write_experiment(h5saver, camera=False, integration=lambda index: 50.)
        with h5py.File(path, 'r+') as file:
            def forget(name, node):
                for attribute_name in ('values_units', 'per_integration'):
                    if attribute_name in node.attrs:
                        del node.attrs[attribute_name]

            file['RawData/Datalogger000'].visititems(forget)

        out = run_export(path, tmp_path, '--per-integration-time')

        _, spectra = read_csv(out / f'{path.stem}_spectrometer1_spectra.csv')
        assert spectra[3][4] == pytest.approx((0.5 + 3.) / 50.)  # (divided here: nothing says it was)
