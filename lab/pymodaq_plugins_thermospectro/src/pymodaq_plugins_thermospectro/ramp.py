"""Temperature program: ramps up and back, repeated, as a function of time

No Qt and no PyMoDAQ here, only the arithmetic, so that it can be tested without any hardware. The clock is
given by the caller (``now``, in seconds, any origin): the set-point only depends on the time elapsed since the
program started (excluding the pauses), not on how often it is asked for, so a late or irregular timer does not
distort the ramp.

A *cycle* is::

    initial --ramp--> final --wait--> final --ramp back--> initial --wait--> initial

repeated ``n_cycles`` times (forever if 0). The final value can be above or below the initial one. If the thermostat
does not start at the initial value, an *approach* leg brings it there first. When the program is stopped, a *return*
leg brings the set-point back to the initial value from wherever it was.

The way up is defined by three of its four numbers (:class:`RampSpec`): initial value, final value, velocity and
duration. The way back is defined in the same way, by default with the same velocity (:class:`BackSpec`).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import Enum
from typing import Optional


class Phase(str, Enum):
    IDLE = 'idle'
    APPROACH = 'approach'  # towards the initial value, before the first cycle
    RAMP = 'ramp'  # initial -> final
    HOLD_FINAL = 'hold final'
    BACK = 'back'  # final -> initial
    HOLD_INITIAL = 'hold initial'
    RETURN = 'return'  # controlled return to the initial value after a stop or an error
    DONE = 'done'


#: Phases are saved as integers, this is the code of each of them
PHASE_CODES = {phase: code for code, phase in enumerate(Phase)}


def format_duration(seconds: float) -> str:
    """A duration as m:ss, or h:mm:ss from one hour on ('?' if it is not known)"""
    if seconds is None or not math.isfinite(seconds):
        return '?'
    seconds = int(round(max(seconds, 0.0)))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f'{hours:d}:{minutes:02d}:{seconds:02d}' if hours else f'{minutes:d}:{seconds:02d}'


# ---------------------------------------------------------------------------------------------------------------------
# Defining a ramp
# ---------------------------------------------------------------------------------------------------------------------

class RampMode(str, Enum):
    """Which three of the four numbers of a ramp are given"""
    VELOCITY = 'initial, final, velocity'
    DURATION = 'initial, final, duration'
    FINAL = 'initial, velocity, duration'


@dataclass(frozen=True)
class RampSpec:
    """The way from the initial to the final value

    The units are the ones of the thermostat for the values, per minute for the velocity and minutes for the duration.
    Only the numbers named by the mode are used, :meth:`solve` computes the others.

    Parameters
    ----------
    mode: RampMode
    initial: float
    final: float
        used if mode is VELOCITY or DURATION
    velocity: float
        used if mode is VELOCITY (its sign is then ignored: the direction is the one of the final value) or FINAL (a
        negative velocity cools down)
    duration: float
        used if mode is DURATION or FINAL
    """
    mode: RampMode
    initial: float
    final: float = math.nan
    velocity: float = math.nan
    duration: float = math.nan

    def problems(self) -> list[str]:
        """What prevents to solve the ramp (empty if it can)"""
        mode = RampMode(self.mode)
        needed = {RampMode.VELOCITY: ('initial', 'final', 'velocity'),
                  RampMode.DURATION: ('initial', 'final', 'duration'),
                  RampMode.FINAL: ('initial', 'velocity', 'duration')}[mode]
        bad = [name for name in needed if not math.isfinite(getattr(self, name))]
        if bad:
            return [f"{', '.join(bad)}: not a finite number"]
        problems = []
        if mode in (RampMode.VELOCITY, RampMode.DURATION) and self.final == self.initial:
            problems.append('the final value must differ from the initial one')
        if mode in (RampMode.VELOCITY, RampMode.FINAL) and self.velocity == 0:
            problems.append('the velocity cannot be 0')
        if mode in (RampMode.DURATION, RampMode.FINAL) and self.duration <= 0:
            problems.append('the duration must be strictly positive')
        return problems

    def solve(self) -> 'RampSpec':
        """The ramp with its four numbers: the one which was not given is computed

        Raises
        ------
        ValueError
            if the ramp is not defined (see :meth:`problems`)
        """
        problems = self.problems()
        if problems:
            raise ValueError('; '.join(problems))
        mode = RampMode(self.mode)
        if mode == RampMode.VELOCITY:
            velocity = math.copysign(abs(self.velocity), self.final - self.initial)
            return replace(self, velocity=velocity, duration=abs(self.final - self.initial) / abs(self.velocity))
        if mode == RampMode.DURATION:
            return replace(self, velocity=(self.final - self.initial) / self.duration)
        return replace(self, final=self.initial + self.velocity * self.duration)


class BackMode(str, Enum):
    """How the way back is defined"""
    SAME = 'same as the ramp'
    VELOCITY = 'velocity'
    DURATION = 'duration'


@dataclass(frozen=True)
class BackSpec:
    """The way from the final back to the initial value: the same velocity as the way there by default

    Parameters
    ----------
    mode: BackMode
    velocity: float
        used if mode is VELOCITY, per minute, its sign is ignored
    duration: float
        used if mode is DURATION, in minutes
    """
    mode: BackMode = BackMode.SAME
    velocity: float = math.nan
    duration: float = math.nan

    def problems(self) -> list[str]:
        mode = BackMode(self.mode)
        if mode == BackMode.VELOCITY and not (math.isfinite(self.velocity) and self.velocity != 0):
            return ['the velocity of the way back must be a number other than 0']
        if mode == BackMode.DURATION and not (math.isfinite(self.duration) and self.duration > 0):
            return ['the duration of the way back must be strictly positive']
        return []

    def rate(self, span: float, rate_out: float) -> float:
        """The speed of the way back, per minute

        Parameters
        ----------
        span: float
            the distance between the initial and the final values (positive)
        rate_out: float
            the speed of the way there, per minute (positive)
        """
        mode = BackMode(self.mode)
        if mode == BackMode.VELOCITY:
            return abs(self.velocity)
        if mode == BackMode.DURATION:
            return span / self.duration
        return rate_out


@dataclass(frozen=True)
class RampProgram:
    """The parameters of a temperature cycle

    The values are in the unit of the thermostat, the rates in this unit per second and the holds in seconds (see
    :func:`make_program` to build it from what is shown to the user).

    Parameters
    ----------
    initial: float
        The value the cycles start from and come back to
    final: float
        The value reached at the top of the cycle, above or below the initial one
    rate_out, rate_back: float
        The speed of the way there (initial -> final) and of the way back, strictly positive
    hold_final, hold_initial: float
        How long the value is kept at the final and at the initial value
    n_cycles: int
        The number of cycles, 0 to repeat them until the program is stopped
    """
    initial: float
    final: float
    rate_out: float
    rate_back: float
    hold_final: float = 0.0
    hold_initial: float = 0.0
    n_cycles: int = 0

    def problems(self) -> list[str]:
        """What prevents to run this program (empty if it is valid)"""
        numbers = ('initial', 'final', 'rate_out', 'rate_back', 'hold_final', 'hold_initial')
        bad = [name for name in numbers if not math.isfinite(getattr(self, name))]
        if bad:
            return [f"{', '.join(bad)}: not a finite number"]
        problems = []
        if self.final == self.initial:
            problems.append('the final value must differ from the initial one')
        if self.rate_out <= 0:
            problems.append('the velocity of the ramp must be strictly positive')
        if self.rate_back <= 0:
            problems.append('the velocity of the way back must be strictly positive')
        if self.hold_final < 0 or self.hold_initial < 0:
            problems.append('the waiting times cannot be negative')
        if int(self.n_cycles) != self.n_cycles or self.n_cycles < 0:
            problems.append('the number of cycles must be a positive integer, or 0 to run until stopped')
        return problems

    def validate(self):
        problems = self.problems()
        if problems:
            raise ValueError('; '.join(problems))

    @property
    def direction(self) -> int:
        """+1 if the final value is above the initial one, -1 below"""
        return 1 if self.final > self.initial else -1

    @property
    def span(self) -> float:
        return abs(self.final - self.initial)

    @property
    def duration_out(self) -> float:
        return self.span / self.rate_out

    @property
    def duration_back(self) -> float:
        return self.span / self.rate_back

    @property
    def cycle_duration(self) -> float:
        """Duration of one cycle in s"""
        return self.duration_out + self.hold_final + self.duration_back + self.hold_initial

    @property
    def total_duration(self) -> Optional[float]:
        """Duration of all the cycles in s, None when they are repeated until stopped"""
        return self.n_cycles * self.cycle_duration if self.n_cycles > 0 else None


def make_program(ramp: RampSpec, back: BackSpec = BackSpec(), hold_final: float = 0.0, hold_initial: float = 0.0,
                 n_cycles: int = 0) -> RampProgram:
    """Build the program from what is shown to the user

    Parameters
    ----------
    ramp: RampSpec
        the way there, solved or not
    back: BackSpec
        the way back
    hold_final, hold_initial: float
        the waiting times at the final and at the initial value, in minutes
    n_cycles: int
        the number of cycles, 0 to repeat them until stopped

    Raises
    ------
    ValueError
        if the program is not defined
    """
    solved = ramp.solve()
    problems = back.problems()
    if problems:
        raise ValueError('; '.join(problems))
    span = abs(solved.final - solved.initial)
    rate_out = abs(solved.velocity)
    program = RampProgram(initial=solved.initial, final=solved.final, rate_out=rate_out / 60.,
                          rate_back=back.rate(span, rate_out) / 60., hold_final=hold_final * 60.,
                          hold_initial=hold_initial * 60., n_cycles=n_cycles)
    program.validate()
    return program


# ---------------------------------------------------------------------------------------------------------------------
# Following a program in time
# ---------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class RampState:
    """What the program asks for at a given time

    Attributes
    ----------
    phase: Phase
    setpoint: float
        The value to ask the thermostat for
    cycle: int
        The cycle being run, starting at 1 (0 before the first one)
    finished: bool
        The program is over: nothing more to do
    remaining: float
        Seconds left in the current phase, nan if unknown
    paused: bool
    """
    phase: Phase
    setpoint: float
    cycle: int = 0
    finished: bool = False
    remaining: float = math.nan
    paused: bool = False


class RampRunner:
    """Follow a :class:`RampProgram` in time

    Parameters
    ----------
    program: RampProgram
    tolerance: float
        The distance to the initial value below which the thermostat is considered to be at it

    Examples
    --------
    >>> runner = RampRunner(make_program(RampSpec(RampMode.VELOCITY, 25, 35, 5)))
    >>> runner.begin(now=0., current=25.)
    >>> runner.state(now=60.).setpoint  # one minute at 5 degrees/min
    30.0
    >>> runner.stop(now=60.)  # the return leg starts from there
    """

    def __init__(self, program: RampProgram, tolerance: float = 1e-6):
        program.validate()
        self.program = program
        self.tolerance = tolerance
        self._mode = 'idle'  # idle, cycling, return or done
        self._t0 = 0.0  # the clock value when the current timeline started (pauses shift it)
        self._leg: Optional[tuple[Phase, float, float, float]] = None  # (phase, duration, start, end)
        self._paused_at: Optional[float] = None
        self._last_cycle = 0

    # -- timeline ----------------------------------------------------------------------------------------------

    def _make_leg(self, phase: Phase, start: float, end: float) -> Optional[tuple[Phase, float, float, float]]:
        if abs(end - start) <= self.tolerance:
            return None
        p = self.program
        rate = p.rate_out if (end - start) * p.direction > 0 else p.rate_back
        return phase, abs(end - start) / rate, start, end

    def begin(self, now: float, current: float) -> RampState:
        """Start the program, the thermostat being at the `current` value"""
        self._leg = self._make_leg(Phase.APPROACH, current, self.program.initial)
        self._mode = 'cycling'
        self._t0 = now
        self._paused_at = None
        self._last_cycle = 0
        return self.state(now)

    def stop(self, now: float) -> RampState:
        """Bring the set-point back to the initial value, from where it is now, at the speed of the way there or back

        Does nothing if the program is not running or already returning
        """
        if self._mode != 'cycling':
            return self.state(now)
        current = self.state(now)
        if current.finished:
            return current
        self._last_cycle = current.cycle
        self._leg = self._make_leg(Phase.RETURN, current.setpoint, self.program.initial)
        self._mode = 'return'
        self._t0 = now
        self._paused_at = None
        return self.state(now)

    def abort(self) -> RampState:
        """End the program right where it is: the set-point is not followed any more (and is not brought back)"""
        if self._mode in ('cycling', 'return'):
            self._mode = 'done'
            self._paused_at = None
        return RampState(Phase.DONE, self.program.initial, cycle=self._last_cycle, finished=True, remaining=0.0)

    def pause(self, now: float):
        """Freeze the set-point"""
        if self._mode in ('cycling', 'return') and self._paused_at is None:
            self._paused_at = now

    def resume(self, now: float):
        """Carry on from where the program was paused"""
        if self._paused_at is not None:
            self._t0 += now - self._paused_at
            self._paused_at = None

    @property
    def is_paused(self) -> bool:
        return self._paused_at is not None

    @property
    def mode(self) -> str:
        """'idle', 'cycling', 'return' or 'done'"""
        return self._mode

    # -- evaluation --------------------------------------------------------------------------------------------

    def _elapsed(self, now: float) -> float:
        reference = self._paused_at if self._paused_at is not None else now
        return max(0.0, reference - self._t0)

    def _done(self, cycle: int) -> RampState:
        self._mode = 'done'
        self._paused_at = None
        return RampState(Phase.DONE, self.program.initial, cycle=cycle, finished=True, remaining=0.0)

    @staticmethod
    def _along(leg: tuple[Phase, float, float, float], elapsed: float) -> float:
        _, duration, start, end = leg
        return start + (end - start) * min(elapsed / duration, 1.0)

    def state(self, now: float) -> RampState:
        """The state of the program at the time `now`"""
        p = self.program
        if self._mode == 'idle':
            return RampState(Phase.IDLE, p.initial)
        if self._mode == 'done':
            return RampState(Phase.DONE, p.initial, cycle=self._last_cycle, finished=True, remaining=0.0)

        elapsed = self._elapsed(now)
        paused = self._paused_at is not None

        if self._mode == 'return':
            if self._leg is None or elapsed >= self._leg[1]:
                return self._done(self._last_cycle)
            return RampState(Phase.RETURN, self._along(self._leg, elapsed), cycle=self._last_cycle,
                             remaining=self._leg[1] - elapsed, paused=paused)

        # cycling
        approach = self._leg[1] if self._leg is not None else 0.0
        if elapsed < approach:
            return RampState(Phase.APPROACH, self._along(self._leg, elapsed), remaining=approach - elapsed,
                             paused=paused)

        u = elapsed - approach
        period = p.cycle_duration
        index = int(u // period)
        if p.n_cycles > 0 and index >= p.n_cycles:
            return self._done(p.n_cycles)
        w = u - index * period
        cycle = index + 1
        self._last_cycle = cycle
        end_out = p.duration_out
        end_hold = end_out + p.hold_final
        end_back = end_hold + p.duration_back
        if w < end_out:
            phase, setpoint, remaining = Phase.RAMP, p.initial + p.direction * p.rate_out * w, end_out - w
        elif w < end_hold:
            phase, setpoint, remaining = Phase.HOLD_FINAL, p.final, end_hold - w
        elif w < end_back:
            phase = Phase.BACK
            setpoint, remaining = p.final - p.direction * p.rate_back * (w - end_hold), end_back - w
        else:
            phase, setpoint, remaining = Phase.HOLD_INITIAL, p.initial, period - w
        return RampState(phase, setpoint, cycle=cycle, remaining=remaining, paused=paused)
