"""A cycle of ramps, then the log goes on at the temperature they ended at until the user stops it

* the setting *When the ramps are over*: stay at the initial value (the bottom), at the final value (the top), or end the log;
* "End at the top" / "End at the bottom": end the ramps at any time, and go on logging there;
* Stop: the ramps are brought back to the initial value and the log ends, as before.
"""
import time

import pytest

from pymodaq_gui.parameter import Parameter
from pymodaq_gui.utils.custom_app import WorkFlowActions

from pymodaq_plugins_thermospectro.ramp import PHASE_CODES, Phase
from pymodaq_plugins_thermospectro.rois import default_rois
from pymodaq_plugins_thermospectro.unit import Problem, UnitState
from pymodaq_plugins_thermospectro.unit_settings import (CONTROL_HOLD, CONTROL_RAMP, END_CHOICES, END_FINAL, END_INITIAL,
                                                         END_LOG, RAMP_FIELDS, UnitSettings, unit_definition)

from pymodaq_plugins_thermospectro.extensions import thermo_spectro
from test_extension import H5Experiment, run, spy_setpoints, ui_enabled, wait_for, wait_for_end
from test_extension_log import hold, log_for
from test_hold_and_log import Bench, visible

pytestmark = pytest.mark.filterwarnings('ignore')

HOLD_CODE = float(PHASE_CODES[Phase.HOLD])


def ramp_settings(settings, n_cycles=2, end_at=END_INITIAL):
    """20 -> 30 at 1 a second (10 s), no wait, and back: a cycle is 20 s"""
    ramp = settings.param.child('ramp')
    ramp['initial'], ramp['final'], ramp['velocity'] = 20., 30., 60.
    ramp['hold_final'], ramp['hold_initial'], ramp['n_cycles'], ramp['end_at'] = 0., 0., n_cycles, end_at
    settings.refresh_ramp()


# ----------------------------------------------------------------------------------------------------------------------
# the setting
# ----------------------------------------------------------------------------------------------------------------------
class TestTheSetting:
    @pytest.fixture
    def settings(self):
        return UnitSettings(Parameter.create(**unit_definition(1, default_rois(1))), 1)

    def test_by_default_the_log_goes_on_at_the_initial_value(self, settings):
        assert settings.end_at == END_INITIAL and settings.after_program == 'initial'

    def test_the_three_ways_to_end(self, settings):
        assert settings.param.child('ramp', 'end_at').opts['limits'] == [END_INITIAL, END_FINAL, END_LOG]
        assert END_CHOICES == {END_INITIAL: 'initial', END_FINAL: 'final', END_LOG: None}
        settings.param['ramp', 'end_at'] = END_FINAL
        assert settings.after_program == 'final'
        settings.param['ramp', 'end_at'] = END_LOG
        assert settings.after_program is None

    def test_the_words_of_the_choices_are_those_of_the_user(self):
        assert 'initial value (bottom)' in END_INITIAL and 'final value (top)' in END_FINAL
        assert 'go on logging' in END_INITIAL and 'go on logging' in END_FINAL

    def test_the_program_ends_where_it_is_told_to(self, settings):
        assert settings.program().end_at == 'initial'
        settings.param['ramp', 'end_at'] = END_FINAL
        assert settings.program().end_at == 'final' and settings.program().rest == settings.program().final
        settings.param['ramp', 'end_at'] = END_LOG
        assert settings.program().end_at == 'initial'  # (the controller stays at the initial value, the log ends)

    def test_the_description_says_what_happens(self, settings):
        ramp_settings(settings, n_cycles=3)
        text = settings.child('ramp', 'cycle_duration')
        assert text.value() == '0:20 (ramp 0:10, way back 0:10), 3 cycles: 1:00'  # (the usual: nothing more is said)
        assert text.opts['tip'].endswith('3 cycles: 1:00, then the log goes on at the initial value until you press Stop')
        settings.param['ramp', 'end_at'] = END_FINAL
        settings.refresh_ramp()
        assert text.value() == '0:20 (ramp 0:10, way back 0:10), 3 cycles: 0:50 (ends at the top)'  # (no last way back)
        assert text.opts['tip'].endswith('(ends at the top), then the log goes on at the final value until you press Stop')
        settings.param['ramp', 'end_at'] = END_LOG
        settings.refresh_ramp()
        assert text.value().endswith('3 cycles: 1:00, then the log ends') and text.opts['tip'].endswith('then the log ends')
        settings.param['ramp', 'n_cycles'] = 0
        settings.refresh_ramp()
        assert text.value().endswith(', until stopped')
        assert text.opts['tip'].endswith('repeated until you end the ramps or press Stop')

    def test_it_is_a_field_of_the_ramp_not_of_the_hold(self, settings):
        assert 'end_at' in RAMP_FIELDS and visible(settings, 'end_at')
        settings.param['ramp', 'control'] = CONTROL_HOLD
        settings.refresh_ramp()
        assert not visible(settings, 'end_at') and visible(settings, 'hold_temperature')

    def test_every_application_starts_with_the_log_going_on_after_the_ramps(self):
        for control in (CONTROL_HOLD, CONTROL_RAMP):
            settings = UnitSettings(Parameter.create(**unit_definition(1, default_rois(1), control=control)), 1)
            assert settings.end_at == END_INITIAL


# ----------------------------------------------------------------------------------------------------------------------
# a unit, with ramps driven by hand
# ----------------------------------------------------------------------------------------------------------------------
class TestAUnitWhoseRampsAreOver:
    @pytest.fixture
    def bench(self, qtbot):
        bench = Bench(qtbot, control=CONTROL_RAMP)
        ramp_settings(bench.settings)
        yield bench
        if bench.unit.state != UnitState.IDLE:
            bench.unit._release()

    def test_the_unit_stays_where_the_ramps_ended_and_goes_on(self, bench):
        bench.run()
        bench.unit.program_tick(10.)
        assert not bench.unit.settled  # (half way)

        state = bench.unit.program_tick(40.)

        assert state.finished and bench.unit.settled and bench.unit.after_program == 'initial'
        assert bench.unit.hold_target == 20. and bench.thermostat.targets[-1] == 20.
        assert bench.unit.state == UnitState.RUNNING and bench.unit.program_over

    def test_what_is_measured_after_the_ramps_is_a_hold_with_the_last_cycle(self, bench):
        bench.run()
        bench.unit.program_tick(15.)
        bench.wait_for(2)
        assert bench.measurements[-1].phase in (Phase.BACK, Phase.RAMP, Phase.HOLD_FINAL)
        bench.measurements.clear()

        bench.unit.program_tick(40.)
        bench.wait_for(3)

        measurement = bench.measurements[-1]
        assert measurement.phase == Phase.HOLD and measurement.setpoint == 20. and measurement.cycle == 2

    def test_at_the_top_it_stays_at_the_top(self, bench):
        ramp_settings(bench.settings, n_cycles=2, end_at=END_FINAL)
        bench.run()

        bench.unit.program_tick(29.)
        assert not bench.unit.settled
        state = bench.unit.program_tick(30.)  # (up, down, up: the last cycle has no way back)

        assert state.finished and bench.unit.settled and bench.unit.after_program == 'final'
        assert bench.unit.hold_target == 30. and bench.thermostat.targets[-1] == 30.
        bench.measurements.clear()
        bench.wait_for(2)
        assert bench.measurements[-1].phase == Phase.HOLD and bench.measurements[-1].setpoint == 30.
        assert bench.measurements[-1].cycle == 2

    def test_when_the_log_ends_with_the_ramps_the_unit_does_not_settle(self, bench):
        ramp_settings(bench.settings, end_at=END_LOG)
        bench.run()

        state = bench.unit.program_tick(40.)

        assert state.finished and not bench.unit.settled and bench.unit.after_program is None
        assert bench.unit.hold_target is None and bench.unit.program_over

    def test_the_status_says_the_ramps_are_over(self, bench):
        bench.run()
        bench.unit.program_tick(40.)

        text = bench.unit.status_text()

        assert 'ramps over, held at 20' in text and 'cycle' not in text
        assert bench.settings.child('status', 'phase').value() == Phase.HOLD.value
        assert any('the ramps are over, held at 20' in notice and 'the log goes on' in notice for notice in bench.notices)

    def test_the_watchdog_tells_and_goes_on_as_for_a_held_controller(self, bench):
        bench.run()
        bench.unit.program_tick(40.)
        bench.wait_for(2)
        bench.unit.temperature.time -= 100.

        found = bench.unit.silent_for(time.monotonic(), 30.)

        assert found[0] == Problem.THERMOSTAT_SILENT and 'the log goes on' in found[1] and 'brought back' not in found[1]
        assert not bench.unit.ramping

    def test_a_ramp_which_is_going_on_is_ramping_and_a_settled_one_is_not(self, bench):
        bench.run()
        assert bench.unit.ramping
        bench.unit.program_tick(40.)
        assert not bench.unit.ramping and bench.unit.settled

    def test_a_unit_which_is_stopped_does_not_settle_the_log_ends_when_it_is_back(self, bench):
        bench.run()
        bench.unit.program_tick(15.)  # (on the way back)

        bench.unit.stop_program(15.)
        assert bench.unit.after_program is None
        bench.unit.program_tick(30.)

        assert not bench.unit.settled and bench.unit.program_over and bench.thermostat.targets[-1] == 20.

    @pytest.mark.errors_expected
    def test_a_failure_during_the_ramps_ends_the_log_when_it_is_back(self, bench):
        bench.run()
        bench.unit.program_tick(5.)
        bench.unit.clock = lambda: 5.  # (the clock of the ramp, which the failure reads)

        bench.unit.fail(Problem.SPECTROMETER, 'closed')
        assert bench.unit.returning and bench.unit.after_program is None
        bench.unit.program_tick(40.)

        assert not bench.unit.settled and bench.unit.program_over and bench.thermostat.targets[-1] == 20.

    @pytest.mark.errors_expected
    def test_a_thermostat_which_is_lost_is_not_said_to_be_holding(self, bench):
        bench.run()
        bench.unit.program_tick(5.)

        bench.unit.fail(Problem.THERMOSTAT_LOST, 'closed')
        bench.unit.program_tick(40.)

        assert not bench.unit.settled and bench.unit.hold_target is None

    def test_the_ramps_can_be_ended_at_the_top_at_any_time(self, bench):
        bench.run()
        bench.unit.program_tick(14.)  # (4 down from the top: 26)

        state = bench.unit.finish_program(14., 'final')

        assert state.phase == Phase.RAMP and state.setpoint == pytest.approx(26.)
        assert bench.unit.after_program == 'final' and bench.unit.returning and not bench.unit.settled
        assert any('ending the ramps at the final value (30' in notice for notice in bench.notices)
        bench.unit.program_tick(18.)  # (4 up at 1 a second)
        assert bench.unit.settled and bench.unit.hold_target == 30. and bench.thermostat.targets[-1] == 30.

    def test_the_ramps_can_be_ended_at_the_bottom_at_any_time(self, bench):
        bench.run()
        bench.unit.program_tick(4.)  # (24)

        state = bench.unit.finish_program(4., 'initial')

        assert state.phase == Phase.RETURN and bench.unit.after_program == 'initial'
        bench.unit.program_tick(7.)
        assert not bench.unit.settled  # (4 degrees back at 1 a second)
        bench.unit.program_tick(8.)
        assert bench.unit.settled and bench.unit.hold_target == 20. and bench.thermostat.targets[-1] == 20.

    def test_ending_at_the_top_wins_over_the_setting_which_ends_the_log(self, bench):
        ramp_settings(bench.settings, end_at=END_LOG)
        bench.run()
        bench.unit.program_tick(4.)

        bench.unit.finish_program(4., 'final')
        bench.unit.program_tick(10.)

        assert bench.unit.settled and bench.unit.hold_target == 30.  # (the user asked for it: the log goes on)

    def test_a_ramp_which_is_already_on_its_way_is_left_alone(self, bench):
        bench.run()
        bench.unit.program_tick(4.)
        bench.unit.finish_program(4., 'final')

        assert bench.unit.finish_program(5., 'initial') == bench.unit.ramp_state  # (nothing changes)

        assert bench.unit.after_program == 'final'

    def test_a_unit_without_ramp_has_nothing_to_end(self, qtbot):
        bench = Bench(qtbot)  # (it holds a temperature)
        bench.run()

        assert bench.unit.finish_program(0., 'final') is None and not bench.unit.settled
        bench.unit._release()

    def test_quitting_does_not_send_the_initial_value_to_a_controller_which_stays(self, bench):
        ramp_settings(bench.settings, end_at=END_FINAL)
        bench.run()
        bench.unit.program_tick(30.)
        assert bench.unit.settled
        sent = len(bench.thermostat.targets)

        bench.unit.abort_program(31., jump=True)

        assert len(bench.thermostat.targets) == sent and bench.thermostat.targets[-1] == 30.

    def test_quitting_during_the_ramps_still_sends_the_initial_value(self, bench):
        bench.run()
        bench.unit.program_tick(5.)

        bench.unit.abort_program(6., jump=True)

        assert bench.thermostat.targets[-1] == 20.

    def test_a_new_run_forgets_it(self, bench):
        bench.run()
        bench.unit.program_tick(40.)
        assert bench.unit.settled
        bench.unit.end_run()
        assert not bench.unit.settled and bench.unit.after_program is None and bench.unit.hold_target is None

        bench.unit.begin_run(time.time(), 0.0)

        assert not bench.unit.settled and bench.unit.after_program == 'initial' and bench.unit.hold_target is None


# ----------------------------------------------------------------------------------------------------------------------
# the extension: a log from the start of the ramps to the click on Stop
# ----------------------------------------------------------------------------------------------------------------------
FAST = dict(velocity=180.)  # 3 a second: 0 -> 3 takes 1 s, a cycle 2 s


def one_unit(ext, end_at=END_INITIAL, n_cycles=2):
    ext.unit(2).settings.child('enabled').setValue(False)
    unit = ext.unit(1)
    unit.settings.child('ramp', 'end_at').setValue(end_at)
    unit.settings.child('ramp', 'n_cycles').setValue(n_cycles)
    return unit


class TestTheWholeLog:
    def test_a_cycle_of_ramps_then_the_log_goes_on_until_stop(self, ext, qtbot, tmp_path):
        """Set a cycle of ramps, log everything, start the ramps, still log after they end, finish with one click"""
        unit = one_unit(ext)
        sent = spy_setpoints(ext)
        run(qtbot, ext, **FAST)

        wait_for(qtbot, lambda: unit.settled, timeout=30000)
        measured = unit.n_measurements
        qtbot.wait(1500)

        assert ext.run_active and unit.state == UnitState.RUNNING  # (the ramps are over, the log is not)
        assert unit.n_measurements >= measured + 8  # (a readout every 0.1 s)
        assert 'ramps over' in unit.status_text() and ext.settings['experiment', 'remaining'] == 'until Stop'
        sent_before_stop = list(sent[1])
        ext.stop_run()

        assert not ext.run_active  # (one click: there is nothing to bring back)
        assert ext.get_action('start').isEnabled() and not ext.get_action('stop').isEnabled()
        assert sent[1] == sent_before_stop and sent[1][-1] == 0.0  # (it was left at the initial value, nothing sent)
        qtbot.waitUntil(lambda: not ext._worker.is_saving, timeout=20000)
        with H5Experiment(tmp_path) as saved:
            setpoint, cycle, phase = (saved.zero_d('Unit1_Program', index) for index in range(3))
            n = len(setpoint)
            assert saved.zero_d('Unit1_ROIs', 0).size == n == saved.zero_d('Unit1_Temperature').size
            codes = phase.tolist()
            assert float(PHASE_CODES[Phase.RAMP]) in codes and float(PHASE_CODES[Phase.BACK]) in codes
            assert codes[-1] == HOLD_CODE and codes[-8:] == [HOLD_CODE] * 8  # (the log of the end, held)
            assert setpoint[-1] == 0.0 and cycle[-1] == 2.0 and max(setpoint) > 2.5
            first_hold = codes.index(HOLD_CODE)
            assert all(code == HOLD_CODE for code in codes[first_hold:])  # (and nothing else after the ramps)
            assert n - first_hold >= 8

    def test_ending_at_the_top_the_log_goes_on_at_the_high_temperature(self, ext, qtbot, tmp_path):
        unit = one_unit(ext, end_at=END_FINAL)
        sent = spy_setpoints(ext)
        run(qtbot, ext, **FAST)

        wait_for(qtbot, lambda: unit.settled, timeout=30000)
        qtbot.wait(1200)

        assert ext.run_active and unit.hold_target == 3.0 and sent[1][-1] == 3.0
        sent_before_stop = list(sent[1])
        ext.stop_run()

        assert not ext.run_active and sent[1] == sent_before_stop  # (it stays at the top: no way back)
        qtbot.waitUntil(lambda: not ext._worker.is_saving, timeout=20000)
        with H5Experiment(tmp_path) as saved:
            setpoint, cycle, phase = (saved.zero_d('Unit1_Program', index) for index in range(3))
            codes = phase.tolist()
            assert codes[-1] == HOLD_CODE and setpoint[-1] == 3.0 and cycle[-1] == 2.0
            first_hold = codes.index(HOLD_CODE)
            assert set(setpoint[first_hold:].tolist()) == {3.0} and len(codes) - first_hold >= 8
            # (up, down, up: the last cycle has no way back: no BACK phase after the first one is complete)
            assert (saved.zero_d('Unit1_Temperature')[-1] > 2.0)  # (the Mock thermostat follows its set-point)

    def test_the_log_which_ends_with_the_ramps_still_does(self, ext, qtbot):
        unit = one_unit(ext, end_at=END_LOG, n_cycles=1)
        run(qtbot, ext, **FAST)

        wait_for_end(qtbot, ext, timeout=30000)

        assert not ext.run_active and not unit.settled

    def test_stop_during_the_ramps_brings_them_back_and_ends_the_log(self, ext, qtbot):
        unit = one_unit(ext, n_cycles=0)
        sent = spy_setpoints(ext)
        run(qtbot, ext, velocity=60.)  # (1 a second: 3 s up)
        qtbot.wait(1500)
        assert sent[1][-1] > 1.0 and not unit.settled

        ext.stop_run()

        assert ext.run_active  # (on its way back)
        wait_for_end(qtbot, ext, timeout=30000)
        assert sent[1][-1] == 0.0 and not unit.settled  # (it did not go on logging: Stop is the end of the log)

    def test_the_duration_ends_a_log_which_went_on_after_the_ramps(self, ext, qtbot):
        unit = one_unit(ext, n_cycles=1)
        log_for(ext, 6.0)
        run(qtbot, ext, **FAST)

        wait_for(qtbot, lambda: unit.settled, timeout=30000)
        assert ext.run_active  # (the ramps are over after 2 s, the log lasts 6 s)
        wait_for_end(qtbot, ext, timeout=30000)

    def test_two_units_one_goes_on_after_its_ramps_the_other_holds(self, ext, qtbot):
        unit = ext.unit(1)
        unit.settings.child('ramp', 'end_at').setValue(END_INITIAL)
        unit.settings.child('ramp', 'n_cycles').setValue(1)
        unit.settings.child('ramp', 'velocity').setValue(180.)
        hold(ext, (2.0,), numbers=(2,))
        ext.start_run()
        qtbot.waitUntil(lambda: ext.run_active, timeout=20000)

        wait_for(qtbot, lambda: unit.settled, timeout=30000)
        qtbot.wait(800)

        assert ext.run_active and ext.unit(2).hold_target == 2.0
        ext.stop_run()
        assert not ext.run_active  # (nothing to bring back: both stay where they are)

    @pytest.mark.errors_expected
    def test_a_silent_thermostat_after_the_ramps_is_told_and_the_log_goes_on(self, ext, qtbot):
        unit = one_unit(ext, n_cycles=1)
        ext.settings['experiment', 'watchdog'] = 1.0
        run(qtbot, ext, **FAST)
        wait_for(qtbot, lambda: unit.settled, timeout=30000)
        qtbot.wait(300)
        unit._disconnect(unit.thermostat)  # (only its temperature stops coming)
        unit.temperature.time -= 1000.

        wait_for(qtbot, lambda: any('No temperature from Thermo1' in text for _, text, _ in ext.notifications), timeout=15000)
        qtbot.wait(500)

        text = [text for _, text, _ in ext.notifications if 'No temperature from Thermo1' in text][0]
        assert 'the log goes on' in text and 'brought back' not in text
        assert ext.run_active and unit.state == UnitState.RUNNING and not unit.failed
        ext.stop_run()

    def test_quitting_leaves_a_controller_which_stays_at_the_top(self, ext, qtbot):
        unit = one_unit(ext, end_at=END_FINAL, n_cycles=1)
        sent = spy_setpoints(ext)
        run(qtbot, ext, **FAST)
        wait_for(qtbot, lambda: unit.settled, timeout=30000)
        before = list(sent[1])
        ext.answers['quit'] = 'jump'  # (what happens when PyMoDAQ quits: the initial values at once)

        assert ext._quit_fun()

        assert not ext.run_active and sent[1] == before and sent[1][-1] == 3.0  # (it stays at the top)

    def test_the_quit_dialog_says_nothing_is_brought_back(self, ext, qtbot, monkeypatch):
        from qtpy import QtWidgets
        unit = one_unit(ext, end_at=END_FINAL, n_cycles=1)
        run(qtbot, ext, **FAST)
        wait_for(qtbot, lambda: unit.settled, timeout=30000)
        shown = []

        def execute(box):
            shown.append((box.windowTitle(), box.text(), [b.text() for b in box.buttons()]))
            box.target = next(b for b in box.buttons() if b.text() == clicked[0])

        clicked = ['Close the log']
        monkeypatch.setattr(QtWidgets.QMessageBox, 'exec', execute)
        monkeypatch.setattr(QtWidgets.QMessageBox, 'clickedButton', lambda box: box.target)

        assert thermo_spectro.ThermoSpectro._ask_quit_choice(ext) == 'jump'
        clicked[0] = 'Cancel'
        assert thermo_spectro.ThermoSpectro._ask_quit_choice(ext) == 'cancel'

        title, text, buttons = shown[0]
        assert title == 'A log is going on' and 'stay where they are (Thermo1: 3 °C)' in text
        assert 'initial' not in text and sorted(buttons) == ['Cancel', 'Close the log']
        ext.stop_run()

    def test_the_quit_dialog_of_ramps_which_are_going_on_says_which_controllers_stay(self, ext, qtbot, monkeypatch):
        from qtpy import QtWidgets
        one_unit(ext, n_cycles=0)
        ext.unit(2).settings.child('enabled').setValue(True)
        hold(ext, (2.0,), numbers=(2,))
        ext.unit(1).settings.child('ramp', 'velocity').setValue(60.)
        ext.start_run()
        qtbot.waitUntil(lambda: ext.run_active, timeout=20000)
        shown = []

        def execute(box):
            shown.append((box.text(), [b.text() for b in box.buttons()]))
            box.target = box.escapeButton()

        monkeypatch.setattr(QtWidgets.QMessageBox, 'exec', execute)
        monkeypatch.setattr(QtWidgets.QMessageBox, 'clickedButton', lambda box: box.target)

        assert thermo_spectro.ThermoSpectro._ask_quit_choice(ext) == 'cancel'

        text, buttons = shown[0]
        assert 'Set them now and close' in buttons and 'These stay where they are: Thermo2: 2 °C' in text
        ext.stop_run()
        wait_for_end(qtbot, ext, timeout=30000)


# ----------------------------------------------------------------------------------------------------------------------
# the buttons: End at the top, End at the bottom
# ----------------------------------------------------------------------------------------------------------------------
class TestEndAtTheTopOrTheBottom:
    def enabled(self, ext, name):
        return ext.get_action(name).isEnabled()

    def test_the_buttons_are_there_and_off_until_a_ramp_goes_on(self, ext, qtbot):
        for name in ('end_top', 'end_bottom'):
            assert ext.has_action(name) and not self.enabled(ext, name)
        unit = one_unit(ext, n_cycles=0)
        run(qtbot, ext, velocity=60.)

        assert self.enabled(ext, 'end_top') and self.enabled(ext, 'end_bottom') and self.enabled(ext, WorkFlowActions.PAUSE)
        ext.finish_ramps('final')
        assert not self.enabled(ext, 'end_top') and not self.enabled(ext, 'end_bottom')  # (on its way there)
        wait_for(qtbot, lambda: unit.settled, timeout=30000)
        assert not self.enabled(ext, WorkFlowActions.PAUSE)  # (nothing to pause any more)
        ext.stop_run()
        assert not ext.run_active and not self.enabled(ext, 'end_top')

    def test_a_log_which_holds_has_no_ramp_to_end_or_to_pause(self, ext, qtbot):
        hold(ext)
        run(qtbot, ext)

        assert not self.enabled(ext, 'end_top') and not self.enabled(ext, 'end_bottom')
        assert not self.enabled(ext, WorkFlowActions.PAUSE)
        ext.stop_run()

    def test_ending_endless_ramps_at_the_top(self, ext, qtbot, tmp_path):
        """Cycles until stopped: end them at the high temperature, and go on logging there"""
        unit = one_unit(ext, n_cycles=0)
        sent = spy_setpoints(ext)
        run(qtbot, ext, velocity=60.)
        qtbot.wait(1500)  # (1.5 up of 3: heading for the top)

        ext.get_action('end_top').trigger()  # (the button)

        assert ext.run_active
        wait_for(qtbot, lambda: unit.settled, timeout=30000)
        qtbot.wait(800)
        assert ext.run_active and unit.hold_target == 3.0 and sent[1][-1] == 3.0
        assert ext.settings['experiment', 'remaining'] == 'until Stop'
        ext.stop_run()
        assert not ext.run_active and sent[1][-1] == 3.0  # (it stays at the top)
        qtbot.waitUntil(lambda: not ext._worker.is_saving, timeout=20000)
        with H5Experiment(tmp_path) as saved:
            codes = saved.zero_d('Unit1_Program', 2).tolist()
            assert codes[-1] == HOLD_CODE and saved.zero_d('Unit1_Program', 0)[-1] == 3.0

    def test_ending_endless_ramps_at_the_bottom(self, ext, qtbot):
        unit = one_unit(ext, n_cycles=0)
        sent = spy_setpoints(ext)
        run(qtbot, ext, velocity=60.)
        qtbot.wait(2000)  # (2 up of 3)

        ext.get_action('end_bottom').trigger()

        wait_for(qtbot, lambda: unit.settled, timeout=30000)
        qtbot.wait(500)
        assert ext.run_active and unit.hold_target == 0.0 and sent[1][-1] == 0.0
        ext.stop_run()
        assert not ext.run_active

    def test_ending_at_the_top_from_the_way_back_turns_round(self, ext, qtbot):
        unit = one_unit(ext, n_cycles=0)
        sent = spy_setpoints(ext)
        run(qtbot, ext, **FAST)
        wait_for(qtbot, lambda: unit.ramp_state is not None and unit.ramp_state.phase == Phase.BACK, timeout=20000)

        ext.finish_ramps('final')

        wait_for(qtbot, lambda: unit.settled, timeout=30000)
        assert unit.hold_target == 3.0 and sent[1][-1] == 3.0
        ext.stop_run()

    def test_stop_while_the_ramps_are_on_their_way_to_the_top_lets_them_arrive(self, ext, qtbot, monkeypatch):
        unit = one_unit(ext, n_cycles=0)
        sent = spy_setpoints(ext)
        asked = []
        monkeypatch.setattr(ext, '_ask_abort_return', lambda: asked.append(True) or True)
        run(qtbot, ext, velocity=60.)
        qtbot.wait(1200)
        ext.finish_ramps('final')

        ext.stop_run()  # (the log ends, but the controller is not sent back: it was told to end at the top)

        wait_for_end(qtbot, ext, timeout=30000)
        assert asked == [] and sent[1][-1] == 3.0 and unit.hold_target is None  # (the run is over)

    def test_pause_does_not_prevent_it(self, ext, qtbot):
        unit = one_unit(ext, n_cycles=0)
        run(qtbot, ext, velocity=60.)
        qtbot.wait(800)
        ext.get_action(WorkFlowActions.PAUSE).trigger()
        assert unit.runner.is_paused

        ext.finish_ramps('final')

        assert not unit.runner.is_paused and not ext.get_action(WorkFlowActions.PAUSE).isChecked()
        wait_for(qtbot, lambda: unit.settled, timeout=30000)
        ext.stop_run()

    def test_nothing_happens_when_there_is_no_run(self, ext):
        ext.finish_ramps('final')

        assert not ext.run_active

    def test_ending_the_ramps_of_one_unit_only_when_the_other_holds(self, ext, qtbot):
        unit = one_unit(ext, n_cycles=0)
        ext.unit(2).settings.child('enabled').setValue(True)
        hold(ext, (2.0,), numbers=(2,))
        ext.unit(1).settings.child('ramp', 'velocity').setValue(60.)
        ext.start_run()
        qtbot.waitUntil(lambda: ext.run_active, timeout=20000)
        qtbot.wait(800)

        ext.finish_ramps('initial')

        wait_for(qtbot, lambda: unit.settled, timeout=30000)
        assert ext.run_active and ext.unit(2).hold_target == 2.0
        ext.stop_run()
        assert not ext.run_active

    def test_the_setting_cannot_be_changed_during_the_log(self, ext, qtbot):
        unit = one_unit(ext, n_cycles=0)
        run(qtbot, ext, velocity=60.)

        assert not ui_enabled(unit.settings.child('ramp', 'end_at'))
        ext.finish_ramps('final')
        wait_for(qtbot, lambda: unit.settled, timeout=30000)
        ext.stop_run()
        assert ui_enabled(unit.settings.child('ramp', 'end_at'))
