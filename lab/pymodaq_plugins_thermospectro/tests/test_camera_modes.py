"""The ROIs of a camera: what each one gives, an aggregation of its pixels (mean, sum, median, min, max, standard
deviation) or its pixels themselves (a crop)"""
import json
import math
import time

import h5py
import numpy as np
import pytest
from qtpy import QtCore

from pymodaq_gui.parameter import Parameter

from pymodaq_data.h5modules.data_saving import DataBundle

from pymodaq_plugins_thermospectro.camera import (AGGREGATIONS, CROP, DEFAULT_AGGREGATION, MODES, RectRoi, aggregate,
                                                  rect_pixels, rect_value, window_ranges)
from pymodaq_plugins_thermospectro.camera_widgets import ImagePlot, RectRoiManager
from pymodaq_plugins_thermospectro.rois import RoiStore
from pymodaq_plugins_thermospectro.saving import UnitRecording, build_dtes, crop_title
from pymodaq_plugins_thermospectro.unit import UnitState
from pymodaq_plugins_thermospectro.unit_settings import CAMERA, CameraSettings, default_rect_rois, unit_definition
from pymodaq_plugins_thermospectro.views import CameraView

from test_camera import Bench, gradient, measurement_of
from test_export import camera_measurement, make_camera_unit
from test_saving import Experiment, attribute, h5saver, titles  # noqa: F401  (h5saver is a fixture)

pytestmark = pytest.mark.filterwarnings('ignore')

PIXELS = np.array([[1., 2., 3.], [4., 5., 6.]])


class TestAggregate:
    @pytest.mark.parametrize('aggregation, expected', [
        ('mean', 3.5), ('sum', 21.), ('median', 3.5), ('min', 1.), ('max', 6.), ('std', float(np.std(PIXELS)))])
    def test_each_way_to_make_a_number(self, aggregation, expected):
        assert aggregate(PIXELS, aggregation) == pytest.approx(expected)

    def test_the_mean_is_the_default(self):
        assert aggregate(PIXELS) == aggregate(PIXELS, 'mean') == 3.5 and DEFAULT_AGGREGATION == 'mean'

    def test_a_sum_of_16_bits_pixels_does_not_overflow(self):
        pixels = np.full((2, 2), 65535, dtype=np.uint16)

        assert aggregate(pixels, 'sum') == 4 * 65535.

    def test_the_median_of_an_odd_number_of_pixels_is_one_of_them(self):
        assert aggregate(np.array([[5., 1., 9.]]), 'median') == 5.

    def test_the_standard_deviation_is_the_one_of_the_pixels(self):
        assert aggregate(np.array([2., 4., 4., 4., 5., 5., 7., 9.]), 'std') == pytest.approx(2.0)  # (not of an estimate)

    def test_a_crop_is_not_a_number(self):
        with pytest.raises(ValueError, match='is not a way to make a number'):
            aggregate(PIXELS, CROP)

    def test_an_unknown_one_is_a_mistake(self):
        with pytest.raises(ValueError, match='mean, sum, median, min, max, std'):
            aggregate(PIXELS, 'mode')

    def test_the_names(self):
        assert AGGREGATIONS == ('mean', 'sum', 'median', 'min', 'max', 'std') and MODES == AGGREGATIONS + ('crop',)


class TestPixelsOfAWindow:
    def test_the_pixels_are_a_view_of_the_image(self):
        image = gradient()

        pixels = rect_pixels(image, 10, 20, 20, 30)

        assert pixels.shape == (10, 10) and pixels.base is not None
        assert np.array_equal(pixels, image[20:30, 10:20])

    def test_a_window_outside_has_none(self):
        assert rect_pixels(gradient(), 500, 600, 0, 10) is None

    def test_a_window_in_any_order(self):
        assert rect_pixels(gradient(), 20, 10, 30, 20).shape == (10, 10)

    def test_a_3d_image_is_refused(self):
        with pytest.raises(ValueError, match='2D'):
            rect_pixels(np.zeros((2, 3, 4)), 0, 1, 0, 1)

    def test_the_rows_and_the_columns(self):
        assert window_ranges((80, 120), 10, 20, 20, 30) == ((20, 30), (10, 20))
        assert window_ranges((80, 120), 500, 600, 0, 10) is None

    @pytest.mark.parametrize('aggregation', AGGREGATIONS)
    def test_the_value_of_a_window_is_the_aggregation_of_its_pixels(self, aggregation):
        image = gradient()

        assert rect_value(image, 10, 20, 20, 30, aggregation) == pytest.approx(aggregate(image[20:30, 10:20], aggregation))

    def test_outside_it_is_nan_whatever_the_aggregation(self):
        assert all(math.isnan(rect_value(gradient(), 500, 600, 0, 10, aggregation)) for aggregation in AGGREGATIONS)

    def test_an_unknown_aggregation_is_a_mistake_even_outside_of_the_image(self):
        with pytest.raises(ValueError):
            rect_value(gradient(), 500, 600, 0, 10, 'mode')


class TestRectRoiModes:
    def test_a_roi_gives_its_mean_unless_told_otherwise(self):
        roi = RectRoi('Spot', 10, 20, 20, 30)

        assert roi.aggregation == 'mean' and not roi.is_crop
        assert roi.value(gradient()) == pytest.approx(14.5 + 100. * 24.5)

    @pytest.mark.parametrize('aggregation', AGGREGATIONS)
    def test_its_value_is_the_aggregation(self, aggregation):
        roi = RectRoi('Spot', 10, 20, 20, 30, aggregation=aggregation)

        assert roi.value(gradient()) == pytest.approx(aggregate(gradient()[20:30, 10:20], aggregation))

    def test_a_crop_has_no_value_but_its_pixels(self):
        roi = RectRoi('Window', 10, 20, 20, 30, aggregation=CROP)

        assert roi.is_crop and math.isnan(roi.value(gradient()))
        assert np.array_equal(roi.pixels(gradient()), gradient()[20:30, 10:20])

    def test_the_label_says_what_it_gives(self):
        assert RectRoi('Spot', 10, 20, 20, 30).label(with_range=True) == 'Spot mean (x 10-20, y 20-30 pixel)'
        assert RectRoi('Spot', 10, 20, 20, 30, aggregation='sum').label(with_range=True) == \
            'Spot sum (x 10-20, y 20-30 pixel)'
        assert RectRoi('Spot', 10, 20, 20, 30, aggregation=CROP).label(with_range=True) == \
            'Spot crop (x 10-20, y 20-30 pixel)'
        assert RectRoi('Spot', 10, 20, 20, 30, aggregation='sum').label() == 'Spot'  # (the plots have the name)

    def test_it_is_saved_and_read_back(self):
        roi = RectRoi('Spot', 10, 20, 20, 30, record=False, plot=False, aggregation='median')

        assert roi.to_dict()['aggregation'] == 'median'
        assert RectRoi.from_dict(roi.to_dict()) == roi

    def test_a_roi_saved_before_it_existed_is_a_mean(self):
        old = {'name': 'Spot', 'x0': 10, 'x1': 20, 'y0': 20, 'y1': 30, 'record': True, 'plot': True}

        assert RectRoi.from_dict(old).aggregation == 'mean'

    def test_an_unknown_aggregation_in_a_file_is_a_mean(self):
        saved = {'name': 'Spot', 'x0': 10, 'x1': 20, 'y0': 20, 'y1': 30, 'aggregation': 'mode'}

        assert RectRoi.from_dict(saved).aggregation == 'mean'

    def test_a_roi_which_is_made_with_an_unknown_one_is_not_valid(self):
        assert 'is not one of' in RectRoi('Spot', 10, 20, 20, 30, aggregation='mode').problems()[0]

    def test_scaling_keeps_it(self):
        assert RectRoi('Spot', 10, 20, 20, 30, aggregation=CROP).scaled(2., 2.).aggregation == CROP

    def test_the_store_remembers_it(self, tmp_path):
        store = RoiStore(tmp_path / 'rois.json')
        rois = [RectRoi('A', 1, 5, 1, 5), RectRoi('B', 10, 20, 10, 20, aggregation='sum'),
                RectRoi('C', 30, 50, 30, 50, aggregation=CROP)]

        store.save(3, rois)

        assert store.get(3, RectRoi) == rois

    def test_a_store_made_before_gives_means(self, tmp_path):
        (tmp_path / 'rois.json').write_text('{"units": {"unit3": {"rois": [{"name": "A", "x0": 1, "x1": 5, "y0": 1, '
                                            '"y1": 5, "record": true, "plot": true}]}}}')

        assert [roi.aggregation for roi in RoiStore(tmp_path / 'rois.json').get(3, RectRoi)] == ['mean']


class TestSettingsOfACamera:
    @pytest.fixture
    def settings(self, qapp):
        settings = CameraSettings(Parameter.create(**unit_definition(3, default_rect_rois(3), kind=CAMERA)), 3)
        settings.set_rois([RectRoi('A', 1, 5, 1, 5), RectRoi('B', 10, 20, 10, 20, aggregation='sum'),
                           RectRoi('C', 30, 50, 30, 50, aggregation=CROP)])
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
        key = settings.add_roi(settings.new_roi(50, 40))

        assert settings.roi(key).aggregation == 'mean'

    def test_the_values_are_locked_during_an_experiment_and_the_plot_boxes_are_not(self, settings):
        settings.lock(True)

        assert not settings.child('rois', 'roi00', 'aggregation').opts['enabled']
        assert settings.child('rois', 'roi00', 'plot').opts['enabled']

    def test_the_crops_are_saved_every_so_often(self, settings):
        assert settings.crop_interval == 0.0  # (at each measurement)
        assert settings.child('record', 'crop_interval').title() == 'Crops every:'

        settings.child('record', 'crop_interval').setValue(30.)

        assert settings.crop_interval == 30.

    def test_a_tree_from_an_older_file_saves_the_crops_at_each_measurement(self, settings):
        settings.child('record', 'crop_interval').remove()

        assert settings.crop_interval == 0.0

    def test_the_options_of_the_spectra_are_not_the_ones_of_a_camera(self, settings):
        assert 'spectrum_per_integration' not in settings.param.names  # (the images are saved as they come)
        assert not settings.spectrum_per_integration and settings.spectrum_units == 'A.U.'


@pytest.fixture
def camera_bench(qtbot):
    bench = Bench(qtbot)
    bench.settings.set_rois([RectRoi('Mean', 10, 20, 20, 30), RectRoi('Sum', 10, 20, 20, 30, aggregation='sum'),
                             RectRoi('Window', 100, 120, 60, 80, aggregation=CROP)])
    yield bench
    if bench.unit.state != UnitState.IDLE:
        bench.unit._release()


class TestMeasuredByTheUnit:
    def test_each_roi_gives_what_it_is_told_to(self, camera_bench):
        camera_bench.unit.start_live(time.time())
        camera_bench.wait_for(2)

        values = camera_bench.measurements[-1].values
        pixels = gradient()[20:30, 10:20]
        assert values[0] == pytest.approx(pixels.mean()) and values[1] == pytest.approx(pixels.sum())
        assert math.isnan(values[2])  # (the crop has no value)

    def test_a_crop_outside_of_the_image_is_not_a_warning_of_the_values(self, camera_bench, caplog):
        camera_bench.settings.set_rois([RectRoi('Far', 1000, 1100, 1000, 1100, aggregation=CROP)])
        camera_bench.unit.start_live(time.time())
        camera_bench.wait_for(2)

        assert not [record for record in caplog.records if 'outside of the image' in record.getMessage()]

    def test_the_sum_per_ms_of_exposure(self, camera_bench):
        camera_bench.settings.child('per_integration').setValue(True)
        camera_bench.unit.start_live(time.time())
        camera_bench.wait_for(2)

        pixels = gradient()[20:30, 10:20]
        values = camera_bench.measurements[-1].values
        assert values[0] == pytest.approx(pixels.mean() / 20.) and values[1] == pytest.approx(pixels.sum() / 20.)
        assert camera_bench.measurements[-1].integration_time == 20. and camera_bench.unit.value_units == 'A.U./ms'

    def test_the_crops_are_due_at_each_measurement_unless_told_otherwise(self, camera_bench):
        camera_bench.unit.start_live(time.time())
        camera_bench.wait_for(3)

        assert all(measurement.crop_due for measurement in camera_bench.measurements)

    def test_the_crops_every_so_often(self, camera_bench):
        camera_bench.settings.child('record', 'crop_interval').setValue(0.35)
        camera_bench.unit.start_live(time.time())
        camera_bench.wait_for(8)

        due = [measurement.crop_due for measurement in camera_bench.measurements]
        assert due[0] and not all(due) and sum(due) >= 2
        assert due[1] is False  # (the readout time is 0.1 s)


class TestCropsInTheFile:
    @pytest.fixture
    def unit(self, qapp):
        rois = [RectRoi('Mean', 1, 4, 1, 3), RectRoi('Window', 2, 6, 0, 3, aggregation=CROP),
                RectRoi('Other', 0, 3, 3, 6, aggregation=CROP), RectRoi('Skipped', 0, 3, 3, 6, record=False,
                                                                        aggregation=CROP)]
        return make_camera_unit(rois=rois)

    def test_the_crops_are_groups_of_their_own(self, unit):
        recording = UnitRecording.from_unit(unit)

        assert recording.titles == ['Unit3_Image', 'Unit3_ROIs', 'Unit3_Crop1', 'Unit3_Crop2', 'Unit3_Temperature']
        assert [roi.name for roi in recording.crops] == ['Window', 'Other']  # (not the one which is not recorded)
        assert [roi.name for roi in recording.rois] == ['Mean'] and recording.roi_indices == [0]
        assert crop_title(3, 2) == 'Unit3_Crop2'

    def test_a_crop_alone_still_makes_a_group_when_the_roi_values_are_not_recorded(self, unit):
        unit.settings.child('rois', 'roi00', 'record').setValue(False)

        recording = UnitRecording.from_unit(unit)

        assert 'Unit3_ROIs' not in recording.titles and 'Unit3_Crop1' in recording.titles

    def test_the_pixels_of_each_crop_are_built_with_the_axes_of_the_image(self, unit):
        recording = UnitRecording.from_unit(unit)
        measurement = camera_measurement(unit, 2, 1000., channels=1)

        dtes = {dte.name: dte for dte in build_dtes(recording, measurement)}

        window = dtes['Unit3_Crop1'][0]
        rows, columns = np.mgrid[0:6, 0:8]
        assert window.origin == 'Camera1' and window.name == 'Window'
        assert np.array_equal(window.data[0], (10 * rows + columns + 2)[0:3, 2:6]) and window.data[0].dtype == np.uint16
        assert [axis.label for axis in window.axes] == ['y', 'x']
        assert window.axes[0].get_data().tolist() == [0., 1., 2.] and window.axes[1].get_data().tolist() == [2., 3., 4., 5.]
        assert dtes['Unit3_Crop2'][0].data[0].shape == (3, 3)

    def test_the_colors_of_a_crop_are_kept(self, unit):
        recording = UnitRecording.from_unit(unit)
        measurement = camera_measurement(unit, 0, 1000., channels=3)

        window = [dte for dte in build_dtes(recording, measurement) if dte.name == 'Unit3_Crop1'][0][0]

        assert len(window.data) == 3 and window.data[2].shape == (3, 4)
        rows, columns = np.mgrid[0:6, 0:8]
        assert np.array_equal(window.data[2], (2000 + 10 * rows + columns)[0:3, 2:6])

    def test_a_crop_is_only_built_when_it_is_due(self, unit):
        recording = UnitRecording.from_unit(unit)
        measurement = camera_measurement(unit, 0, 1000.)
        measurement.crop_due = False

        names = [dte.name for dte in build_dtes(recording, measurement)]

        assert 'Unit3_Crop1' not in names and 'Unit3_ROIs' in names and 'Unit3_Image' in names

    def test_the_value_of_the_rois_is_the_one_of_the_aggregations_only(self, unit):
        recording = UnitRecording.from_unit(unit)
        measurement = camera_measurement(unit, 1, 1000.)

        rois = [dte for dte in build_dtes(recording, measurement) if dte.name == 'Unit3_ROIs'][0][0]

        assert rois.labels == ['Mean mean (x 1-4, y 1-3 pixel)'] and len(rois.data) == 1

    def test_a_crop_which_is_outside_of_the_image_stops_the_experiment_from_starting(self, unit):
        unit.spectrum = measurement_of(unit).spectrum  # (an image of 120 x 80 pixels)
        unit.settings.set_rois([RectRoi('Far', 1000, 1100, 1000, 1100, aggregation=CROP)])

        with pytest.raises(ValueError, match='the crop Far .* is outside of the image \\(120 x 80 pixels\\)'):
            UnitRecording.from_unit(unit)

    def test_a_crop_inside_of_the_image_is_fine(self, unit):
        unit.spectrum = measurement_of(unit).spectrum
        unit.settings.set_rois([RectRoi('In', 10, 20, 10, 20, aggregation=CROP)])

        assert len(UnitRecording.from_unit(unit).crops) == 1

    def test_what_the_file_holds(self, h5saver, unit):
        experiment = Experiment(h5saver, [unit])
        for index in range(4):
            for dte in build_dtes(experiment.recordings[0], camera_measurement(unit, index, 1000.)):
                experiment.saver.add_data_bundle(DataBundle(dte=dte))
        assert experiment.saver.swmr_started  # (every group, the crops too, had its first data)
        experiment.finish()

        with h5py.File(experiment.path, 'r') as f:
            groups = titles(f['RawData/Datalogger000'])
            window = groups['Unit3_Crop1']
            assert window['Data2D/CH00/EnlData00'].shape == (4, 3, 4)
            assert groups['Unit3_Crop2']['Data2D/CH00/EnlData00'].shape == (4, 3, 3)
            assert window['NavAxes/Axis00'][:].tolist() == [1000., 1001., 1002., 1003.]
            roi = json.loads(attribute(window, 'roi'))
            assert roi['name'] == 'Window' and roi['x0'] == 2 and roi['y1'] == 3 and roi['aggregation'] == 'crop'
            assert attribute(window, 'camera') == 'Camera1' and attribute(window, 'intensity_units') == 'A.U.'
            rois = groups['Unit3_ROIs']
            assert [json.loads(attribute(rois, 'rois'))[0]['aggregation']] == ['mean']
            assert attribute(rois, 'values_units') == 'A.U.'


class TestTheManagerOfACamera:
    @pytest.fixture
    def manager(self, qtbot):
        widget = RectRoiManager()
        qtbot.addWidget(widget)
        widget.show()
        rois = [RectRoi('A', 10, 20, 30, 40), RectRoi('B', 50, 60, 5, 15, aggregation='sum'),
                RectRoi('C', 70, 80, 5, 15, aggregation=CROP)]
        widget.set_rois(['roi00', 'roi01', 'roi02'], rois, ['#1f77b4', '#ff7f0e', '#2ca02c'], (80, 120))
        return widget

    def test_a_column_says_what_each_roi_gives(self, manager):
        assert manager.header_text('aggregation') == 'Value'
        assert [manager.row(key).aggregation.currentText() for key in manager.keys] == ['mean', 'sum', 'crop']
        assert [manager.row('roi00').aggregation.itemText(i) for i in range(manager.row('roi00').aggregation.count())] \
            == list(MODES)

    def test_choosing_one_says_so(self, manager, qtbot):
        row = manager.row('roi00')

        with qtbot.waitSignal(manager.aggregation_edited) as signal:
            row.aggregation.setCurrentIndex(MODES.index('median'))
            row.aggregation.activated.emit(MODES.index('median'))  # (a click on an item emits it)

        assert signal.args == ['roi00', 'median']

    def test_showing_a_roi_says_nothing(self, manager, qtbot):
        with qtbot.assertNotEmitted(manager.aggregation_edited):
            manager.set_rois(['roi00'], [RectRoi('A', 10, 20, 30, 40, aggregation='max')], ['#1f77b4'], (80, 120))

        assert manager.row('roi00').aggregation.currentText() == 'max'

    def test_a_crop_has_nothing_to_plot(self, manager):
        crop = manager.row('roi02')

        assert not crop.plot.isChecked() and not crop.plot.isEnabled()
        assert 'no value to plot' in crop.plot.toolTip()
        assert manager.row('roi00').plot.isEnabled()

    def test_it_can_be_plotted_again_once_it_gives_a_number(self, manager):
        manager.set_rois(['roi02'], [RectRoi('C', 70, 80, 5, 15, plot=True)], ['#2ca02c'], (80, 120))

        assert manager.row('roi02').plot.isEnabled() and manager.row('roi02').plot.isChecked()

    def test_a_crop_which_was_plotted_is_still_to_be_plotted_when_it_is_a_number_again(self, manager):
        manager.set_rois(['roi00'], [RectRoi('A', 10, 20, 30, 40, plot=True, aggregation=CROP)], ['#1f77b4'], (80, 120))
        assert not manager.row('roi00').plot.isChecked()  # (shown off: it has no value)

        manager.set_rois(['roi00'], [RectRoi('A', 10, 20, 30, 40, plot=True)], ['#1f77b4'], (80, 120))

        assert manager.row('roi00').plot.isChecked()

    def test_locked_during_an_experiment(self, manager):
        manager.set_locked(True)

        assert not manager.row('roi00').aggregation.isEnabled()
        assert not manager.row('roi01').aggregation.isEnabled()
        manager.set_locked(False)
        assert manager.row('roi00').aggregation.isEnabled()

    def test_the_hint_tells_about_the_value(self):
        from pymodaq_plugins_thermospectro.camera_widgets import HINT
        assert 'crop' in HINT and 'median' in HINT


class TestTheRectanglesOnTheImage:
    @pytest.fixture
    def plot(self, qtbot):
        widget = ImagePlot('Camera')
        qtbot.addWidget(widget)
        widget.resize(400, 300)
        widget.show()
        widget.set_image(gradient())
        return widget

    def test_a_crop_is_dashed(self, plot):
        plot.set_roi('roi00', 10, 20, 30, 50, '#1f77b4', '1', crop=True)
        plot.set_roi('roi01', 30, 40, 30, 50, '#1f77b4', '2')

        assert plot._rois['roi00'].pen.style() == QtCore.Qt.PenStyle.DashLine
        assert plot._rois['roi01'].pen.style() == QtCore.Qt.PenStyle.SolidLine

    def test_a_rectangle_changes_its_style_with_its_value(self, plot):
        plot.set_roi('roi00', 10, 20, 30, 50, '#1f77b4', '1')

        plot.set_roi('roi00', 10, 20, 30, 50, '#1f77b4', '1', crop=True)

        assert plot._rois['roi00'].pen.style() == QtCore.Qt.PenStyle.DashLine
        plot.set_roi('roi00', 10, 20, 30, 50, '#1f77b4', '1')
        assert plot._rois['roi00'].pen.style() == QtCore.Qt.PenStyle.SolidLine


class TestTheViewOfACamera:
    @pytest.fixture
    def view(self, qtbot, camera_bench):
        view = CameraView(camera_bench.unit)
        qtbot.addWidget(view.spectrum_plot)
        view.update_definitions()
        return view

    def test_a_crop_has_no_curve_and_no_button(self, view):
        assert [curve.name() for curve in view.curves_plot.curves] == ['Mean (mean)', 'Sum (sum)']
        assert view.toggles.names == ['roi00', 'roi01', 'temperature']  # (not roi02, the crop)

    def test_the_rectangle_of_a_crop_is_dashed_and_the_manager_shows_its_value(self, view):
        assert view.spectrum_plot._rois['roi02'].pen.style() == QtCore.Qt.PenStyle.DashLine
        assert view.manager.row('roi02').aggregation.currentText() == 'crop'

    def test_the_legend_tells_the_aggregations_when_they_differ(self, view):
        assert [curve.name() for curve in view.curves_plot.curves] == ['Mean (mean)', 'Sum (sum)']
        text, units = view.value_label([roi for roi in view.unit.settings.rois if not roi.is_crop])
        assert text == 'ROIs: see the legend' and units == 'A.U.'

    def test_the_legend_has_the_names_when_all_curves_are_the_same(self, view, camera_bench):
        camera_bench.settings.child('rois', 'roi01', 'aggregation').setValue('mean')

        view.update_definitions()

        assert [curve.name() for curve in view.curves_plot.curves] == ['Mean', 'Sum']
        text, _ = view.value_label(camera_bench.settings.rois[:2])
        assert text == 'ROIs: mean of the pixels'

    @pytest.mark.parametrize('aggregation, text', [
        ('mean', 'mean'), ('sum', 'sum'), ('median', 'median'), ('min', 'minimum'), ('max', 'maximum'),
        ('std', 'standard deviation')])
    def test_the_axis_says_what_the_curves_are(self, view, camera_bench, aggregation, text):
        camera_bench.settings.set_rois([RectRoi('A', 10, 20, 20, 30, aggregation=aggregation)])

        view.update_definitions()

        assert view.curves_plot.getPlotItem().getAxis('left').labelText == f'ROIs: {text} of the pixels'

    def test_the_axis_has_the_units_of_the_values(self, view, camera_bench):
        assert view.curves_plot.getPlotItem().getAxis('left').labelUnits == 'A.U.'

        camera_bench.settings.child('per_integration').setValue(True)
        camera_bench.settings.child('intensity_units').setValue('V')
        view.update_definitions()

        assert view.curves_plot.getPlotItem().getAxis('left').labelUnits == 'V/ms'

    def test_a_choice_in_the_manager_goes_to_the_settings_through_the_view(self, view, qtbot):
        with qtbot.waitSignal(view.roi_field_edited) as signal:
            view.manager.row('roi00').aggregation.setCurrentIndex(MODES.index('max'))
            view.manager.row('roi00').aggregation.activated.emit(MODES.index('max'))

        assert signal.args == [3, 'roi00', 'aggregation', 'max']

    def test_the_curves_of_the_crops_are_not_drawn_and_the_others_are(self, view, camera_bench):
        camera_bench.unit.start_live(time.time())
        camera_bench.wait_for(3)

        view.refresh()

        assert len(view.curves_plot.curves) == 2 and len(view.curves_plot.curves[1].getData()[0]) >= 3
