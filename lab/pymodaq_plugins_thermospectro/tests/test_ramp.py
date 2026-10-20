import math

import numpy as np
import pytest

from pymodaq_plugins_thermospectro.ramp import (BackMode, BackSpec, PHASE_CODES, Phase, RampMode, RampProgram,
                                                RampRunner, RampSpec, format_duration, make_program)


# 20 -> 30 at 1/s (10 s), 3 s at 30, 30 -> 20 at 2/s (5 s), 4 s at 20: a period of 22 s
def program(**kwargs):
    values = dict(initial=20., final=30., rate_out=1., rate_back=2., hold_final=3., hold_initial=4., n_cycles=0)
    values.update(kwargs)
    return RampProgram(**values)


def started(prog=None, current=20., now=0.):
    runner = RampRunner(prog or program())
    runner.begin(now, current)
    return runner


class TestRampSpec:
    """A ramp is defined by three of: initial value, final value, velocity (per minute), duration (minutes)"""

    def test_initial_final_velocity(self):
        spec = RampSpec(RampMode.VELOCITY, initial=20., final=50., velocity=3.).solve()

        assert spec.duration == pytest.approx(10.)  # minutes
        assert spec.velocity == pytest.approx(3.)
        assert spec.final == 50.

    def test_initial_final_duration(self):
        spec = RampSpec(RampMode.DURATION, initial=20., final=50., duration=15.).solve()

        assert spec.velocity == pytest.approx(2.)
        assert spec.duration == 15.

    def test_initial_velocity_duration(self):
        spec = RampSpec(RampMode.FINAL, initial=20., velocity=3., duration=10.).solve()

        assert spec.final == pytest.approx(50.)

    def test_cooling_down(self):
        down = RampSpec(RampMode.VELOCITY, initial=50., final=20., velocity=3.).solve()
        assert down.velocity == pytest.approx(-3.)  # the sign says the direction
        assert down.duration == pytest.approx(10.)

        assert RampSpec(RampMode.DURATION, initial=50., final=20., duration=10.).solve().velocity == pytest.approx(-3.)
        assert RampSpec(RampMode.FINAL, initial=50., velocity=-3., duration=10.).solve().final == pytest.approx(20.)

    def test_the_sign_of_the_velocity_is_ignored_when_the_final_value_is_given(self):
        spec = RampSpec(RampMode.VELOCITY, initial=20., final=50., velocity=-3.).solve()

        assert spec.velocity == pytest.approx(3.)
        assert spec.duration == pytest.approx(10.)

    def test_the_numbers_which_are_not_used_are_ignored(self):
        spec = RampSpec(RampMode.VELOCITY, initial=20., final=50., velocity=3., duration=999.).solve()

        assert spec.duration == pytest.approx(10.)

    def test_the_three_ways_agree(self):
        a = RampSpec(RampMode.VELOCITY, 20., 50., velocity=2.).solve()
        b = RampSpec(RampMode.DURATION, 20., 50., duration=a.duration).solve()
        c = RampSpec(RampMode.FINAL, 20., velocity=a.velocity, duration=a.duration).solve()

        for spec in (a, b, c):
            assert (spec.initial, spec.final) == (20., pytest.approx(50.))
            assert spec.velocity == pytest.approx(2.) and spec.duration == pytest.approx(15.)

    @pytest.mark.parametrize('spec, message', [
        (RampSpec(RampMode.VELOCITY, 20., 20., velocity=3.), 'differ'),
        (RampSpec(RampMode.VELOCITY, 20., 30., velocity=0.), 'velocity cannot be 0'),
        (RampSpec(RampMode.DURATION, 20., 20., duration=3.), 'differ'),
        (RampSpec(RampMode.DURATION, 20., 30., duration=0.), 'duration must be strictly positive'),
        (RampSpec(RampMode.DURATION, 20., 30., duration=-1.), 'duration must be strictly positive'),
        (RampSpec(RampMode.FINAL, 20., velocity=0., duration=3.), 'velocity cannot be 0'),
        (RampSpec(RampMode.FINAL, 20., velocity=1., duration=0.), 'duration'),
        (RampSpec(RampMode.VELOCITY, 20., 30.), 'velocity'),  # not given
        (RampSpec(RampMode.DURATION, 20., math.nan, duration=3.), 'final'),
        (RampSpec(RampMode.FINAL, 20., velocity=1.), 'duration'),
        (RampSpec(RampMode.VELOCITY, math.inf, 30., velocity=1.), 'initial'),
    ])
    def test_what_cannot_be_solved_is_said(self, spec, message):
        assert any(message in problem for problem in spec.problems())
        with pytest.raises(ValueError, match=message):
            spec.solve()

    def test_a_valid_spec_has_no_problem(self):
        assert RampSpec(RampMode.VELOCITY, 20., 30., velocity=1.).problems() == []

    def test_modes_are_the_texts_of_the_list(self):
        assert [mode.value for mode in RampMode] == ['initial, final, velocity', 'initial, final, duration',
                                                     'initial, velocity, duration']
        assert RampMode('initial, final, duration') == RampMode.DURATION


class TestBackSpec:
    def test_by_default_the_way_back_is_as_fast_as_the_way_there(self):
        assert BackSpec().rate(span=30., rate_out=3.) == 3.

    def test_other_velocity(self):
        assert BackSpec(BackMode.VELOCITY, velocity=-5.).rate(span=30., rate_out=3.) == 5.  # (sign ignored)

    def test_duration(self):
        assert BackSpec(BackMode.DURATION, duration=6.).rate(span=30., rate_out=3.) == 5.

    @pytest.mark.parametrize('spec', [BackSpec(BackMode.VELOCITY, velocity=0.), BackSpec(BackMode.VELOCITY),
                                      BackSpec(BackMode.DURATION, duration=0.), BackSpec(BackMode.DURATION)])
    def test_problems(self, spec):
        assert spec.problems()

    def test_no_problem(self):
        assert BackSpec().problems() == []
        assert BackSpec(BackMode.DURATION, duration=1.).problems() == []


class TestMakeProgram:
    def test_units_are_per_second(self):
        prog = make_program(RampSpec(RampMode.VELOCITY, 25., 40., velocity=3.), BackSpec(BackMode.VELOCITY, velocity=6.),
                            hold_final=2., hold_initial=0.5, n_cycles=4)

        assert prog.initial == 25. and prog.final == 40.
        assert prog.rate_out == pytest.approx(0.05)
        assert prog.rate_back == pytest.approx(0.1)
        assert prog.hold_final == 120. and prog.hold_initial == 30.
        assert prog.n_cycles == 4
        assert prog.duration_out == pytest.approx(300.)  # 15 degrees at 3 per minute
        assert prog.duration_back == pytest.approx(150.)

    def test_the_way_back_is_the_same_by_default(self):
        prog = make_program(RampSpec(RampMode.DURATION, 20., 50., duration=15.))

        assert prog.rate_back == prog.rate_out == pytest.approx(2. / 60.)
        assert prog.duration_back == pytest.approx(prog.duration_out)

    def test_a_way_back_in_given_time(self):
        prog = make_program(RampSpec(RampMode.DURATION, 20., 50., duration=15.), BackSpec(BackMode.DURATION, duration=5.))

        assert prog.duration_back == pytest.approx(300.)

    def test_cooling_program(self):
        prog = make_program(RampSpec(RampMode.FINAL, 50., velocity=-3., duration=10.))

        assert prog.direction == -1
        assert prog.final == pytest.approx(20.)
        assert prog.rate_out == pytest.approx(0.05)

    def test_what_is_wrong_raises(self):
        with pytest.raises(ValueError, match='velocity cannot be 0'):
            make_program(RampSpec(RampMode.VELOCITY, 20., 30., velocity=0.))
        with pytest.raises(ValueError, match='way back'):
            make_program(RampSpec(RampMode.VELOCITY, 20., 30., velocity=1.), BackSpec(BackMode.DURATION, duration=-1.))
        with pytest.raises(ValueError, match='waiting times'):
            make_program(RampSpec(RampMode.VELOCITY, 20., 30., velocity=1.), hold_final=-1.)
        with pytest.raises(ValueError, match='cycles'):
            make_program(RampSpec(RampMode.VELOCITY, 20., 30., velocity=1.), n_cycles=-2)


class TestProgram:
    def test_durations(self):
        prog = program(n_cycles=3)
        assert prog.duration_out == 10
        assert prog.duration_back == 5
        assert prog.cycle_duration == 22
        assert prog.total_duration == 66

    def test_total_duration_is_unknown_until_stopped(self):
        assert program(n_cycles=0).total_duration is None

    def test_a_valid_program_has_no_problem(self):
        assert program().problems() == []
        assert program(final=10.).problems() == []  # below the initial value

    @pytest.mark.parametrize('kwargs, message', [
        (dict(final=20.), 'final value must differ'),
        (dict(rate_out=0.), 'velocity of the ramp'),
        (dict(rate_out=-1.), 'velocity of the ramp'),
        (dict(rate_back=0.), 'way back'),
        (dict(hold_final=-1.), 'waiting times'),
        (dict(hold_initial=-1.), 'waiting times'),
        (dict(n_cycles=-1), 'cycles'),
        (dict(n_cycles=1.5), 'cycles'),
        (dict(final=math.nan), 'finite'),
        (dict(rate_out=math.inf), 'finite'),
    ])
    def test_invalid_programs_are_refused_with_a_reason(self, kwargs, message):
        prog = program(**kwargs)
        assert any(message in problem for problem in prog.problems())
        with pytest.raises(ValueError, match=message):
            RampRunner(prog)

    def test_phase_codes_are_distinct_integers(self):
        assert len(set(PHASE_CODES.values())) == len(Phase)
        assert all(isinstance(code, int) for code in PHASE_CODES.values())


class TestCycles:
    @pytest.mark.parametrize('now, phase, setpoint', [
        (0., Phase.RAMP, 20.),
        (5., Phase.RAMP, 25.),
        (9.99, Phase.RAMP, 29.99),
        (10., Phase.HOLD_FINAL, 30.),
        (12.99, Phase.HOLD_FINAL, 30.),
        (13., Phase.BACK, 30.),
        (15.5, Phase.BACK, 25.),
        (17.99, Phase.BACK, 20.02),
        (18., Phase.HOLD_INITIAL, 20.),
        (21.99, Phase.HOLD_INITIAL, 20.),
    ])
    def test_the_first_cycle(self, now, phase, setpoint):
        state = started().state(now)

        assert state.phase == phase
        assert state.setpoint == pytest.approx(setpoint)
        assert state.cycle == 1
        assert not state.finished

    @pytest.mark.parametrize('now, phase, setpoint', [
        (0., Phase.RAMP, 30.),
        (5., Phase.RAMP, 25.),
        (10., Phase.HOLD_FINAL, 20.),
        (13., Phase.BACK, 20.),
        (15.5, Phase.BACK, 25.),
        (18., Phase.HOLD_INITIAL, 30.),
    ])
    def test_a_ramp_down_and_back_up(self, now, phase, setpoint):
        # 30 -> 20 at 1/s, 3 s at 20, 20 -> 30 at 2/s, 4 s at 30
        state = started(program(initial=30., final=20.), current=30.).state(now)

        assert state.phase == phase
        assert state.setpoint == pytest.approx(setpoint)

    def test_the_cycle_starts_again(self):
        runner = started()

        assert runner.state(22.).phase == Phase.RAMP
        assert runner.state(22.).cycle == 2
        assert runner.state(22.).setpoint == pytest.approx(20.)
        assert runner.state(27.).setpoint == pytest.approx(25.)

    def test_cycles_are_repeated_until_stopped(self):
        runner = started()
        state = runner.state(22. * 1000 + 5.)

        assert state.cycle == 1001
        assert state.phase == Phase.RAMP
        assert state.setpoint == pytest.approx(25.)
        assert not state.finished

    def test_the_number_of_cycles_is_the_one_asked_for(self):
        runner = started(program(n_cycles=3))

        assert runner.state(22. * 3 - 0.01).cycle == 3
        assert not runner.state(22. * 3 - 0.01).finished
        assert runner.state(22. * 3).finished

    def test_finite_cycles_end_at_the_initial_value(self):
        runner = started(program(n_cycles=2))

        assert runner.state(43.99).cycle == 2
        assert not runner.state(43.99).finished
        state = runner.state(44.)
        assert state.finished and state.phase == Phase.DONE
        assert state.setpoint == 20.
        assert state.cycle == 2
        assert runner.mode == 'done'
        assert runner.state(1000.).finished

    def test_no_wait(self):
        runner = started(program(hold_final=0., hold_initial=0.))

        assert runner.state(10.).phase == Phase.BACK
        assert runner.state(15.).phase == Phase.RAMP
        assert runner.state(15.).cycle == 2

    def test_remaining_time_in_the_phase(self):
        runner = started()

        assert runner.state(4.).remaining == pytest.approx(6.)
        assert runner.state(11.).remaining == pytest.approx(2.)
        assert runner.state(14.).remaining == pytest.approx(4.)
        assert runner.state(19.).remaining == pytest.approx(3.)

    def test_the_program_does_not_depend_on_how_often_it_is_asked(self):
        runner = started()
        sparse = [runner.state(t).setpoint for t in (3., 17., 40.)]
        runner = started()
        for t in np.arange(0, 41, 0.01):
            runner.state(t)

        assert [runner.state(t).setpoint for t in (3., 17., 40.)] == sparse

    def test_a_late_start_of_the_clock_is_not_a_problem(self):
        runner = started(now=1e6)

        assert runner.state(1e6 + 5.).setpoint == pytest.approx(25.)

    def test_before_the_start_the_set_point_is_the_initial_value(self):
        runner = RampRunner(program())

        assert runner.state(10.).phase == Phase.IDLE
        assert runner.state(10.).setpoint == 20.

    @pytest.mark.parametrize('prog', [program(rate_out=0.7, rate_back=1.3, hold_final=2.1, hold_initial=0.9),
                                      program(initial=33., final=20., rate_out=0.7, rate_back=1.3, hold_final=2.1,
                                              hold_initial=0.9)])
    def test_the_set_point_never_jumps(self, prog):
        runner = started(prog, current=26.)
        step = 0.05
        times = np.arange(0, 400, step)
        setpoints = np.array([runner.state(t).setpoint for t in times])

        assert np.all(np.abs(np.diff(setpoints)) <= 1.3 * step + 1e-9)
        assert setpoints.min() >= 20 - 1e-9
        assert setpoints.max() <= 33. + 1e-9


class TestApproach:
    def test_going_to_the_initial_value_first_from_above(self):
        runner = started(current=26.)  # 6 above: the way back is at 2/s, 3 s

        assert runner.state(0.).phase == Phase.APPROACH
        assert runner.state(0.).setpoint == pytest.approx(26.)
        assert runner.state(1.5).setpoint == pytest.approx(23.)
        assert runner.state(1.5).cycle == 0
        assert runner.state(1.5).remaining == pytest.approx(1.5)
        assert runner.state(3.).phase == Phase.RAMP
        assert runner.state(3.).cycle == 1
        assert runner.state(8.).setpoint == pytest.approx(25.)

    def test_going_to_the_initial_value_first_from_below(self):
        runner = started(current=14.)  # 6 below: the way there is at 1/s, 6 s

        assert runner.state(3.).phase == Phase.APPROACH
        assert runner.state(3.).setpoint == pytest.approx(17.)
        assert runner.state(6.).phase == Phase.RAMP

    def test_when_the_ramp_goes_down_the_directions_are_swapped(self):
        prog = program(initial=30., final=20.)  # the way there goes down at 1/s, the way back up at 2/s

        from_below = started(prog, current=24.)  # 6 below the initial value: up, at the speed of the way back
        assert from_below.state(3.).setpoint == pytest.approx(30.)
        assert from_below.state(3.).phase == Phase.RAMP
        from_above = started(prog, current=36.)  # 6 above: down, at the speed of the way there
        assert from_above.state(6.).phase == Phase.RAMP

    def test_no_approach_when_already_at_the_initial_value(self):
        runner = started(current=20. + 1e-9)

        assert runner.state(0.).phase == Phase.RAMP

    def test_the_approach_does_not_count_as_a_cycle(self):
        runner = started(program(n_cycles=1), current=26.)

        assert runner.state(3. + 21.9).cycle == 1
        assert not runner.state(3. + 21.9).finished
        assert runner.state(3. + 22.).finished


class TestStop:
    @pytest.mark.parametrize('now, setpoint, rate', [
        (5., 25., 2.),  # during the ramp
        (11., 30., 2.),  # at the final value
        (15., 26., 2.),  # during the way back
    ])
    def test_it_returns_to_the_initial_value_from_where_it_is(self, now, setpoint, rate):
        runner = started()
        runner.state(now)

        state = runner.stop(now)

        assert state.phase == Phase.RETURN
        assert state.setpoint == pytest.approx(setpoint)
        assert runner.mode == 'return'
        duration = (setpoint - 20.) / rate
        assert state.remaining == pytest.approx(duration)
        assert runner.state(now + duration / 2).setpoint == pytest.approx(20. + (setpoint - 20.) / 2)
        assert not runner.state(now + duration - 0.01).finished
        assert runner.state(now + duration).finished
        assert runner.state(now + duration).setpoint == 20.
        assert runner.mode == 'done'

    def test_the_return_of_a_ramp_down(self):
        runner = started(program(initial=30., final=20.), current=30.)

        state = runner.stop(5.)  # at 25, going down

        assert state.setpoint == pytest.approx(25.)
        assert runner.state(5. + 1.).setpoint == pytest.approx(27.)  # back up to 30 at 2/s
        assert runner.state(5. + 2.5).finished

    def test_the_return_is_monotonic_and_never_beyond_the_initial_value(self):
        runner = started()
        runner.stop(6.)
        setpoints = [runner.state(t).setpoint for t in np.arange(6., 12., 0.01)]

        assert all(a >= b for a, b in zip(setpoints, setpoints[1:]))
        assert min(setpoints) == 20.

    def test_nothing_to_return_from_when_at_the_initial_value(self):
        runner = started()

        state = runner.stop(19.)  # waiting at the initial value

        assert state.finished
        assert state.phase == Phase.DONE

    def test_stopping_during_the_approach_goes_on_to_the_initial_value(self):
        runner = started(current=26.)

        state = runner.stop(1.5)  # set-point at 23

        assert state.phase == Phase.RETURN
        assert state.setpoint == pytest.approx(23.)
        assert runner.state(1.5 + 1.5).finished

    def test_stopping_during_an_approach_from_below(self):
        runner = started(current=14.)

        runner.stop(3.)  # set-point at 17: 3 to go at 1/s

        assert not runner.state(3. + 2.9).finished
        assert runner.state(3. + 3.).finished

    def test_stopping_twice_does_not_restart_the_return(self):
        runner = started()
        runner.stop(5.)
        before = runner.state(6.)

        runner.stop(6.)

        assert runner.state(6.) == before
        assert runner.state(6.).phase == Phase.RETURN

    def test_stopping_a_finished_program_does_nothing(self):
        runner = started(program(n_cycles=1))
        assert runner.state(30.).finished

        assert runner.stop(31.).finished

    def test_stopping_before_the_start_does_nothing(self):
        runner = RampRunner(program())

        assert runner.stop(1.).phase == Phase.IDLE

    def test_the_cycle_that_was_running_is_remembered(self):
        runner = started()
        runner.stop(22. * 2 + 5.)  # third cycle

        assert runner.state(22. * 2 + 5.).cycle == 3
        assert runner.state(1000.).cycle == 3


class TestPause:
    def test_the_set_point_is_frozen(self):
        runner = started()
        runner.state(5.)
        runner.pause(5.)

        for now in (5., 50., 5000.):
            state = runner.state(now)
            assert state.setpoint == pytest.approx(25.)
            assert state.paused

    def test_the_program_goes_on_after_the_pause(self):
        runner = started()
        runner.pause(5.)
        runner.resume(100.)

        assert not runner.state(100.).paused
        assert runner.state(100.).setpoint == pytest.approx(25.)
        assert runner.state(105.).setpoint == pytest.approx(30.)
        assert runner.state(105.).phase == Phase.HOLD_FINAL

    def test_several_pauses(self):
        runner = started()
        runner.pause(2.)
        runner.resume(10.)
        runner.pause(13.)  # 5 s of program
        runner.resume(20.)

        assert runner.state(20. + 5.).setpoint == pytest.approx(30.)  # 10 s of program

    def test_pausing_twice_or_resuming_without_pause_changes_nothing(self):
        runner = started()
        runner.pause(5.)
        runner.pause(8.)
        runner.resume(10.)
        runner.resume(12.)

        assert runner.state(10.).setpoint == pytest.approx(25.)  # 5 s of program
        assert runner.state(12.).setpoint == pytest.approx(27.)  # 7 s of program: the clock went on

    def test_the_return_can_be_paused(self):
        runner = started()
        runner.stop(5.)  # 25 -> 20 at 2/s
        runner.pause(6.)  # 23
        assert runner.state(60.).setpoint == pytest.approx(23.)
        runner.resume(60.)

        assert runner.state(60. + 1.5).finished

    def test_stopping_while_paused_returns_from_the_frozen_set_point(self):
        runner = started()
        runner.pause(5.)

        state = runner.stop(50.)

        assert state.phase == Phase.RETURN
        assert state.setpoint == pytest.approx(25.)
        assert not state.paused
        assert runner.state(50. + 2.5).finished

    def test_nothing_to_pause_before_the_start_or_after_the_end(self):
        runner = RampRunner(program())
        runner.pause(1.)
        assert not runner.is_paused

        runner = started(program(n_cycles=1))
        runner.state(100.)
        runner.pause(101.)
        assert not runner.is_paused


class TestAbort:
    def test_the_program_ends_where_it_is(self):
        runner = started()
        runner.state(22. + 5.)  # second cycle

        state = runner.abort()

        assert state.finished and state.phase == Phase.DONE
        assert runner.mode == 'done'
        assert state.cycle == 2
        assert runner.state(1000.).finished and runner.state(1000.).cycle == 2

    def test_a_return_can_be_aborted_too(self):
        runner = started()
        runner.stop(5.)

        runner.abort()

        assert runner.mode == 'done'
        assert runner.state(6.).finished

    def test_an_aborted_program_is_not_paused_any_more(self):
        runner = started()
        runner.pause(5.)

        runner.abort()

        assert not runner.is_paused


class TestEndingAtTheTop:
    """A program which ends at the final value: the last cycle stops when the top is reached (no wait, no way back)"""

    def test_the_last_cycle_stops_when_the_top_is_reached(self):
        runner = started(program(n_cycles=2, end_at='final'))

        # the first cycle is complete: up (0-10), wait (10-13), back (13-18), wait (18-22)
        assert runner.state(11.).phase == Phase.HOLD_FINAL and runner.state(11.).cycle == 1
        assert runner.state(20.).phase == Phase.HOLD_INITIAL
        # the second one goes up from 22 to 32, and stops there
        state = runner.state(31.5)
        assert state.phase == Phase.RAMP and state.cycle == 2 and state.setpoint == pytest.approx(29.5)
        assert not state.finished
        state = runner.state(32.)
        assert state.finished and state.phase == Phase.DONE and state.setpoint == 30. and state.cycle == 2
        assert runner.mode == 'done'
        assert runner.state(1000.).setpoint == 30.  # (it stays there)

    def test_one_cycle_is_a_ramp_to_the_top(self):
        runner = started(program(n_cycles=1, end_at='final'))

        assert not runner.state(9.9).finished
        state = runner.state(10.)
        assert state.finished and state.setpoint == 30. and state.cycle == 1

    def test_the_total_duration_has_no_last_way_back(self):
        assert program(n_cycles=2, end_at='final').total_duration == pytest.approx(22. + 10.)
        assert program(n_cycles=2).total_duration == pytest.approx(44.)
        assert program(n_cycles=0, end_at='final').total_duration is None

    def test_cycles_which_go_on_until_stopped_do_not_end_by_themselves(self):
        runner = started(program(n_cycles=0, end_at='final'))

        assert not runner.state(10 * 22. + 10.).finished and not runner.state(10 * 22. + 15.).finished

    def test_a_cooling_program_ends_at_its_final_value_below(self):
        runner = started(program(initial=30., final=20., rate_out=2., rate_back=1., n_cycles=1, end_at='final'),
                         current=30.)

        assert not runner.state(4.9).finished
        state = runner.state(5.)
        assert state.finished and state.setpoint == 20.

    def test_the_rest_value_is_where_the_program_ends(self):
        assert program().rest == 20. and program(end_at='final').rest == 30.
        assert started(program(end_at='final')).rest == 30. and started().rest == 20.

    def test_a_program_which_ends_at_the_initial_value_is_as_before(self):
        runner = started(program(n_cycles=2, end_at='initial'))

        state = runner.state(44.)
        assert state.finished and state.setpoint == 20. and state.cycle == 2

    def test_an_unknown_end_is_refused(self):
        assert any('ends at' in problem for problem in program(end_at='middle').problems())
        with pytest.raises(ValueError):
            RampRunner(program(end_at='middle'))

    def test_the_program_made_from_what_is_shown_has_it(self):
        made = make_program(RampSpec(RampMode.VELOCITY, 20., 30., 60.), end_at='final')

        assert made.end_at == 'final' and make_program(RampSpec(RampMode.VELOCITY, 20., 30., 60.)).end_at == 'initial'


class TestFinish:
    """End the program at the top or at the bottom at any time: the set-point goes there from where it is, and stays"""

    def test_at_the_top_the_set_point_goes_there_from_where_it_is(self):
        runner = started()
        runner.state(4.)  # 24, going up at 1 a second

        state = runner.finish(4., 'final')

        assert state.phase == Phase.RAMP and state.setpoint == pytest.approx(24.) and not state.finished
        assert state.remaining == pytest.approx(6.) and runner.mode == 'return' and runner.rest == 30.
        assert runner.state(7.).setpoint == pytest.approx(27.)
        state = runner.state(10.)
        assert state.finished and state.phase == Phase.DONE and state.setpoint == 30. and state.cycle == 1
        assert runner.mode == 'done' and runner.state(500.).setpoint == 30.  # (it stays)

    def test_at_the_bottom_it_is_the_return_to_the_initial_value(self):
        runner = started()
        runner.state(4.)

        state = runner.finish(4., 'initial')

        assert state.phase == Phase.RETURN and state.setpoint == pytest.approx(24.)
        assert state.remaining == pytest.approx(2.)  # (4 degrees at the 2 a second of the way back)
        end = runner.state(6.)
        assert end.finished and end.setpoint == 20. and runner.rest == 20.

    def test_stopping_is_ending_at_the_bottom(self):
        stopped, finished = started(), started()
        stopped.state(4.), finished.state(4.)

        stopped.stop(4.)
        finished.finish(4., 'initial')

        for now in (4., 5., 5.9, 6., 100.):
            assert stopped.state(now) == finished.state(now)

    def test_at_the_top_from_the_way_back_it_turns_round(self):
        runner = started()
        runner.state(15.)  # on the way back: 30 - 2 * (15 - 13) = 26

        state = runner.finish(15., 'final')

        assert state.setpoint == pytest.approx(26.) and state.phase == Phase.RAMP
        assert state.remaining == pytest.approx(4.)  # (up at 1 a second)
        assert runner.state(17.).setpoint == pytest.approx(28.)
        assert runner.state(19.).finished and runner.state(19.).setpoint == 30.

    def test_at_the_top_when_it_is_there_already(self):
        runner = started()
        runner.state(11.)  # waiting at 30

        state = runner.finish(11., 'final')

        assert state.finished and state.setpoint == 30. and state.cycle == 1 and runner.mode == 'done'

    def test_at_the_bottom_when_it_is_there_already(self):
        runner = started()
        runner.state(20.)  # waiting at 20

        state = runner.finish(20., 'initial')

        assert state.finished and state.setpoint == 20. and runner.mode == 'done'

    def test_the_set_point_does_not_jump_when_it_is_told(self):
        for at in ('final', 'initial'):
            for now in (1., 9., 11., 14., 19.):
                runner = started()
                before = runner.state(now).setpoint

                after = runner.finish(now, at).setpoint

                assert after == pytest.approx(before, abs=1e-9), (at, now)

    def test_the_cycle_which_was_running_is_remembered(self):
        runner = started()
        runner.state(22. + 8.)  # the second cycle, on its way up

        runner.finish(30., 'final')

        assert runner.state(30.).cycle == 2 and runner.state(1000.).cycle == 2

    def test_ending_twice_does_not_restart_the_leg(self):
        runner = started()
        runner.state(4.)
        runner.finish(4., 'final')

        runner.finish(6., 'initial')  # (it is on its way to the top: it goes on)

        assert runner.state(6.).setpoint == pytest.approx(26.) and runner.rest == 30.

    def test_a_program_which_is_over_or_which_did_not_start_cannot_be_ended(self):
        idle = RampRunner(program())
        assert idle.finish(0., 'final').phase == Phase.IDLE and idle.mode == 'idle'
        done = started(program(n_cycles=1))
        done.state(100.)
        assert done.finish(101., 'final').finished and done.state(101.).setpoint == 20.  # (it ended at the bottom)

    def test_pausing_does_not_prevent_it(self):
        runner = started()
        runner.state(4.)
        runner.pause(4.)

        state = runner.finish(100., 'final')

        assert state.setpoint == pytest.approx(24.) and not state.paused and not runner.is_paused
        assert runner.state(103.).setpoint == pytest.approx(27.)

    def test_during_the_approach_it_goes_straight_there(self):
        runner = started(current=26.)  # 6 above the initial value: the approach takes 3 s
        assert runner.state(1.).setpoint == pytest.approx(24.)

        state = runner.finish(1., 'final')

        assert state.setpoint == pytest.approx(24.) and state.remaining == pytest.approx(6.)
        assert runner.state(7.).finished and runner.state(7.).setpoint == 30.

    def test_the_program_can_be_ended_at_the_top_when_it_was_to_end_at_the_bottom_and_the_reverse(self):
        up = started(program(n_cycles=3, end_at='initial'))
        down = started(program(n_cycles=3, end_at='final'))
        up.state(4.), down.state(4.)

        up.finish(4., 'final')
        down.finish(4., 'initial')

        assert up.state(10.).setpoint == 30. and down.state(6.).setpoint == 20.  # (what is told last wins)

    def test_an_unknown_end_is_refused(self):
        with pytest.raises(ValueError):
            started().finish(0., 'middle')


@pytest.mark.parametrize('seconds, text', [(0, '0:00'), (59.4, '0:59'), (61, '1:01'), (3600, '1:00:00'),
                                           (3725, '1:02:05'), (-5, '0:00'), (math.nan, '?'), (None, '?'),
                                           (math.inf, '?')])
def test_format_duration(seconds, text):
    assert format_duration(seconds) == text
