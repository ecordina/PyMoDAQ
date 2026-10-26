import json

import numpy as np
import pytest

from pymodaq_plugins_thermospectro.rois import (DEFAULT_WIDTHS, N_DEFAULT, NOMINAL_RANGE, Roi, RoiStore, default_rois,
                                                renamed, roi_value, unique_names, window_mask)


@pytest.fixture
def axis():
    return np.linspace(500., 600., 101)  # 1 nm per pixel


@pytest.fixture
def spectrum(axis):
    return axis - 500.  # the intensity of a pixel is its wavelength minus 500: 0 ... 100


class TestWindow:
    def test_the_pixels_inside_the_window(self, axis):
        mask = window_mask(axis, 545., 555.)

        assert axis[mask].tolist() == [545., 546., 547., 548., 549., 550., 551., 552., 553., 554., 555.]

    def test_the_edges_are_included(self, axis):
        assert window_mask(axis, 549., 551.).sum() == 3

    def test_the_edges_can_be_given_in_any_order(self, axis):
        assert window_mask(axis, 555., 545.).tolist() == window_mask(axis, 545., 555.).tolist()

    def test_a_window_narrower_than_the_pixels_takes_the_nearest_one(self, axis):
        assert axis[window_mask(axis, 550.25, 550.35)].tolist() == [550.]

    def test_a_zero_width_is_a_single_pixel(self, axis):
        assert axis[window_mask(axis, 580.4, 580.4)].tolist() == [580.]

    def test_a_window_partly_outside_of_the_spectrum_keeps_what_is_inside(self, axis):
        assert axis[window_mask(axis, 497., 507.)].tolist() == [500., 501., 502., 503., 504., 505., 506., 507.]

    @pytest.mark.parametrize('center', [400., 499., 601., 700.])
    def test_a_window_out_of_the_spectrum_selects_nothing(self, axis, center):
        # (one pixel of tolerance beyond the ends: 499 and 601 are the first pixels outside, they still select the edge)
        mask = window_mask(axis, center - 0.1, center + 0.1)

        assert mask.sum() == (1 if center in (499., 601.) else 0)

    def test_a_decreasing_axis(self, axis):
        mask = window_mask(axis[::-1], 548., 552.)

        assert sorted(axis[::-1][mask].tolist()) == [548., 549., 550., 551., 552.]

    def test_non_uniform_pixels(self):
        x = np.array([400., 410., 430., 460., 500.])

        assert x[window_mask(x, 420., 460.)].tolist() == [430., 460.]
        assert x[window_mask(x, 439., 441.)].tolist() == [430.]  # nearest

    def test_empty_axis(self):
        assert window_mask(np.array([]), 5., 6.).size == 0

    def test_axis_with_nan(self):
        x = np.array([np.nan, 1., 2., 3.])

        assert window_mask(x, 1.9, 2.1).tolist() == [False, False, True, False]


class TestValue:
    def test_it_is_the_mean_of_the_window(self, axis, spectrum):
        assert roi_value(axis, spectrum, 545., 555.) == pytest.approx(50.)

    def test_one_nm_window_on_pixels_of_a_tenth_of_a_nm(self):
        wavelengths = np.arange(600., 700., 0.1)
        counts = np.where(np.isclose(wavelengths, 650.), 1000., 10.)

        value = roi_value(wavelengths, counts, 649.5, 650.5)

        inside = (wavelengths >= 649.5) & (wavelengths <= 650.5)
        assert value == pytest.approx(counts[inside].mean())
        assert 10. < value < 1000.

    def test_outside_of_the_spectrum_is_nan(self, axis, spectrum):
        assert np.isnan(roi_value(axis, spectrum, 895., 905.))

    def test_spectrum_and_axis_must_match(self, axis, spectrum):
        with pytest.raises(ValueError, match='pixels'):
            roi_value(axis, spectrum[:-1], 545., 555.)

    def test_integer_spectra(self, axis):
        counts = np.arange(101, dtype=np.uint16)

        assert roi_value(axis, counts, 550., 550.) == 50.


class TestRoi:
    def test_geometry(self):
        roi = Roi('ROI 1', 600., 610.)

        assert roi.center == 605. and roi.width == 10.
        assert roi.record and roi.plot

    def test_value(self, axis, spectrum):
        assert Roi('a', 545., 555.).value(axis, spectrum) == pytest.approx(50.)

    def test_label(self):
        assert Roi('ROI 1', 650., 651.).label() == 'ROI 1'
        assert Roi('ROI 1', 650., 651.5).label('nm', with_range=True) == 'ROI 1 mean (650-651.5 nm)'
        assert Roi('ROI 1', 650., 651.).label('', with_range=True) == 'ROI 1 mean (650-651)'
        assert Roi('ROI 1', 650., 651., aggregation='sum').label('nm', with_range=True) == 'ROI 1 sum (650-651 nm)'

    def test_problems(self):
        assert Roi('a', 1., 2.).problems() == []
        assert Roi('a', 2., 2.).problems() == []
        assert 'after' in Roi('a', 3., 2.).problems()[0]
        assert 'finite' in Roi('a', float('nan'), 2.).problems()[0]

    def test_dict_round_trip(self):
        roi = Roi('peak', 650.1, 655.2, record=False, plot=True)

        assert Roi.from_dict(roi.to_dict()) == roi
        assert json.loads(json.dumps(roi.to_dict())) == roi.to_dict()

    @pytest.mark.parametrize('values', [{}, {'name': 'a'}, {'name': 'a', 'start': 'x', 'stop': 1},
                                        {'name': 'a', 'start': 3, 'stop': 1}, 'text', None])
    def test_not_a_roi(self, values):
        with pytest.raises(ValueError):
            Roi.from_dict(values)

    def test_the_flags_are_optional(self):
        roi = Roi.from_dict({'name': 'a', 'start': 1, 'stop': 2})

        assert roi.record and roi.plot


class TestDefaults:
    def test_six_rois(self):
        rois = default_rois(1)

        assert len(rois) == N_DEFAULT == 6
        assert [roi.name for roi in rois] == [f'ROI {i}' for i in range(1, 7)]

    def test_they_are_valid_and_inside_the_nominal_range(self):
        for unit in (1, 2, 3):
            for roi in default_rois(unit):
                assert roi.problems() == []
                assert NOMINAL_RANGE[0] <= roi.start < roi.stop <= NOMINAL_RANGE[1]
                assert DEFAULT_WIDTHS[0] - 0.11 <= roi.width <= DEFAULT_WIDTHS[1] + 0.11
                assert roi.record and roi.plot

    def test_they_do_not_overlap(self):
        for unit in (1, 2):
            rois = default_rois(unit)
            for first, second in zip(rois, rois[1:]):
                assert first.stop < second.start

    def test_they_are_always_the_same_for_a_given_spectrometer(self):
        assert default_rois(1) == default_rois(1)
        assert default_rois(2) == default_rois(2)

    def test_but_not_the_same_for_the_two_spectrometers(self):
        assert default_rois(1) != default_rois(2)

    def test_they_are_random_not_evenly_spaced(self):
        centers = np.diff([roi.center for roi in default_rois(1)])

        assert centers.std() > 0.5

    def test_other_numbers_and_ranges(self):
        rois = default_rois(1, n=3, x_range=(1000., 1100.))

        assert len(rois) == 3
        assert all(1000. <= roi.start and roi.stop <= 1100. for roi in rois)

    def test_the_edges_are_rounded_for_display(self):
        for roi in default_rois(1):
            assert roi.start == round(roi.start, 1) and roi.stop == round(roi.stop, 1)


class TestStore:
    def test_the_first_time_the_defaults_are_given_and_remembered(self, tmp_path):
        store = RoiStore(tmp_path / 'sub' / 'rois.json')

        rois = store.load(1)

        assert rois == default_rois(1)
        assert store.path.is_file()
        assert store.get(1) == rois

    def test_what_was_saved_comes_back_exactly(self, tmp_path):
        store = RoiStore(tmp_path / 'rois.json')
        edited = [Roi('a', 601.123456789, 603.5, record=False, plot=True), Roi('b', 700., 700., record=True, plot=False)]

        store.save(1, edited)

        assert RoiStore(tmp_path / 'rois.json').load(1) == edited  # (from another object: from the file)

    def test_each_spectrometer_has_its_own(self, tmp_path):
        store = RoiStore(tmp_path / 'rois.json')
        store.save(1, [Roi('one', 1., 2.)])
        store.save(2, [Roi('two', 3., 4.)])

        assert [roi.name for roi in store.load(1)] == ['one']
        assert [roi.name for roi in store.load(2)] == ['two']

    def test_no_roi_at_all_is_remembered_too(self, tmp_path):
        store = RoiStore(tmp_path / 'rois.json')
        store.save(1, [])

        assert store.load(1) == []

    def test_a_third_spectrometer_starts_with_its_defaults_and_keeps_the_others(self, tmp_path):
        store = RoiStore(tmp_path / 'rois.json')
        store.save(1, [Roi('one', 1., 2.)])

        assert store.load(3) == default_rois(3)
        assert [roi.name for roi in store.load(1)] == ['one']

    def test_a_file_which_cannot_be_read_is_kept_aside(self, tmp_path):
        path = tmp_path / 'rois.json'
        path.write_text('{ not json')
        store = RoiStore(path)

        assert store.load(1) == default_rois(1)
        assert (tmp_path / 'rois.json.bad').read_text() == '{ not json'
        assert json.loads(path.read_text())['units']['unit1']  # and a good one was written

    @pytest.mark.parametrize('content', ['[]', '{"units": 3}', '{"version": 1}', 'null'])
    def test_a_file_with_something_else_in_it(self, tmp_path, content):
        path = tmp_path / 'rois.json'
        path.write_text(content)

        assert RoiStore(path).load(2) == default_rois(2)
        assert (tmp_path / 'rois.json.bad').exists()

    def test_invalid_rois_in_the_file_are_replaced_by_the_defaults(self, tmp_path):
        path = tmp_path / 'rois.json'
        path.write_text(json.dumps({'version': 1, 'units': {'unit1': {'rois': [{'name': 'a', 'start': 5, 'stop': 1}]}}}))

        assert RoiStore(path).load(1) == default_rois(1)

    def test_a_missing_folder_is_created(self, tmp_path):
        store = RoiStore(tmp_path / 'a' / 'b' / 'rois.json')
        store.save(1, [Roi('x', 1., 2.)])

        assert store.get(1) == [Roi('x', 1., 2.)]

    def test_no_temporary_file_is_left(self, tmp_path):
        store = RoiStore(tmp_path / 'rois.json')
        store.save(1, [Roi('x', 1., 2.)])

        assert sorted(path.name for path in tmp_path.iterdir()) == ['rois.json']

    def test_the_file_can_be_read_and_edited_by_hand(self, tmp_path):
        store = RoiStore(tmp_path / 'rois.json')
        store.save(1, [Roi('x', 1., 2.)])

        content = json.loads(store.path.read_text())

        assert content == {'version': 1, 'units': {'unit1': {'rois': [
            {'name': 'x', 'start': 1.0, 'stop': 2.0, 'record': True, 'plot': True, 'aggregation': 'mean'}]}}}

    def test_unwritable_location_does_not_stop_the_defaults(self, tmp_path):
        blocker = tmp_path / 'blocker'
        blocker.write_text('a file where a folder should be')
        store = RoiStore(blocker / 'rois.json')

        assert store.load(1) == default_rois(1)


def test_unique_names():
    rois = [Roi('a', 1, 2), Roi('b', 1, 2), Roi('a', 3, 4), Roi('a', 5, 6)]

    assert unique_names(rois) == ['a', 'b', 'a (2)', 'a (3)']
    assert [roi.name for roi in renamed(rois)] == ['a', 'b', 'a (2)', 'a (3)']
    assert renamed(rois)[2].start == 3
