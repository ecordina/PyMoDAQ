"""The time of a measurement of a camera is the time its image reached the software, not the time it was asked for; the
delay of an image is told in two parts: the plugin had it after..., the software got it..."""
import time

import pytest

from pymodaq_data.data import DataRaw, DataToExport

from pymodaq_plugins_thermospectro.unit import CameraUnit, Unit

from test_camera import Bench, bench  # noqa: F401  (bench is a fixture)
from test_camera_exposure import run, wait_run

pytestmark = pytest.mark.filterwarnings('ignore')


def stamp_images(bench: Bench, stamp):
    """The images of the fake camera are stamped by `stamp()` (the plugin stamps the data as the image comes)"""
    def answer():
        camera = bench.camera
        camera.in_flight -= 1
        dwa = DataRaw('image', data=[camera.image()])
        dwa.timestamp = stamp()
        camera.grab_done_signal.emit(DataToExport(camera.title, data=[dwa]))
    bench.camera._answer = answer


def record_snaps(bench: Bench) -> list:
    """When each readout was asked for (epoch)"""
    asked = []
    snap = bench.camera.snap

    def spy():
        asked.append(time.time())
        snap()
    bench.camera.snap = spy
    return asked


class TestTheTimeIsTheOneOfTheImage:
    def test_it_is_not_the_time_the_readout_was_asked_for(self, qtbot):
        bench = Bench(qtbot, delay=300)  # (the image takes 0.3 s: the plugin makes the data when it has the image)
        try:
            asked = record_snaps(bench)
            run(bench, readout=3.)
            wait_run(bench, 1)

            measurement = bench.measurements[bench.base]
            snapped = min(asked, key=lambda a: abs(a - measurement.t))  # (the readout it answers: the nearest)

            assert measurement.t - snapped >= 0.25  # (it was exposed and transferred in the meantime)
            assert measurement.t <= time.time()
        finally:
            bench.unit._release()

    def test_it_is_the_stamp_of_the_data_of_the_plugin(self, bench):
        run(bench, readout=3.)
        wait_run(bench, 1)
        stamp_images(bench, lambda: time.time() - 0.005)  # (the plugin had the image a moment ago)
        bench.unit._pending = (time.time() - 0.02, time.monotonic() - 0.02, 20.)  # (asked for a moment before that)
        done = len(bench.measurements)

        bench.camera._answer()

        assert len(bench.measurements) == done + 1
        assert time.time() - 0.02 < bench.measurements[-1].t <= time.time() - 0.001

    def test_a_stamp_is_used_as_it_is(self, bench):
        run(bench, readout=3.)
        wait_run(bench, 1)
        stamps = []

        def stamp():
            stamps.append(time.time() - 0.004)  # (4 ms before the unit gets it)
            return stamps[-1]
        stamp_images(bench, stamp)
        bench.unit._pending = (time.time() - 0.02, time.monotonic() - 0.02, 20.)

        bench.camera._answer()

        assert bench.measurements[-1].t == stamps[-1]

    def test_an_image_made_a_moment_before_the_readout_was_asked_for_has_its_own_time(self, bench):
        """A camera which keeps its newest images gives one which was made before the readout (up to the images of its buffer)"""
        run(bench, readout=3.)
        wait_run(bench, 1)
        made = time.time() - 0.3
        stamp_images(bench, lambda: made)
        bench.unit._pending = (time.time() - 0.01, time.monotonic() - 0.01, 20.)  # (asked for 0.01 s ago)

        bench.camera._answer()

        assert bench.measurements[-1].t == made  # (0.29 s before the readout: not later than the time it was made)
        assert bench.unit.plugin_sum < 0  # (the plugin delay is negative: the image was there already)

    def test_a_stamp_from_before_the_readout_is_not_the_time_of_its_image(self, bench):
        run(bench, readout=3.)
        wait_run(bench, 1)
        stamp_images(bench, lambda: time.time() - 60.)  # (data made long before: a camera which gives an old image)
        asked = time.time() - 0.02
        bench.unit._pending = (asked, time.monotonic() - 0.02, 20.)

        bench.camera._answer()

        assert asked <= bench.measurements[-1].t <= time.time()  # (the time the unit got it)

    def test_a_stamp_from_the_future_is_not_either(self, bench):
        run(bench, readout=3.)
        wait_run(bench, 1)
        stamp_images(bench, lambda: time.time() + 3600.)
        bench.unit._pending = (time.time() - 0.02, time.monotonic() - 0.02, 20.)

        bench.camera._answer()

        assert bench.measurements[-1].t <= time.time()

    def test_the_curves_and_the_file_follow(self, bench):
        run(bench, readout=3.)
        wait_run(bench, 2)

        times = [m.t for m in bench.measurements[bench.base:]]
        series = bench.unit.series_rois

        assert times == sorted(times) and len(series) >= 2

    def test_the_rules(self):
        assert CameraUnit.STAMPS_IMAGE and not Unit.STAMPS_IMAGE
        camera = CameraUnit.__new__(CameraUnit)
        spectrometer = Unit.__new__(Unit)

        assert camera._measurement_time(10., 12., 11.) == 11.  # (the stamp of the plugin)
        assert camera._measurement_time(10., 12., None) == 12.  # (else when the unit got it)
        assert spectrometer._measurement_time(10., 12., 11.) == 10.  # (a spectrometer: when the readout was asked for)


class TestTheDelayInTwoParts:
    def test_the_plugin_and_the_software_parts_are_counted(self, qtbot):
        bench = Bench(qtbot, delay=250)  # (the plugin has the image 0.25 s after the readout)
        try:
            run(bench, readout=3.)
            wait_run(bench, 2)

            unit = bench.unit

            assert unit.n_split >= 2
            assert 0.2 <= unit.plugin_max < 1.0
            assert unit.software_max < 0.2  # (the software gets what the plugin has at once)
            assert abs(unit.plugin_sum + unit.software_sum - unit.latency_sum) < 0.05
        finally:
            bench.unit._release()

    def test_a_late_image_says_which_part_was_late(self, qtbot, caplog):
        bench = Bench(qtbot, delay=250)
        try:
            run(bench, readout=0.1)
            wait_run(bench, 2)

            messages = [r.getMessage() for r in caplog.records if 'more than the readout time' in r.getMessage()]

            assert messages and 'the plugin had the image' in messages[-1] and 'the software got it' in messages[-1]
        finally:
            bench.unit._release()

    def test_the_summary_has_both(self, qtbot):
        bench = Bench(qtbot, delay=250)
        try:
            run(bench, readout=0.1)
            wait_run(bench, 3)

            summary = bench.unit.latency_summary()

            assert 'stamped by the plugin' in summary and 'got by the software' in summary
        finally:
            bench.unit._release()

    def test_the_note_of_the_file_has_the_parts_of_the_longest(self, qtbot):
        bench = Bench(qtbot, delay=250)
        try:
            run(bench, readout=0.1)
            bench.unit.LATENCY_REPORT_EVERY = 0.4
            bench.unit._window[0] = time.monotonic()
            wait_run(bench, 5)

            notes = [text for text in bench.journal if 'later than the readout time' in text]

            assert notes and '(the longest: the plugin had the image' in notes[0] and 'the software got it' in notes[0]
        finally:
            bench.unit._release()

    def test_a_spectrometer_has_no_parts(self, bench):
        bench.unit._data_time = None
        bench.unit._note_latency(1.0)

        assert bench.unit.n_split == 0 and bench.unit.plugin_max == 0.

    def test_the_counters_start_with_the_experiment(self, bench):
        bench.unit.n_split, bench.unit.plugin_max = 4, 9.

        bench.unit.reset_counters()

        assert bench.unit.n_split == 0 and bench.unit.plugin_max == 0. and bench.unit._window[5] is None


class TestTheWordsOfTheDelay:
    def test_the_plugin_then_the_software(self):
        words = Unit._delay_words(3.94, 1.81)

        assert words == ('the plugin had the image 3.9 s after the readout was asked for, the software got it 1.8 s later')

    def test_an_image_which_was_made_before_the_readout(self):
        words = Unit._delay_words(-0.31, 0.5)

        assert words == ('the image had been made 0.3 s before the readout was asked for, the software got it 0.5 s after it '
                         'was made')
