"""The ROIs of a camera which gives colors: the channels they are measured on (boxes to tick), the results of the channels
being averaged: with green and blue and the standard deviation, a ROI gives the mean of std(green) and std(blue)"""
import json
import math
import time

import h5py
import numpy as np
import pytest

from pymodaq_data.data import DataRaw
from pymodaq_data.h5modules.data_saving import DataBundle
from pymodaq_gui.parameter import Parameter

from pymodaq_plugins_thermospectro.camera import (AGGREGATIONS, ALL_CHANNELS, CHANNELS_RULE, CROP, Frame, RectRoi,
                                                  as_channels, channel_letter, channel_name, channels_name,
                                                  default_rect_rois, fit_channels, format_channels, parse_channels,
                                                  rect_value, selected_channels)
from pymodaq_plugins_thermospectro.camera_widgets import RectRoiManager, channels_width
from pymodaq_plugins_thermospectro.saving import UnitRecording, build_dtes
from pymodaq_plugins_thermospectro.unit import UnitState
from pymodaq_plugins_thermospectro.unit_settings import CAMERA, CameraSettings, unit_definition
from pymodaq_plugins_thermospectro.views import CameraView

from test_camera import Bench
from test_export import camera_measurement, make_camera_unit, read_csv, run_export, write_experiment
from test_saving import Experiment, attribute, h5saver, titles  # noqa: F401  (h5saver is a fixture)

pytestmark = pytest.mark.filterwarnings('ignore')

ROWS, COLUMNS = np.mgrid[0:6, 0:8]
BASE = 10 * ROWS + COLUMNS
#: the channel c is (c + 1) times the base: the standard deviation, the sum... of the channels all differ
IMAGE = [((channel + 1) * BASE).astype(np.uint16) for channel in range(3)]
WINDOW = (1, 5, 1, 4)  # the columns 1 to 4 and the rows 1 to 3
NUMPY = {'mean': np.mean, 'sum': np.sum, 'median': np.median, 'min': np.min, 'max': np.max, 'std': np.std}


def of_channel(aggregation, channel):
    """What numpy says of the pixels of the window in a channel"""
    return float(NUMPY[aggregation](IMAGE[channel][1:4, 1:5].astype(float)))


class TestTheNamesOfTheChannels:
    def test_they_are_red_green_and_blue_then_numbers(self):
        assert [channel_name(i) for i in range(5)] == ['red', 'green', 'blue', 'ch4', 'ch5']
        assert [channel_letter(i) for i in range(5)] == ['R', 'G', 'B', '4', '5']

    def test_a_choice_has_a_name(self):
        assert channels_name((1, 2)) == 'green+blue' and channels_name((2, 1)) == 'green+blue'
        assert channels_name((0, 3)) == 'red+ch4'

    @pytest.mark.parametrize('text, channels', [
        ('all', None), ('', None), ('  ', None), ('2, 3', (1, 2)), ('3,2', (1, 2)), ('1', (0,)), ('G+B', (1, 2)),
        ('green blue', (1, 2)), ('Red', (0,)), ('ch4', (3,)), ('0', None), ('nonsense', None), ('2 2', (1,))])
    def test_what_is_written_in_the_settings(self, text, channels):
        assert parse_channels(text) == channels

    def test_it_is_written_from_1(self):
        assert format_channels(None) == ALL_CHANNELS == 'all'
        assert format_channels((1, 2)) == '2, 3' and format_channels((0,)) == '1'
        assert parse_channels(format_channels((0, 2, 4))) == (0, 2, 4)

    def test_the_channels_of_an_image(self):
        assert selected_channels(None, 3) == (0, 1, 2)
        assert selected_channels((2, 1), 3) == (1, 2)
        assert selected_channels((1, 5), 3) == (1,)  # (the one which is there)
        assert selected_channels((5,), 3) == (0, 1, 2)  # (none of them is there: a ROI always has something to measure)
        assert selected_channels((2,), 1) == (0,)

    def test_all_the_channels_is_none(self):
        assert fit_channels((0, 1, 2), 3) is None and fit_channels(None, 3) is None
        assert fit_channels((1, 2), 3) == (1, 2)
        assert fit_channels((2,), 1) is None  # (a gray camera: nothing to choose)


class TestTheValueOfSeveralChannels:
    @pytest.mark.parametrize('aggregation', AGGREGATIONS)
    @pytest.mark.parametrize('channels', [None, (0,), (1,), (1, 2), (0, 2), (0, 1, 2)])
    def test_it_is_the_mean_of_the_aggregations_of_the_channels(self, aggregation, channels):
        chosen = selected_channels(channels, 3)

        value = rect_value(IMAGE, *WINDOW, aggregation, channels)

        assert value == pytest.approx(np.mean([of_channel(aggregation, channel) for channel in chosen]))

    def test_the_example_of_the_user_green_and_blue_with_the_standard_deviation(self):
        std_green, std_blue = np.std(2. * BASE[1:4, 1:5]), np.std(3. * BASE[1:4, 1:5])

        value = rect_value(IMAGE, *WINDOW, 'std', (1, 2))

        assert value == pytest.approx((std_green + std_blue) / 2.) == pytest.approx(2.5 * np.std(BASE[1:4, 1:5]))

    def test_it_is_not_the_standard_deviation_of_the_pixels_of_the_two_channels_together(self):
        pooled = np.std(np.concatenate([IMAGE[1][1:4, 1:5].ravel(), IMAGE[2][1:4, 1:5].ravel()]).astype(float))

        assert rect_value(IMAGE, *WINDOW, 'std', (1, 2)) != pytest.approx(pooled)

    def test_it_is_not_the_sum_of_the_channels_either(self):
        value = rect_value(IMAGE, *WINDOW, 'sum', (1, 2))

        assert value == pytest.approx((of_channel('sum', 1) + of_channel('sum', 2)) / 2.)
        assert value != pytest.approx(of_channel('sum', 1) + of_channel('sum', 2))

    def test_the_default_is_all_the_channels(self):
        assert rect_value(IMAGE, *WINDOW) == pytest.approx(np.mean([of_channel('mean', c) for c in range(3)]))

    def test_an_image_with_one_channel_is_not_touched_by_the_choice(self):
        image = IMAGE[1]

        assert rect_value(image, *WINDOW, 'max', (2,)) == rect_value(image, *WINDOW, 'max') == of_channel('max', 1)
        assert rect_value([image], *WINDOW, 'max', (1, 2)) == of_channel('max', 1)

    def test_a_choice_which_is_not_in_the_image_gives_all_the_channels(self):
        assert rect_value(IMAGE, *WINDOW, 'mean', (7,)) == rect_value(IMAGE, *WINDOW, 'mean')

    def test_a_window_outside_has_no_value_whatever_the_channels(self):
        assert math.isnan(rect_value(IMAGE, 50, 60, 1, 4, 'mean', (1, 2)))

    def test_an_unknown_aggregation_is_a_mistake_even_for_a_window_outside(self):
        with pytest.raises(ValueError, match='mean, sum, median, min, max, std'):
            rect_value(IMAGE, 50, 60, 1, 4, 'mode', (1, 2))

    def test_the_sums_do_not_overflow_in_16_bits(self):
        big = [np.full((4, 4), 60000, dtype=np.uint16)] * 2

        assert rect_value(big, 0, 4, 0, 4, 'sum', (0, 1)) == 16 * 60000.

    def test_a_3d_array_is_a_sequence_of_channels(self):
        stacked = np.stack(IMAGE)

        assert rect_value(stacked, *WINDOW, 'mean', (1, 2)) == pytest.approx(rect_value(IMAGE, *WINDOW, 'mean', (1, 2)))

    def test_what_is_not_an_image_is_a_mistake(self):
        with pytest.raises(ValueError):
            as_channels(np.zeros(5))
        with pytest.raises(ValueError):
            as_channels([])


class TestARoiWithChannels:
    def test_all_the_channels_unless_told_otherwise(self):
        roi = RectRoi('A', 1, 5, 1, 4)

        assert roi.channels is None
        assert roi.value(IMAGE) == pytest.approx(rect_value(IMAGE, *WINDOW))

    def test_its_value_is_measured_on_its_channels(self):
        roi = RectRoi('A', 1, 5, 1, 4, aggregation='std', channels=(1, 2))

        assert roi.value(IMAGE) == pytest.approx(2.5 * np.std(BASE[1:4, 1:5]))

    def test_a_crop_has_no_value_whatever_the_channels(self):
        assert math.isnan(RectRoi('A', 1, 5, 1, 4, aggregation=CROP, channels=(1,)).value(IMAGE))

    def test_the_label_says_which_channels_when_it_is_not_all_of_them(self):
        assert RectRoi('A', 1, 5, 1, 4, aggregation='std').label('pixel', True) == 'A std (x 1-5, y 1-4 pixel)'
        assert RectRoi('A', 1, 5, 1, 4, aggregation='std', channels=(1, 2)).label('pixel', True) == \
            'A std green+blue (x 1-5, y 1-4 pixel)'
        assert RectRoi('A', 1, 5, 1, 4, channels=(0,)).label('pixel', True) == 'A mean red (x 1-5, y 1-4 pixel)'
        assert RectRoi('A', 1, 5, 1, 4, channels=(1, 2)).label() == 'A'  # (the plots have the name)

    def test_the_label_of_a_crop_does_not_say_it_keeps_all_the_channels(self):
        assert RectRoi('A', 1, 5, 1, 4, aggregation=CROP, channels=(1,)).label('pixel', True) == \
            'A crop (x 1-5, y 1-4 pixel)'

    def test_it_is_saved_and_read_back(self):
        roi = RectRoi('A', 1, 5, 1, 4, record=False, aggregation='median', channels=(1, 2))

        assert roi.to_dict()['channels'] == [1, 2]
        assert RectRoi.from_dict(roi.to_dict()) == roi
        assert RectRoi.from_dict(json.loads(json.dumps(roi.to_dict()))) == roi

    def test_nothing_is_said_of_all_the_channels(self):
        assert 'channels' not in RectRoi('A', 1, 5, 1, 4).to_dict()

    def test_a_roi_saved_before_the_channels_could_be_chosen_has_all_of_them(self):
        assert RectRoi.from_dict({'name': 'a', 'x0': 0, 'x1': 1, 'y0': 0, 'y1': 1}).channels is None

    @pytest.mark.parametrize('what', ['green', 3, [], [-1], None, {'a': 1}, ['x']])
    def test_what_is_not_a_list_of_channels_is_all_of_them(self, what):
        values = {'name': 'a', 'x0': 0, 'x1': 1, 'y0': 0, 'y1': 1, 'channels': what}

        assert RectRoi.from_dict(values).channels is None

    def test_the_channels_are_sorted_and_counted_once(self):
        values = {'name': 'a', 'x0': 0, 'x1': 1, 'y0': 0, 'y1': 1, 'channels': [2, 1, 2]}

        assert RectRoi.from_dict(values).channels == (1, 2)

    def test_it_is_not_valid_with_no_channel(self):
        assert 'at least one' in RectRoi('A', 1, 5, 1, 4, channels=()).problems()[0]
        assert 'at least one' in RectRoi('A', 1, 5, 1, 4, channels=(-1,)).problems()[0]

    def test_its_channels_follow_when_it_is_scaled(self):
        assert RectRoi('A', 1, 5, 1, 4, channels=(1, 2)).scaled(2, 2).channels == (1, 2)


class TestTheFrame:
    def test_the_channels_are_the_arrays_the_camera_gave(self):
        frame = Frame(IMAGE[0].astype(float), time.monotonic(), DataRaw('image', data=IMAGE))

        assert frame.n_channels == 3 and all(a is b or np.array_equal(a, b) for a, b in zip(frame.channels, IMAGE))
        assert frame.channels[2].dtype == np.uint16  # (nothing is converted)

    def test_a_gray_image_has_one(self):
        frame = Frame(IMAGE[0].astype(float), time.monotonic(), DataRaw('image', data=[IMAGE[0]]))

        assert frame.n_channels == 1

    def test_an_array_of_another_shape_is_not_a_channel(self):
        data = DataRaw('image', data=[IMAGE[0], IMAGE[1]])
        frame = Frame(IMAGE[0].astype(float), time.monotonic(), data)
        frame.dwa.data[1] = np.zeros((2, 2))

        assert frame.n_channels == 1


def release(bench):
    if bench.unit.state != UnitState.IDLE:
        bench.unit._release()


@pytest.fixture
def settings(qapp):
    return CameraSettings(Parameter.create(**unit_definition(3, default_rect_rois(3), kind=CAMERA)), 3)


class TestSettingsOfACamera:
    def test_each_roi_has_its_channels_which_are_all_of_them(self, settings):
        leaf = settings.child('rois', 'roi00', 'channels')

        assert leaf.type() == 'str' and leaf.value() == 'all' and leaf.title() == 'Channels:'
        assert all(roi.channels is None for roi in settings.rois)

    def test_the_tip_says_what_several_channels_do(self, settings):
        tip = settings.child('rois', 'roi00', 'channels').opts['tip']

        assert 'averaged' in tip and 'std(green)' in tip and 'std(blue)' in tip and CHANNELS_RULE in tip

    def test_it_is_not_shown_until_the_images_have_several_channels(self, settings):
        assert not settings.child('rois', 'roi00', 'channels').opts['visible']

        settings.set_channel_count(1)
        assert not settings.child('rois', 'roi00', 'channels').opts['visible']
        settings.set_channel_count(3)
        assert settings.child('rois', 'roi00', 'channels').opts['visible']
        settings.set_channel_count(1)
        assert not settings.child('rois', 'roi00', 'channels').opts['visible']

    def test_the_choice_is_read(self, settings):
        settings.set_channel_count(3)
        settings.child('rois', 'roi00', 'channels').setValue('2, 3')
        settings.child('rois', 'roi01', 'channels').setValue('G')

        assert [roi.channels for roi in settings.rois][:3] == [(1, 2), (1,), None]

    def test_all_the_channels_written_out_is_all_of_them(self, settings):
        settings.set_channel_count(3)
        settings.child('rois', 'roi00', 'channels').setValue('1, 2, 3')

        assert settings.roi('roi00').channels is None

    def test_what_was_asked_before_the_images_came_is_kept(self, settings):
        settings.child('rois', 'roi00', 'channels').setValue('2, 3')  # (the camera is not there yet: nothing is known)

        assert settings.roi('roi00').channels == (1, 2)

    def test_a_roi_which_asks_for_channels_the_camera_does_not_have_is_given_all_of_them(self, settings):
        settings.child('rois', 'roi00', 'channels').setValue('3')

        changed = settings.set_channel_count(1)

        assert changed and settings.child('rois', 'roi00', 'channels').value() == 'all'
        assert settings.roi('roi00').channels is None

    def test_a_choice_which_is_still_possible_is_kept(self, settings):
        settings.child('rois', 'roi00', 'channels').setValue('2, 3')

        assert not settings.set_channel_count(3)
        assert settings.roi('roi00').channels == (1, 2)
        assert settings.set_channel_count(2)  # (the blue one is gone)
        assert settings.roi('roi00').channels == (1,)

    def test_a_roi_added_later_follows_the_images(self, settings):
        settings.set_channel_count(3)

        settings.set_rois([RectRoi('New', 10, 20, 10, 20, channels=(0, 2))])

        assert settings.child('rois', 'roi00', 'channels').opts['visible']
        assert settings.roi('roi00').channels == (0, 2)

    def test_a_change_is_classified(self, settings):
        assert settings.classify(settings.child('rois', 'roi01', 'channels')) == ('roi_channels', 'roi01')

    def test_it_cannot_be_changed_during_an_experiment(self, settings):
        settings.lock(True)

        assert not settings.child('rois', 'roi00', 'channels').opts['enabled']
        assert settings.child('rois', 'roi00', 'plot').opts['enabled']
        settings.lock(False)
        assert settings.child('rois', 'roi00', 'channels').opts['enabled']

    def test_a_tree_from_an_older_file_has_all_the_channels(self, settings):
        settings.child('rois', 'roi00', 'channels').remove()

        assert settings.roi('roi00').channels is None
        assert not settings.set_channel_count(3)  # (nothing to show or to change)


@pytest.fixture
def color_bench(qtbot):
    bench = Bench(qtbot, channels=3)
    bench.settings.set_rois([RectRoi('Mean', 10, 20, 20, 30), RectRoi('GB', 10, 20, 20, 30, channels=(1, 2)),
                             RectRoi('Blue max', 10, 20, 20, 30, aggregation='max', channels=(2,)),
                             RectRoi('Window', 100, 120, 60, 80, aggregation=CROP)])
    bench.channel_counts = []
    bench.unit.channels_changed.connect(bench.channel_counts.append)
    bench.unit.channels_changed.connect(bench.settings.set_channel_count)  # (as the extension does)
    yield bench
    release(bench)


class TestMeasuredByTheUnit:
    """The fake camera gives ``x + 100 y + 1000 c`` in the channel c: the window of the ROIs (10-20, 20-30) has the mean
    2464.5 + 1000 c and the maximum 2919 + 1000 c"""

    def test_each_roi_is_measured_on_its_channels(self, color_bench):
        color_bench.unit.start_live(time.time())
        color_bench.wait_for(2)

        values = color_bench.measurements[-1].values

        assert values[0] == pytest.approx(2464.5 + 1000.)  # (all the channels: red, green and blue)
        assert values[1] == pytest.approx(2464.5 + 1500.)  # (green and blue)
        assert values[2] == pytest.approx(2919. + 2000.)  # (blue)
        assert math.isnan(values[3])  # (the crop)

    def test_the_number_of_channels_is_told_with_the_first_image_only(self, color_bench):
        color_bench.unit.start_live(time.time())
        color_bench.wait_for(3)

        assert color_bench.channel_counts == [3] and color_bench.unit.n_channels == 3

    def test_a_gray_camera_says_one(self, qtbot):
        bench = Bench(qtbot)
        counts = []
        bench.unit.channels_changed.connect(counts.append)
        try:
            bench.unit.start_live(time.time())
            bench.wait_for(2)

            assert counts == [1] and bench.unit.n_channels == 1
        finally:
            release(bench)

    def test_the_choice_of_a_roi_does_not_matter_to_a_gray_camera(self, qtbot):
        bench = Bench(qtbot)
        bench.settings.set_rois([RectRoi('A', 10, 20, 20, 30, channels=(1, 2))])
        try:
            bench.unit.start_live(time.time())
            bench.wait_for(2)

            assert bench.measurements[-1].values[0] == pytest.approx(2464.5)
        finally:
            release(bench)

    def test_before_the_first_image_a_gray_camera_is_assumed(self, qtbot):
        bench = Bench(qtbot)
        try:
            assert bench.unit.n_channels == 1
        finally:
            release(bench)


@pytest.fixture
def manager(qtbot):
    widget = RectRoiManager()
    qtbot.addWidget(widget)
    widget.resize(900, 300)
    widget.show()
    rois = [RectRoi('A', 10, 20, 10, 20), RectRoi('B', 30, 40, 10, 20, aggregation='std', channels=(1, 2)),
            RectRoi('C', 50, 60, 10, 20, aggregation=CROP)]
    widget.set_rois(['roi00', 'roi01', 'roi02'], rois, ['#1f77b4', '#ff7f0e', '#2ca02c'], (100, 200))
    return widget


def ticked(row):
    return [box.isChecked() for box in row.channel_boxes]


class TestTheBoxesOfTheChannels:
    def test_there_are_none_for_a_camera_with_one_channel(self, manager):
        assert manager.n_channels == 0
        for key in manager.keys:
            row = manager.row(key)
            assert row.channel_boxes == [] and not row.channels_cell.isVisibleTo(manager)
        assert not manager._labels['channels'].isVisibleTo(manager)  # (no title of the column either)

    def test_setting_one_channel_keeps_them_away(self, manager):
        manager.set_channels(1)

        assert manager.n_channels == 0 and not manager.row('roi00').channels_cell.isVisibleTo(manager)

    def test_a_box_for_each_channel_of_a_color_camera(self, manager):
        manager.set_channels(3)

        assert manager.n_channels == 3 and manager._labels['channels'].isVisibleTo(manager)
        assert manager.header_text('channels') == 'Channels'
        for key in manager.keys:
            row = manager.row(key)
            assert row.channels_cell.isVisibleTo(manager)
            assert [box.text() for box in row.channel_boxes] == ['R', 'G', 'B']
            assert row.channels_cell.width() == channels_width(3)

    def test_they_show_the_channels_of_each_roi(self, manager):
        manager.set_channels(3)

        assert ticked(manager.row('roi00')) == [True, True, True]  # (all of them)
        assert ticked(manager.row('roi01')) == [False, True, True]  # (green and blue)

    def test_more_channels_have_numbers(self, manager):
        manager.set_channels(4)

        assert [box.text() for box in manager.row('roi00').channel_boxes] == ['R', 'G', 'B', '4']

    def test_two_channels_are_red_and_green(self, manager):
        manager.set_channels(2)

        assert [box.text() for box in manager.row('roi00').channel_boxes] == ['R', 'G']
        assert ticked(manager.row('roi01')) == [False, True]  # (the blue one is not there)

    def test_the_mouse_over_a_box_says_what_several_channels_do(self, manager):
        manager.set_channels(3)

        for index, name in enumerate(('Red', 'Green', 'Blue')):
            tip = manager.row('roi00').channel_boxes[index].toolTip()
            assert tip.startswith(f'{name} channel') and CHANNELS_RULE in tip
            assert 'averaged' in tip and 'mean of std(green) and std(blue)' in tip
        assert 'averaged' in manager._labels['channels'].toolTip()

    def test_unticking_a_box_says_which_channels_are_left(self, manager, qtbot):
        manager.set_channels(3)

        with qtbot.waitSignal(manager.channels_edited) as signal:
            manager.row('roi00').channel_boxes[0].setChecked(False)

        assert signal.args == ['roi00', '2, 3']

    def test_ticking_them_all_again_says_all(self, manager, qtbot):
        manager.set_channels(3)
        manager.row('roi01').channel_boxes[0].setChecked(True)  # (the red one was off)

        assert ticked(manager.row('roi01')) == [True, True, True]

    def test_all_of_them_is_said_as_all(self, manager, qtbot):
        manager.set_channels(3)
        manager.row('roi00').channel_boxes[2].setChecked(False)

        with qtbot.waitSignal(manager.channels_edited) as signal:
            manager.row('roi00').channel_boxes[2].setChecked(True)

        assert signal.args == ['roi00', 'all']

    def test_one_channel_stays_ticked(self, manager, qtbot):
        manager.set_channels(3)
        row = manager.row('roi01')  # (green and blue)
        row.channel_boxes[1].setChecked(False)

        with qtbot.assertNotEmitted(manager.channels_edited):
            row.channel_boxes[2].setChecked(False)  # (the last one)

        assert ticked(row) == [False, False, True]

    def test_a_roi_which_is_shown_does_not_say_anything(self, manager, qtbot):
        manager.set_channels(3)

        with qtbot.assertNotEmitted(manager.channels_edited):
            manager.set_rois(['roi00', 'roi01', 'roi02'], [RectRoi('A', 10, 20, 10, 20, channels=(0,)),
                                                             RectRoi('B', 30, 40, 10, 20), RectRoi('C', 50, 60, 10, 20)],
                             ['#1f77b4', '#ff7f0e', '#2ca02c'], (100, 200))

        assert ticked(manager.row('roi00')) == [True, False, False]
        assert ticked(manager.row('roi01')) == [True, True, True]

    def test_a_crop_keeps_all_the_channels_and_says_so(self, manager):
        manager.set_channels(3)
        row = manager.row('roi02')

        assert ticked(row) == [True, True, True] and not any(box.isEnabled() for box in row.channel_boxes)
        assert all('A crop keeps all the channels' in box.toolTip() for box in row.channel_boxes)
        assert all(box.isEnabled() for box in manager.row('roi00').channel_boxes)

    def test_a_crop_which_becomes_a_number_has_its_choice_again(self, manager):
        manager.set_channels(3)

        manager.set_rois(['roi02'], [RectRoi('C', 50, 60, 10, 20, channels=(0, 1))], ['#2ca02c'], (100, 200))

        row = manager.row('roi02')
        assert ticked(row) == [True, True, False] and all(box.isEnabled() for box in row.channel_boxes)

    def test_they_are_locked_during_an_experiment(self, manager):
        manager.set_channels(3)

        manager.set_locked(True)
        assert not any(box.isEnabled() for key in manager.keys for box in manager.row(key).channel_boxes)
        manager.set_locked(False)
        assert all(box.isEnabled() for box in manager.row('roi00').channel_boxes)
        assert not any(box.isEnabled() for box in manager.row('roi02').channel_boxes)  # (the crop)

    def test_a_roi_added_later_has_them_too(self, manager):
        manager.set_channels(3)

        manager.set_rois(['roi00', 'roi01', 'roi02', 'roi03'], [RectRoi('A', 10, 20, 10, 20)] * 4,
                         ['#1f77b4'] * 4, (100, 200))

        assert [box.text() for box in manager.row('roi03').channel_boxes] == ['R', 'G', 'B']

    def test_they_go_away_again_with_a_gray_camera(self, manager):
        manager.set_channels(3)

        manager.set_channels(1)

        assert all(manager.row(key).channel_boxes == [] for key in manager.keys)
        assert not manager._labels['channels'].isVisibleTo(manager)

    def test_the_title_of_the_column_follows_the_number_of_boxes(self, manager):
        manager.set_channels(2)
        two = manager._labels['channels'].width()
        manager.set_channels(3)

        assert two == channels_width(2) < manager._labels['channels'].width() == channels_width(3)


class TestTheView:
    @pytest.fixture
    def view(self, qtbot, color_bench):
        view = CameraView(color_bench.unit)
        qtbot.addWidget(view.spectrum_plot)
        view.update_definitions()
        return view

    def test_the_boxes_come_with_the_first_image_of_a_color_camera(self, view, color_bench):
        assert view.manager.n_channels == 0

        color_bench.unit.start_live(time.time())
        color_bench.wait_for(2)
        view.refresh()

        assert view.manager.n_channels == 3
        assert [box.text() for box in view.manager.row('roi00').channel_boxes] == ['R', 'G', 'B']
        assert ticked(view.manager.row('roi01')) == [False, True, True]

    def test_a_gray_camera_has_none(self, qtbot):
        bench = Bench(qtbot)
        view = CameraView(bench.unit)
        qtbot.addWidget(view.spectrum_plot)
        try:
            bench.unit.start_live(time.time())
            bench.wait_for(2)
            view.refresh()

            assert view.manager.n_channels == 0
            assert all(view.manager.row(key).channel_boxes == [] for key in view.manager.keys)
        finally:
            release(bench)

    def test_a_box_is_a_change_of_the_channels_of_the_roi(self, view, color_bench, qtbot):
        color_bench.unit.start_live(time.time())
        color_bench.wait_for(2)
        view.refresh()

        with qtbot.waitSignal(view.roi_field_edited) as signal:
            view.manager.row('roi00').channel_boxes[0].setChecked(False)

        assert signal.args == [3, 'roi00', 'channels', '2, 3']


class TestInTheFile:
    @pytest.fixture
    def unit(self, qapp):
        rois = [RectRoi('Spot', 1, 4, 1, 3, aggregation='std', channels=(1, 2)), RectRoi('All', 1, 4, 1, 3),
                RectRoi('Window', 2, 6, 0, 3, aggregation=CROP, channels=(0,))]
        return make_camera_unit(rois=rois, channels=3)

    def test_the_label_says_which_channels(self, unit):
        recording = UnitRecording.from_unit(unit)

        dtes = {dte.name: dte for dte in build_dtes(recording, camera_measurement(unit, 0, 1000., channels=3))}

        assert dtes['Unit3_ROIs'][0].labels == ['Spot std green+blue (x 1-4, y 1-3 pixel)',
                                                'All mean (x 1-4, y 1-3 pixel)']

    def test_a_crop_keeps_all_the_channels(self, unit):
        recording = UnitRecording.from_unit(unit)

        window = [dte for dte in build_dtes(recording, camera_measurement(unit, 0, 1000., channels=3))
                  if dte.name == 'Unit3_Crop1'][0][0]

        assert len(window.data) == 3

    def test_the_recording_knows_how_many_channels_there_are(self, unit):
        assert UnitRecording.from_unit(unit).n_channels == 3
        assert UnitRecording.from_unit(make_camera_unit()).n_channels == 1

    def test_the_values_are_the_ones_of_the_channels(self, unit):
        measurement = camera_measurement(unit, 0, 1000., channels=3)

        # (the helper: the channel c is 1000 c + 10 y + x + index, a window of the columns 1 to 3 and the rows 1 to 2)
        base = np.mean([10 * y + x for y in (1, 2) for x in (1, 2, 3)])
        assert measurement.values[1] == pytest.approx(base + 1000.)  # (all: red 0, green 1000, blue 2000)
        assert measurement.values[0] == pytest.approx(np.std([10 * y + x for y in (1, 2) for x in (1, 2, 3)]))

    def test_what_the_file_says(self, h5saver, unit):
        experiment = Experiment(h5saver, [unit])
        for index in range(3):
            for dte in build_dtes(experiment.recordings[0], camera_measurement(unit, index, 1000., channels=3)):
                experiment.saver.add_data_bundle(DataBundle(dte=dte))
        experiment.finish()

        with h5py.File(experiment.path, 'r') as f:
            groups = titles(f['RawData/Datalogger000'])
            rois = groups['Unit3_ROIs']
            assert attribute(rois, 'n_channels') in (3, np.int64(3))
            saved = json.loads(attribute(rois, 'rois'))
            assert saved[0]['channels'] == [1, 2] and 'channels' not in saved[1]
            assert 'channels' in json.loads(attribute(groups['Unit3_Crop1'], 'roi'))  # (kept in the ROI: it is the ROI)


class TestTheConverter:
    ROIS = [RectRoi('Spot', 1, 4, 1, 3, aggregation='std', channels=(1, 2)),
            RectRoi('Level', 1, 4, 1, 3, aggregation='max', channels=(2,)), RectRoi('All', 1, 4, 1, 3)]

    def test_the_labels_of_the_csv_say_which_channels(self, h5saver, qapp, tmp_path):
        path = write_experiment(h5saver, spectrometer=False, channels=3, camera_rois=self.ROIS)

        out = run_export(path, tmp_path)

        header, rows = read_csv(out / f'{path.stem}_camera.csv')
        assert 'Spot std green+blue (x 1-4, y 1-3 pixel) [A.U.]' in header
        assert 'Level max blue (x 1-4, y 1-3 pixel) [A.U.]' in header
        assert 'All mean (x 1-4, y 1-3 pixel) [A.U.]' in header

    def test_the_numbers_are_the_averages_of_the_channels(self, h5saver, qapp, tmp_path):
        path = write_experiment(h5saver, spectrometer=False, channels=3, camera_rois=self.ROIS)

        out = run_export(path, tmp_path)

        header, rows = read_csv(out / f'{path.stem}_camera.csv')
        column = {name.split(' (')[0]: index for index, name in enumerate(header) if '(x ' in name}
        window = [10 * y + x for y in (1, 2) for x in (1, 2, 3)]
        assert rows[0][column['All mean']] == pytest.approx(np.mean(window) + 1000.)  # (red 0, green 1000, blue 2000)
        assert rows[0][column['Level max blue']] == pytest.approx(max(window) + 2000.)
        assert rows[0][column['Spot std green+blue']] == pytest.approx(np.std(window))  # (the same in both channels)

    def test_the_text_says_which_channels_each_roi_was_measured_on(self, h5saver, qapp, tmp_path):
        path = write_experiment(h5saver, spectrometer=False, channels=3, camera_rois=self.ROIS)

        out = run_export(path, tmp_path)

        text = (out / f'{path.stem}_experiment.txt').read_text(encoding='utf-8-sig')
        assert 'Spot: standard deviation of the pixels in x 1-4, y 1-3 pixel, on the green and blue channels ' \
               '(the results are averaged)' in text
        assert 'Level: maximum of the pixels in x 1-4, y 1-3 pixel, on the blue channel' in text
        assert 'All: mean of the pixels in x 1-4, y 1-3 pixel, on all 3 channels (the results are averaged)' in text

    def test_a_gray_camera_has_no_word_about_channels(self, h5saver, qapp, tmp_path):
        path = write_experiment(h5saver, spectrometer=False, camera_rois=[RectRoi('Spot', 1, 4, 1, 3)])

        out = run_export(path, tmp_path)

        text = (out / f'{path.stem}_experiment.txt').read_text(encoding='utf-8-sig')
        assert 'Spot: mean of the pixels in x 1-4, y 1-3 pixel' in text and 'channel' not in text.split('Files')[0]

    def test_a_file_made_before_the_channels_could_be_chosen(self, h5saver, qapp, tmp_path):
        path = write_experiment(h5saver, spectrometer=False, channels=3, camera_rois=[RectRoi('Spot', 1, 4, 1, 3)])
        with h5py.File(path, 'r+') as file:
            def forget(name, node):
                if 'n_channels' in node.attrs:
                    del node.attrs['n_channels']

            file['RawData/Datalogger000'].visititems(forget)

        out = run_export(path, tmp_path)

        text = (out / f'{path.stem}_experiment.txt').read_text(encoding='utf-8-sig')
        assert 'Spot: mean of the pixels in x 1-4, y 1-3 pixel' in text  # (nothing is invented)
