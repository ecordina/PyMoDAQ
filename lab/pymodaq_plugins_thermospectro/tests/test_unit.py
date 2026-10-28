import math
import re
import time

import numpy as np
import pytest

from pymodaq_gui.parameter import Parameter

from fakes import FakeDetector, FakeManager, FakeThermostat
from pymodaq_plugins_thermospectro.ramp import Phase
from pymodaq_plugins_thermospectro.rois import Roi, default_rois
from pymodaq_plugins_thermospectro.unit import Problem, Unit, UnitState
from pymodaq_plugins_thermospectro.unit_settings import UnitSettings, unit_definition


pytestmark = pytest.mark.filterwarnings('ignore')


class Bench:
    """A unit with a fake spectrometer and a fake thermostat, and what it said"""

    def __init__(self, qtbot, thermostat=True, detector_kwargs=None):
        self.qtbot = qtbot
        self.detector = FakeDetector('Spectro1', **(detector_kwargs or {}))
        self.thermostat = FakeThermostat('Thermo1') if thermostat else None
        modules = [self.detector] + ([self.thermostat] if thermostat else [])
        self.settings = UnitSettings(Parameter.create(**unit_definition(1, default_rois(1))), 1)
        self.settings.set_modules(['Spectro1'], ['Thermo1'] if thermostat else [], 'Spectro1',
                                  'Thermo1' if thermostat else None)
        self.settings.child('integration_time').setValue(20.)  # (the defaults are 500 ms and 1 s: too slow for tests)
        self.settings.child('readout_time').setValue(0.1)
        self.unit = Unit(self.settings, FakeManager(*modules))
        self.now = 0.0
        self.unit.clock = lambda: self.now  # the clock of the ramp is given by the test
        self.unit.attach()
        self.measurements = []
        self.notices = []
        self.events = []
        self.problems = []
        self.unit.measurement.connect(self.measurements.append)
        self.unit.notice.connect(self.notices.append)
        self.unit.journal.connect(self.events.append)
        self.unit.problem.connect(lambda unit, kind, message: self.problems.append((kind, message)))

    def wait_for(self, n, timeout=10000):
        self.qtbot.waitUntil(lambda: len(self.measurements) >= n, timeout=timeout)

    def program(self, **values):
        for name, value in values.items():
            self.settings.child('ramp', name).setValue(value)
        self.settings.refresh_ramp()

    def run(self, now=0.0, origin=None):
        """Live view, then the experiment (the clock of the ramp is given by the test)"""
        self.unit.start_live(time.time())
        self.qtbot.waitUntil(lambda: self.unit.ready, timeout=10000)
        self.now = now
        self.unit.begin_run(origin or time.time(), now)

    def tick(self, now):
        self.now = now
        return self.unit.program_tick(now)


@pytest.fixture
def bench(qtbot):
    bench = Bench(qtbot)
    yield bench
    if bench.unit.state != UnitState.IDLE:
        bench.unit._release()


@pytest.fixture
def bench_without_thermostat(qtbot):
    bench = Bench(qtbot, thermostat=False)
    yield bench
    if bench.unit.state != UnitState.IDLE:
        bench.unit._release()


class TestAttach:
    def test_the_modules_are_found(self, bench):
        assert bench.unit.detector is bench.detector
        assert bench.unit.thermostat is bench.thermostat
        assert bench.unit.title == 'Spectro1'
        assert bench.unit.units == '°C'
        assert bench.settings.child('ramp', 'initial').opts['suffix'] == '°C'

    def test_nothing_selected(self, qtbot):
        settings = UnitSettings(Parameter.create(**unit_definition(2, default_rois(2))), 2)
        unit = Unit(settings, FakeManager())
        unit.attach()

        assert unit.detector is None and unit.thermostat is None
        assert unit.title == 'None' and unit.units == ''

    def test_a_module_which_does_not_exist(self, bench):
        bench.settings.child('spectrometer').setLimits(['None', 'Spectro1', 'Gone'])
        bench.settings.child('spectrometer').setValue('Gone')

        bench.unit.attach()

        assert bench.unit.detector is None
        assert 'does not exist' in bench.unit.problems()[0]


class TestProblems:
    def test_a_valid_unit_has_none(self, bench):
        assert bench.unit.problems() == []

    def test_no_spectrometer(self, bench):
        bench.settings.child('spectrometer').setValue('None')
        bench.unit.attach()

        assert 'no spectrometer is selected' in bench.unit.problems()[0]

    def test_not_initialized_modules(self, bench):
        bench.detector.initialized_state = False
        bench.thermostat.initialized_state = False

        problems = bench.unit.problems()

        assert any('detector Spectro1 is not initialized' in p for p in problems)
        assert any('thermostat Thermo1 is not initialized' in p for p in problems)

    def test_the_readout_time_must_be_longer_than_the_integration_time(self, bench):
        bench.settings.child('integration_time').setValue(500.)
        bench.settings.child('readout_time').setValue(0.4)

        problems = bench.unit.problems()

        assert len(problems) == 1 and 'readout time' in problems[0] and 'integration time' in problems[0]

    def test_an_invalid_ramp(self, bench):
        bench.program(final=25.)  # the initial value

        assert 'ramp is not valid' in bench.unit.problems()[0]

    def test_an_invalid_ramp_does_not_matter_when_it_is_not_run(self, bench):
        bench.program(final=25.)
        bench.settings.child('ramp', 'run').setValue(False)

        assert bench.unit.problems() == []

    def test_without_thermostat_there_is_no_ramp_and_no_problem(self, bench_without_thermostat):
        unit = bench_without_thermostat.unit

        assert unit.problems() == []
        assert not unit.has_program

    def test_the_ramp_is_run_when_asked_and_possible(self, bench):
        assert bench.unit.has_program
        bench.settings.child('ramp', 'run').setValue(False)
        assert not bench.unit.has_program


class TestLiveView:
    def test_it_reads_out_once_per_readout_time(self, bench, qtbot):
        bench.unit.start_live(time.time())
        bench.wait_for(5)

        times = np.diff([m.t for m in bench.measurements[:5]])
        assert bench.unit.state == UnitState.LIVE
        assert np.all(times > 0.09) and np.all(times < 0.5)
        assert bench.detector.snaps >= 5
        assert [m.index for m in bench.measurements[:5]] == [0, 1, 2, 3, 4]

    def test_the_first_readout_is_at_once(self, bench, qtbot):
        bench.settings.child('readout_time').setValue(30.)

        started = time.time()
        bench.unit.start_live(started)
        bench.wait_for(1, timeout=2000)

        assert bench.measurements[0].t - started < 1.

    def test_the_temperature_is_asked_at_each_readout(self, bench):
        bench.unit.start_live(time.time())
        bench.wait_for(5)

        assert bench.thermostat.reads >= 5
        assert all(m.temperature == 20. for m in bench.measurements)

    def test_the_integration_time_is_set(self, bench):
        bench.settings.child('integration_time').setValue(500.)
        bench.settings.child('readout_time').setValue(1.)  # (the defaults)
        bench.unit.start_live(time.time())

        assert bench.detector.settings['detector_settings', 'integration'] == 500.

    def test_the_integration_time_is_set_as_asked(self, bench):
        bench.settings.child('integration_time').setValue(1200.)
        bench.unit.start_live(time.time())

        assert bench.detector.settings['detector_settings', 'integration'] == 1200.

    def test_a_spectrometer_without_integration_time_is_left_alone(self, qtbot):
        bench = Bench(qtbot, detector_kwargs=dict(integration=False))

        bench.unit.start_live(time.time())
        bench.wait_for(1)
        bench.unit._release()

        assert len(bench.measurements) >= 1

    def test_a_continuous_grab_is_stopped(self, bench):
        bench.detector.grab_state = True

        bench.unit.start_live(time.time())

        assert bench.detector.stopped_grabs == 1
        assert any('was grabbing' in notice for notice in bench.notices)

    def test_the_value_of_each_roi(self, bench):
        bench.unit.start_live(time.time())
        bench.wait_for(1)
        measurement = bench.measurements[0]

        assert len(measurement.values) == 6
        for roi, value in zip(measurement.rois, measurement.values):
            # the intensity of a pixel is its wavelength minus 400
            assert value == pytest.approx(roi.center - 400., abs=0.6)

    def test_a_roi_outside_of_the_spectrum_is_nan(self, bench):
        bench.settings.set_rois([Roi('in', 500., 510.), Roi('out', 1000., 1010.)])
        bench.unit.start_live(time.time())
        bench.wait_for(1)

        values = bench.measurements[0].values

        assert values[0] == pytest.approx(105., abs=0.6)
        assert math.isnan(values[1])

    def test_the_rois_are_read_at_each_readout(self, bench):
        bench.unit.start_live(time.time())
        bench.wait_for(2)
        bench.settings.set_rois([Roi('only', 600., 610.)])
        bench.unit.reset_series()
        n = len(bench.measurements)

        bench.wait_for(n + 2)

        assert len(bench.measurements[-1].values) == 1
        assert bench.measurements[-1].rois[0].name == 'only'
        assert bench.unit.series_rois.n_columns == 1

    def test_the_series_follow_the_measurements(self, bench):
        origin = time.time()
        bench.unit.start_live(origin)
        bench.wait_for(4)

        series = bench.unit.series_rois
        assert len(series) >= 4
        assert series.n_columns == 6
        assert series.t[0] == pytest.approx(bench.measurements[0].t - origin)
        assert len(bench.unit.series_temperature) == len(series)
        assert bench.unit.series_temperature.column(0)[0] == 20.
        assert math.isnan(bench.unit.series_temperature.column(1)[0])  # no ramp in the live view

    def test_nothing_is_driven_in_the_live_view(self, bench):
        bench.unit.start_live(time.time())
        bench.wait_for(3)

        assert bench.thermostat.targets == []
        assert all(math.isnan(m.setpoint) and m.phase is None and m.cycle == 0 for m in bench.measurements)

    def test_a_slow_spectrometer_skips_readouts_instead_of_piling_them_up(self, qtbot):
        bench = Bench(qtbot, detector_kwargs=dict(delay=350))  # 3.5 readouts to answer

        bench.unit.start_live(time.time())
        bench.wait_for(2, timeout=10000)
        bench.unit._release()

        assert bench.unit.n_skipped >= 2
        assert bench.detector.snaps <= len(bench.measurements) + 1

    def test_the_spectrum_is_there_for_the_display(self, bench):
        bench.unit.start_live(time.time())
        bench.wait_for(1)

        assert bench.unit.spectrum_dirty
        assert bench.unit.spectrum.x[0] == 400. and bench.unit.axis_units == 'nm'

    def test_the_units_of_the_axis_are_announced_once(self, bench):
        seen = []
        bench.unit.axis_units_changed.connect(seen.append)
        bench.unit.start_live(time.time())
        bench.wait_for(4)

        assert seen == ['nm']

    def test_a_pixel_axis_is_announced_too(self, qtbot):
        bench = Bench(qtbot)
        seen = []
        bench.unit.axis_units_changed.connect(seen.append)
        bench.detector.x = np.arange(1001.)

        def answer():
            from pymodaq_data import DataToExport
            from pymodaq_data.data import Axis, DataRaw
            axis = Axis('pixel', '', bench.detector.x, index=0)
            bench.detector.grab_done_signal.emit(DataToExport('Spectro1', data=[DataRaw('s', data=[bench.detector.x], axes=[axis])]))

        bench.detector._answer = answer
        bench.unit.start_live(time.time())
        bench.wait_for(2)
        bench.unit._release()

        assert seen == ['']

    def test_the_axis_sent_only_once_is_remembered(self, qtbot):
        bench = Bench(qtbot, detector_kwargs=dict(delay=5))
        bench.detector.axis_on_first_only = True

        bench.unit.start_live(time.time())
        bench.wait_for(4)
        bench.unit._release()

        assert all(m.spectrum.x[0] == 400. and len(m.spectrum.x) == 1001 for m in bench.measurements)

    def test_a_wavelength_axis_in_meters_is_converted_to_nm(self, bench):
        bench.detector.x = np.linspace(400e-9, 900e-9, 1001)

        def answer():
            from pymodaq_data import DataToExport
            from pymodaq_data.data import Axis, DataRaw
            dwa = DataRaw('spectrum', data=[bench.detector.x * 1e9], axes=[Axis('wavelength', 'm', bench.detector.x, index=0)])
            bench.detector.grab_done_signal.emit(DataToExport('Spectro1', data=[dwa]))

        bench.detector._answer = answer
        bench.unit.start_live(time.time())
        bench.wait_for(1)

        assert bench.unit.spectrum.units == 'nm'
        assert bench.unit.spectrum.x[0] == pytest.approx(400.)

    def test_stop_live(self, bench):
        bench.unit.start_live(time.time())
        bench.wait_for(2)

        bench.unit.stop_live()
        n = len(bench.measurements)
        bench.qtbot.wait(400)

        assert bench.unit.state == UnitState.IDLE
        assert len(bench.measurements) == n
        assert bench.unit._connections == []

    def test_a_spectrum_nobody_asked_for_is_shown_but_not_measured(self, bench):
        bench.settings.child('readout_time').setValue(30.)
        bench.unit.start_live(time.time())
        bench.wait_for(1)
        n = len(bench.measurements)

        bench.detector.snap()  # (as the viewer window of the module would)
        bench.qtbot.wait(150)

        assert len(bench.measurements) == n

    def test_a_new_readout_time_applies_to_the_running_view(self, bench):
        bench.settings.child('readout_time').setValue(30.)
        bench.unit.start_live(time.time())
        bench.wait_for(1)

        bench.settings.child('readout_time').setValue(0.1)
        bench.unit.update_timing()

        bench.wait_for(4, timeout=5000)

    def test_no_thermostat_no_temperature(self, bench_without_thermostat):
        bench = bench_without_thermostat
        bench.unit.start_live(time.time())
        bench.wait_for(2)

        assert all(math.isnan(m.temperature) for m in bench.measurements)
        assert bench.unit.ready


class TestTemperatureAge:
    def test_a_temperature_which_is_too_old_is_not_given(self, bench):
        bench.unit.start_live(time.time())
        bench.wait_for(2)
        assert bench.unit.current_temperature() == 20.

        bench.thermostat.silent = True  # it stops answering
        bench.unit.temperature.time -= 10.

        assert math.isnan(bench.unit.current_temperature())
        n = len(bench.measurements)
        bench.wait_for(n + 2)
        assert math.isnan(bench.measurements[-1].temperature)  # (it is not recorded as if it went on being 20)
        assert not math.isnan(bench.measurements[0].temperature)

    def test_no_temperature_yet(self, bench):
        assert math.isnan(bench.unit.current_temperature())


class TestOneReadoutAtATime:
    """Two readouts at once could make the detector lose one (the Mock does): there is never more than one"""

    def test_never_two_snaps_at_once(self, qtbot):
        bench = Bench(qtbot, detector_kwargs=dict(delay=70))  # 70 ms to answer, a readout every 100 ms
        bench.unit.start_live(time.time())
        bench.wait_for(10)
        bench.unit._release()

        assert bench.detector.max_in_flight == 1

    def test_a_readout_in_progress_when_the_experiment_begins_is_not_dropped_nor_recorded(self, qtbot):
        bench = Bench(qtbot, detector_kwargs=dict(delay=60))
        bench.unit.start_live(time.time())
        qtbot.waitUntil(lambda: bench.unit.ready, timeout=10000)
        qtbot.waitUntil(lambda: bench.unit._pending is not None, timeout=10000)  # a snap is in the air

        begin = time.time()
        bench.now = 0.
        bench.unit.begin_run(begin, 0.)

        assert bench.unit._pending is not None  # (still waiting for it: no second snap on top of it)
        n = len(bench.measurements)
        bench.wait_for(n + 3)
        bench.unit._release()
        after = bench.measurements[n:]
        # the one asked for before the start is not one of them (time.time() ticks every 15 ms on Windows)
        assert all(m.t >= begin - 0.05 for m in after)
        assert after[0].index == 0
        assert bench.detector.max_in_flight == 1

    def test_a_lost_spectrum_is_asked_for_again(self, qtbot):
        bench = Bench(qtbot)
        bench.unit.pending_timeout = lambda: 0.3
        bench.detector.drop_next = 1  # the first snap is never answered

        bench.unit.start_live(time.time())
        bench.wait_for(2, timeout=10000)
        bench.unit._release()

        assert bench.unit.n_lost == 1
        assert bench.detector.snaps >= 3

    @staticmethod
    def lost_bench(qtbot, restart_after=None, fixes=True):
        bench = Bench(qtbot)
        bench.unit.pending_timeout = lambda: 0.2
        bench.detector.silent, bench.detector.restart_fixes = True, fixes
        if restart_after is not None:
            bench.settings.child('restart_after').setValue(restart_after)
        return bench

    def test_a_spectrometer_is_closed_and_initialized_again_after_two_missed_readouts(self, qtbot):
        bench = self.lost_bench(qtbot)
        assert bench.settings.restart_after == 2  # (the default)

        bench.unit.start_live(time.time())
        bench.wait_for(1, timeout=20000)
        bench.unit._release()

        assert bench.detector.closes == 1 and bench.detector.inits == 1
        assert bench.unit.n_lost == 2 and bench.unit.n_reinits == 1

    def test_the_spectrometer_is_restarted_at_the_first_missed_readout_if_it_is_told_to(self, qtbot):
        bench = self.lost_bench(qtbot, restart_after=1)

        bench.unit.start_live(time.time())
        qtbot.waitUntil(lambda: bench.detector.closes >= 1, timeout=20000)
        n_lost = bench.unit.n_lost
        bench.unit._release()

        assert n_lost == 1

    def test_the_spectrometer_is_restarted_after_as_many_missed_readouts_as_it_is_told_to(self, qtbot):
        bench = self.lost_bench(qtbot, restart_after=3)

        bench.unit.start_live(time.time())
        qtbot.waitUntil(lambda: bench.detector.closes >= 1, timeout=20000)
        n_lost = bench.unit.n_lost
        bench.unit._release()

        assert n_lost == 3

    def test_a_spectrometer_is_never_restarted_if_it_is_told_not_to(self, qtbot):
        bench = self.lost_bench(qtbot, restart_after=0, fixes=False)

        bench.unit.start_live(time.time())
        qtbot.waitUntil(lambda: bench.unit.n_lost >= 4, timeout=20000)  # (asked again after each missed one)
        bench.unit._release()

        assert bench.detector.closes == 0 and bench.unit.n_reinits == 0
        assert bench.detector.snaps >= 4

    def test_when_to_restart_can_be_changed_during_an_experiment(self, qtbot):
        bench = Bench(qtbot)

        bench.settings.lock(True)

        assert bench.settings.child('restart_after').opts['enabled']
        assert not bench.settings.child('readout_time').opts['enabled']

    def test_the_time_to_wait_for_a_spectrum_depends_on_the_times_of_the_unit(self, bench):
        bench.settings.child('integration_time').setValue(60000.)
        bench.settings.child('readout_time').setValue(70.)

        assert bench.unit.pending_timeout() == pytest.approx(2. * 60. + 10.)
        bench.settings.child('integration_time').setValue(20.)
        bench.settings.child('readout_time').setValue(0.1)
        assert bench.unit.pending_timeout() == pytest.approx(10., abs=0.1)  # (never less)


@pytest.mark.errors_expected  # (a lost detector is logged as an error)
class TestALostDetector:
    """A detector which gives nothing is closed and initialized again 'Restart attempts' times, then it is lost"""

    @staticmethod
    def silent_bench(qtbot, after=1, attempts=2, fixes=False, thermostat=True):
        bench = Bench(qtbot, thermostat=thermostat)
        bench.unit.pending_timeout = lambda: 0.2
        bench.detector.silent, bench.detector.restart_fixes = True, fixes
        bench.settings.child('restart_after').setValue(after)
        bench.settings.child('restart_attempts').setValue(attempts)
        return bench

    @staticmethod
    def wait_until_lost(bench, timeout=20000):
        bench.qtbot.waitUntil(lambda: bench.unit.failed, timeout=timeout)

    def test_the_default_is_three_attempts_and_it_can_be_changed_during_an_experiment(self, bench):
        assert bench.settings.restart_attempts == 3

        bench.settings.lock(True)

        assert bench.settings.child('restart_attempts').opts['enabled']
        assert not bench.settings.child('readout_time').opts['enabled']

    def test_it_is_restarted_as_many_times_as_told_and_then_lost(self, qtbot):
        bench = self.silent_bench(qtbot, after=1, attempts=2)

        bench.unit.start_live(time.time())
        self.wait_until_lost(bench)

        assert bench.detector.closes == 2 and bench.detector.inits == 2 and bench.unit.n_reinits == 2
        assert len(bench.problems) == 1
        kind, message = bench.problems[0]
        assert kind == Problem.SPECTROMETER and 'lost' in message and 'after 2 restarts' in message
        assert bench.unit.state == UnitState.LIVE and not bench.unit._timer.isActive()
        snaps = bench.detector.snaps
        qtbot.wait(500)
        assert bench.detector.snaps == snaps  # (it is not read out any more)

    def test_each_restart_is_told_for_the_record_of_the_experiment(self, qtbot):
        bench = self.silent_bench(qtbot, after=1, attempts=2)

        bench.unit.start_live(time.time())
        self.wait_until_lost(bench)

        assert len(bench.events) == 2  # (the loss itself is the problem the extension writes)
        assert 'closed and initialized again (restart 1 of 2' in bench.events[0]
        assert 'closed and initialized again (restart 2 of 2' in bench.events[1]
        assert all(event.startswith('Spectro1') for event in bench.events)

    def test_the_data_coming_back_after_a_restart_are_told_for_the_record(self, qtbot):
        bench = self.silent_bench(qtbot, after=1, attempts=3)
        bench.unit.start_live(time.time())
        qtbot.waitUntil(lambda: bench.detector.closes >= 1, timeout=20000)
        bench.detector.silent = False

        bench.wait_for(1)
        bench.unit._release()

        again = [event for event in bench.events if 'delivers spectra again' in event]
        assert len(again) == 1
        assert re.fullmatch(r'Spectro1 delivers spectra again after \d+ readouts? missed in a row and 1 restart', again[0])

    def test_a_thermostat_which_answers_again_is_told_for_the_record(self, qtbot):
        bench = Bench(qtbot)
        bench.run()
        bench.wait_for(2)
        bench.unit.fail(Problem.THERMOSTAT_SILENT, 'No temperature', halt=False)
        bench.events.clear()

        bench.thermostat.get_actuator_value()
        qtbot.waitUntil(lambda: bool(bench.events), timeout=5000)
        bench.unit._release()

        assert bench.events == ['Spectrometer 1: Thermo1 gives its temperature again']

    def test_nothing_is_told_for_the_record_when_all_goes_well(self, qtbot):
        bench = Bench(qtbot)
        bench.unit.start_live(time.time())
        bench.wait_for(5)
        bench.unit._release()

        assert bench.events == []

    def test_each_attempt_needs_as_many_missed_readouts_in_a_row_as_the_unit_says(self, qtbot):
        bench = self.silent_bench(qtbot, after=2, attempts=1)

        bench.unit.start_live(time.time())
        self.wait_until_lost(bench)

        assert bench.unit.n_lost == 4 and bench.detector.closes == 1  # (2 missed, a restart, 2 missed: lost)
        assert 'after 1 restart)' in bench.problems[0][1] and '2 readouts missed in a row' in bench.problems[0][1]

    def test_with_no_attempt_it_is_lost_at_the_missed_readouts_without_being_restarted(self, qtbot):
        bench = self.silent_bench(qtbot, after=2, attempts=0)

        bench.unit.start_live(time.time())
        self.wait_until_lost(bench)

        assert bench.unit.n_lost == 2 and bench.detector.closes == 0 and bench.unit.n_reinits == 0
        assert 'not restarted' in bench.problems[0][1]

    def test_a_detector_which_gives_data_again_is_not_lost_and_the_attempts_start_again(self, qtbot):
        bench = self.silent_bench(qtbot, after=1, attempts=2)

        bench.unit.start_live(time.time())
        qtbot.waitUntil(lambda: bench.detector.closes >= 1, timeout=20000)
        bench.detector.silent = False  # (it answers again after the first restart)
        bench.wait_for(1)

        assert not bench.unit.failed and bench.unit._attempts == 0 and bench.unit.n_lost_in_a_row == 0
        assert bench.problems == []

        bench.detector.silent = True  # (and stops again: it has all its attempts again)
        self.wait_until_lost(bench)

        assert bench.detector.closes == 3  # (1, then 2 more)

    def test_a_detector_which_cannot_be_initialized_again_is_lost_after_the_attempts(self, qtbot):
        bench = self.silent_bench(qtbot, after=1, attempts=2)
        bench.detector.init_fails = 10
        bench.unit.REINIT_TIMEOUT = 0.2
        bench.unit.REINIT_WAITS = (0.05, 0.05)

        bench.unit.start_live(time.time())
        self.wait_until_lost(bench)

        assert bench.detector.closes == 2
        assert 'could not be initialized again' in bench.problems[0][1] and 'lost' in bench.problems[0][1]

    def test_it_is_not_given_up_on_for_missed_readouts_if_it_is_told_never_to_restart(self, qtbot):
        bench = self.silent_bench(qtbot, after=0, attempts=1)

        bench.unit.start_live(time.time())
        qtbot.waitUntil(lambda: bench.unit.n_lost >= 6, timeout=20000)
        bench.unit._release()

        assert not bench.unit.failed and bench.detector.closes == 0 and bench.problems == []

    def test_the_time_without_data_decides_only_for_a_detector_which_is_never_restarted(self, qtbot):
        bench = Bench(qtbot, thermostat=False)
        bench.run(now=0.)
        bench.unit.spectrum.time -= 1000.

        bench.settings.child('restart_after').setValue(2)
        assert bench.unit.silent_for(time.monotonic(), 30.) is None  # (the restarts decide)

        bench.settings.child('restart_after').setValue(0)
        found = bench.unit.silent_for(time.monotonic(), 30.)
        assert found is not None and found[0] == Problem.SPECTROMETER

    def test_the_thermostat_is_read_while_the_detector_gives_nothing(self, qtbot):
        bench = self.silent_bench(qtbot, after=0)

        bench.unit.start_live(time.time())
        qtbot.waitUntil(lambda: bench.unit.n_lost >= 1, timeout=20000)  # (a readout is waiting for its data)
        reads = bench.thermostat.reads
        qtbot.wait(700)
        bench.unit._release()

        assert bench.thermostat.reads >= reads + 4  # (a readout every 0.1 s: the thermostat is not silent for that)

    def test_the_thermostat_is_read_while_the_detector_is_initialized_again(self, qtbot):
        bench = self.silent_bench(qtbot, after=1, attempts=3)
        bench.detector.init_delay = 2000

        bench.unit.start_live(time.time())
        qtbot.waitUntil(lambda: bench.unit._reiniting, timeout=20000)
        reads = bench.thermostat.reads
        qtbot.wait(700)
        bench.unit._release()

        assert bench.thermostat.reads >= reads + 4

    def test_the_status_says_that_it_is_lost(self, qtbot):
        bench = self.silent_bench(qtbot, after=1, attempts=0)

        bench.unit.start_live(time.time())
        self.wait_until_lost(bench)

        assert 'Spectro1 LOST' in bench.unit.status_text()

    def test_a_lost_detector_is_read_out_again_when_it_is_asked_to_with_all_its_attempts(self, qtbot):
        bench = self.silent_bench(qtbot, after=1, attempts=1, fixes=False)
        bench.unit.start_live(time.time())
        self.wait_until_lost(bench)
        bench.detector.silent = False  # (the instrument is back)

        bench.unit.revive()

        assert not bench.unit.failed and bench.unit._attempts == 0 and bench.unit._timer.isActive()
        bench.wait_for(1)
        n = bench.detector.snaps
        bench.unit.revive()  # (nothing to read out again: nothing happens)
        assert bench.detector.snaps == n
        bench.unit._release()

    def test_a_new_experiment_tries_a_detector_which_was_lost_in_the_live_view(self, qtbot):
        bench = self.silent_bench(qtbot, after=1, attempts=0, fixes=True)
        bench.unit.start_live(time.time())
        self.wait_until_lost(bench)
        bench.detector.silent = False
        bench.unit.thermostat_failed = True  # (from an earlier experiment)

        bench.unit.begin_run(time.time(), 0.)

        assert not bench.unit.failed and not bench.unit.thermostat_failed and bench.unit._attempts == 0
        bench.wait_for(1)
        bench.unit._release()


@pytest.mark.errors_expected  # (a failure is logged as an error)
class TestUnitsWhichShareAThermostat:
    """The camera next to a spectrometer reads the temperature of the thermostat the spectrometer drives"""

    class Clock:
        now = 0.

    @classmethod
    def pair(cls, qtbot):
        thermostat = FakeThermostat('Thermo1')
        detectors = [FakeDetector('Spectro1'), FakeDetector('Spectro2')]
        manager = FakeManager(thermostat, *detectors)
        clock = cls.Clock()
        units = []
        for number, detector in enumerate(detectors, 1):
            settings = UnitSettings(Parameter.create(**unit_definition(number, default_rois(number))), number)
            settings.set_modules([detector.title], ['Thermo1'], detector.title, 'Thermo1')
            settings.child('integration_time').setValue(20.)
            settings.child('readout_time').setValue(0.1)
            unit = Unit(settings, manager)
            unit.attach()
            units.append(unit)
        driver, reader = units
        reader.settings.child('ramp', 'run').setValue(False)  # (it only reads the temperature)
        driver.settings.child('ramp', 'initial').setValue(20.)
        driver.settings.child('ramp', 'final').setValue(30.)
        driver.settings.child('ramp', 'velocity').setValue(60.)
        driver.settings.refresh_ramp()
        for unit in units:
            unit.peers = lambda units=units: units
            unit.clock = lambda: clock.now
            unit.start_live(time.time())
        qtbot.waitUntil(lambda: all(unit.ready for unit in units), timeout=10000)
        for unit in units:
            unit.begin_run(time.time(), 0.)
        return driver, reader, thermostat, clock

    @staticmethod
    def tick(unit, clock, now):
        clock.now = now
        return unit.program_tick(now)

    @staticmethod
    def release(*units):
        for unit in units:
            unit._release()

    def test_a_unit_knows_whether_another_one_reads_its_thermostat(self, qtbot):
        driver, reader, thermostat, clock = self.pair(qtbot)

        assert driver.thermostat_read_by_others() and reader.thermostat_read_by_others()
        reader.fail(Problem.SPECTROMETER, 'lost')
        assert not driver.thermostat_read_by_others()  # (a lost one reads nothing)
        self.release(driver, reader)

    def test_the_ramp_of_a_lost_spectrometer_goes_on_while_the_other_unit_reads_the_temperature(self, qtbot):
        driver, reader, thermostat, clock = self.pair(qtbot)
        self.tick(driver, clock, 5.)

        driver.fail(Problem.SPECTROMETER, 'lost')

        assert driver.failed and not driver.returning and driver.runner.mode == 'cycling'
        assert driver.thermostat_failed is False
        self.tick(driver, clock, 7.)
        assert thermostat.targets[-1] == pytest.approx(27.)  # (the ramp is not stopped)
        self.release(driver, reader)

    def test_it_is_brought_back_as_soon_as_the_other_unit_does_not_read_any_more(self, qtbot):
        driver, reader, thermostat, clock = self.pair(qtbot)
        self.tick(driver, clock, 5.)
        driver.fail(Problem.SPECTROMETER, 'lost')
        driver.release_orphan_program(5.)
        assert not driver.returning  # (the other one reads it)

        events = []
        driver.journal.connect(events.append)
        reader.fail(Problem.SPECTROMETER, 'lost too')
        clock.now = 6.
        driver.release_orphan_program(6.)

        assert driver.returning
        assert len(events) == 1 and 'no other unit reads the temperature any more' in events[0]
        self.tick(driver, clock, 7.)
        assert thermostat.targets[-1] < 26.  # (on its way down)
        self.release(driver, reader)

    def test_the_ramp_of_a_lost_spectrometer_is_brought_back_if_nobody_else_reads_the_thermostat(self, qtbot):
        driver, reader, thermostat, clock = self.pair(qtbot)
        reader.thermostat = None  # (not the same thermostat)
        self.tick(driver, clock, 5.)

        driver.fail(Problem.SPECTROMETER, 'lost')

        assert driver.returning
        self.release(driver, reader)

    def test_a_failed_thermostat_is_noted_for_the_experiment(self, qtbot):
        driver, reader, thermostat, clock = self.pair(qtbot)

        driver.fail(Problem.THERMOSTAT_SILENT, 'silent')
        reader.fail(Problem.THERMOSTAT_LOST, 'closed')

        assert driver.thermostat_failed and reader.thermostat_failed
        assert not driver.failed and not reader.failed  # (the spectrometers go on)
        self.release(driver, reader)

    def test_the_watchdog_goes_on_watching_a_ramp_which_goes_on_for_a_lost_detector(self, qtbot):
        driver, reader, thermostat, clock = self.pair(qtbot)
        self.tick(driver, clock, 5.)
        driver.fail(Problem.SPECTROMETER, 'lost')
        driver.temperature.time -= 100.

        found = driver.silent_for(time.monotonic(), 30.)

        assert found is not None and found[0] == Problem.THERMOSTAT_SILENT
        self.release(driver, reader)


class TestFirstReadout:
    def test_the_first_measurement_of_an_experiment_comes_after_the_integration_time(self, qtbot):
        bench = Bench(qtbot)
        bench.settings.child('readout_time').setValue(30.)  # (the first readout is not a readout time after the start)
        bench.unit.start_live(time.time())
        qtbot.waitUntil(lambda: bench.unit.ready, timeout=10000)
        before = len(bench.measurements)

        bench.now = 0.
        bench.unit.begin_run(time.time(), 0.)
        bench.wait_for(before + 1, timeout=8000)
        remaining = bench.unit._timer.remainingTime()
        bench.unit._release()

        assert bench.measurements[-1].index == 0
        assert remaining > 20000  # (the next one is a readout time after the first)


class TestAveraging:
    """The Naverage of a DAQ_Viewer makes a readout last that many integration times"""

    @staticmethod
    def averaging(bench, n):
        from pymodaq_gui.parameter import Parameter
        bench.detector.settings.addChild(Parameter.create(name='main_settings', type='group', children=[
            {'name': 'Naverage', 'type': 'int', 'value': n}]))

    def test_one_average_by_default(self, bench):
        assert bench.unit.n_averages() == 1
        assert bench.unit.acquisition_time() == pytest.approx(0.02)

    def test_the_readout_has_to_be_longer_than_all_the_averages(self, bench):
        self.averaging(bench, 5)
        bench.settings.child('integration_time').setValue(500.)
        bench.settings.child('readout_time').setValue(2.)  # 5 x 0.5 s: 2.5 s

        problems = bench.unit.problems()

        assert bench.unit.n_averages() == 5 and bench.unit.acquisition_time() == pytest.approx(2.5)
        assert len(problems) == 1 and 'integration time (500 ms x 5 averages)' in problems[0]

        bench.settings.child('readout_time').setValue(3.)
        assert bench.unit.problems() == []

    def test_the_time_to_wait_for_a_spectrum_counts_the_averages(self, bench):
        self.averaging(bench, 10)
        bench.settings.child('integration_time').setValue(500.)
        bench.settings.child('readout_time').setValue(6.)

        assert bench.unit.pending_timeout() == pytest.approx(2. * 5. + 10.)

    def test_a_module_without_the_setting_counts_one(self, bench):
        bench.unit.detector = None

        assert bench.unit.n_averages() == 1


class TestReady:
    def test_ready_when_the_spectrometer_and_the_thermostat_gave_something(self, bench):
        assert not bench.unit.ready
        assert bench.unit.missing() == ['Spectro1', 'Thermo1']

        bench.unit.start_live(time.time())
        bench.qtbot.waitUntil(lambda: bench.unit.ready, timeout=5000)

        assert bench.unit.missing() == []

    def test_a_silent_thermostat_is_missing(self, bench):
        bench.thermostat.silent = True
        bench.unit.start_live(time.time())
        bench.wait_for(2)

        assert not bench.unit.ready
        assert bench.unit.missing() == ['Thermo1']

    def test_an_old_temperature_is_not_enough(self, bench):
        bench.unit.start_live(time.time())
        bench.qtbot.waitUntil(lambda: bench.unit.ready, timeout=5000)
        bench.thermostat.silent = True
        bench.unit.temperature.time -= 10.

        assert not bench.unit.ready


class TestRun:
    def test_the_ramp_sends_set_points_to_the_thermostat(self, bench):
        bench.program(initial=20., final=30., velocity=60., hold_final=0., n_cycles=1)  # 10 °C in 10 s
        bench.run(now=100.)

        bench.unit.program_tick(105.)
        bench.unit.program_tick(108.)

        assert bench.thermostat.targets[-2:] == [pytest.approx(25.), pytest.approx(28.)]
        assert bench.unit.state == UnitState.RUNNING
        assert bench.unit.ramp_state.phase == Phase.RAMP

    def test_the_measurements_carry_the_state_of_the_ramp(self, bench):
        bench.program(initial=20., final=30., velocity=60.)
        bench.run(now=100.)
        bench.unit.program_tick(105.)
        n = len(bench.measurements)

        bench.wait_for(n + 2)

        last = bench.measurements[-1]
        assert last.setpoint == pytest.approx(25.)
        assert last.phase == Phase.RAMP
        assert last.cycle == 1
        assert last.temperature == pytest.approx(25.)  # (the fake thermostat follows)

    def test_the_whole_program(self, bench):
        # 20 -> 30 in 10 s, 2 s at 30, 30 -> 20 in 10 s, 1 s at 20: 23 s a cycle
        bench.program(initial=20., final=30., velocity=60., hold_final=2. / 60., hold_initial=1. / 60., n_cycles=2)
        bench.run(now=0.)
        phases = []
        for now in np.arange(0.5, 47., 0.5):
            state = bench.unit.program_tick(now)
            phases.append(state.phase)
            if state.finished:
                break

        assert state.finished and state.cycle == 2
        assert bench.unit.program_over
        assert bench.thermostat.targets[-1] == 20.
        assert [p for i, p in enumerate(phases) if i == 0 or p != phases[i - 1]] == [
            Phase.RAMP, Phase.HOLD_FINAL, Phase.BACK, Phase.HOLD_INITIAL, Phase.RAMP, Phase.HOLD_FINAL, Phase.BACK,
            Phase.HOLD_INITIAL, Phase.DONE]

    def test_set_points_are_not_repeated(self, bench):
        bench.program(initial=20., final=30., velocity=60., hold_final=1.)
        bench.run(now=0.)
        for now in (1., 2., 10., 10.5, 11., 11.5, 12.):  # the last ones: waiting at 30
            bench.unit.program_tick(now)

        assert bench.thermostat.targets.count(30.) == 1

    def test_the_status_of_the_unit(self, bench):
        bench.program(initial=20., final=30., velocity=60.)
        bench.run(now=0.)
        bench.unit.program_tick(5.)

        status = bench.settings.child('status')
        assert status['phase'] == 'ramp'
        assert status['cycle'] == 1
        assert status['setpoint'] == pytest.approx(25.)
        assert 'cycle 1/1 ramp' in bench.unit.status_text()
        assert 'set-point 25 °C' in bench.unit.status_text()

    def test_the_approach_starts_from_the_current_temperature(self, bench):
        bench.thermostat.value = 26.
        bench.program(initial=20., final=30., velocity=60.)
        bench.run(now=0.)

        state = bench.unit.program_tick(3.)

        assert state.phase == Phase.APPROACH
        assert bench.unit.ramp_state.cycle == 0
        assert bench.thermostat.targets[-1] == pytest.approx(23.)

    def test_a_unit_without_ramp_only_records(self, bench):
        bench.settings.child('ramp', 'run').setValue(False)
        bench.run(now=0.)

        assert bench.unit.state == UnitState.RUNNING
        assert bench.unit.runner is None and bench.unit.program_over
        assert bench.unit.program_tick(1.) is None
        n = len(bench.measurements)
        bench.wait_for(n + 2)
        assert bench.thermostat.targets == []
        assert math.isnan(bench.measurements[-1].setpoint)

    def test_the_experiment_cannot_begin_without_the_live_view(self, bench):
        with pytest.raises(RuntimeError, match='live'):
            bench.unit.begin_run(time.time(), 0.)

    def test_the_series_start_again_with_the_experiment(self, bench):
        bench.unit.start_live(time.time())
        bench.wait_for(4)
        bench.qtbot.waitUntil(lambda: bench.unit.ready)
        origin = time.time()

        bench.unit.begin_run(origin, 0.)

        assert len(bench.unit.series_rois) == 0
        assert bench.unit.origin == origin
        assert bench.unit.n_measurements == 0
        bench.wait_for(len(bench.measurements) + 2)
        assert bench.unit.series_rois.t[0] > 0.

    def test_end_of_the_experiment(self, bench):
        bench.run(now=0.)

        bench.unit.end_run()

        assert bench.unit.state == UnitState.LIVE
        assert bench.unit.runner is None
        assert bench.settings.child('status', 'phase').value() == Phase.IDLE.value


class TestStop:
    def test_the_thermostat_goes_back_to_the_initial_value_at_the_speed_of_the_ramp(self, bench):
        bench.program(initial=20., final=30., velocity=60.)
        bench.run(now=0.)
        bench.unit.program_tick(5.)  # 25

        state = bench.unit.stop_program(5.)

        assert state.phase == Phase.RETURN
        assert bench.unit.returning
        bench.unit.program_tick(7.)
        assert bench.thermostat.targets[-1] == pytest.approx(23.)
        state = bench.unit.program_tick(10.)
        assert state.finished and bench.thermostat.targets[-1] == 20.
        assert bench.unit.program_over

    def test_a_second_stop_changes_nothing(self, bench):
        bench.program(initial=20., final=30., velocity=60.)
        bench.run(now=0.)
        bench.unit.program_tick(5.)
        bench.unit.stop_program(5.)

        before = bench.unit.program_tick(6.)
        bench.unit.stop_program(6.)

        assert bench.unit.program_tick(6.).setpoint == before.setpoint

    def test_abort_leaves_the_thermostat_where_it_is(self, bench):
        bench.program(initial=20., final=30., velocity=60.)
        bench.run(now=0.)
        bench.unit.program_tick(5.)
        bench.unit.stop_program(5.)

        bench.unit.abort_program(6.)

        assert bench.unit.program_over
        assert bench.thermostat.targets[-1] == pytest.approx(25.)

    def test_abort_with_a_jump_sets_the_initial_value_at_once(self, bench):
        bench.program(initial=20., final=30., velocity=60.)
        bench.run(now=0.)
        bench.unit.program_tick(5.)

        bench.unit.abort_program(5., jump=True)

        assert bench.thermostat.targets[-1] == 20.
        assert bench.unit.program_over

    def test_pause_freezes_the_set_point(self, bench):
        bench.program(initial=20., final=30., velocity=60.)
        bench.run(now=0.)
        bench.unit.program_tick(3.)

        bench.unit.pause(3., True)
        bench.unit.program_tick(6.)
        bench.unit.program_tick(9.)

        assert bench.thermostat.targets[-1] == pytest.approx(23.)
        assert bench.unit.ramp_state.paused
        assert 'paused' in bench.settings.child('status', 'phase').value()

        bench.unit.pause(9., False)
        bench.unit.program_tick(11.)
        assert bench.thermostat.targets[-1] == pytest.approx(25.)

    def test_stopping_a_unit_without_ramp_does_nothing(self, bench):
        bench.settings.child('ramp', 'run').setValue(False)
        bench.run(now=0.)

        assert bench.unit.stop_program(1.) is None


@pytest.mark.errors_expected  # (a failure is logged as an error)
class TestFailures:
    def test_a_silent_spectrometer_is_detected_and_the_thermostat_is_sent_back(self, bench):
        bench.settings.child('restart_after').setValue(0)  # (the time decides: with restarts, they do, see TestALostDetector)
        bench.program(initial=20., final=30., velocity=60.)
        bench.run(now=0.)
        bench.tick(5.)
        bench.detector.silent = True
        bench.unit.spectrum.time -= 100.

        found = bench.unit.silent_for(time.monotonic(), 30.)
        assert found is not None and found[0] == Problem.SPECTROMETER and 'Spectro1' in found[1]
        bench.unit.fail(*found)

        assert bench.unit.failed
        assert bench.unit.returning
        assert bench.problems == [(Problem.SPECTROMETER, found[1])]
        bench.tick(7.)
        assert bench.thermostat.targets[-1] == pytest.approx(23.)

    def test_a_silent_thermostat_is_detected_too(self, bench):
        bench.run(now=0.)
        bench.thermostat.silent = True
        bench.unit.temperature.time -= 100.

        found = bench.unit.silent_for(time.monotonic(), 30.)

        assert found[0] == Problem.THERMOSTAT_SILENT
        assert 'does not answer' in found[1]
        bench.unit.fail(*found)
        assert bench.unit.returning and not bench.unit.failed  # (the spectrometer goes on)

    def test_nothing_is_wrong_when_data_comes(self, bench):
        bench.run(now=0.)

        assert bench.unit.silent_for(time.monotonic(), 30.) is None

    def test_no_watchdog_when_the_limit_is_zero(self, bench):
        bench.run(now=0.)
        bench.unit.spectrum.time -= 1000.

        assert bench.unit.silent_for(time.monotonic(), 0.) is None

    def test_the_watchdog_waits_for_the_first_data_for_the_whole_limit(self, bench):
        bench.run(now=0.)
        bench.unit.spectrum = None
        bench.unit.temperature = None

        assert bench.unit.silent_for(bench.unit._started_at + 10., 30.) is None
        assert bench.unit.silent_for(bench.unit._started_at + 31., 30.)[0] == Problem.THERMOSTAT_SILENT

    def test_a_problem_is_reported_once(self, bench):
        bench.run(now=0.)

        bench.unit.fail(Problem.SPECTROMETER, 'first')
        bench.unit.fail(Problem.SPECTROMETER, 'second')

        assert bench.problems == [(Problem.SPECTROMETER, 'first')]
        assert bench.unit.silent_for(time.monotonic() + 1e6, 30.) is None  # (nothing more to say)

    def test_a_returning_ramp_is_not_a_reason_to_complain_again(self, bench):
        bench.program(initial=20., final=30., velocity=60.)
        bench.run(now=0.)
        bench.tick(5.)
        bench.unit.stop_program(5.)
        bench.unit.spectrum.time -= 1000.

        assert bench.unit.silent_for(time.monotonic(), 30.) is None

    def test_a_closed_spectrometer(self, bench):
        bench.program(initial=20., final=30., velocity=60.)
        bench.run(now=0.)
        bench.tick(5.)

        bench.detector.close()

        assert bench.unit.failed and bench.unit.returning
        assert bench.problems[0][0] == Problem.SPECTROMETER and 'was closed' in bench.problems[0][1]
        n = bench.detector.snaps
        bench.qtbot.wait(300)
        assert bench.detector.snaps == n  # not read any more

    def test_a_closed_thermostat_cannot_be_brought_back_so_the_ramp_ends(self, bench):
        bench.program(initial=20., final=30., velocity=60.)
        bench.run(now=0.)
        bench.tick(5.)

        bench.thermostat.close()

        assert bench.unit.thermostat is None
        assert bench.unit.program_over
        assert bench.problems[0][0] == Problem.THERMOSTAT_LOST
        assert bench.unit.last_sent == pytest.approx(25.)
        reads, n = bench.thermostat.reads, len(bench.measurements)
        bench.wait_for(n + 2)  # the spectrometer goes on, without temperature
        assert math.isnan(bench.measurements[-1].temperature)
        assert bench.thermostat.reads == reads  # (and nobody asks the closed module any more)

    def test_a_closed_module_is_ignored_when_the_unit_is_not_in_use(self, bench):
        bench.detector.close()

        assert bench.problems == []

    def test_live_notices_say_that_a_module_gives_nothing(self, bench):
        bench.detector.silent = True
        bench.thermostat.silent = True
        bench.unit.start_live(time.time())

        assert bench.unit.live_notices(time.monotonic()) == []  # too early
        notices = bench.unit.live_notices(time.monotonic() + 11.)

        assert len(notices) == 2 and 'no 1D data' in notices[0] and 'temperature' in notices[1]
        assert bench.unit.live_notices(time.monotonic() + 12.) == []  # (once)

    @pytest.mark.errors_expected
    def test_an_exception_in_a_slot_is_logged_not_raised(self, bench, caplog):
        bench.unit.start_live(time.time())
        bench.wait_for(1)

        def boom(*args):
            raise RuntimeError('boom')

        bench.unit._measure = boom
        bench.qtbot.wait(300)

        assert any('boom' in record.getMessage() or 'failed' in record.getMessage() for record in caplog.records)
        assert bench.unit.state == UnitState.LIVE


class TestOddities:
    @pytest.mark.errors_expected
    def test_an_unreadable_temperature_is_logged_and_ignored(self, bench):
        bench.unit._on_temperature(object())  # (nothing which has a value)

        assert bench.unit.temperature is None

    def test_a_wavelength_axis_which_looks_wrong_is_said_once(self, qtbot):
        bench = Bench(qtbot)
        bench.detector.x = np.linspace(400., 900., 1001) * 1e9  # as an old plugin gave them: a billion times too big

        bench.unit.start_live(time.time())
        bench.wait_for(4)
        bench.unit._release()

        wrong = [notice for notice in bench.notices if 'looks wrong' in notice]
        assert len(wrong) == 1 and 'Spectro1' in wrong[0] and 'plugin' in wrong[0]

    def test_an_integration_time_the_instrument_does_not_accept_is_clamped(self, bench, caplog):
        bench.settings.child('integration_time').setValue(70000.)

        with caplog.at_level('WARNING'):
            bench.unit.apply_integration_time()

        assert bench.detector.settings['detector_settings', 'integration'] == 60000.
        assert any('closest to 70000 ms' in record.getMessage() for record in caplog.records)

    def test_a_thermostat_which_disappeared_is_a_problem(self, bench):
        bench.settings.child('thermostat').setLimits(['None', 'Thermo1', 'Gone'])
        bench.settings.child('thermostat').setValue('Gone')
        bench.unit.attach()

        assert any('thermostat Gone does not exist any more' in problem for problem in bench.unit.problems())

    def test_the_live_view_cannot_be_stopped_during_an_experiment(self, bench):
        bench.run(now=0.)

        with pytest.raises(RuntimeError, match='stop it first'):
            bench.unit.stop_live()

    @pytest.mark.errors_expected
    def test_a_jump_which_cannot_be_sent_does_not_raise(self, bench):
        bench.program(initial=20., final=30., velocity=60.)
        bench.run(now=0.)

        def failing(value):
            raise OSError('serial port closed')

        bench.unit.send_setpoint = failing

        bench.unit.abort_program(1., jump=True)

        assert bench.unit.program_over

    def test_a_unit_has_a_readable_name(self, bench):
        assert repr(bench.unit) == '<Unit 1 idle>'

    def test_an_experiment_without_the_thermostat_data_cannot_start_a_ramp_from_nothing(self, bench):
        bench.unit.start_live(time.time())
        bench.wait_for(1)
        bench.unit.temperature = None  # (a thermostat which gave nothing)

        assert not bench.unit.ready
        assert bench.unit.missing() == ['Thermo1']


class TestDisarm:
    """The detector can be stopped between the readouts: each readout arms it again"""

    def test_the_detector_is_left_as_it_is_by_default(self, qtbot):
        bench = Bench(qtbot)
        assert not bench.settings.disarm

        bench.unit.start_live(time.time())
        bench.wait_for(3)
        bench.unit._release()

        assert bench.detector.stops == 0

    def test_the_detector_is_stopped_after_each_readout_which_came_back_and_the_readouts_keep_their_rhythm(self, qtbot):
        bench = Bench(qtbot)
        bench.settings.child('disarm').setValue(True)

        bench.unit.start_live(time.time())
        bench.wait_for(4)
        bench.unit._release()

        assert bench.detector.stops >= 4
        assert bench.detector.snaps >= bench.detector.stops  # (it is armed again by a snap, never by anything else)
        assert bench.detector.max_in_flight == 1
        times = [m.t for m in bench.measurements[:4]]
        assert all(0.05 < later - earlier < 0.25 for earlier, later in zip(times, times[1:]))  # (the readout time is 0.1 s)

    def test_the_detector_is_stopped_after_a_missed_readout_so_that_the_next_one_arms_it_again(self, qtbot):
        bench = TestOneReadoutAtATime.lost_bench(qtbot, restart_after=0, fixes=False)
        bench.settings.child('disarm').setValue(True)

        bench.unit.start_live(time.time())
        qtbot.waitUntil(lambda: bench.unit.n_lost >= 2, timeout=20000)
        bench.unit._release()

        assert bench.detector.stops >= 2

    def test_it_can_be_changed_during_an_experiment(self, qtbot):
        bench = Bench(qtbot)

        bench.settings.lock(True)

        assert bench.settings.child('disarm').opts['enabled']

    def test_a_tree_saved_by_an_older_version_does_not_disarm(self, qtbot):
        bench = Bench(qtbot)
        bench.settings.param.removeChild(bench.settings.child('disarm'))

        assert not bench.settings.disarm

    def test_there_is_a_warning_when_the_detector_would_be_armed_again_within_five_seconds(self, qtbot):
        bench = Bench(qtbot)
        bench.settings.child('integration_time').setValue(500.)
        bench.settings.child('readout_time').setValue(5.4)  # (4.9 s left)
        bench.settings.child('disarm').setValue(True)

        bench.unit.start_live(time.time())
        bench.unit._release()

        warnings = [notice for notice in bench.notices if 'not advised' in notice]
        assert len(warnings) == 1 and '4.9 s' in warnings[0] and 'Spectrometer 1' in warnings[0]

    def test_there_is_no_warning_when_the_detector_is_idle_for_at_least_five_seconds(self, qtbot):
        bench = Bench(qtbot)
        bench.settings.child('integration_time').setValue(500.)
        bench.settings.child('readout_time').setValue(5.5)
        bench.settings.child('disarm').setValue(True)

        bench.unit.start_live(time.time())
        bench.unit._release()

        assert not [notice for notice in bench.notices if 'not advised' in notice]

    def test_there_is_no_warning_when_the_detector_is_not_disarmed(self, qtbot):
        bench = Bench(qtbot)  # (a readout time of 0.1 s)

        bench.unit.start_live(time.time())
        bench.unit._release()

        assert not [notice for notice in bench.notices if 'not advised' in notice]

    def test_the_warning_is_given_again_when_the_times_change_but_not_when_they_do_not(self, qtbot):
        bench = Bench(qtbot)
        bench.unit.start_live(time.time())
        try:
            bench.settings.child('disarm').setValue(True)
            bench.unit.disarm_changed()
            bench.unit.update_timing()
            bench.unit.check_disarm()
            assert len([notice for notice in bench.notices if 'not advised' in notice]) == 1

            bench.settings.child('readout_time').setValue(0.2)
            bench.unit.update_timing()
            assert len([notice for notice in bench.notices if 'not advised' in notice]) == 2

            bench.settings.child('readout_time').setValue(20.)  # (enough time: nothing more is said)
            bench.unit.update_timing()
            assert len([notice for notice in bench.notices if 'not advised' in notice]) == 2
        finally:
            bench.unit._release()

    def test_the_averages_of_the_module_count_in_the_time_the_detector_is_busy(self, qtbot):
        bench = Bench(qtbot)
        bench.settings.child('integration_time').setValue(1000.)
        bench.settings.child('readout_time').setValue(8.)
        bench.unit.n_averages = lambda: 4  # (4 s of integration: 4 s left)

        assert bench.unit.disarm_gap() == pytest.approx(4.)
        bench.settings.child('disarm').setValue(True)
        assert 'not advised' in bench.unit.disarm_warning()
