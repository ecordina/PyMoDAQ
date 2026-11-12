"""The exposure time of a camera, changed while an experiment runs: given to the camera at once between two readouts, a
readout which comes right after waits until the camera ran with it, and the images which come late are told"""
import time

import pytest

from pymodaq_plugins_thermospectro.unit import UnitState

from test_camera import Bench, bench  # noqa: F401  (bench is a fixture)

pytestmark = pytest.mark.filterwarnings('ignore')

EXPOSURE = ('camera_settings', 'exposure_time')


def run(bench: Bench, readout=0.1, margin=0.3):
    """Live view, then the experiment, with the journal of the unit and a short margin to settle"""
    bench.journal = []
    bench.unit.journal.connect(bench.journal.append)
    bench.settings.child('readout_time').setValue(readout)
    bench.unit.SETTLE_MARGIN = margin
    bench.unit.start_live(time.time())
    bench.qtbot.waitUntil(lambda: bench.unit.ready, timeout=10000)
    bench.unit.begin_run(time.time(), 0.)
    bench.base = len(bench.measurements)  # (the live view made some: the ones of the experiment come after)


def wait_run(bench: Bench, n: int):
    """Wait for `n` measurements of the experiment"""
    bench.wait_for(bench.base + n)


def change(bench: Bench, milliseconds: float):
    """What the user does, and what the extension does when the setting changes"""
    bench.settings.child('integration_time').setValue(milliseconds)
    bench.unit.integration_changed()


class TestGivenAtOnce:
    def test_a_camera_is_given_the_new_time_between_two_readouts(self, bench):
        run(bench, readout=3.)  # (the camera idles for 3 s after each image)
        wait_run(bench, 1)
        assert bench.unit._pending is None and bench.camera.settings[EXPOSURE] == 20.

        change(bench, 40.)

        assert bench.camera.settings[EXPOSURE] == 40.  # (not before the next readout, which is 3 s away)
        assert bench.unit.integration_time_now() == 40.

    def test_it_is_told_and_written_in_the_journal(self, bench):
        run(bench, readout=3.)
        wait_run(bench, 1)

        change(bench, 40.)

        assert any('changed from 20 to 40 ms' in notice for notice in bench.notices)
        assert [text for text in bench.journal if 'changed from 20 to 40 ms' in text] == \
               ['Camera: the exposure time was changed from 20 to 40 ms']

    def test_a_time_which_does_not_change_is_not_told(self, bench):
        run(bench, readout=3.)
        wait_run(bench, 1)

        change(bench, 20.)

        assert not bench.journal and not any('changed' in notice for notice in bench.notices)

    def test_a_readout_on_its_way_is_not_touched(self, qtbot):
        bench = Bench(qtbot, delay=300)
        try:
            run(bench, readout=0.5)
            qtbot.waitUntil(lambda: bench.unit._pending is not None, timeout=10000)

            change(bench, 40.)

            assert bench.camera.settings[EXPOSURE] == 20.  # (the image on its way is exposed with the old time)
            wait_run(bench, 1)
            assert bench.measurements[0].integration_time == 20.
            # ... and the new time is given as soon as the image is back, not before the readout which follows
            assert bench.camera.settings[EXPOSURE] == 40.
        finally:
            bench.unit._release()

    def test_a_time_which_is_not_shorter_than_the_readout_time_is_refused_at_once(self, bench):
        run(bench, readout=3.)
        wait_run(bench, 1)

        change(bench, 3500.)

        assert bench.camera.settings[EXPOSURE] == 20. and bench.settings.integration_time == 20.
        assert any('not shorter than the readout time' in notice for notice in bench.notices)

    def test_only_a_camera_is_given_it_at_once(self):
        from pymodaq_plugins_thermospectro.unit import CameraUnit, Unit
        assert CameraUnit.ADJUSTS_AT_ONCE and not Unit.ADJUSTS_AT_ONCE  # (a spectrometer: before its next readout)


class TestTheReadoutWaits:
    def test_the_first_readout_after_the_change_comes_once_the_camera_ran_with_the_time(self, bench):
        run(bench, readout=0.1, margin=0.3)
        wait_run(bench, 2)
        asked = time.time()

        change(bench, 40.)
        done = len(bench.measurements)
        bench.wait_for(done + 3)

        after = [m for m in bench.measurements[done:] if m.integration_time == 40.]
        assert after and all(m.t >= asked + 0.3 + 0.06 - 0.02 for m in after)  # (20 ms + 40 ms + the margin)
        assert all(m.integration_time in (20., 40.) for m in bench.measurements)
        # none of them has the old time after the new one
        times = [m.integration_time for m in bench.measurements]
        assert times == sorted(times)
        assert bench.unit.n_skipped == 0  # (a readout which waits is not a missed one)

    def test_nothing_is_asked_of_the_camera_while_it_settles(self, bench):
        run(bench, readout=0.1, margin=0.3)
        wait_run(bench, 2)

        change(bench, 40.)
        before = bench.camera.snaps
        bench.qtbot.wait(150)  # (less than the wait: 0.36 s, the readouts are asked for every 0.1 s)

        assert bench.camera.snaps == before

    def test_a_camera_which_is_disarmed_between_its_readouts_does_not_wait(self, bench):
        run(bench, readout=0.1, margin=5.)
        bench.settings.child('disarm').setValue(True)
        wait_run(bench, 1)

        change(bench, 40.)

        assert not bench.unit._must_wait()

    def test_the_wait_is_over_when_the_experiment_is(self, bench):
        run(bench, readout=0.1, margin=5.)
        wait_run(bench, 1)

        change(bench, 40.)
        bench.qtbot.waitUntil(lambda: bench.unit._settle_timer.isActive(), timeout=5000)
        bench.unit.end_run()

        assert not bench.unit._settle_timer.isActive() and bench.unit.state == UnitState.LIVE

    def test_a_second_change_waits_again(self, bench):
        run(bench, readout=3., margin=0.3)
        wait_run(bench, 1)

        change(bench, 40.)
        change(bench, 60.)

        assert bench.camera.settings[EXPOSURE] == 60.
        left = bench.unit._settle_until - time.monotonic()
        assert left == pytest.approx((40. + 60.) / 1000. + 0.3, abs=0.15)  # (an image of the last two times)

    def test_in_the_live_view_nothing_waits(self, bench):
        bench.unit.start_live(time.time())
        bench.qtbot.waitUntil(lambda: bench.unit.ready, timeout=10000)

        change(bench, 40.)

        assert bench.camera.settings[EXPOSURE] == 40. and not bench.unit._must_wait()


class TestLateImages:
    def test_an_image_which_comes_after_the_readout_time_is_said_with_how_late_it_was(self, qtbot, caplog):
        bench = Bench(qtbot, delay=250)  # (the images take 0.25 s, the readouts are asked every 0.1 s)
        try:
            run(bench, readout=0.1)
            wait_run(bench, 2)

            assert bench.unit.n_late >= 2 and bench.unit.latency_max >= 0.2
            messages = [r.getMessage() for r in caplog.records if 'more than the readout time' in r.getMessage()]
            assert 1 <= len(messages) <= 2  # (said at the first one, of the live view and of the experiment: then at the 10th)
            assert 'Camera1' in messages[-1] and 'as soon as it is back' in messages[-1]  # (in the experiment it catches up)
        finally:
            bench.unit._release()

    def test_the_summary_of_the_experiment(self, qtbot):
        bench = Bench(qtbot, delay=250)
        try:
            run(bench, readout=0.1)
            wait_run(bench, 3)

            summary = bench.unit.latency_summary()

            assert 'images came back' in summary and 'at most' in summary
            assert f'{bench.unit.n_skipped} readouts skipped' in summary
            assert 'made as soon as the image was back' in summary
        finally:
            bench.unit._release()

    def test_nothing_is_said_of_an_image_in_time(self, bench):
        run(bench, readout=3.)
        wait_run(bench, 1)

        assert bench.unit.n_late == 0 and 0. < bench.unit.latency_max < 1.

    def test_the_counters_start_with_the_experiment(self, bench):
        bench.unit.start_live(time.time())
        bench.qtbot.waitUntil(lambda: bench.unit.ready, timeout=10000)
        bench.unit.n_latency, bench.unit.n_late, bench.unit.latency_max = 5, 2, 9.

        bench.unit.begin_run(time.time(), 0.)

        assert bench.unit.n_latency <= 1 and bench.unit.n_late == 0 and bench.unit.latency_max < 1.


class TestTheFileIsTold:
    """How late the images were is written in the file, as an observation, every hour: when some were late"""

    def test_only_a_camera_does_it(self):
        from pymodaq_plugins_thermospectro.unit import CameraUnit, Unit
        assert CameraUnit.JOURNALS_LATENCY and not Unit.JOURNALS_LATENCY and CameraUnit.LATENCY_REPORT_EVERY == 3600.

    def test_late_images_are_in_the_journal_once_the_window_is_over(self, qtbot):
        bench = Bench(qtbot, delay=250)  # (every image is later than the readout time)
        try:
            run(bench, readout=0.1)
            bench.unit.LATENCY_REPORT_EVERY = 0.4  # (instead of an hour)
            bench.unit._window[0] = time.monotonic()
            wait_run(bench, 5)

            notes = [text for text in bench.journal if 'later than the readout time' in text]

            assert notes and notes[0].startswith('Camera: in the last ')
            assert 'came back later than the readout time (0.1 s)' in notes[0]
            assert 'readouts were made as soon as the image was back' in notes[0]
        finally:
            bench.unit._release()

    def test_a_window_without_a_late_image_says_nothing(self, bench):
        run(bench, readout=0.1)
        bench.unit.LATENCY_REPORT_EVERY = 0.3
        bench.unit._window[0] = time.monotonic()

        wait_run(bench, 8)  # (more than one window, the images take 10 ms)

        assert not [text for text in bench.journal if 'later than' in text]
        assert bench.unit._window[0] > 0 and bench.unit.n_late == 0

    def test_the_window_starts_again_after_a_report(self, qtbot):
        bench = Bench(qtbot, delay=250)
        try:
            run(bench, readout=0.1)
            bench.unit.LATENCY_REPORT_EVERY = 0.3
            bench.unit._window[0] = time.monotonic()
            wait_run(bench, 8)

            notes = [text for text in bench.journal if 'later than the readout time' in text]

            assert len(notes) >= 2  # (one for each window, not one with everything since the start)
        finally:
            bench.unit._release()

    def test_nothing_is_written_in_the_live_view(self, qtbot):
        bench = Bench(qtbot, delay=250)
        try:
            bench.journal = []
            bench.unit.journal.connect(bench.journal.append)
            bench.settings.child('readout_time').setValue(0.1)
            bench.unit.LATENCY_REPORT_EVERY = 0.05
            bench.unit.start_live(time.time())
            bench.qtbot.waitUntil(lambda: bench.unit.ready, timeout=10000)
            bench.qtbot.wait(700)

            assert bench.unit.n_late >= 1 and not bench.journal
        finally:
            bench.unit._release()
