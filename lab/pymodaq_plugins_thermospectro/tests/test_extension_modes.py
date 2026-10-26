"""The extension on Mock modules: the integration time (saved, changed during an experiment, dividing the values), the
units, the ROIs of a camera or of a spectrometer which are a crop or give another aggregation than the mean, and the full
spectra divided by the integration time"""
import json

import h5py
import numpy as np
import pytest
from qtpy import QtCore

from pymodaq_plugins_thermospectro.camera import CROP, MODES, RectRoi
from pymodaq_plugins_thermospectro.rois import Roi
from pymodaq_plugins_thermospectro import export

from test_export import read_csv
from test_extension import H5Experiment, end_run, run, ui_enabled, wait_for, wait_for_end

pytestmark = pytest.mark.filterwarnings('ignore')

SLOW = dict(velocity=30.)  # (6 s for the ramp)


def record_integration(ext, on=True):
    for unit in ext.units:
        unit.settings.child('record', 'integration_time').setValue(on)


def integration_series(experiment, number=1) -> list[float]:
    return experiment.zero_d(f'Unit{number}_Integration').tolist()


class TestTheIntegrationTimeIsSaved:
    def test_it_is_a_group_of_each_unit_with_its_units(self, ext, qtbot, tmp_path):
        record_integration(ext)

        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: all(unit.n_measurements >= 4 for unit in ext.units[:2]))
        end_run(qtbot, ext)

        with H5Experiment(tmp_path) as experiment:
            assert 'Unit1_Integration' in experiment.titles and 'Unit2_Integration' in experiment.titles
            values = integration_series(experiment)
            assert len(values) >= 3 and set(values) == {20.}
            assert experiment.attribute('Unit1_Integration', 'integration_units') == 'ms'
            assert experiment.label('Unit1_Integration', 0) == 'integration time'
            assert experiment.zero_d('Unit1_Integration').shape == experiment.zero_d('Unit1_Temperature').shape

    def test_it_is_not_there_when_it_is_left_out(self, ext, qtbot, tmp_path):
        run(qtbot, ext, **SLOW)
        end_run(qtbot, ext)

        with H5Experiment(tmp_path) as experiment:
            assert not [title for title in experiment.titles if 'Integration' in title]

    def test_it_is_recorded_by_default(self, qapp):
        from pymodaq_gui.parameter import Parameter
        from pymodaq_plugins_thermospectro.rois import default_rois
        from pymodaq_plugins_thermospectro.unit_settings import UnitSettings, unit_definition

        assert UnitSettings(Parameter.create(**unit_definition(1, default_rois(1))), 1).record_integration


class TestChangedWhileItRuns:
    def test_the_field_stays_usable_and_the_others_are_locked(self, ext, qtbot):
        run(qtbot, ext, **SLOW)
        try:
            settings = ext.unit(1).settings
            assert ui_enabled(settings.child('integration_time'))
            assert not ui_enabled(settings.child('readout_time')) and not ui_enabled(settings.child('per_integration'))
            assert not ui_enabled(settings.child('intensity_units'))
        finally:
            end_run(qtbot, ext)
        assert ui_enabled(ext.unit(1).settings.child('per_integration'))

    def test_the_file_follows_the_changes(self, ext, qtbot, tmp_path):
        record_integration(ext)
        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: ext.unit(1).n_measurements >= 3)

        ext.unit(1).settings.child('integration_time').setValue(30.)
        wait_for(qtbot, lambda: ext.unit(1).integration_time_now() == 30.)
        done = ext.unit(1).n_measurements
        wait_for(qtbot, lambda: ext.unit(1).n_measurements >= done + 3)
        ext.unit(1).settings.child('integration_time').setValue(25.)
        wait_for(qtbot, lambda: ext.unit(1).integration_time_now() == 25.)
        done = ext.unit(1).n_measurements
        wait_for(qtbot, lambda: ext.unit(1).n_measurements >= done + 3)
        end_run(qtbot, ext)

        with H5Experiment(tmp_path) as experiment:
            values = integration_series(experiment)
            steps = [values[0]] + [after for before, after in zip(values, values[1:]) if after != before]
            assert steps == [20., 30., 25.]  # (changed twice, each between two readouts)
            assert set(integration_series(experiment, 2)) == {20.}  # (the other unit was not touched)

    def test_a_time_longer_than_the_readout_time_is_refused_and_told(self, ext, qtbot, tmp_path):
        record_integration(ext)
        messages = []
        ext.status_signal.connect(messages.append)
        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: ext.unit(1).n_measurements >= 2)

        ext.unit(1).settings.child('integration_time').setValue(500.)  # (the readout time is 0.1 s)
        wait_for(qtbot, lambda: any('not shorter than the readout time' in message for message in messages))
        wait_for(qtbot, lambda: ext.unit(1).settings.integration_time == 20.)
        end_run(qtbot, ext)

        with H5Experiment(tmp_path) as experiment:
            assert set(integration_series(experiment)) == {20.}


class TestDividedByTheIntegrationTime:
    def test_the_plot_says_the_values_are_per_ms(self, ext):
        axis = ext.view(1).curves_plot.getPlotItem().getAxis('left')
        assert axis.labelUnits == 'A.U.'

        ext.unit(1).settings.child('per_integration').setValue(True)

        assert axis.labelUnits == 'A.U./ms'
        ext.unit(1).settings.child('intensity_units').setValue('V')
        assert axis.labelUnits == 'V/ms'

    def test_the_curves_start_again_when_the_option_changes(self, ext, qtbot):
        ext.start_monitoring()
        wait_for(qtbot, lambda: len(ext.unit(1).series_rois) >= 3)

        ext.unit(1).settings.child('per_integration').setValue(True)

        assert len(ext.unit(1).series_rois) < 3  # (what was drawn was not per ms)
        ext.stop_monitoring()

    def test_the_file_says_it(self, ext, qtbot, tmp_path):
        record_integration(ext)
        ext.unit(1).settings.child('per_integration').setValue(True)
        ext.unit(1).settings.child('intensity_units').setValue('counts')

        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: all(unit.n_measurements >= 2 for unit in ext.units[:2]))
        end_run(qtbot, ext)

        with H5Experiment(tmp_path) as experiment:
            assert experiment.attribute('Unit1_ROIs', 'values_units') == 'counts/ms'
            assert experiment.attribute('Unit1_ROIs', 'per_integration') in (True, 'True')
            assert experiment.attribute('Unit2_ROIs', 'values_units') == 'A.U.'  # (the other unit: not divided, default unit)
            assert str(experiment.attribute('Unit1_Spectrum', 'intensity_units')) == 'counts'

    def test_the_options_cannot_be_changed_while_it_runs(self, ext, qtbot):
        run(qtbot, ext, **SLOW)
        try:
            assert not ui_enabled(ext.unit(1).settings.child('per_integration'))
        finally:
            end_run(qtbot, ext)


@pytest.mark.parametrize('dashboard', ['camera_only'], indirect=True)
class TestCropsOfACamera:
    """A camera and its thermostat alone: the ROIs of the camera are 8 x 8 pixels squares of an image of 200 x 100"""

    def choose(self, ext, key, aggregation):
        row = ext.view(3).manager.row(key)
        row.aggregation.setCurrentIndex(MODES.index(aggregation))
        row.aggregation.activated.emit(MODES.index(aggregation))  # (what a click on the list does)

    def test_the_value_of_a_roi_is_chosen_in_the_manager(self, ext):
        self.choose(ext, 'roi00', 'sum')
        self.choose(ext, 'roi01', CROP)

        settings = ext.unit(3).settings
        assert settings.roi('roi00').aggregation == 'sum' and settings.roi('roi01').is_crop
        assert settings.child('rois', 'roi00', 'aggregation').value() == 'sum'

    def test_the_value_is_remembered_with_the_rois(self, ext, tmp_path):
        self.choose(ext, 'roi01', 'median')
        ext.save_rois()

        saved = json.loads((tmp_path / 'remembered' / 'rois.json').read_text())['units']['unit3']['rois']

        assert [roi['aggregation'] for roi in saved][:2] == ['mean', 'median']

    def test_a_crop_has_no_curve_but_a_dashed_rectangle(self, ext):
        self.choose(ext, 'roi01', CROP)

        view = ext.view(3)
        assert len(view.curves_plot.curves) == 2 and 'roi01' not in view.toggles.names
        assert view.spectrum_plot._rois['roi01'].pen.style() == QtCore.Qt.PenStyle.DashLine

    def test_the_values_which_are_numbers_are_saved_with_their_aggregation_and_the_crop_as_images(self, ext, qtbot,
                                                                                                tmp_path):
        self.choose(ext, 'roi00', 'sum')
        self.choose(ext, 'roi01', CROP)

        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: ext.unit(3).n_measurements >= 4)
        end_run(qtbot, ext)

        with H5Experiment(tmp_path) as experiment:
            assert 'Unit3_Crop1' in experiment.titles and 'Unit3_ROIs' in experiment.titles
            labels = [experiment.label('Unit3_ROIs', index) for index in range(experiment.n_zero_d('Unit3_ROIs'))]
            assert labels[0].startswith('ROI 1 sum (x 10-18, y 5-13') and labels[1].startswith('ROI 3 mean')
            images = experiment.modules['Unit3_Crop1']['Data2D']['CH00']['EnlData00'][:]
            rois = ext.unit(3).settings
            width, height = int(rois.roi('roi01').width), int(rois.roi('roi01').height)
            assert images.ndim == 3 and images.shape[1:] == (height, width) and images.shape[0] >= 3
            assert images.shape[0] == len(experiment.time('Unit3_Crop1'))
            roi = json.loads(experiment.attribute('Unit3_Crop1', 'roi'))
            assert roi['name'] == 'ROI 2' and roi['aggregation'] == CROP

    def test_the_converter_makes_a_tif_of_the_crop_and_a_csv_of_the_values(self, ext, qtbot, tmp_path):
        tifffile = pytest.importorskip('tifffile')
        self.choose(ext, 'roi00', 'max')
        self.choose(ext, 'roi01', CROP)
        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: ext.unit(3).n_measurements >= 4)
        end_run(qtbot, ext)
        path = next((tmp_path / 'data').rglob('*.h5'))

        assert export.main([str(path), '-o', str(tmp_path / 'out')]) == 0

        out = tmp_path / 'out'
        with tifffile.TiffFile(out / f'{path.stem}_camera_crop_ROI_2.tif') as tif:
            frames = tif.asarray()
        with h5py.File(path, 'r') as file:
            n_crops = len(export.read_experiment(file)[3].crops[0])
        assert frames.shape[0] == n_crops >= 3 and frames.shape[1:] == (8, 8)
        header = read_csv(out / f'{path.stem}_camera.csv')[0]
        assert header[0] == 'time (s)' and any(name.startswith('ROI 1 max') for name in header)
        assert not any('ROI 2' in name for name in header)  # (a crop has no column)
        assert 'ROI 2' in (out / f'{path.stem}_experiment.txt').read_text(encoding='utf-8-sig')

    @pytest.mark.errors_expected
    def test_a_crop_outside_of_the_image_stops_the_experiment_from_starting(self, ext, qtbot, tmp_path):
        ext.unit(3).settings.set_rois([RectRoi('Far', 1000., 1100., 1000., 1100., aggregation=CROP)])
        ext._rois_changed(ext.unit(3), reset=True)

        ext.start_run()
        wait_for(qtbot, lambda: bool(ext.notifications))

        assert not ext.run_active
        title, text, error = ext.notifications[-1]
        assert title == 'Cannot start' and 'the crop Far' in text and 'is outside of the image' in text
        assert not list((tmp_path / 'data').rglob('*.h5')) or not ext.run_active

    def test_the_crops_can_be_saved_less_often(self, ext, qtbot, tmp_path):
        self.choose(ext, 'roi01', CROP)
        ext.unit(3).settings.child('record', 'crop_interval').setValue(0.5)

        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: ext.unit(3).n_measurements >= 16)
        end_run(qtbot, ext)

        with H5Experiment(tmp_path) as experiment:
            readouts, crops = len(experiment.time('Unit3_ROIs')), len(experiment.time('Unit3_Crop1'))
            assert 2 <= crops < readouts  # (a readout every 0.1 s, a crop every 0.5 s)

    def test_the_value_and_the_interval_cannot_be_changed_during_an_experiment(self, ext, qtbot):
        run(qtbot, ext, **SLOW)
        try:
            settings = ext.unit(3).settings
            assert not ui_enabled(settings.child('rois', 'roi00', 'aggregation'))
            assert not ui_enabled(settings.child('record', 'crop_interval'))
            assert not ext.view(3).manager.row('roi00').aggregation.isEnabled()
        finally:
            end_run(qtbot, ext)


class TestValuesAndCropsOfASpectrometer:
    """The first spectrometer of the Mock modules: ROIs of 10 nm each 50 nm, from 50 to 300 nm of the Mock spectrum"""

    AGGREGATIONS = {'mean': np.mean, 'sum': np.sum, 'median': np.median, 'min': np.min, 'max': np.max, 'std': np.std}

    def choose(self, ext, key, aggregation, number=1):
        row = ext.view(number).manager.row(key)
        row.aggregation.setCurrentIndex(MODES.index(aggregation))
        row.aggregation.activated.emit(MODES.index(aggregation))  # (what a click on the list does)

    def test_the_value_of_a_roi_is_chosen_in_the_manager(self, ext):
        self.choose(ext, 'roi00', 'sum')
        self.choose(ext, 'roi01', CROP)

        settings = ext.unit(1).settings
        assert settings.roi('roi00').aggregation == 'sum' and settings.roi('roi01').is_crop
        assert settings.child('rois', 'roi00', 'aggregation').value() == 'sum'
        assert ext.unit(2).settings.roi('roi00').aggregation == 'mean'  # (the other spectrometer is not touched)

    def test_a_choice_in_the_tree_is_shown_in_the_manager(self, ext):
        ext.unit(1).settings.child('rois', 'roi02', 'aggregation').setValue('std')

        assert ext.view(1).manager.row('roi02').aggregation.currentText() == 'std'

    def test_the_value_is_remembered_with_the_rois(self, ext, tmp_path):
        self.choose(ext, 'roi01', 'median')
        ext.save_rois()

        saved = json.loads((tmp_path / 'remembered' / 'rois.json').read_text())['units']['unit1']['rois']

        assert [roi['aggregation'] for roi in saved][:2] == ['mean', 'median']

    def test_a_crop_has_no_curve_but_a_dashed_window(self, ext):
        self.choose(ext, 'roi01', CROP)

        view = ext.view(1)
        assert len(view.curves_plot.curves) == 5 and 'roi01' not in view.toggles.names
        lines = view.spectrum_plot._regions['roi01'].lines
        assert all(line.pen.style() == QtCore.Qt.PenStyle.DashLine for line in lines)

    def test_the_curves_start_again_with_the_other_values(self, ext, qtbot):
        ext.start_monitoring()
        wait_for(qtbot, lambda: len(ext.unit(1).series_rois) >= 3)

        self.choose(ext, 'roi00', 'max')

        assert len(ext.unit(1).series_rois) < 3  # (what was drawn was the mean)
        ext.stop_monitoring()

    def test_each_aggregation_is_the_one_of_the_saved_spectrum_in_the_window(self, ext, qtbot, tmp_path):
        for index, aggregation in enumerate(self.AGGREGATIONS):
            self.choose(ext, f'roi{index:02d}', aggregation)

        run(qtbot, ext)
        wait_for_end(qtbot, ext)

        with H5Experiment(tmp_path) as saved:
            wavelengths, spectra = saved.wavelengths('Unit1_Spectrum'), saved.spectra('Unit1_Spectrum')
            assert saved.n_zero_d('Unit1_ROIs') == 6
            for index, (roi, (aggregation, function)) in enumerate(zip(ext.unit(1).settings.rois,
                                                                       self.AGGREGATIONS.items())):
                mask = (wavelengths >= roi.start) & (wavelengths <= roi.stop)
                assert mask.any()
                assert saved.zero_d('Unit1_ROIs', index) == pytest.approx(function(spectra[:, mask], axis=1), rel=1e-5)
                assert saved.label('Unit1_ROIs', index) == f'{roi.name} {aggregation} ({roi.start:g}-{roi.stop:g} nm)'
            assert saved.n_zero_d('Unit2_ROIs') == 6 and saved.label('Unit2_ROIs', 1) == 'ROI 2 mean (100-110 nm)'

    def test_the_crops_are_saved_as_spectra_and_the_values_with_their_aggregation(self, ext, qtbot, tmp_path):
        self.choose(ext, 'roi00', 'sum')
        self.choose(ext, 'roi01', CROP)

        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: ext.unit(1).n_measurements >= 4)
        end_run(qtbot, ext)

        with H5Experiment(tmp_path) as experiment:
            assert 'Unit1_Crop1' in experiment.titles and 'Unit1_ROIs' in experiment.titles
            labels = [experiment.label('Unit1_ROIs', index) for index in range(experiment.n_zero_d('Unit1_ROIs'))]
            assert len(labels) == 5 and labels[0] == 'ROI 1 sum (50-60 nm)' and labels[1] == 'ROI 3 mean (150-160 nm)'
            window = experiment.modules['Unit1_Crop1']['Data1D']['CH00']
            spectra, wavelengths = window['EnlData00'][:], window['Axis00'][:]
            assert spectra.ndim == 2 and spectra.shape[1] == wavelengths.size >= 2 and spectra.shape[0] >= 3
            assert wavelengths.min() >= 100. and wavelengths.max() <= 110.  # (ROI 2: 100-110 nm)
            assert spectra.shape[0] == len(experiment.time('Unit1_Crop1'))
            roi = json.loads(experiment.attribute('Unit1_Crop1', 'roi'))
            assert roi['name'] == 'ROI 2' and roi['aggregation'] == CROP
            full_wavelengths = experiment.wavelengths('Unit1_Spectrum')
            assert np.array_equal(full_wavelengths[(full_wavelengths >= 100.) & (full_wavelengths <= 110.)], wavelengths)
            assert 'Unit2_Crop1' not in experiment.titles  # (the other spectrometer has none)

    def test_the_converter_makes_a_csv_of_the_crop_and_a_csv_of_the_values(self, ext, qtbot, tmp_path):
        self.choose(ext, 'roi00', 'max')
        self.choose(ext, 'roi01', CROP)
        ext.unit(1).settings.child('record', 'integration_time').setValue(True)
        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: ext.unit(1).n_measurements >= 4)
        end_run(qtbot, ext)
        path = next((tmp_path / 'data').rglob('*.h5'))

        assert export.main([str(path), '-o', str(tmp_path / 'out')]) == 0

        out = tmp_path / 'out'
        header, rows = read_csv(out / f'{path.stem}_spectrometer1_crop_ROI_2.csv')
        assert header[0] == 'time (s)' and header[1].startswith('temperature') and header[2] == 'integration time (ms)'
        assert len(header) > 4 and all(name.endswith(' nm') for name in header[3:]) and len(rows) >= 3
        assert all(100. <= float(name.split()[0]) <= 110. for name in header[3:])
        assert {row[2] for row in rows} == {20.}
        values = read_csv(out / f'{path.stem}_spectrometer1.csv')[0]
        assert any(name.startswith('ROI 1 max (50-60 nm)') for name in values)
        assert not any('ROI 2' in name for name in values)  # (a crop has no column of values)
        text = (out / f'{path.stem}_experiment.txt').read_text(encoding='utf-8-sig')
        assert 'ROI 2: crop of the spectrum in 100-110 nm' in text
        assert 'ROI 1: maximum of the pixels in 50-60 nm' in text

    @pytest.mark.errors_expected
    def test_a_crop_outside_of_the_spectrum_stops_the_experiment_from_starting(self, ext, qtbot, tmp_path):
        ext.unit(1).settings.set_rois([Roi('Far', 5000., 5100., aggregation=CROP)])
        ext._rois_changed(ext.unit(1), reset=True)

        ext.start_run()
        wait_for(qtbot, lambda: bool(ext.notifications))

        assert not ext.run_active
        title, text, error = ext.notifications[-1]
        assert title == 'Cannot start' and 'the crop Far' in text and 'is outside of the spectrum' in text

    def test_the_crops_can_be_saved_less_often(self, ext, qtbot, tmp_path):
        self.choose(ext, 'roi01', CROP)
        ext.unit(1).settings.child('record', 'crop_interval').setValue(0.5)

        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: ext.unit(1).n_measurements >= 16)
        end_run(qtbot, ext)

        with H5Experiment(tmp_path) as experiment:
            readouts, crops = len(experiment.time('Unit1_ROIs')), len(experiment.time('Unit1_Crop1'))
            assert 2 <= crops < readouts  # (a readout every 0.1 s, a crop every 0.5 s)

    def test_the_value_and_the_interval_cannot_be_changed_during_an_experiment(self, ext, qtbot):
        run(qtbot, ext, **SLOW)
        try:
            settings = ext.unit(1).settings
            assert not ui_enabled(settings.child('rois', 'roi00', 'aggregation'))
            assert not ui_enabled(settings.child('record', 'crop_interval'))
            assert not ui_enabled(settings.child('spectrum_per_integration'))
            assert not ext.view(1).manager.row('roi00').aggregation.isEnabled()
            assert not ext.view(2).manager.row('roi00').aggregation.isEnabled()
        finally:
            end_run(qtbot, ext)
        assert ext.view(1).manager.row('roi00').aggregation.isEnabled()


class TestFullSpectraDividedByTheIntegrationTime:
    def test_they_are_not_unless_it_is_asked(self, ext):
        assert not any(unit.settings.spectrum_per_integration for unit in ext.units[:2])

    def test_the_option_is_in_the_tree_of_a_spectrometer_only(self, ext):
        assert ext.unit(1).settings.child('spectrum_per_integration').title() == 'Spectra per ms of integration:'

    def test_changing_it_does_not_restart_the_curves_of_the_values(self, ext, qtbot):
        ext.start_monitoring()
        wait_for(qtbot, lambda: len(ext.unit(1).series_rois) >= 3)
        n = len(ext.unit(1).series_rois)

        ext.unit(1).settings.child('spectrum_per_integration').setValue(True)

        assert len(ext.unit(1).series_rois) >= n  # (the curves are the values of the ROIs: another option)
        ext.stop_monitoring()

    def test_the_file_says_it(self, ext, qtbot, tmp_path):
        record_integration(ext)
        ext.unit(1).settings.child('spectrum_per_integration').setValue(True)
        ext.unit(1).settings.child('rois', 'roi01', 'aggregation').setValue(CROP)

        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: all(unit.n_measurements >= 2 for unit in ext.units[:2]))
        end_run(qtbot, ext)

        with H5Experiment(tmp_path) as experiment:
            for title in ('Unit1_Spectrum', 'Unit1_Crop1'):
                assert experiment.attribute(title, 'values_units') == 'A.U./ms'
                assert experiment.attribute(title, 'per_integration') in (True, 'True')
                assert experiment.attribute(title, 'intensity_units') == 'A.U.'
            assert experiment.attribute('Unit1_ROIs', 'values_units') == 'A.U.'  # (the values of the ROIs: the other option)
            assert experiment.attribute('Unit2_Spectrum', 'values_units') == 'A.U.'  # (the other spectrometer)

    def test_the_values_of_the_rois_follow_their_own_option(self, ext, qtbot, tmp_path):
        record_integration(ext)
        ext.unit(1).settings.child('per_integration').setValue(True)
        ext.unit(2).settings.child('spectrum_per_integration').setValue(True)

        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: all(unit.n_measurements >= 2 for unit in ext.units[:2]))
        end_run(qtbot, ext)

        with H5Experiment(tmp_path) as experiment:
            assert experiment.attribute('Unit1_ROIs', 'values_units') == 'A.U./ms'
            assert experiment.attribute('Unit1_Spectrum', 'values_units') == 'A.U.'
            assert experiment.attribute('Unit2_ROIs', 'values_units') == 'A.U.'
            assert experiment.attribute('Unit2_Spectrum', 'values_units') == 'A.U./ms'

    def test_the_converter_does_not_divide_a_second_time(self, ext, qtbot, tmp_path, capsys):
        record_integration(ext)
        ext.unit(1).settings.child('spectrum_per_integration').setValue(True)
        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: all(unit.n_measurements >= 3 for unit in ext.units[:2]))
        end_run(qtbot, ext)
        path = next((tmp_path / 'data').rglob('*.h5'))

        assert export.main([str(path), '-o', str(tmp_path / 'as_saved')]) == 0
        assert export.main([str(path), '-o', str(tmp_path / 'divided'), '--per-integration-time']) == 0

        def spectra(folder, number):
            header, rows = read_csv(tmp_path / folder / f'{path.stem}_spectrometer{number}_spectra.csv')
            columns = [index for index, name in enumerate(header) if name.endswith(' nm')]
            assert columns and len(rows) >= 2
            return np.array([[row[index] for index in columns] for row in rows])

        assert np.allclose(spectra('as_saved', 1), spectra('divided', 1), rtol=1e-4, atol=1e-9)  # (it was divided already)
        assert np.allclose(spectra('as_saved', 2) / 20., spectra('divided', 2), rtol=1e-4, atol=1e-9)  # (this one was not)
        assert 'divided by the integration time when they were measured' in capsys.readouterr().err


def set_mock_colors(unit, n):
    """The Mock camera gives `n` channels (copies of its image: what is measured on each one is the same)"""
    from pymodaq_plugins_thermospectro.spectrometers import _walk
    next(param for param in _walk(unit.detector.settings) if param.name() == 'Nimagescolor').setValue(n)


@pytest.mark.parametrize('dashboard', ['camera_only'], indirect=True)
class TestChannelsOfACamera:
    """A camera and its thermostat alone: ROIs of 8 x 8 pixels on the image of 100 x 200 pixels of the Mock camera"""

    def color_camera(self, qtbot, ext, n=3):
        set_mock_colors(ext.unit(3), n)
        ext.start_monitoring()
        wait_for(qtbot, lambda: ext.unit(3).n_channels == n and ext.view(3).manager.n_channels == n, timeout=20000)

    def test_a_gray_camera_has_no_boxes_to_tick(self, ext, qtbot):
        ext.start_monitoring()
        wait_for(qtbot, lambda: ext.unit(3).spectrum is not None)
        qtbot.wait(600)

        manager = ext.view(3).manager
        assert ext.unit(3).n_channels == 1 and manager.n_channels == 0
        assert all(manager.row(key).channel_boxes == [] for key in manager.keys)
        assert not manager._labels['channels'].isVisibleTo(manager)
        assert not ext.unit(3).settings.child('rois', 'roi00', 'channels').opts['visible']
        ext.stop_monitoring()

    def test_a_color_camera_has_a_box_for_each_channel(self, ext, qtbot):
        self.color_camera(qtbot, ext)

        manager = ext.view(3).manager
        assert manager._labels['channels'].isVisibleTo(manager)
        for key in manager.keys:
            assert [box.text() for box in manager.row(key).channel_boxes] == ['R', 'G', 'B']
            assert all(box.isChecked() for box in manager.row(key).channel_boxes)  # (all of them by default)
        assert ext.unit(3).settings.child('rois', 'roi00', 'channels').opts['visible']
        ext.stop_monitoring()

    def test_the_boxes_say_what_several_channels_do_when_the_mouse_is_over_them(self, ext, qtbot):
        self.color_camera(qtbot, ext)

        tip = ext.view(3).manager.row('roi00').channel_boxes[1].toolTip()

        assert tip.startswith('Green channel') and 'averaged' in tip and 'mean of std(green) and std(blue)' in tip
        ext.stop_monitoring()

    def test_ticking_the_boxes_changes_the_settings_of_the_roi(self, ext, qtbot):
        self.color_camera(qtbot, ext)

        ext.view(3).manager.row('roi00').channel_boxes[0].setChecked(False)  # (the red one)

        settings = ext.unit(3).settings
        assert settings.child('rois', 'roi00', 'channels').value() == '2, 3'
        assert settings.roi('roi00').channels == (1, 2) and settings.roi('roi01').channels is None
        assert len(ext.unit(3).series_rois) < 3  # (the curve was not of these channels: it starts again)
        ext.stop_monitoring()

    def test_the_settings_show_the_boxes_too(self, ext, qtbot):
        self.color_camera(qtbot, ext)

        ext.unit(3).settings.child('rois', 'roi01', 'channels').setValue('blue')

        assert [box.isChecked() for box in ext.view(3).manager.row('roi01').channel_boxes] == [False, False, True]
        ext.stop_monitoring()

    def test_the_choice_is_remembered_with_the_rois(self, ext, qtbot, tmp_path):
        self.color_camera(qtbot, ext)
        ext.view(3).manager.row('roi01').channel_boxes[0].setChecked(False)
        ext.save_rois()

        saved = json.loads((tmp_path / 'remembered' / 'rois.json').read_text())['units']['unit3']['rois']

        assert [roi.get('channels') for roi in saved][:3] == [None, [1, 2], None]
        ext.stop_monitoring()

    def test_the_boxes_cannot_be_changed_during_an_experiment(self, ext, qtbot):
        self.color_camera(qtbot, ext)
        ext.stop_monitoring()
        run(qtbot, ext, **SLOW)
        try:
            row = ext.view(3).manager.row('roi00')
            assert not any(box.isEnabled() for box in row.channel_boxes)
            assert not ui_enabled(ext.unit(3).settings.child('rois', 'roi00', 'channels'))
        finally:
            end_run(qtbot, ext)
        assert all(box.isEnabled() for box in ext.view(3).manager.row('roi00').channel_boxes)

    def test_an_experiment_saves_which_channels_the_values_are_measured_on(self, ext, qtbot, tmp_path):
        self.color_camera(qtbot, ext)
        manager = ext.view(3).manager
        manager.row('roi00').aggregation.setCurrentIndex(MODES.index('std'))
        manager.row('roi00').aggregation.activated.emit(MODES.index('std'))
        manager.row('roi00').channel_boxes[0].setChecked(False)  # (green and blue)
        ext.stop_monitoring()

        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: ext.unit(3).n_measurements >= 4)
        end_run(qtbot, ext)

        with H5Experiment(tmp_path) as experiment:
            labels = [experiment.label('Unit3_ROIs', index) for index in range(experiment.n_zero_d('Unit3_ROIs'))]
            assert labels[0] == 'ROI 1 std green+blue (x 10-18, y 5-13 pixel)'
            assert labels[1] == 'ROI 2 mean (x 20-28, y 10-18 pixel)'  # (all the channels)
            assert int(experiment.attribute('Unit3_ROIs', 'n_channels')) == 3
            saved = json.loads(experiment.attribute('Unit3_ROIs', 'rois'))
            assert saved[0]['channels'] == [1, 2] and 'channels' not in saved[1]

    def test_the_converter_says_which_channels(self, ext, qtbot, tmp_path):
        self.color_camera(qtbot, ext)
        ext.view(3).manager.row('roi02').channel_boxes[0].setChecked(False)
        ext.view(3).manager.row('roi02').channel_boxes[1].setChecked(False)  # (blue only)
        ext.stop_monitoring()
        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: ext.unit(3).n_measurements >= 4)
        end_run(qtbot, ext)
        path = next((tmp_path / 'data').rglob('*.h5'))

        assert export.main([str(path), '-o', str(tmp_path / 'out')]) == 0

        out = tmp_path / 'out'
        header = read_csv(out / f'{path.stem}_camera.csv')[0]
        assert any(name.startswith('ROI 3 mean blue (x 30-38') for name in header)
        text = (out / f'{path.stem}_experiment.txt').read_text(encoding='utf-8-sig')
        assert 'ROI 3: mean of the pixels in x 30-38, y 15-23 pixel, on the blue channel' in text
        assert 'ROI 1: mean of the pixels in x 10-18, y 5-13 pixel, on all 3 channels (the results are averaged)' in text

    def test_a_gray_camera_which_replaces_a_color_one_gives_all_the_channels_back(self, ext, qtbot):
        self.color_camera(qtbot, ext)
        ext.unit(3).settings.child('rois', 'roi00', 'channels').setValue('3')
        assert ext.unit(3).settings.roi('roi00').channels == (2,)

        set_mock_colors(ext.unit(3), 1)
        wait_for(qtbot, lambda: ext.unit(3).n_channels == 1 and ext.view(3).manager.n_channels == 0, timeout=20000)

        assert ext.unit(3).settings.child('rois', 'roi00', 'channels').value() == 'all'
        assert ext.unit(3).settings.roi('roi00').channels is None
        ext.stop_monitoring()
