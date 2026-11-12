"""A camera whose image comes after the next readout was due: that readout is not skipped, it is asked for as soon as the
image is back (and the readout time counts again from there)"""
import time

import pytest

from pymodaq_plugins_thermospectro.unit import CameraUnit, Unit

from test_camera import Bench, bench  # noqa: F401  (bench is a fixture)
from test_camera_exposure import run, wait_run

pytestmark = pytest.mark.filterwarnings('ignore')


def late_image(bench: Bench, ticks: int):
    """An image comes back after `ticks` readouts were due while it was on its way (what the camera does when it is slow)"""
    bench.unit._pending = (time.time(), time.monotonic(), 20.)
    bench.unit._skipped_pending = ticks
    bench.camera._answer()


class TestWhatIsDone:
    def test_only_a_camera_catches_up(self):
        assert CameraUnit.CATCHES_UP and not Unit.CATCHES_UP

    def test_the_next_readout_is_asked_for_at_once(self, bench):
        run(bench, readout=3.)  # (the next regular readout is 3 s away)
        wait_run(bench, 1)
        snaps = bench.camera.snaps

        late_image(bench, 2)

        assert bench.unit._resume_timer.isActive() and not bench.unit._timer.isActive()
        bench.qtbot.waitUntil(lambda: bench.camera.snaps > snaps, timeout=1000)  # (far less than the readout time)
        assert bench.unit._timer.isActive() and bench.unit._timer.interval() == 3000  # (counts again from there)

    def test_the_readouts_due_meanwhile_are_not_skipped_nor_missed(self, bench):
        run(bench, readout=3.)
        wait_run(bench, 1)

        late_image(bench, 2)

        assert bench.unit.n_caught_up == 2 and bench.unit.n_skipped == 0 and bench.unit.n_missed == 0
        assert 'made late' in bench.unit.status_text() and '2 made late' in bench.unit.status_text()

    def test_the_image_is_a_measurement_like_the_others(self, bench):
        run(bench, readout=3.)
        wait_run(bench, 1)
        done = len(bench.measurements)

        late_image(bench, 1)

        assert len(bench.measurements) == done + 1

    def test_nothing_is_caught_up_when_the_image_was_in_time(self, bench):
        run(bench, readout=3.)
        wait_run(bench, 1)

        late_image(bench, 0)

        assert not bench.unit._resume_timer.isActive() and bench.unit._timer.isActive()
        assert bench.unit.n_caught_up == 0

    def test_in_the_live_view_the_readout_is_skipped_as_before(self, bench):
        bench.settings.child('readout_time').setValue(3.)
        bench.unit.start_live(time.time())
        bench.qtbot.waitUntil(lambda: bench.unit.ready, timeout=10000)

        late_image(bench, 2)

        assert bench.unit.n_skipped == 2 and bench.unit.n_caught_up == 0 and not bench.unit._resume_timer.isActive()

    def test_a_camera_which_is_disarmed_is_armed_again_after_a_guard(self, bench):
        run(bench, readout=3.)
        bench.settings.child('disarm').setValue(True)
        wait_run(bench, 1)

        late_image(bench, 1)

        assert bench.unit._resume_timer.interval() == int(bench.unit.DISARM_GUARD * 1000)

    def test_an_image_which_never_comes_makes_the_readouts_due_skipped(self, bench):
        run(bench, readout=3.)
        wait_run(bench, 1)
        bench.camera.silent = True
        bench.unit._pending = (time.time(), time.monotonic() - 100., 20.)
        bench.unit._skipped_pending = 3

        bench.unit._on_miss()

        assert bench.unit.n_skipped == 3 and bench.unit.n_lost == 1 and bench.unit.n_caught_up == 0
        assert bench.unit._skipped_pending == 0

    def test_a_new_readout_forgets_the_ones_of_the_last(self, bench):
        run(bench, readout=3.)
        wait_run(bench, 1)
        bench.unit._skipped_pending = 5

        bench.unit._readout()

        assert bench.unit._skipped_pending == 0 or bench.unit._pending is None


class TestInARealRun:
    def test_a_slow_camera_is_read_as_often_as_it_can_deliver(self, qtbot):
        bench = Bench(qtbot, delay=300)  # (an image takes 0.3 s, a readout is due every 0.1 s)
        try:
            run(bench, readout=0.1)
            wait_run(bench, 5)

            times = [m.t for m in bench.measurements[bench.base:]]
            gaps = [b - a for a, b in zip(times, times[1:])]
            assert bench.unit.n_caught_up >= 3 and bench.unit.n_skipped == 0
            assert bench.camera.max_in_flight == 1  # (never two readouts at once)
            assert all(gap >= 0.28 for gap in gaps)  # (never closer than the time an image takes)
            assert max(gaps) < 0.6  # (and no readout is lost in between)
        finally:
            bench.unit._release()
