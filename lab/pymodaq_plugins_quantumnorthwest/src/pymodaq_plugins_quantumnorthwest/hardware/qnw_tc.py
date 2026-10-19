"""Driver of the Quantum Northwest temperature controllers (TC 125, TC 1, TC 1B)

These controllers drive the Peltier-controlled cuvette holders of Quantum Northwest (the qpod, the Luma 40, the CD 250,
the qX2, the t2 Sport...): they set and keep the temperature of the holder, read the one of an external probe in the
cuvette and stir the sample. They are connected by an RS-232 or a USB serial port (19200 baud, 8 data bits, no parity,
one stop bit, no flow control) and talk in messages between square brackets, see :mod:`.protocol`.

:class:`QnwTC` uses only the commands which the TC 125 (firmware 9.1) and the TC 1 / TC 1B (firmware 1.0 to 2.x) have in
common, and finds out what else the controller can do (for instance setting the speed of the stirrer) when it connects.
Unlike what the manuals show, the answers to the commands which *set* something are not waited for, as the TC 125 does
not give any: what matters is checked with a question (the status of the controller says if the temperature control or
the stirrer is on).

The controller may talk on its own (what is changed with the buttons of its front panel is reported, and periodic
reports can be left on by another program): every message is read, whatever it is waited for or not, and what it says
is kept (:attr:`QnwTC.last`).
"""
from __future__ import annotations

import math
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from pymodaq_utils.logger import set_logger, get_module_name

from pymodaq_plugins_quantumnorthwest.hardware import protocol
from pymodaq_plugins_quantumnorthwest.hardware.errors import (QnwConnectionError, QnwError, QnwLimitError,
                                                              QnwRejected, QnwTimeout)
from pymodaq_plugins_quantumnorthwest.hardware.protocol import (ErrorReport, InstrumentStatus, Message,
                                                                MessageParser, QnwProtocolError, format_temperature,
                                                                parse_error, parse_float, parse_instrument_status,
                                                                UNREADABLE_ERROR)
from pymodaq_plugins_quantumnorthwest.hardware.simulator import is_simulated, open_simulated

logger = set_logger(get_module_name(__file__))

DEFAULT_BAUDRATE = 19200
READ_TIMEOUT = 0.05  # s: how long a read of the port waits for a byte
PROBE_TIMEOUT = 0.5  # s: how long the controller is given to answer a command it may not know
REJECT_GRACE = 0.3  # s: an error which does not say which command it is about is waited this long for the answer
QUIET_WAIT = 0.1  # s: how long a command which has no answer is given to be refused, when this is looked at
MAX_READS = 500  # reads of the port at most for one call: the controller may talk without end
SAME_TARGET_TIME = 5.  # s: the same target is not sent again before this time

HOLDER_NAMES = {
    0: 'specialty sample holder',
    10: 'single cuvette holder',
    11: 'single cuvette holder with probe capability',
    12: 'high temperature single cuvette holder',
    13: 'single cuvette holder',
    14: 'single cuvette holder',
    20: 'dual cuvette holder',
    21: 'dual cuvette holder with probe capability',
    22: 'dual-controlled titrator',
    24: 'dual cuvette holder',
    30: '4-position turret',
    31: '4-position turret with probe capability',
    32: '6-position turret or linear cell changer',
    34: 'multi-position sample holder',
    114: 'single cuvette holder',
}
SINGLE_HOLDERS = (10, 11, 12, 13, 14, 114)  # the holders which the sample commands (F1) drive completely


def open_port(port: str, baudrate: int = DEFAULT_BAUDRATE, timeout: float = READ_TIMEOUT):
    """The opened serial port: a device (``/dev/ttyUSB0``, ``COM3``), a URL of pyserial (``socket://host:port``,
    ``rfc2217://``...) or a simulated controller (``sim://``, see :mod:`.simulator`)"""
    if is_simulated(port):
        return open_simulated(port, baudrate, timeout)
    try:
        import serial
    except ImportError:
        raise QnwConnectionError('pyserial is needed to talk to the controller: python -m pip install pyserial') \
            from None
    options = {'exclusive': True} if os.name == 'posix' else {}  # (two openers would steal each other's bytes)
    return serial.serial_for_url(port, baudrate=baudrate, bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                                 stopbits=serial.STOPBITS_ONE, timeout=timeout, write_timeout=2., xonxoff=False,
                                 rtscts=False, dsrdtr=False, **options)


def serial_ports() -> list[str]:
    """The serial ports of the computer (nothing is sent to them)"""
    try:
        from serial.tools import list_ports
        return sorted(port.device for port in list_ports.comports())
    except Exception as e:  # no pyserial, or a platform which cannot list its ports
        logger.debug(f'The serial ports cannot be listed: {e}')
        return []


@dataclass
class Reading:
    """What a message said, and when it was received"""
    value: Any
    time: float


class QnwTC:
    """A Quantum Northwest temperature controller (TC 125, TC 1, TC 1B) on a serial port

    Parameters
    ----------
    port: str
        the serial port, or a pyserial URL, or ``sim://`` for a simulated controller
    baudrate: int
        the controllers talk at 19200 baud
    timeout: float
        s, how long a question waits for its answer
    unit: str
        the holder to drive, ``F1`` is the (first) sample holder
    clock, port_factory:
        to replace the time and the opening of the port (in tests)

    Attributes
    ----------
    firmware: str
        the version of the firmware, known once the controller is open
    holder_id: int or None
        the kind of sample holder (see :data:`HOLDER_NAMES`)
    lowest_target, highest_target: float or None
        °C, the targets the holder accepts
    has_probe: bool or None
        whether an external probe is connected (None: the controller did not say)
    stirrer_speeds: tuple of two floats or None
        rpm, the slowest and the fastest speed of the stirrer: None if its speed is not set by the computer
    fault: ErrorReport or None
        the last fault the controller reported (5 to 8), until the temperature control is switched on again
        (see :meth:`check_faults`)
    last: dict
        what the controller said last, for each of the codes of its messages
    """

    def __init__(self, port: str, baudrate: int = DEFAULT_BAUDRATE, timeout: float = 1., unit: str = 'F1',
                 clock: Callable[[], float] = time.monotonic,
                 port_factory: Callable[..., Any] = open_port):
        self.port = port
        self.baudrate = int(baudrate)
        self.timeout = float(timeout)
        self.unit = unit
        self._clock = clock
        self._factory = port_factory
        self._lock = threading.RLock()
        self._port = None
        self._parser = MessageParser()
        self._probing = False  # the controller is opened: it is asked what it may not know
        self._expecting_refusal = False  # a question which the controller may refuse is being asked
        self._unsupported: set[tuple] = set()  # the questions the controller does not know
        self.last: dict[str, Reading] = {}
        self.firmware = ''
        self.holder_id: Optional[int] = None
        self.lowest_target: Optional[float] = None
        self.highest_target: Optional[float] = None
        self.has_probe: Optional[bool] = None
        self.stirrer_speeds: Optional[tuple[float, float]] = None
        self.fault: Optional[ErrorReport] = None
        self.can_report_errors = True  # the controller answers [F1 ER ?] (a TC 125 of a qpod does not)
        self._errors_seen = 0

    # ----- connection ------------------------------------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._port is not None

    @property
    def holder_name(self) -> str:
        if self.holder_id is None:
            return 'unknown sample holder'
        return HOLDER_NAMES.get(self.holder_id, 'unknown sample holder')

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    def open(self):
        """Open the port and find out what the controller is and can do

        Raises
        ------
        QnwConnectionError
            if the port cannot be opened or if nothing answers on it
        """
        with self._lock:
            if self.is_open:
                return
            try:
                self._port = self._factory(self.port, self.baudrate)
            except QnwError:
                raise
            except Exception as e:
                raise QnwConnectionError(f'The port {self.port} cannot be opened: {e}') from e
            self._reset_knowledge()
            self._probing = True
            try:
                self._identify()
            except QnwProtocolError as e:  # (something else is on the port, which says things in brackets)
                self.close()
                raise QnwConnectionError(f'The device on {self.port} does not talk like a Quantum Northwest '
                                         f'controller: {e}') from e
            except BaseException:
                self.close()
                raise
            finally:
                self._probing = False

    def _reset_knowledge(self):
        self._parser.reset()
        self._unsupported.clear()
        self.last.clear()
        self.firmware = ''
        self.holder_id = self.lowest_target = self.highest_target = self.has_probe = None
        self.stirrer_speeds = None
        self.fault = None
        self.can_report_errors = True
        self._errors_seen = 0

    def close(self):
        """Close the port (the controller goes on as it is)"""
        with self._lock:
            port, self._port = self._port, None
            if port is not None:
                try:
                    port.close()
                except Exception as e:
                    logger.debug(f'Closing {self.port}: {e}')

    def _identify(self):
        try:
            self._port.reset_input_buffer()
        except Exception as e:
            raise QnwConnectionError(f'The port {self.port} does not work: {e}') from e
        alive = False
        # the version first (twice: the first command may arrive in the middle of something the controller says), then
        # whatever else any controller answers: one which does not know the version is there if it answers
        for code, timeout in (('VN', None), ('VN', None), ('ID', PROBE_TIMEOUT), ('CT', PROBE_TIMEOUT)):
            try:
                reply = self._ask(code, '?', timeout=timeout)
            except QnwTimeout:
                continue
            except QnwRejected:
                alive = True
                continue
            alive = True
            if code == 'VN':
                self.firmware = reply.text
            break
        if not alive:
            raise QnwConnectionError(
                f'The controller does not answer on {self.port} at {self.baudrate} baud. Check that it is switched '
                f'on, that it has a serial interface (a USB or RS-232 connector at the back: it is an option of the '
                f'TC 125), the cable and the port')
        self.holder_id = self._optional_int('ID')
        self.highest_target = self._optional_float('MT')
        self.lowest_target = self._optional_float('LT')
        message = self._ask('PS', '?', expect=('PS', 'PR'), optional=True, timeout=PROBE_TIMEOUT)
        if message is not None and message.args[:1] in (('+',), ('-',)):
            self.has_probe = message.args[0] == '+'
        # (firmware 9, the TC 125, 225 and 425: the speed of the stirrer is set by hand, their manual has no command)
        if not self.firmware.startswith('9'):
            low, high = self._optional_float('LS'), self._optional_float('MS')
            if low is not None and high is not None:
                self.stirrer_speeds = (low, high)
        # the reports another program may have left on (T-App does), switched off one at a time: a controller does not
        # always say which command it did not understand (a TC 125 has no heat exchanger sensor, it refuses HT)
        for code in ('CT', 'PT', 'HT', 'IS', 'PA'):
            self._tell_checked(code, '-')
        if self.has_probe:
            self._tell_checked('PX', '+')  # hundredths of a degree
        self._drain_errors()  # (the commands the controller did not know are errors too)
        logger.info(f'Quantum Northwest controller on {self.port}: {self.summary()}, targets from {self.lowest_target} '
                    f'to {self.highest_target} °C, stirrer speeds {self.stirrer_speeds}')

    def _optional_float(self, code: str) -> Optional[float]:
        message = self._ask(code, '?', optional=True, timeout=PROBE_TIMEOUT)
        try:
            return None if message is None else parse_float(message.text)
        except QnwProtocolError:
            return None

    def _optional_int(self, code: str) -> Optional[int]:
        value = self._optional_float(code)
        return None if value is None else int(value)

    # ----- the messages ------------------------------------------------------------------------------------------

    def _check_open(self):
        if self._port is None:
            raise QnwConnectionError('The controller is not open')

    def _send(self, data: bytes):
        try:
            self._port.write(data)
            self._port.flush()
        except Exception as e:
            raise QnwConnectionError(f'The connection to the controller on {self.port} is lost: {e}') from e

    def _fetch(self, deadline: Optional[float] = None) -> list[Message]:
        """The messages which come from the controller: the ones which are there, or the first which comes before the
        deadline"""
        found: list[Message] = []
        for _ in range(MAX_READS):
            try:
                waiting = self._port.in_waiting
                if waiting:
                    data = self._port.read(waiting)
                elif deadline is not None and not found and self._clock() < deadline:
                    data = self._port.read(1)  # (waits for a byte as long as the timeout of the port)
                else:
                    break
            except Exception as e:
                raise QnwConnectionError(f'The connection to the controller on {self.port} is lost: {e}') from e
            found.extend(self._parser.feed(data))
        return found

    def _digest(self, messages: list[Message]):
        for message in messages:
            if message.unit == self.unit:
                try:
                    self._record(message)
                except (QnwProtocolError, ValueError, IndexError) as e:
                    logger.debug(f'Unreadable message [{message.raw}]: {e}')

    def _record(self, message: Message):
        """Keep what a message says"""
        now, code, text = self._clock(), message.code, message.text
        if code in ('CT', 'HT', 'TT', 'MT', 'LT', 'HL', 'LS', 'MS'):
            value = parse_float(text)
            if value is not None:
                self.last[code] = Reading(value, now)
        elif code == 'PT':
            self.last[code] = Reading(parse_float(text), now)
        elif code == 'NOPROBE':
            self.has_probe = False
            self.last['PT'] = Reading(None, now)
        elif code in ('PR', 'PS'):
            if message.args[:1] in (('+',), ('-',)):
                self.has_probe = message.args[0] == '+'
        elif code == 'IS':
            if text.strip().upper().startswith('R'):
                logger.warning('The controller was switched off and on again: its settings are the ones of its '
                               'power-on')
            else:
                status = parse_instrument_status(text)
                self.last['IS'] = Reading(status, now)
                self.last['control'] = Reading(status.control, now)
        elif code == 'TC':
            if message.args[:1] in (('+',), ('-',)):
                self.last['control'] = Reading(message.args[0] == '+', now)
        elif code == 'ER':
            report = parse_error(text)
            if report.code == 9:
                (logger.debug if self._probing or self._expecting_refusal else logger.warning)(
                    f'The controller did not understand a command: {report.detail or "(not told which)"}')
            elif report.is_error:
                self.fault = report
                logger.error(f'The controller reports {report.text}')
            self.last['ER'] = Reading(report, now)
        elif code == 'VN':
            self.last['VN'] = Reading(text, now)
        elif code == 'ID':
            self.last['ID'] = Reading(parse_float(text), now)

    @staticmethod
    def _names_command(message: Message) -> bool:
        """Whether an error says which command it is about (a TC 125 only says ``[F1 ER 9]``)"""
        return bool(parse_error(message.text).detail)

    @staticmethod
    def _rejects(message: Message, sent: str) -> bool:
        """Whether a message says the command which was sent was not understood"""
        if message.code != 'ER' or message.args[:1] not in (('9',), ('09',)):
            return False
        detail = parse_error(message.text).detail
        return not detail or protocol.normalized(detail) == protocol.normalized(sent)

    def _ask(self, code: str, *args, expect: Optional[tuple[str, ...]] = None,
             accept: Optional[Callable[[Message], bool]] = None, timeout: Optional[float] = None,
             optional: bool = False) -> Optional[Message]:
        """Send a command and wait for the answer: the first message whose code is one of ``expect`` (the code of the
        command by default) and which ``accept`` takes

        If the controller says it did not understand the command, or says nothing in time, the answer is None for an
        ``optional`` question, an error otherwise (and the controller is not asked the same thing again).
        """
        expect = expect or (code,)
        sent = protocol.command(code, *args, unit=self.unit)
        label = sent.decode('ascii')
        echo = protocol.normalized(label)
        key = (code, args, expect)
        with self._lock:
            self._check_open()
            if optional and key in self._unsupported:
                return None
            self._digest(self._fetch())  # (what the controller said which nobody waited for)
            self._send(sent)
            wait = self.timeout if timeout is None else timeout
            deadline = self._clock() + wait
            self._expecting_refusal = optional  # (a refusal is nothing to warn about)
            try:
                refused_at: Optional[float] = None  # when an error which may be about this command came
                while True:
                    limit = deadline if refused_at is None else min(deadline, refused_at + REJECT_GRACE)
                    messages = self._fetch(limit)
                    self._digest(messages)
                    for message in messages:
                        if message.unit != self.unit or protocol.normalized(message.raw) == echo:
                            continue  # (not for this controller, or the line sends back what it receives)
                        if message.code in expect and (accept is None or accept(message)):
                            return message
                        if self._rejects(message, label):
                            if self._names_command(message):
                                return self._refused(key, label, optional)
                            # (it may be about another command: the answer to this one may come after it)
                            refused_at = self._clock() if refused_at is None else refused_at
                    now = self._clock()
                    if refused_at is not None and now >= refused_at + REJECT_GRACE:
                        return self._refused(key, label, optional)
                    if now >= deadline:
                        if optional:
                            self._unsupported.add(key)
                            return None
                        raise QnwTimeout(f'No answer to {label} from the controller within {wait:g} s')
            finally:
                self._expecting_refusal = False

    def _refused(self, key: tuple, label: str, optional: bool) -> None:
        """The controller does not understand a command: it is not sent again if it was a question which may be"""
        self._unsupported.add(key)
        if optional:
            return None
        raise QnwRejected(f'The controller did not understand {label}')

    def _tell(self, code: str, *args):
        """Send a command which is not waited for an answer"""
        with self._lock:
            self._check_open()
            self._digest(self._fetch())
            self._send(protocol.command(code, *args, unit=self.unit))

    def _tell_checked(self, code: str, *args) -> bool:
        """Send a command which has no answer and give the controller the time to refuse it: False if it did (it is
        not an error: what is refused is something the controller does not have)"""
        sent = protocol.command(code, *args, unit=self.unit)
        label = sent.decode('ascii')
        with self._lock:
            self._check_open()
            self._digest(self._fetch())
            self._send(sent)
            deadline = self._clock() + QUIET_WAIT
            messages = self._fetch()  # (what is there already)
            while True:
                self._digest(messages)
                if any(self._rejects(message, label) for message in messages if message.unit == self.unit):
                    logger.debug(f'The controller does not know {label}')
                    return False
                if self._clock() >= deadline:
                    break
                messages = self._fetch(deadline)
        return True

    # ----- reading ---------------------------------------------------------------------------------------------

    def _temperature(self, code: str, expect: Optional[tuple[str, ...]] = None) -> Optional[float]:
        message = self._ask(code, '?', expect=expect)
        return None if message.code == 'NOPROBE' else parse_float(message.text)

    def holder_temperature(self, max_age: float = 0.) -> float:
        """°C, the temperature of the sample holder (the one which was read less than ``max_age`` s ago is not read
        again)"""
        with self._lock:
            known = self.last.get('CT')
            if max_age > 0 and known is not None and self._clock() - known.time < max_age:
                return known.value
            value = self._temperature('CT')
            if value is None:
                raise QnwProtocolError('The controller gave no temperature of the holder')
            return value

    def probe_temperature(self) -> Optional[float]:
        """°C, the temperature of the external probe, None if there is none (or if it gives nothing)"""
        return self._temperature('PT', expect=('PT', 'NOPROBE'))

    def heat_exchanger_temperature(self) -> Optional[float]:
        """°C, the temperature of the heat exchanger of the Peltier element, None if the controller does not say"""
        message = self._ask('HT', '?', optional=True, timeout=PROBE_TIMEOUT)
        return None if message is None else parse_float(message.text)

    def target(self) -> float:
        """°C, the target temperature of the controller"""
        value = parse_float(self._ask('TT', '?').text)
        if value is None:
            raise QnwProtocolError('The controller gave no target temperature')
        return value

    def status(self) -> InstrumentStatus:
        """The status of the controller: errors, stirrer, temperature control, stability of the temperature"""
        message = self._ask('IS', '?', accept=lambda m: not m.text.strip().upper().startswith('R'))
        return parse_instrument_status(message.text)

    def error(self) -> ErrorReport:
        """The error the controller reports (it is then told to the computer: it is not reported twice)"""
        return parse_error(self._ask('ER', '?').text)

    def _drain_errors(self):
        """Ask for the errors the controller counts until it says there are none. A controller which says it does not
        understand the question each time does not have it (the status then only counts errors)"""
        status = self.status()
        for _ in range(status.errors + 2):
            if self.error().code <= 0:
                self.can_report_errors = True
                return
        self.can_report_errors = False
        self._errors_seen = status.errors

    def pending_errors(self, status: Optional[InstrumentStatus] = None) -> list[ErrorReport]:
        """The errors the controller did not report yet (it reports one for each question)

        A controller which cannot be asked says only how many errors it counts: a new one is an error which is not
        known (:data:`.protocol.UNREADABLE_ERROR`), told once.
        """
        status = status or self.status()
        if not self.can_report_errors:
            new = status.errors > self._errors_seen
            self._errors_seen = status.errors
            return [ErrorReport(UNREADABLE_ERROR)] if new else []
        reports = []
        for _ in range(status.errors):
            report = self.error()
            if report.code <= 0:  # (nothing more to report)
                break
            reports.append(report)
        return reports

    def check_faults(self, status: Optional[InstrumentStatus] = None) -> list[ErrorReport]:
        """The faults (errors 5 to 8) the controller did not report yet: it reports an error only once

        The last one is kept in :attr:`fault` until the temperature control works again and nothing new is reported.
        """
        with self._lock:
            status = status or self.status()
            faults = [report for report in self.pending_errors(status)
                      if 5 <= report.code <= 8 or report.code == UNREADABLE_ERROR]
            if not faults and status.control:  # (it was repaired and the control switched on from the front panel)
                self.fault = None
            return faults

    # ----- acting ----------------------------------------------------------------------------------------------

    def set_target(self, value: float):
        """Set the target temperature (°C). This does not switch the temperature control on, see
        :meth:`ensure_control`

        Raises
        ------
        QnwLimitError
            if the sample holder does not take this temperature
        """
        value = float(value)
        if not math.isfinite(value):
            raise QnwLimitError(f'{value} is not a temperature')
        low, high = self.lowest_target, self.highest_target
        if low is not None and high is not None and not low <= value <= high:
            raise QnwLimitError(f'{value:g} °C is out of the range of this holder: {low:g} to {high:g} °C')
        text = format_temperature(value)
        value = float(text)
        with self._lock:
            known = self.last.get('TT')
            if known is not None and abs(known.value - value) < 0.005 and self._clock() - known.time < SAME_TARGET_TIME:
                return
            self._tell('TT', 'S', text)
            self.last['TT'] = Reading(value, self._clock())

    def set_control(self, on: bool) -> InstrumentStatus:
        """Switch the temperature control on or off, and check that the controller did it"""
        with self._lock:
            self._tell('TC', '+' if on else '-')
            status = self.status()
            faults = self.check_faults(status)
            if status.control != on:
                raise QnwError(f"The controller did not switch the temperature control {'on' if on else 'off'}"
                               + (f': {faults[-1].text}' if faults else ''))
            self.last['control'] = Reading(on, self._clock())
            if on and not faults:
                self.fault = None
            return status

    def ensure_control(self, max_age: float = 5.) -> bool:
        """Switch the temperature control on if it is not (what was known less than ``max_age`` s ago is not asked
        again). Returns True if it was switched on"""
        with self._lock:
            known = self.last.get('control')
            if known is not None and known.value and self._clock() - known.time < max_age:
                return False
            if self.status().control:
                return False
            self.set_control(True)
            return True

    def set_stirrer(self, on: bool, speed: Optional[float] = None) -> InstrumentStatus:
        """Switch the stirrer on or off, and check that the controller did it

        The speed (rpm) is set when the stirrer is switched on, if the controller takes it: the TC 125 stirs at the
        speed of the knob of its front panel.
        """
        with self._lock:
            if not on:
                self._tell('SS', '-')
            elif speed is not None and self.stirrer_speeds is not None:
                low, high = self.stirrer_speeds
                self._tell('SS', 'S', int(round(min(max(speed, low), high))))
            else:
                self._tell('SS', '+')
            status = self.status()
            if status.stirring != on:
                raise QnwError(f"The controller did not switch the stirrer {'on' if on else 'off'}")
            return status

    def set_ramp_rate(self, rate: float):
        """Make the controller reach a new target at ``rate`` (°C/min) instead of as fast as it can, 0 for no ramp

        The ramp starts when the target temperature is set, from the temperature of the holder. It is set with the time
        and temperature increments (RS and RT) that every controller takes.
        """
        with self._lock:
            if rate <= 0:
                seconds = hundredths = 0
            else:
                try:
                    seconds, hundredths = protocol.ramp_increments(rate)
                except ValueError as e:
                    raise QnwLimitError(str(e)) from None
            self._tell('RS', 'S', seconds)
            self._tell('RT', 'S', hundredths)

    def hold(self) -> float:
        """Keep the holder at the temperature it has now: its target and no ramp. Returns the temperature (°C)"""
        with self._lock:
            temperature = self.holder_temperature()
            self.set_ramp_rate(0)
            self.set_target(temperature)
            return temperature

    def summary(self) -> str:
        """What is known of the controller, in a line"""
        probe = {None: 'probe unknown', True: 'probe connected', False: 'no probe'}[self.has_probe]
        return f"firmware {self.firmware or 'unknown'}, {self.holder_name} (ID {self.holder_id}), {probe}"
