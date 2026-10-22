"""The extension on Mock modules: the integration time (saved, changed during an experiment, dividing the values), the
units, and the ROIs of a camera which are a crop or give another aggregation than the mean"""
import json

import h5py
import pytest
from qtpy import QtCore

from pymodaq_plugins_thermospectro.camera import CROP, MODES, RectRoi
from pymodaq_plugins_thermospectro import export

from test_export import read_csv
from test_extension import H5Experiment, end_run, run, ui_enabled, wait_for

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
        assert axis.labelUnits == 'counts'

        ext.unit(1).settings.child('per_integration').setValue(True)

        assert axis.labelUnits == 'counts/ms'
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
            assert experiment.attribute('Unit2_ROIs', 'values_units') == 'counts'  # (the other unit was not divided)
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
