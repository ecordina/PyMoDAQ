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
from pymodaq_plugins_thermospectro.ramp import (END_AT_FINAL, Phase, RampProgram, RampRunner, RampState,
                                                format_duration)
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
    full_due: bool
        The whole spectrum (image) is to be saved with this measurement (it is saved only every so often in a long log)
    crop_due: bool
        The pixels of the ROIs of a camera which are a crop are to be saved with this measurement (they may be saved
        only every so often too)
    integration_time: float
        The integration (exposure) time the instrument was set to when this measurement was started, in ms: it can be
        changed during an experiment (nan if it is not known)
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
    full_due: bool = True
    crop_due: bool = True
    integration_time: float = math.nan


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
    #: A readout whose data did not come back is given up after at least this time (s), see :meth:`pending_timeout`
    PENDING_MIN = 10.0
    #: The time (s) to wait before trying again after the 1st, 2nd... initialization which did not work (then the last)
    REINIT_WAITS = (30., 60., 120., 300.)
    #: The time (s) an initialization may take
    REINIT_TIMEOUT = 60.
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
        self.hold_target: Optional[float] = None  # the temperature the thermal controller is held at (a run)
        #: What the unit does when its ramps are over: 'initial' or 'final' (the controller stays at that value and the
        #: log goes on) or None (the log ends). A stop, or a failure, makes it None: the log ends once the ramps are back
        self.after_program: Optional[str] = None
        self.settled = False  # the ramps are over, the log goes on with the controller held where they ended
        self._last_full = -math.inf  # when the whole spectrum (image) was last saved (time.monotonic())
        self._last_crop = -math.inf  # when the crops of the ROIs of a camera were last saved
        self._requested_integration: Optional[float] = None  # the integration time last asked of the detector (ms)
        self._applied_integration: Optional[float] = None  # the one it was set to (it accepts the closest it can)
        self.last_sent: Optional[float] = None
        self.failed = False  # the spectrometer is not read out any more
        self.clock = time.perf_counter  # the clock of the ramp (replaced in the tests)
        self.n_measurements = 0
        self.n_skipped = 0  # readouts which were not made because the previous one was not over
        self.n_lost = 0  # readouts whose spectrum never came back
        self.n_lost_in_a_row = 0  # ... without any spectrum coming since the last of them
        self.n_reinits = 0  # how many times the detector was closed and initialized again
        self._reiniting = False  # the detector is being initialized again: nothing is read out
        self._reinit_inited = False  # its initialization said it was done
        self._reinit_failures = 0  # the initializations which did not work, since a spectrum came
        self._down_since: Optional[float] = None  # when the readouts were stopped to initialize the detector again
        self._reinit_deadline = 0.0
        self._reinit_poll_timer = QtCore.QTimer(self)
        self._reinit_poll_timer.setInterval(500)
        self._reinit_poll_timer.timeout.connect(self._reinit_poll)
        self._reinit_retry_timer = QtCore.QTimer(self)
        self._reinit_retry_timer.setSingleShot(True)
        self._reinit_retry_timer.timeout.connect(self._start_reinit)
        # (single shots) a readout which did not come back in time / a readout asked for again once the integration time
        # has passed (after a missed one, or a camera initialized again) / the first readout of an experiment
        self._miss_timer = QtCore.QTimer(self)
        self._miss_timer.setSingleShot(True)
        self._miss_timer.timeout.connect(self._on_miss)
        self._resume_timer = QtCore.QTimer(self)
        self._resume_timer.setSingleShot(True)
        self._resume_timer.timeout.connect(self._resume_readout)
        self._first_timer = QtCore.QTimer(self)
        self._first_timer.setSingleShot(True)
        self._first_timer.timeout.connect(self._first_readout)
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
        """The unit runs a temperature ramp: the user asked for it and there is a thermostat"""
        return self.settings.run_ramp and self.thermostat is not None and not self.settings.holds

    @property
    def drives(self) -> bool:
        """The unit drives its thermostat (a ramp, or a held temperature)"""
        return self.has_program or self.has_hold

    @property
    def has_hold(self) -> bool:
        """The unit holds its thermostat at one temperature: the user asked for it and there is a thermostat"""
        return self.settings.run_ramp and self.thermostat is not None and self.settings.holds

    @property
    def program_over(self) -> bool:
        """There is no ramp to wait for: it is finished (or it was never there)"""
        return self.runner is None or self.runner.mode in ('done', 'idle')

    @property
    def ramping(self) -> bool:
        """The thermal controller follows a ramp (or is on its way to where it was told to stay): not held, not settled"""
        return self.runner is not None and not self.settled

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
        if self.has_hold and not math.isfinite(settings.hold_temperature):
            problems.append(f'{label}: the temperature to hold is not a number')
        return problems

    def thermostat_range(self) -> Optional[tuple[float, float]]:
        """The lowest and the highest temperature the thermostat accepts, None if it is not known

        It is the *Bounds* of the actuator module when they are set, or when they are not the ones of an actuator which
        was never given any (0 to 1): a plugin which knows the range of its controller writes it there (QNW_TC does).
        """
        try:
            group = self.thermostat.settings.child(getattr(self.thermostat, '_hw_settings_name', 'move_settings'),
                                                   'bounds')
            low, high = float(group['min_bound']), float(group['max_bound'])
            if group['is_bounds'] or (low, high) != (0., 1.):
                return low, high
        except Exception:  # (a module without bounds: the controller will say)
            pass
        return None

    def run_problems(self) -> list[str]:
        """What prevents the unit from starting an experiment, besides what prevents it from being read out: a
        temperature which the thermostat refuses (it would be refused again at every set-point, and the log would carry
        on as if all was well)"""
        if not self.drives or self.thermostat is None:
            return []
        limits = self.thermostat_range()
        if limits is None:
            return []
        low, high = limits
        wanted = {}
        if self.has_hold:
            wanted['the temperature to hold'] = self.settings.hold_temperature
        if self.has_program:
            try:
                program = self.settings.program()
            except ValueError:
                return []  # (problems() says it)
            wanted['the initial temperature of the ramp'] = program.initial
            wanted['the final temperature of the ramp'] = program.final
        return [f'{self.label}: {what} ({value:g} {self.units}) is out of the range of {self.thermostat.title}: '
                f'{low:g} to {high:g} {self.units}' for what, value in wanted.items()
                if math.isfinite(value) and not low <= value <= high]

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
        self._requested_integration = wanted
        self._applied_integration = wanted if applied is None else applied  # (no such setting: the nominal one)
        if applied is None:
            logger.info(f'{self.detector.title} has no integration time setting, it is left as it is')
        elif abs(applied - wanted) > 1e-9:
            logger.warning(f'The integration time of {self.detector.title} is {applied:g} ms, '
                           f'the closest to {wanted:g} ms it accepts')
        else:
            logger.info(f'The integration time of {self.detector.title} is {applied:g} ms')

    def integration_time_now(self) -> float:
        """The integration time the instrument is set to (ms): what each measurement is made with, and what is saved"""
        return self._applied_integration if self._applied_integration is not None else self.settings.integration_time

    def integration_changed(self):
        """The integration time was changed in the settings

        In the live view it is set on the instrument at once. During an experiment it is set before the next readout
        (:meth:`_sync_integration`), so that the time saved with a measurement is the one it was made with.
        """
        if self.state == UnitState.LIVE:
            self.apply_integration_time()

    def _sync_integration(self):
        """Before a readout: set on the instrument the integration time the settings ask for, if it changed

        One which is not shorter than the readout time (as many times as the module averages) cannot be followed: it
        is refused, told, and the setting goes back to what the instrument is set to.
        """
        wanted = self.settings.integration_time
        if self._requested_integration is not None and abs(wanted - self._requested_integration) <= 1e-9:
            return
        if self.state == UnitState.RUNNING and self._requested_integration is not None \
                and wanted / 1000. * self.n_averages() >= self.settings.readout_time:
            kept = self._requested_integration
            averages = f' x {self.n_averages()} averages' if self.n_averages() > 1 else ''
            self.notice.emit(f'{self.label}: {wanted:g} ms is not shorter than the readout time '
                             f'({self.settings.readout_time:g} s{averages}): the {self.TIME_NAME} time stays {kept:g} ms')
            with self.settings.busy():
                self.settings.child('integration_time').setValue(kept)
            return
        self.apply_integration_time()

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
        self.reset_counters()
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

    def _stop_timers(self):
        for timer in (self._timer, self._miss_timer, self._resume_timer, self._first_timer):
            timer.stop()

    def _release(self):
        self._stop_timers()
        self._cancel_reinit()
        self._disconnect()
        self._pending = None
        self.state = UnitState.IDLE

    def halt_readouts(self):
        """Stop reading the spectrometer out (it does not answer), the thermostat is left to the ramp"""
        self.failed = True
        self._stop_timers()
        self._pending = None

    # ----------------------------------------------------------------------------------------------------------
    # one readout
    # ----------------------------------------------------------------------------------------------------------
    def pending_timeout(self) -> float:
        """The time (s) after which a readout whose data did not come back is given up (it is then asked again one
        integration time later, and the detector is initialized again after the number of readouts in a row set in the
        settings, see :meth:`_on_miss`)"""
        return max(self.PENDING_MIN, 2. * self.acquisition_time() + self.PENDING_MIN)

    @logged
    def _readout(self):
        if self.state == UnitState.IDLE or self.failed or self.detector is None or self._reiniting:
            return
        if self._pending is not None:
            age = time.monotonic() - self._pending[1]
            if age > self.pending_timeout():  # (the miss timer is a few ms late)
                self._on_miss()
                return
            if age <= self.pending_timeout():
                # the previous spectrum is not here yet: the integration or the transfer takes longer than the
                # readout. Never two readouts at once: the detector may not cope with it
                self.n_skipped += 1
                self._show_counters()
                if self.n_skipped in (1, 10, 100, 1000):
                    logger.warning(f'{self.detector.title}: the readout is late ({self.n_skipped} skipped)')
                return
        self._sync_integration()
        self._pending = (time.time(), time.monotonic(), self.integration_time_now())
        if self.thermostat is not None and self.thermostat.initialized_state:
            self.thermostat.get_actuator_value()
        self.detector.snap()
        self._miss_timer.start(int(self.pending_timeout() * 1000))

    def _integration_ms(self) -> int:
        """The integration time (ms): what is waited for before a readout is asked again"""
        return max(1, int(round(self.settings.integration_time)))

    @logged
    def _on_miss(self):
        """A readout did not come back in time

        The next one is asked for once the integration time has passed, not at the next readout time (the readouts are
        then closer to each other, or farther, than the readout time: data are wanted more than a regular rhythm). When
        as many readouts as the settings say (*Restart after missed*, 2 by default, 0: never) are missed in a row, the
        detector is closed and initialized again, and the next readout is asked for once the integration time has passed
        again. After a readout which came back the next one is a readout time later.
        """
        pending = self._pending
        if self.state == UnitState.IDLE or self.failed or self._reiniting or pending is None or self.detector is None:
            return
        age = time.monotonic() - pending[1]
        self._pending = None
        self.n_lost += 1
        self.n_lost_in_a_row += 1
        self._show_counters()
        logger.warning(f'{self.detector.title}: no {self.DATA} came back from the readout started {age:.1f} s ago '
                       f'({self.n_missed} readouts missed)')
        self._timer.stop()
        restart_after = self.settings.restart_after
        if restart_after > 0 and self.n_lost_in_a_row >= restart_after:
            self._start_reinit()
        else:
            self._resume_timer.start(self._integration_ms())

    @logged
    def _resume_readout(self):
        """Read out now (an integration time after a missed readout, or after the camera was initialized again, or at
        the beginning of an experiment): the next readout is a readout time later"""
        if self.state == UnitState.IDLE or self.failed or self._reiniting or self.detector is None:
            return
        self.update_timing()
        self._readout()

    @logged
    def _first_readout(self):
        """The first readout of an experiment, an integration time after it began (not at the next readout time)"""
        if self.state != UnitState.RUNNING or self.failed or self._reiniting or self.detector is None:
            return
        pending = self._pending
        if pending is not None and pending[1] < self._run_started \
                and time.monotonic() - pending[1] <= self.pending_timeout():
            self._first_timer.start(50)  # (one asked for before it began is on its way: it is not part of it, wait)
            return
        self._resume_readout()

    # ----------------------------------------------------------------------------------------------------------
    # what was missed, and the detector which is initialized again
    # ----------------------------------------------------------------------------------------------------------
    @property
    def n_missed(self) -> int:
        """The readouts which gave nothing since the unit started (or the experiment began): those which were not
        made because the previous one was still waiting for its data, and those which were given up"""
        return self.n_skipped + self.n_lost

    def reset_counters(self):
        """Count from zero (the live view, or the experiment, begins)"""
        self.n_skipped = self.n_lost = self.n_lost_in_a_row = self.n_reinits = self._reinit_failures = 0
        if self._down_since is not None:  # (the detector is being initialized again: that is counted from now on)
            self._down_since = time.monotonic()
        self._show_counters()

    def _show_counters(self):
        status = self.settings.child('status')
        for name, value in (('missed', self.n_missed), ('restarts', self.n_reinits)):
            leaf = status.names.get(name)  # (a tree saved by an older version has no such leaf)
            if leaf is not None and leaf.value() != value:
                leaf.setValue(value)

    def _cancel_reinit(self):
        self._reinit_poll_timer.stop()
        self._reinit_retry_timer.stop()
        self._reiniting = False
        self._reinit_inited = False
        self._down_since = None

    def _count_downtime(self):
        """The readouts which were not made while the detector was being initialized again are missed ones"""
        if self._down_since is not None:
            self.n_skipped += int((time.monotonic() - self._down_since) / max(self.settings.readout_time, 1e-3))
            self._down_since = None
            self._show_counters()

    def _start_reinit(self):
        """Close the detector and initialize it again (a camera which stopped sending images: the plugin only gives up
        on the readout, the camera stays as it is). The readouts start again once it is initialized, or after one of the
        next attempts if it cannot be."""
        detector = self.detector
        if detector is None or self.state == UnitState.IDLE:
            self._cancel_reinit()
            return
        self._reiniting = True
        self._reinit_inited = False
        self.n_reinits += 1
        if self._down_since is None:
            self._down_since = time.monotonic()
        self._stop_timers()
        self._pending = None
        self._show_counters()
        message = (f'{detector.title}: {self.n_lost_in_a_row} readouts lost in a row, it is closed and initialized '
                   f'again (restart {self.n_reinits})')
        logger.warning(message)
        self.notice.emit(message)
        try:
            if detector.grab_state:
                detector.stop_grab()
            self._init_detector(False)  # (waits for the plugin to be closed)
            self._init_detector(True)
        except Exception:
            logger.exception(f'{detector.title} could not be initialized again')
        self._reinit_deadline = time.monotonic() + self.REINIT_TIMEOUT
        self._reinit_poll_timer.start()

    def _init_detector(self, do_init: bool):
        """Press the init button of the detector (without a user interface the button does nothing: ask directly)"""
        detector = self.detector
        if getattr(detector, 'ui', True) is None and hasattr(detector, 'init_hardware'):
            detector.init_hardware(do_init)
        else:
            detector.init_hardware_ui(do_init)

    def _reinit_poll(self):
        """Is the detector initialized again?"""
        detector = self.detector
        if detector is None or self.state == UnitState.IDLE:
            self._cancel_reinit()
            return
        if self._reinit_inited and detector.initialized_state:
            self._reinit_poll_timer.stop()
            self._reiniting = False
            self._count_downtime()
            logger.info(f'{detector.title} is initialized again ({self.n_missed} readouts missed so far)')
            self._requested_integration = None  # (the new plugin is told the integration time again)
            self.apply_integration_time()
            if not self.failed:
                self._resume_timer.start(self._integration_ms())  # (the first image after the restart: not at once)
            return
        if time.monotonic() < self._reinit_deadline:
            return
        self._reinit_poll_timer.stop()
        self._reinit_failures += 1
        wait = self.REINIT_WAITS[min(self._reinit_failures, len(self.REINIT_WAITS)) - 1]
        message = (f'{detector.title} could not be initialized again within {self.REINIT_TIMEOUT:g} s '
                   f'(attempt {self._reinit_failures}), trying again in {wait:g} s')
        logger.error(message)
        self.notice.emit(message)
        self._reinit_retry_timer.start(int(wait * 1000))

    @logged
    def _on_spectrum(self, dte: DataToExport):
        if not self._store_data(dte):
            return
        if self.detector is not None:
            self.recovered(Problem.SPECTROMETER, f'{self.detector.title} delivers data again')
            if self.n_lost_in_a_row:
                logger.info(f'{self.detector.title} delivers {self.DATA}s again after {self.n_lost_in_a_row} readouts '
                            f'lost in a row ({self.n_missed} missed, {self.n_reinits} restarts)')
        self.n_lost_in_a_row = 0
        self._reinit_failures = 0
        pending, self._pending = self._pending, None
        if pending is not None:
            self._miss_timer.stop()
        if pending is None or self.state == UnitState.IDLE:
            return  # a spectrum nobody asked for (the module was grabbing on its own): shown, not measured
        if self.state == UnitState.RUNNING and pending[1] < self._run_started:
            return  # asked for before the experiment began: shown, but it is not part of it
        self._measure(pending[0], pending[2] if len(pending) > 2 else None)

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
        if self.thermostat is not None:
            self.recovered(Problem.THERMOSTAT_SILENT, f'{self.thermostat.title} gives its temperature again')

    def _roi_values(self, rois: list[Roi]) -> list[float]:
        values = []
        spectrum = self.spectrum
        for roi in rois:
            if roi.is_crop:  # (no value: the part of the spectrum is saved)
                values.append(math.nan)
                continue
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

    def _per_integration(self, values: list[float], integration: float) -> list[float]:
        """The values divided by the integration time, in ms (nan if it is not a time)"""
        if not (math.isfinite(integration) and integration > 0):
            if 'integration' not in self._warnings:  # once, not at every readout
                self._warnings.add('integration')
                logger.warning(f'{self.label}: the values cannot be divided by an integration time of {integration:g} ms')
            return [math.nan] * len(values)
        return [value / integration for value in values]

    @property
    def value_units(self) -> str:
        """The units of the values of the ROIs (see :attr:`UnitSettings.value_units`)"""
        return self.settings.value_units

    def _measure(self, started: float, integration: Optional[float] = None):
        started_monotonic = time.monotonic()
        integration = self.integration_time_now() if integration is None else integration
        rois = self.settings.rois
        values = self._roi_values(rois)
        if self.settings.per_integration:
            values = self._per_integration(values, integration)
        temperature = self.current_temperature()
        state = self.ramp_state if self.state == UnitState.RUNNING else None
        setpoint, phase, cycle = (state.setpoint, state.phase, state.cycle) if state is not None \
            else (math.nan, None, 0)
        if self.settled and self.state == UnitState.RUNNING and self.hold_target is not None:
            setpoint, phase = self.hold_target, Phase.HOLD  # (the ramps are over: held where they ended, cycle: the last)
        elif state is None and self.state == UnitState.RUNNING and self.hold_target is not None:
            setpoint, phase = self.hold_target, Phase.HOLD  # (held at one temperature)
        interval = self.settings.full_interval
        full_due = interval <= 0 or started_monotonic - self._last_full >= interval - 1e-6
        if full_due:
            self._last_full = started_monotonic
        interval = self.settings.crop_interval
        crop_due = interval <= 0 or started_monotonic - self._last_crop >= interval - 1e-6
        if crop_due:
            self._last_crop = started_monotonic
        measurement = Measurement(self.number, self.n_measurements, started, self.spectrum, rois, values, temperature,
                                  setpoint, phase, cycle, full_due, crop_due, integration)
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
        if self._reiniting and module is self.detector:  # (closed on purpose, to be initialized again)
            if initialized:
                self._reinit_inited = True
            return
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
        self.reset_counters()  # (what was lost during the live view is not part of the experiment)
        self.runner = None
        self.ramp_state = None
        self.last_sent = None
        self._last_phase = None
        self.program = None
        self._reported.clear()
        if self.failed:  # (a module which did not answer during the live view: the experiment tries it again)
            self.failed = False
            self.update_timing()
            self._readout()
        # (a readout in progress is not dropped: there is never more than one: its spectrum is displayed, but it is
        # not part of the experiment, see _on_spectrum)
        self._started_at = self._run_started = time.monotonic()
        self._last_full = -math.inf  # (the first measurement has the whole spectrum)
        self._last_crop = -math.inf  # (and the crops)
        self.hold_target = None
        self.settled = False
        self.after_program = None
        if self.has_program:
            self.program = self.settings.program()
            self.after_program = self.settings.after_program
            self.runner = RampRunner(self.program)
            self.ramp_state = self.runner.begin(now, self.temperature.value)
        self.state = UnitState.RUNNING
        if self.has_hold:
            self.hold_target = self.settings.hold_temperature
            self.send_setpoint(self.hold_target)
            logger.info(f'{self.label}: {self.thermostat.title} is held at {self.hold_target:g} {self.units}')
            self.notice.emit(f'{self.label}: held at {self.hold_target:g} {self.units}')
            self.settings.child('status', 'phase').setValue(Phase.HOLD.value)
            self.settings.child('status', 'setpoint').setValue(float(self.hold_target))
        self.program_tick(now)
        self._first_timer.start(self._integration_ms())  # (the first measurement does not wait for the next readout)

    def end_run(self):
        """The experiment is over: back to the live view"""
        if self.state == UnitState.RUNNING:
            self.state = UnitState.LIVE
        self.runner = None
        self.ramp_state = None
        self.hold_target = None
        self.settled = False
        self.after_program = None
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
        if state.finished and not self.settled and self.after_program is not None and self.thermostat is not None:
            self._settle(state)
        self._show_state(state)
        return state

    def _settle(self, state: RampState):
        """The ramps are over and the log goes on: the thermal controller stays at the value they ended at

        Its set-point was sent with the last state of the program. From now on the unit is read as one held at that
        temperature (phase ``hold``, the cycle being the last one), by the watchdog as by the file.
        """
        self.settled = True
        self.hold_target = float(state.setpoint)
        where = 'final' if self.after_program == END_AT_FINAL else 'initial'
        logger.info(f'{self.label}: the ramps are over ({state.cycle} cycles), {self.thermostat.title} stays at the '
                    f'{where} value {self.hold_target:g} {self.units}, the log goes on')
        self.notice.emit(f'{self.label}: the ramps are over, held at {self.hold_target:g} {self.units}: '
                         'the log goes on until you stop it')

    def _show_state(self, state: RampState):
        status = self.settings.child('status')
        phase = Phase.HOLD if self.settled else state.phase
        status.child('phase').setValue(phase.value + (' (paused)' if state.paused else ''))
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
        self.after_program = None  # (stopped: the log ends once the thermostat is back)
        self.ramp_state = self.runner.stop(now)
        logger.info(f'{self.label}: {reason}, returning to the initial temperature')
        self.notice.emit(f'{self.label}: {reason}: returning to the initial temperature')
        return self.program_tick(now)

    def finish_program(self, now: float, at: str) -> Optional[RampState]:
        """End the ramps at their final value (the top) or their initial one (the bottom), from wherever they are

        The set-point goes there (at the speed of the ramp, or of the way back) and stays: the log goes on, with the
        controller held at that temperature, until it is stopped. Does nothing if there is no ramp going on, or if it
        is already on its way to where it ends (a stop, a failure).
        """
        if self.runner is None or self.runner.mode != 'cycling':
            return self.ramp_state
        self.after_program = at
        self.ramp_state = self.runner.finish(now, at)
        where = 'final' if at == END_AT_FINAL else 'initial'
        value = self.runner.rest
        logger.info(f'{self.label}: ending the ramps at the {where} value {value:g} {self.units}')
        self.notice.emit(f'{self.label}: ending the ramps at the {where} value ({value:g} {self.units}), '
                         'the log goes on')
        return self.program_tick(now)

    def abort_program(self, now: float, jump: bool = False):
        """End the ramp right there (the user said so), or set the initial value at once (``jump``)"""
        if self.runner is None:
            return
        self.after_program = None
        if jump and self.thermostat is not None and self.program is not None and not self.program_over:
            try:  # (a controller which stays where the ramps ended, or is held, is left there)
                self.send_setpoint(self.program.initial)
            except Exception:
                logger.exception('The initial temperature could not be sent')
        self.ramp_state = self.runner.abort()  # nothing follows: the program is over wherever it was

    # ----------------------------------------------------------------------------------------------------------
    # when something goes wrong
    # ----------------------------------------------------------------------------------------------------------
    def fail(self, kind: Problem, message: str, halt: bool = True):
        """A module does not work: do what can be done for the thermostat, and say it (once per kind)

        A spectrometer (camera) which does not answer is not read out any more, unless `halt` is False: a log which
        holds a temperature goes on asking, and takes the data again when they come back (see :meth:`recovered`).
        """
        kind = Problem(kind)
        if kind in self._reported:
            return
        self._reported.add(kind)
        logger.error(f'{self.label}: {message}')
        if kind == Problem.THERMOSTAT_LOST:
            self._disconnect(self.thermostat)
            self.after_program = None
            if self.runner is not None:
                self.ramp_state = self.runner.abort()
            self.thermostat = None
            self.temperature = None
        else:
            if kind == Problem.SPECTROMETER and halt:
                self.halt_readouts()
            if self.runner is not None and self.runner.mode == 'cycling':
                self.stop_program(self.clock(), reason=message)  # first the safety action, whatever happens to the message
        self.problem.emit(self, kind, message)

    def silent_for(self, now: float, limit: float) -> Optional[tuple[Problem, str]]:
        """A module of a running experiment which delivered nothing for `limit` seconds: (kind, message), else None"""
        if limit <= 0 or self.state != UnitState.RUNNING or self.failed:
            return None
        if self.ramping and self.runner.mode != 'cycling':
            return None  # on its way back, or over: the thermostat is already being sent to the initial value
        if self.thermostat is not None and Problem.THERMOSTAT_SILENT not in self._reported:
            last = self.temperature.time if self.temperature is not None else self._started_at
            if now - last > limit:
                if not self.ramping:  # (held at a temperature, or only read: the controller goes on by itself)
                    return (Problem.THERMOSTAT_SILENT,
                            f'No temperature from {self.thermostat.title} for {limit:g} s: the log goes on, and '
                            'the controller keeps its set-point')
                return (Problem.THERMOSTAT_SILENT,
                        f'No temperature from {self.thermostat.title} for {limit:g} s: the set-point is brought '
                        'back to the initial value, but the thermostat does not answer: nothing says that it gets it')
        if Problem.SPECTROMETER not in self._reported and self.detector is not None:
            last = self.spectrum.time if self.spectrum is not None else self._started_at
            if now - last > limit:
                return Problem.SPECTROMETER, f'No {self.DATA} from {self.detector.title} for {limit:g} s'
        return None

    def recovered(self, kind: Problem, message: str):
        """A module which was said not to answer delivers again: say so, the problem can be told again later"""
        if kind in self._reported and not (kind == Problem.SPECTROMETER and self.failed):
            self._reported.discard(kind)
            logger.info(f'{self.label}: {message}')
            self.notice.emit(f'{self.label}: {message}')

    def live_notices(self, now: float) -> list[str]:
        """Tell once, after a few seconds, that a module of the live view delivers nothing usable"""
        if self.state != UnitState.LIVE or now - self._started_at < self.SILENCE_NOTICE:
            return []
        messages = []
        if self.spectrum is None and 'spectrum' not in self._silent:
            self._silent.add('spectrum')
            article = 'an' if self.DATA[0] in 'aeiou' else 'a'
            messages.append(f'{self.title} has delivered no {self.DIMENSION} data ({article} {self.DATA})')
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
        if self.settled:
            text += f': ramps over, held at {self.hold_target:.4g} {self.units}'
        elif self.hold_target is not None:
            text += f': held at {self.hold_target:.4g} {self.units}'
        state = self.ramp_state
        if state is not None and self.program is not None and not self.settled:
            total = self.program.n_cycles or '∞'
            text += f': cycle {state.cycle}/{total} {state.phase.value}'
            text += ' (paused)' if state.paused else f' ({format_duration(state.remaining)} left)'
            text += f', set-point {state.setpoint:.4g} {self.units}'
        if self.temperature is not None:
            text += f', measured {self.temperature.value:.4g} {self.units}'
        if self.n_missed:
            text += f', {self.n_missed} readouts missed'
        return text.strip()


class CameraUnit(Unit):
    """A camera and its thermostat: the unit of a spectrometer, with images instead of spectra

    Each readout takes one image; the value of each ROI is an aggregation (the mean, by default) of the pixels inside its
    rectangle (of the channels of the image it is told to measure: the results of the channels are averaged), or, if it is a
    crop, it has none: its pixels are saved (see :mod:`.camera`). The ramp, the thermostat, the watchdogs and the recording
    work as in :class:`Unit`: the last image is :attr:`spectrum` (a :class:`~.camera.Frame`), what the two kinds of unit have
    in common keeps the same names.

    Attributes
    ----------
    channels_changed: Signal(int)
        The images have not the same number of channels as before (it is emitted with the first image): the settings of the
        ROIs and the ROI manager show the choice of the channels if there are several
    """
    channels_changed = QtCore.Signal(int)
    _n_channels = 0  # (how many channels the last image had: 0 before the first one)
    PENDING_MIN = 30.0  # (the plugin of a camera gives up on its own when a frame does not come in 20 s)
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
        n = self.spectrum.n_channels
        if n != self._n_channels:  # (before the ROIs are measured: they are given what is left of their channels)
            self._n_channels = n
            self.channels_changed.emit(n)
        return True

    @property
    def n_channels(self) -> int:
        """How many channels the last image had (1 until the first one comes)"""
        return max(self._n_channels, 1)

    def _roi_values(self, rois) -> list[float]:
        values = []
        image = self.spectrum.y
        channels = self.spectrum.channels  # (a ROI cuts its pixels out of each channel: nothing is converted)
        for roi in rois:
            value = roi.value(channels)
            if roi.is_crop:  # (no value: its pixels are saved)
                values.append(value)
                continue
            if math.isnan(value) and roi.name not in self._warnings:  # once per ROI, not at every readout
                self._warnings.add(roi.name)
                logger.warning(f'{roi.name}: the window is outside of the image ({image.shape[1]} x '
                               f'{image.shape[0]} pixels)')
            values.append(value)
        return values
