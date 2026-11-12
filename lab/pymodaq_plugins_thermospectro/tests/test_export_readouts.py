"""lab_export tells how many readouts an experiment did not make (a readout which was skipped, lost or put off leaves a gap in
the times of the readouts)"""
import numpy as np
import pytest

from test_export import export, run_export, write_experiment
from test_export import h5saver  # noqa: F401  (a fixture)
from test_export_notes import read_text

pytestmark = pytest.mark.filterwarnings('ignore')


class TestTheGaps:
    def test_regular_readouts_miss_nothing(self):
        assert export.readout_gaps(np.arange(0, 50, 5.), 5.) == (0, 10, 5.)

    def test_a_gap_of_two_readout_times_is_one_missing_readout(self):
        times = np.array([0., 5., 10., 20., 25.])  # (the readout of 15 s was skipped)

        assert export.readout_gaps(times, 5.) == (1, 6, 10.)

    def test_a_long_gap_is_several_missing_readouts(self):
        times = np.array([0., 5., 52., 57.])  # (a lost image: 47 s)

        missing, expected, longest = export.readout_gaps(times, 5.)

        assert missing == 8 and expected == 12 and longest == 47.

    def test_the_jitter_is_not_a_gap(self):
        times = np.cumsum([0., 5.01, 4.99, 5.02, 4.98, 5.0])

        assert export.readout_gaps(times, 5.)[0] == 0

    def test_a_readout_asked_again_a_little_late_is_not_a_gap(self):
        times = np.array([0., 5., 12., 17.])  # (7 s: 1.4 readout times)

        assert export.readout_gaps(times, 5.)[0] == 0

    def test_the_times_do_not_have_to_be_in_order(self):
        assert export.readout_gaps(np.array([10., 0., 20.]), 10.)[:2] == (0, 3)

    @pytest.mark.parametrize('times, readout', [([0.], 5.), ([], 5.), ([0., 5.], 0.), ([0., 5.], -1.)])
    def test_it_cannot_be_said(self, times, readout):
        assert export.readout_gaps(np.array(times), readout) is None

    def test_what_is_not_a_time_is_left_out(self):
        assert export.readout_gaps(np.array([0., np.nan, 10.]), 10.) == (0, 2, 10.)


class TestTheReport:
    def text(self, h5saver, tmp_path, **kwargs):
        path = write_experiment(h5saver, spectrometer=False, **kwargs)
        out = run_export(path, tmp_path)
        return read_text(next(out.glob('*_experiment.txt')))

    def test_a_regular_experiment_misses_nothing(self, h5saver, qapp, tmp_path):
        text = self.text(h5saver, tmp_path, camera_times=(1000.4, 1.0, 5))  # (the readout time is 1 s)

        assert '  readouts: 5 made, none missing' in text

    def test_missing_readouts_are_counted_and_said(self, h5saver, qapp, tmp_path):
        text = self.text(h5saver, tmp_path, camera_times=(1000.4, 2.0, 5),  # (one every 2 s, the readout time is 1 s)
                         camera_integration=lambda index: 100.)

        line = next(line for line in text.splitlines() if line.startswith('  readouts:'))
        assert line.startswith('  readouts: 5 made, 4 missing of 9 (44.4 %)')
        assert 'the longest time is 2 s' in line and 'late, or was lost' in line

    def test_it_comes_after_what_was_saved(self, h5saver, qapp, tmp_path):
        lines = self.text(h5saver, tmp_path, camera_times=(1000.4, 2.0, 5),
                          camera_integration=lambda index: 100.).splitlines()

        saved = next(i for i, line in enumerate(lines) if line.startswith('  saved:'))
        assert lines[saved + 1].startswith('  readouts:')

    def test_one_readout_says_nothing(self, h5saver, qapp, tmp_path):
        text = self.text(h5saver, tmp_path, camera_times=(1000.4, 1.0, 1))

        assert '  readouts:' not in text


class TestTheTimesOfAnImage:
    """The time of a readout of a camera is the one its image reached the software, up to an exposure after the readout"""

    def test_an_image_may_come_up_to_an_exposure_after_its_readout_without_a_readout_missing(self):
        times = np.array([0., 5., 13., 18.])  # (the second image of 8 s: a 4 s exposure, a readout time of 5 s)

        assert export.readout_gaps(times, 5.)[0] == 1  # (on the grid of the readouts a gap of 8 s is a missing readout)
        assert export.readout_gaps(times, 5., slack=4.) == (0, 4, 8.)

    def test_a_real_gap_is_still_one(self):
        times = np.array([0., 5., 25., 30.])

        assert export.readout_gaps(times, 5., slack=4.)[0] == 3

    def test_the_file_says_what_the_time_of_a_readout_is(self, h5saver, qapp, tmp_path):
        path = write_experiment(h5saver, spectrometer=False)

        text = read_text(next(run_export(path, tmp_path).glob('*_experiment.txt')))

        assert '  time of a readout: when its image reached the software' in text

    def test_a_spectrometer_says_nothing_of_it(self, h5saver, qapp, tmp_path):
        path = write_experiment(h5saver, camera=False)

        text = read_text(next(run_export(path, tmp_path).glob('*_experiment.txt')))

        assert 'time of a readout' not in text

    def test_an_older_file_says_it_was_when_the_readout_was_asked_for(self, h5saver, qapp, tmp_path):
        import h5py
        path = write_experiment(h5saver, spectrometer=False)
        with h5py.File(path, 'r+') as file:
            def drop(name, node):
                if 'time_stamp' in node.attrs:
                    del node.attrs['time_stamp']
            file.visititems(drop)

        text = read_text(next(run_export(path, tmp_path).glob('*_experiment.txt')))

        assert '  time of a readout: when it was asked for (a file written before' in text

    def test_the_slack_is_the_longest_exposure(self, h5saver, qapp, tmp_path):
        # (images 2 s apart, a readout time of 1 s: missing readouts, unless the exposure is a second or more)
        path = write_experiment(h5saver, spectrometer=False, camera_times=(1000.4, 2.0, 5), camera_integration=lambda i: 1500.)

        text = read_text(next(run_export(path, tmp_path).glob('*_experiment.txt')))

        assert 'readouts: 5 made, none missing' in text
