"""A unit at run time: a spectrometer and the thermostat which controls its temperature

A unit reads its spectrometer out once per *readout time* (it asks for one spectrum, which is integrated during the
*integration time*), asks the thermostat for its temperature at the same time, and gives out a :class:`Measurement`:
the spectrum, the value of each ROI on it, the temperature and, during an experiment, the state of the temperature
ramp. The ramp is run by a software set-point sent to the thermostat at regular intervals (any thermostat with a
PyMoDAQ plugin can be used), and it is brought back to the initial value on stop or on error.

The units of an experiment are independent: each one has its own modules, times, ROIs and ramp. The extension
(:mod:`.extensions.thermo_spectro`) creates the units, shows them and saves what they measure.
"""
from __future__ import annotations

import functools
import math
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import numpy as np
from qtpy import QtCore

from pymodaq_utils.logger import set_logger, get_module_name
from pymodaq_utils.utils import ThreadCommand

from pymodaq_data import DataToExport

from pymodaq.control_modules.thread_commands import ControlToHardwareMove
from pymodaq.utils.data import DataActuator
from pymodaq.utils.managers.modules import ModuleType

from pymodaq_plugins_thermospectro.buffers import TimeSeries
from pymodaq_plugins_thermospectro.camera import UNITS as PIXELS, Frame, extract_frame
from pymodaq_plugins_thermospectro.ramp import Phase, RampProgram, RampRunner, RampState, format_duration
from pymodaq_plugins_thermospectro.rois import Roi
from pymodaq_plugins_thermospectro.spectrometers import (Reading, Spectrum, extract_spectrum, set_integration_time,
                                                         wavelength_axis)
from pymodaq_plugins_thermospectro.unit_settings import NONE, UnitSettings

logger = set_logger(get_module_name(__file__))


def logged(function):
    """Log what a slot raises: an exception which escapes from a slot would abort the application"""
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:
            logger.exception(f'{function.__qualname__} failed')
    return wrapper


class UnitState(str, Enum):
    IDLE = 'idle'  # the modules are left alone
    LIVE = 'live'  # the spectrometer is read out, nothing is driven nor recorded
    RUNNING = 'running'  # an experiment: the ramp is run (if there is one) and the measurements are recorded


class Problem(str, Enum):
    """What went wrong with a unit (see :attr:`Unit.problem`)"""
    SPECTROMETER = 'spectrometer'  # it delivers nothing, or it was closed
    THERMOSTAT_SILENT = 'thermostat_silent'  # it does not give its temperature any more
    THERMOSTAT_LOST = 'thermostat_lost'  # it was closed: nothing can bring it back to the initial value


@dataclass
class Measurement:
    """What a unit measured at one readout

    Attributes
    ----------
    unit: int
        The number of the unit (1, 2)
    index: int
        The number of this measurement since the unit started reading out
    t: float
        When the readout started (epoch seconds): the integration of the spectrum follows
    spectrum: Spectrum
    rois: list of Roi
        The ROIs as they were when the spectrum was measured
    values: list of float
        The value of each ROI (nan if it is outside of the spectrum)
    temperature: float
        As measured by the thermostat (nan if it is not known)
    setpoint: float
        Asked to the thermostat by the ramp (nan if there is no ramp running)
    phase: Phase or None
        The phase of the ramp (None if there is no ramp running)
    cycle: int
        The cycle of the ramp (0 if there is no ramp running)
    """
    unit: int
    index: int
    t: float
    spectrum: Spectrum
    rois: list
    values: list
    temperature: float = math.nan
    setpoint: float = math.nan
    phase: Optional[Phase] = None
    cycle: int = 0


class Unit(QtCore.QObject):
    """A spectrometer and its thermostat, read out and driven during the live view or an experiment

    Parameters
    ----------
    settings: UnitSettings
        What the user set up (the parameter tree of the unit)
    modules_manager: ModulesManager
        To find the modules of the Dashboard by their name

    Attributes
    ----------
    measurement: Signal(Measurement)
        Emitted for each readout, once the spectrum came back
    notice: Signal(str)
        Something to tell in the status bar
    problem: Signal(Unit, Problem, str)
        A module does not work any more. When it is emitted the unit has already done what it can for the
        thermostat: the ramp is on its way back to the initial value (nothing can be done if the thermostat is lost)
    axis_units_changed: Signal(str)
        The units of the wavelengths of the spectrum are not the same as before (nm, or the axis as it is)
    """
    measurement = QtCore.Signal(object)
    notice = QtCore.Signal(str)
    problem = QtCore.Signal(object, object, str)
    axis_units_changed = QtCore.Signal(str)

    #: A module which delivers nothing is said to the user after this time (s) of live view
    SILENCE_NOTICE = 10.0
    #: What the detector of the unit is, in the messages
    WHAT = 'spectrometer'
    DATA = 'spectrum'
    DIMENSION = '1D'
    TIME_NAME = 'integration'

    def __init__(self, settings: UnitSettings, modules_manager, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.number = settings.number
        self._manager = modules_manager
        self.detector = None
        self.thermostat = None
        self.state = UnitState.IDLE
        self.spectrum: Optional[Spectrum] = None
        self.spectrum_dirty = False
        self.axis_units = ''
        self._axis_known = False  # a spectrum came since the unit was attached: its units were announced
        self.temperature: Optional[Reading] = None
        self.origin = time.time()  # the plots show the time from there
        self.series_rois = TimeSeries(0)
        self.series_temperature = TimeSeries(2)
        self.program: Optional[RampProgram] = None
        self.runner: Optional[RampRunner] = None
        self.ramp_state: Optional[RampState] = None
        self.last_sent: Optional[float] = None
        self.failed = False  # the spectrometer is not read out any more
        self.clock = time.perf_counter  # the clock of the ramp (replaced in the tests)
        self.n_measurements = 0
        self.n_skipped = 0  # readouts which were not made because the previous one was not over
        self.n_lost = 0  # readouts whose spectrum never came back
        self._pending: Optional[tuple[float, float]] = None  # (epoch, monotonic) of the readout waiting for its spectrum
        self._started_at = 0.0
        self._run_started = 0.0  # time.monotonic() when the experiment began
        self._last_phase: Optional[Phase] = None
        self._connections: list[tuple] = []  # (module, signal, slot)
        self._warnings: set[str] = set()
        self._silent: set[str] = set()
        self._reported: set[Problem] = set()

        self._timer = QtCore.QTimer(self)
        self._timer.setTimerType(QtCore.Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._readout)

    def __repr__(self):
        return f'<Unit {self.number} {self.state.value}>'

    # ----------------------------------------------------------------------------------------------------------
    # modules
    # ----------------------------------------------------------------------------------------------------------
    def attach(self):
        """Look for the modules selected in the settings (the unit must not be in use)"""
        name = self.settings.spectrometer
        self.detector = None if name == NONE else self._manager.get_mod_from_name(name, mod=ModuleType.Detector)
        name = self.settings.thermostat
        self.thermostat = None if name == NONE else self._manager.get_mod_from_name(name, mod=ModuleType.Actuator)
        self.spectrum = None
        self.spectrum_dirty = True
        self.axis_units = ''
        self._axis_known = False
        self.temperature = None
        self.settings.set_units(self.units)
        self.reset_series()

    @property
    def label(self) -> str:
        """What the messages call the unit"""
        return f'Spectrometer {self.number}'

    @property
    def units(self) -> str:
        """The temperature units"""
        return self.thermostat.units if self.thermostat is not None else ''

    @property
    def title(self) -> str:
        """The name of the spectrometer"""
        return self.detector.title if self.detector is not None else NONE

    @property
    def has_program(self) -> bool:
        """The unit drives its thermostat: the user asked for a ramp and there is a thermostat"""
        return self.settings.run_ramp and self.thermostat is not None

    @property
    def program_over(self) -> bool:
        """There is no ramp to wait for: it is finished (or it was never there)"""
        return self.runner is None or self.runner.mode in ('done', 'idle')

    def problems(self) -> list[str]:
        """What prevents the unit from taking part in the live view or an experiment"""
        settings = self.settings
        label = self.label
        problems = []
        if settings.spectrometer == NONE:
            problems.append(f'{label}: no {self.WHAT} is selected')
        elif self.detector is None:
            problems.append(f'{label}: the detector {settings.spectrometer} does not exist any more')
        elif not self.detector.initialized_state:
            problems.append(f'{label}: the detector {self.detector.title} is not initialized')
        if settings.thermostat != NONE:
            if self.thermostat is None:
                problems.append(f'{label}: the thermostat {settings.thermostat} does not exist any more')
            elif not self.thermostat.initialized_state:
                problems.append(f'{label}: the thermostat {self.thermostat.title} is not initialized')
        if settings.readout_time <= self.acquisition_time():
            averaged = f' x {self.n_averages()} averages' if self.n_averages() > 1 else ''
            problems.append(f'{label}: the readout time ({settings.readout_time:g} s) must be longer than the '
                            f'{self.TIME_NAME} time ({settings.integration_time:g} ms{averaged})')
        if self.has_program:
            try:
                settings.program()
            except ValueError as e:
                problems.append(f'{label}: the temperature ramp is not valid: {e}')
        return problems

    def n_averages(self) -> int:
        """How many spectra the spectrometer averages for one readout (the *Naverage* of its module)"""
        try:
            return max(1, int(self.detector.settings['main_settings', 'Naverage']))
        except Exception:
            return 1

    def acquisition_time(self) -> float:
        """How long a readout takes in the spectrometer, in s: the integration time, as many times as it averages"""
        return self.settings.integration_time / 1000. * self.n_averages()

    def apply_integration_time(self):
        """Set the integration time of the spectrometer (if its plugin has such a setting)"""
        if self.detector is None:
            return
        wanted = self.settings.integration_time
        applied = set_integration_time(self.detector, wanted)
        if applied is None:
            logger.info(f'{self.detector.title} has no integration time setting, it is left as it is')
        elif abs(applied - wanted) > 1e-9:
            logger.warning(f'The integration time of {self.detector.title} is {applied:g} ms, '
                           f'the closest to {wanted:g} ms it accepts')
        else:
            logger.info(f'The integration time of {self.detector.title} is {applied:g} ms')

    # ----------------------------------------------------------------------------------------------------------
    # plots
    # ----------------------------------------------------------------------------------------------------------
    def reset_series(self, origin: Optional[float] = None):
        """Forget what was measured (when the ROIs change, or an experiment starts)"""
        if origin is not None:
            self.origin = origin
        self.series_rois = TimeSeries(len(self.settings.roi_keys))
        self.series_temperature = TimeSeries(2)

    # ----------------------------------------------------------------------------------------------------------
    # connecting the modules
    # ----------------------------------------------------------------------------------------------------------
    def _connect(self, owner, signal, slot):
        signal.connect(slot)
        self._connections.append((owner, signal, slot))

    def _disconnect(self, owner=None):
        """Disconnect what comes from a module (or from all of them)"""
        kept = []
        for module, signal, slot in self._connections:
            if owner is None or module is owner:
                try:
                    signal.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass
            else:
                kept.append((module, signal, slot))
        self._connections = kept

    # ----------------------------------------------------------------------------------------------------------
    # live view
    # ----------------------------------------------------------------------------------------------------------
    def start_live(self, origin: float):
        """Read the spectrometer out, and ask for the temperature, once per readout time

        The problems must have been checked (:meth:`problems`).
        """
        if self.state != UnitState.IDLE:
            return
        self.state = UnitState.LIVE
        self.origin = origin
        self.failed = False
        self._pending = None
        self._warnings.clear()
        self._silent.clear()
        self._reported.clear()
        self._started_at = time.monotonic()
        self.apply_integration_time()
        if self.detector.grab_state:  # the readouts are driven from here
            self.notice.emit(f'{self.detector.title} was grabbing: it is read out once per readout time instead')
            self.detector.stop_grab()
        self._connect(self.detector, self.detector.grab_done_signal, self._on_spectrum)
        self._connect(self.detector, self.detector.init_signal,
                      lambda ok, m=self.detector: self._on_module_init(m, ok))
        if self.thermostat is not None:
            # (not move_done_signal: some plugins send the target back as if it were measured)
            self._connect(self.thermostat, self.thermostat.current_value_signal, self._on_temperature)
            self._connect(self.thermostat, self.thermostat.init_signal,
                          lambda ok, m=self.thermostat: self._on_module_init(m, ok))
        self.reset_series()
        self.update_timing()
        self._readout()

    def update_timing(self):
        """The readout time was changed"""
        if self.state != UnitState.IDLE and not self.failed:
            self._timer.start(int(round(self.settings.readout_time * 1000)))

    def stop_live(self):
        if self.state == UnitState.IDLE:
            return
        if self.state == UnitState.RUNNING:
            raise RuntimeError('An experiment is going on, stop it first')
        self._release()

    def _release(self):
        self._timer.stop()
        self._disconnect()
        self._pending = None
        self.state = UnitState.IDLE

    def halt_readouts(self):
        """Stop reading the spectrometer out (it does not answer), the thermostat is left to the ramp"""
        self.failed = True
        self._timer.stop()
        self._pending = None

    # ----------------------------------------------------------------------------------------------------------
    # one readout
    # ----------------------------------------------------------------------------------------------------------
    def pending_timeout(self) -> float:
        """The time (s) after which a readout whose spectrum did not come back is given up"""
        return max(5.0, 5. * (self.settings.readout_time + self.acquisition_time()))

    @logged
    def _readout(self):
        if self.state == UnitState.IDLE or self.failed or self.detector is None:
            return
        if self._pending is not None:
            age = time.monotonic() - self._pending[1]
            if age <= self.pending_timeout():
                # the previous spectrum is not here yet: the integration or the transfer takes longer than the
                # readout. Never two readouts at once: the detector may not cope with it
                self.n_skipped += 1
                if self.n_skipped in (1, 10, 100, 1000):
                    logger.warning(f'{self.detector.title}: the readout is late ({self.n_skipped} skipped)')
                return
            self.n_lost += 1
            logger.warning(f'{self.detector.title}: no {self.DATA} came back from the readout started {age:.1f} s ago, '
                           'reading out again')
            self._pending = None
        self._pending = (time.time(), time.monotonic())
        if self.thermostat is not None and self.thermostat.initialized_state:
            self.thermostat.get_actuator_value()
        self.detector.snap()

    @logged
    def _on_spectrum(self, dte: DataToExport):
        if not self._store_data(dte):
            return
        pending, self._pending = self._pending, None
        if pending is None or self.state == UnitState.IDLE:
            return  # a spectrum nobody asked for (the module was grabbing on its own): shown, not measured
        if self.state == UnitState.RUNNING and pending[1] < self._run_started:
            return  # asked for before the experiment began: shown, but it is not part of it
        self._measure(pending[0])

    def _store_data(self, dte: DataToExport) -> bool:
        """Keep what the detector delivered (False if there is nothing usable in it)"""
        previous = self.spectrum
        found = extract_spectrum(dte, previous.axis if previous is not None else None)
        if found is None:
            return False
        dwa, y, axis = found
        if previous is not None and axis is previous.axis:  # the same axis: no need to convert it again
            x, units = previous.x, previous.units
        else:
            x, units = wavelength_axis(axis.get_data(), axis.units or '')
            if units == 'nm' and x.size and np.nanmax(np.abs(x)) > 1e5 and 'axis' not in self._warnings:
                # e.g. the Oceaninsight plugin before it gave its wavelengths in nm (they were 1e9 times too big)
                self._warnings.add('axis')
                message = (f'The wavelengths of {self.title} go up to {np.nanmax(x):.3g} nm: its axis looks wrong, '
                           'is the plugin of the spectrometer up to date?')
                logger.warning(message)
                self.notice.emit(message)
        self.spectrum = Spectrum(axis, y, time.monotonic(), dwa, x, units)
        self.spectrum_dirty = True
        if units != self.axis_units or not self._axis_known:
            self.axis_units, self._axis_known = units, True
            self.axis_units_changed.emit(units)
        return True

    @logged
    def _on_temperature(self, value):
        try:
            number = float(value.value())
        except Exception:
            logger.exception('Unreadable temperature')
            return
        if not math.isfinite(number):  # a thermostat which does not know its temperature: as if nothing came
            logger.warning(f'The thermostat gave a temperature of {number}')
            return
        self.temperature = Reading(number, time.monotonic())

    def _roi_values(self, rois: list[Roi]) -> list[float]:
        values = []
        spectrum = self.spectrum
        for roi in rois:
            try:
                values.append(roi.value(spectrum.x, spectrum.y))
            except ValueError as e:
                if roi.name not in self._warnings:  # once per ROI, not at every readout
                    self._warnings.add(roi.name)
                    logger.warning(f'{roi.name}: {e}')
                values.append(math.nan)
        return values

    def current_temperature(self) -> float:
        """The last temperature of the thermostat, nan if there is none or if it is too old: a thermostat which
        stopped answering must not look as if it went on giving the same value"""
        reading = self.temperature
        if reading is None or time.monotonic() - reading.time > max(5.0, 5. * self.settings.readout_time):
            return math.nan
        return reading.value

    def _measure(self, started: float):
        rois = self.settings.rois
        values = self._roi_values(rois)
        temperature = self.current_temperature()
        state = self.ramp_state if self.state == UnitState.RUNNING else None
        measurement = Measurement(self.number, self.n_measurements, started, self.spectrum, rois, values, temperature,
                                  state.setpoint if state is not None else math.nan,
                                  state.phase if state is not None else None, state.cycle if state is not None else 0)
        self.n_measurements += 1
        if len(values) == self.series_rois.n_columns:
            self.series_rois.append(started - self.origin, values)
            self.series_temperature.append(started - self.origin, [temperature, measurement.setpoint])
        self.settings.child('status', 'measurements').setValue(self.n_measurements)
        if not math.isnan(temperature):
            self.settings.child('status', 'temperature').setValue(temperature)
        self.measurement.emit(measurement)

    @logged
    def _on_module_init(self, module, initialized: bool):
        """A module of the unit was closed (or initialized)"""
        if initialized or self.state == UnitState.IDLE:
            return
        if module is self.thermostat:
            self.fail(Problem.THERMOSTAT_LOST, f'The thermostat {module.title} was closed')
        else:
            self.fail(Problem.SPECTROMETER, f'The {self.WHAT} {module.title} was closed')

    # ----------------------------------------------------------------------------------------------------------
    # the experiment
    # ----------------------------------------------------------------------------------------------------------
    @property
    def ready(self) -> bool:
        """The spectrometer and the thermostat gave something: an experiment can start"""
        if self.spectrum is None:
            return False
        if self.thermostat is not None:
            return self.temperature is not None and time.monotonic() - self.temperature.time < 5.
        return True

    def missing(self) -> list[str]:
        """The modules which have not delivered anything usable yet"""
        missing = []
        if self.spectrum is None:
            missing.append(self.title)
        if self.thermostat is not None and (self.temperature is None
                                            or time.monotonic() - self.temperature.time >= 5.):
            missing.append(self.thermostat.title)
        return missing

    def begin_run(self, origin: float, now: float):
        """Start the experiment: record, and run the ramp if there is one

        Parameters
        ----------
        origin: float
            The time (epoch seconds) the plots start from
        now: float
            The clock of the ramp (time.perf_counter())
        """
        if self.state != UnitState.LIVE:
            raise RuntimeError('The unit must be read out (live) before an experiment starts')
        self.reset_series(origin)
        self.n_measurements = 0
        self.runner = None
        self.ramp_state = None
        self.last_sent = None
        self._last_phase = None
        self.program = None
        self._reported.clear()
        # (a readout in progress is not dropped: there is never more than one: its spectrum is displayed, but it is
        # not part of the experiment, see _on_spectrum)
        self._started_at = self._run_started = time.monotonic()
        if self.has_program:
            self.program = self.settings.program()
            self.runner = RampRunner(self.program)
            self.ramp_state = self.runner.begin(now, self.temperature.value)
        self.state = UnitState.RUNNING
        self.program_tick(now)

    def end_run(self):
        """The experiment is over: back to the live view"""
        if self.state == UnitState.RUNNING:
            self.state = UnitState.LIVE
        self.runner = None
        self.ramp_state = None
        self.settings.child('status', 'phase').setValue(Phase.IDLE.value)

    def send_setpoint(self, value: float):
        thermostat = self.thermostat
        data = DataActuator(thermostat.title, data=float(value), units=thermostat.units)
        thermostat.command_hardware.emit(ThreadCommand(ControlToHardwareMove.MOVE_ABS, [data, False]))
        self.last_sent = float(value)

    @logged
    def program_tick(self, now: float) -> Optional[RampState]:
        """Follow the ramp: send the thermostat a new set-point if it changed

        Returns
        -------
        RampState or None
            None when there is no ramp. When its ``finished`` is True the set-point was sent: it is the initial value
        """
        if self.runner is None:
            return None
        state = self.runner.state(now)
        self.ramp_state = state
        if state.phase != self._last_phase:
            self._last_phase = state.phase
            logger.info(f'{self.label}: cycle {state.cycle}, {state.phase.value}')
            self.notice.emit(f'{self.label}, cycle {state.cycle}: {state.phase.value}' if state.cycle
                             else f'{self.label}: {state.phase.value}')
        if not state.paused and self.thermostat is not None and (
                self.last_sent is None or abs(state.setpoint - self.last_sent) > 1e-9):
            self.send_setpoint(state.setpoint)
        self._show_state(state)
        return state

    def _show_state(self, state: RampState):
        status = self.settings.child('status')
        status.child('phase').setValue(state.phase.value + (' (paused)' if state.paused else ''))
        status.child('cycle').setValue(state.cycle)
        status.child('setpoint').setValue(float(state.setpoint))

    def pause(self, now: float, paused: bool):
        """Freeze the set-point (the readouts go on), or carry on"""
        if self.runner is None:
            return
        self.runner.pause(now) if paused else self.runner.resume(now)
        self.program_tick(now)

    @property
    def returning(self) -> bool:
        """The ramp is on its way back to the initial value"""
        return self.runner is not None and self.runner.mode == 'return'

    def stop_program(self, now: float, reason: str = 'Stopped by the user') -> Optional[RampState]:
        """Bring the thermostat back to the initial value, from where it is, at the speed of the ramp

        Does nothing if there is no ramp, or if it is already on its way back (see :meth:`abort_program`)
        """
        if self.runner is None or self.runner.mode != 'cycling':
            return self.ramp_state
        self.ramp_state = self.runner.stop(now)
        logger.info(f'{self.label}: {reason}, returning to the initial temperature')
        self.notice.emit(f'{self.label}: {reason}: returning to the initial temperature')
        return self.program_tick(now)

    def abort_program(self, now: float, jump: bool = False):
        """End the ramp right there (the user said so), or set the initial value at once (``jump``)"""
        if self.runner is None:
            return
        if jump and self.thermostat is not None and self.program is not None:
            try:
                self.send_setpoint(self.program.initial)
            except Exception:
                logger.exception('The initial temperature could not be sent')
        self.ramp_state = self.runner.abort()  # nothing follows: the program is over wherever it was

    # ----------------------------------------------------------------------------------------------------------
    # when something goes wrong
    # ----------------------------------------------------------------------------------------------------------
    def fail(self, kind: Problem, message: str):
        """A module does not work: do what can be done for the thermostat, and say it (once per kind)"""
        kind = Problem(kind)
        if kind in self._reported:
            return
        self._reported.add(kind)
        logger.error(f'{self.label}: {message}')
        if kind == Problem.THERMOSTAT_LOST:
            self._disconnect(self.thermostat)
            if self.runner is not None:
                self.ramp_state = self.runner.abort()
            self.thermostat = None
            self.temperature = None
        else:
            if kind == Problem.SPECTROMETER:
                self.halt_readouts()
            if self.runner is not None and self.runner.mode == 'cycling':
                self.stop_program(self.clock(), reason=message)  # first the safety action, whatever happens to the message
        self.problem.emit(self, kind, message)

    def silent_for(self, now: float, limit: float) -> Optional[tuple[Problem, str]]:
        """A module of a running experiment which delivered nothing for `limit` seconds: (kind, message), else None"""
        if limit <= 0 or self.state != UnitState.RUNNING or self.failed:
            return None
        if self.runner is not None and self.runner.mode != 'cycling':
            return None  # on its way back, or over: the thermostat is already being sent to the initial value
        if self.thermostat is not None and Problem.THERMOSTAT_SILENT not in self._reported:
            last = self.temperature.time if self.temperature is not None else self._started_at
            if now - last > limit:
                return (Problem.THERMOSTAT_SILENT,
                        f'No temperature from {self.thermostat.title} for {limit:g} s: the set-point is brought '
                        'back to the initial value, but the thermostat does not answer: nothing says that it gets it')
        if Problem.SPECTROMETER not in self._reported and self.detector is not None:
            last = self.spectrum.time if self.spectrum is not None else self._started_at
            if now - last > limit:
                return Problem.SPECTROMETER, f'No {self.DATA} from {self.detector.title} for {limit:g} s'
        return None

    def live_notices(self, now: float) -> list[str]:
        """Tell once, after a few seconds, that a module of the live view delivers nothing usable"""
        if self.state != UnitState.LIVE or now - self._started_at < self.SILENCE_NOTICE:
            return []
        messages = []
        if self.spectrum is None and 'spectrum' not in self._silent:
            self._silent.add('spectrum')
            messages.append(f'{self.title} has delivered no {self.DIMENSION} data ({'an' if self.DATA[0] in 'aeiou' else 'a'} {self.DATA})')
        if self.thermostat is not None and self.temperature is None and 'temperature' not in self._silent:
            self._silent.add('temperature')
            messages.append(f'{self.thermostat.title} has not given its temperature')
        for message in messages:
            logger.warning(message)
        return messages

    # ----------------------------------------------------------------------------------------------------------
    # what to tell
    # ----------------------------------------------------------------------------------------------------------
    def status_text(self) -> str:
        """A line for the status bar"""
        text = self.label
        state = self.ramp_state
        if state is not None and self.program is not None:
            total = self.program.n_cycles or '∞'
            text += f': cycle {state.cycle}/{total} {state.phase.value}'
            text += ' (paused)' if state.paused else f' ({format_duration(state.remaining)} left)'
            text += f', set-point {state.setpoint:.4g} {self.units}'
        if self.temperature is not None:
            text += f', measured {self.temperature.value:.4g} {self.units}'
        return text.strip()


class CameraUnit(Unit):
    """A camera and its thermostat: the unit of a spectrometer, with images instead of spectra

    Each readout takes one image; the value of each ROI is the mean of the pixels inside its rectangle. The ramp, the
    thermostat, the watchdogs and the recording work as in :class:`Unit`: the last image is :attr:`spectrum` (a
    :class:`~.camera.Frame`), what the two kinds of unit have in common keeps the same names.
    """
    WHAT = 'camera'
    DATA = 'image'
    DIMENSION = '2D'
    TIME_NAME = 'exposure'

    @property
    def label(self) -> str:
        return 'Camera'

    def _store_data(self, dte: DataToExport) -> bool:
        found = extract_frame(dte)
        if found is None:
            return False
        dwa, image = found
        self.spectrum = Frame(image, time.monotonic(), dwa)
        self.spectrum_dirty = True
        if not self._axis_known:
            self.axis_units, self._axis_known = PIXELS, True
            self.axis_units_changed.emit(PIXELS)
        return True

    def _roi_values(self, rois) -> list[float]:
        values = []
        image = self.spectrum.y
        for roi in rois:
            value = roi.value(image)
            if math.isnan(value) and roi.name not in self._warnings:  # once per ROI, not at every readout
                self._warnings.add(roi.name)
                logger.warning(f'{roi.name}: the window is outside of the image ({image.shape[1]} x '
                               f'{image.shape[0]} pixels)')
            values.append(value)
        return values
