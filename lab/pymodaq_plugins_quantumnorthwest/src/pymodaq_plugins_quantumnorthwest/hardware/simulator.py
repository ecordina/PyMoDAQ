"""A simulated Quantum Northwest controller on a simulated serial port

To try the plugin without a controller, and to test it. ``sim://`` stands for it as the *port* of the plugin:

* ``sim://tc125`` (or just ``sim://``) behaves like a TC 125 (firmware 9.1): the stirrer is on or off, there is no query
  of the temperature control, the commands which set something are not answered, a ramp is made with RS and RT;
* ``sim://qpod`` is a TC 125 as one was found to answer: no limits of the target (MT, LT), no heat exchanger sensor (HT,
  HL), does not answer ER ? and says ``[F1 ER 9]`` (not counted in its status) for what it does not know;
* ``sim://tc1b`` behaves like a TC 1B (firmware 2.6): the stirrer has a speed, the temperature control can be queried,
  the target temperature is confirmed, the ramp rate has a command (RR).

Options follow a ``?``, like ``sim://tc125?speed=60&probe=0``: ``speed`` is how many times the simulated time (of the
temperature of the holder) runs faster than the real time (1), ``probe`` tells whether an external probe is connected
(1), ``latency`` is the time before an answer (0 s), ``unknown`` what the controller does with a command it does not
understand: ``quote`` (it says ``[F1 ER 9 <<the command>>]``), ``code`` (``[F1 ER 09]``, the default of the TC 125) or
``silent`` (nothing, but the error is counted).

The temperature of the holder goes to the target like the one of a Peltier holder (a time constant of 15 s, at most 7 °C
per minute when heating and 5 when cooling), the probe in the cuvette follows it with a delay, the controller says that
the temperature is stable when it stayed within 0.05 °C of the target for a minute. Without control, it goes back to the
temperature of the room.
"""
from __future__ import annotations

import math
import re
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import Callable, Optional
from urllib.parse import parse_qs, urlsplit

from pymodaq_plugins_quantumnorthwest.hardware.protocol import MessageParser, ramp_rate_of

AMBIENT = 22.  # °C
TAU = 15.  # s, of the holder
TAU_OFF = 400.  # s, of the holder when the control is off
PROBE_TAU = 25.  # s, of the probe in the cuvette
HEAT_RATE = 0.12  # °C/s
COOL_RATE = 0.08  # °C/s
STABLE_BAND = 0.05  # °C
STABLE_TIME = 60.  # s
TIME_STEP = 0.5  # s of the simulated time


@dataclass(frozen=True)
class Model:
    """What differs from a controller to the other"""
    name: str
    firmware: str
    holder_id: int
    lowest: float
    highest: float
    stirrer_speed: bool  # SS S rpm, SS ?, MS ? and LS ?
    control_query: bool  # TC ?
    confirms: bool  # the controller repeats the target it was given: [F1 TT 23.10]
    ramp_rate_command: bool  # RR
    no_probe: str  # what PT ? answers when there is no probe
    unknown: str  # 'quote', 'code' or 'silent'
    error_format: str  # how an error code is written
    limits: bool = True  # MT ? and LT ?: the highest and the lowest target
    exchanger: bool = True  # HT ?, HL ? and the periodic HT report: the heat exchanger sensor
    error_query: bool = True  # ER ?: which error it counts
    counts_unknown: bool = True  # a command which is not understood is an error of the status (IS)


TC125 = Model('TC 125', '9.1', 11, -30., 105., False, False, False, False, 'PT NA', 'code', '{:02d}')
TC1B = Model('TC 1B', '2.6.04', 114, -40., 110., True, True, True, True, 'NOPROBE', 'quote', '{:d}')
# the TC 125 of a qpod, as it was found to answer (firmware 9.1): no limits of the target and no heat exchanger sensor, an
# error which says only its number, ``[F1 ER 9]``
QPOD = Model('TC 125', '9.1', 11, -30., 105., False, False, False, False, 'PT NA', 'code', '{:d}', limits=False,
             exchanger=False, error_query=False, counts_unknown=False)
MODELS = {'tc125': TC125, 'tc1b': TC1B, 'qpod': QPOD}
STIRRER_SPEEDS = (300, 2500)


class SimulatedController:
    """The controller: it reads commands and says what it answers, as bytes (see :class:`SimulatedSerial`)"""

    def __init__(self, model: Model = TC125, speed: float = 1., probe: bool = True, latency: float = 0.,
                 clock: Callable[[], float] = time.monotonic):
        self.model = model
        self.speed = float(speed)
        self.has_probe = bool(probe)
        self.latency = float(latency)
        self._clock = clock
        self._started = clock()
        self._offset = 0.  # simulated seconds which were skipped
        self._last = 0.  # simulated time of the last step
        self._parser = MessageParser()
        self._output: deque[list] = deque()  # [real time when it is ready, bytes]

        self.temperature = AMBIENT
        self.probe_temperature = AMBIENT
        self.target = 25.
        self.control = False
        self.stirring = False
        self.stir_speed = 1000
        self.ramp_seconds = 0
        self.ramp_hundredths = 0
        self.ramp_rate = 0.  # °C/min
        self._ramp_value: Optional[float] = None
        self._stable_since: Optional[float] = None
        self.errors: list[int] = []  # the errors which were not reported
        self.faulty = False  # the coolant does not flow
        self.report_errors = False
        self.report_status = False
        self.show_ramp = False
        self._periodic: dict[str, list] = {}  # code -> [interval, next simulated time, active]
        self.received: list[str] = []  # the content of all the messages received, for the tests

    # ----- time and physics ------------------------------------------------------------------------------------

    def simulated_time(self) -> float:
        return (self._clock() - self._started) * self.speed + self._offset

    def skip(self, seconds: float):
        """Let the simulated time run by ``seconds`` at once"""
        self._offset += seconds
        self._advance()

    def _advance(self):
        now = self.simulated_time()
        while self._last < now:
            step = min(TIME_STEP, now - self._last)
            self._step(step)
            self._last += step

    def _step(self, dt: float):
        if self.control:
            goal = self.target
            if self._ramp_value is not None:
                increment = self.ramp_rate / 60. * dt
                self._ramp_value += max(-increment, min(increment, self.target - self._ramp_value))
                goal = self._ramp_value
                if self._ramp_value == self.target:
                    self._ramp_value = None
            change = (goal - self.temperature) * (1. - math.exp(-dt / TAU))
            change = max(-COOL_RATE * dt, min(HEAT_RATE * dt, change))
        else:
            change = (AMBIENT - self.temperature) * (1. - math.exp(-dt / TAU_OFF))
        self.temperature += change
        self.probe_temperature += (self.temperature - self.probe_temperature) * (1. - math.exp(-dt / PROBE_TAU))
        if self.control and self._ramp_value is None and abs(self.temperature - self.target) <= STABLE_BAND:
            if self._stable_since is None:
                self._stable_since = self._last + dt
        else:
            self._stable_since = None

    @property
    def stable(self) -> bool:
        return self._stable_since is not None and self.simulated_time() - self._stable_since >= STABLE_TIME

    @property
    def exchanger_temperature(self) -> float:
        return AMBIENT + 0.3 * abs(self.temperature - AMBIENT) + (35. if self.faulty else 0.)

    # ----- what happens to the controller (for the tests) ------------------------------------------------------

    def fail(self, code: int = 8):
        """A fault: with 8 (inadequate coolant) the temperature control shuts down"""
        self._advance()
        if code == 8:
            self.faulty = True
            self.control = False
        self._error(code)

    def repair(self):
        self.faulty = False

    def inject(self, data: bytes, delay: float = 0.):
        """Bytes which come from the controller as they are (garbage, a cut message...)"""
        self._output.append([self._clock() + delay, bytes(data)])

    def drop_output(self):
        self._output.clear()

    # ----- the stream --------------------------------------------------------------------------------------------

    def receive(self, data: bytes):
        """The bytes which the computer sends"""
        for message in self._parser.feed(data):
            self.received.append(message.raw)
            if message.unit == 'F1':
                self._advance()
                self._handle(message)

    def available(self) -> int:
        """The number of bytes which can be read now"""
        self._pump()
        now = self._clock()
        return sum(len(data) for ready, data in self._output if ready <= now)

    def take(self, size: int) -> bytes:
        """At most ``size`` of the bytes which can be read now"""
        self._pump()
        now = self._clock()
        taken = b''
        while self._output and len(taken) < size and self._output[0][0] <= now:
            entry = self._output[0]
            wanted = size - len(taken)
            taken += entry[1][:wanted]
            if len(entry[1]) > wanted:
                entry[1] = entry[1][wanted:]
            else:
                self._output.popleft()
        return taken

    def _say(self, text: str):
        self._output.append([self._clock() + self.latency, f'[F1 {text}]'.encode('ascii')])

    def _pump(self):
        """The periodic reports which are due"""
        if not self._periodic:
            return
        self._advance()
        now = self.simulated_time()
        for code, entry in self._periodic.items():
            interval, due, active = entry
            if active and now >= due:
                self._say(self._value_of(code))
                entry[1] = due + interval if now - due < interval else now + interval

    def _value_of(self, code: str) -> str:
        if code == 'CT':
            return f'CT {self.temperature:.2f}'
        if code == 'PT':
            return f'PT {self.probe_temperature:.2f}' if self.has_probe else self.model.no_probe
        return f'HT {self.exchanger_temperature:.0f}'

    # ----- the commands ------------------------------------------------------------------------------------------

    def _handle(self, message):
        handler = getattr(self, f'_cmd_{message.code}', None)
        if handler is None or not handler(message.args):
            self._error(9, message.raw)

    def _error(self, code: int, command: str = ''):
        if code != 9 or self.model.counts_unknown:
            self.errors.append(code)
        if code == 9:
            if self.model.unknown == 'quote':
                self._say(f'ER 9 <<[{command}]>>')
            elif self.model.unknown == 'code':
                self._say(f'ER {self.model.error_format.format(9)}')
        elif self.report_errors:
            self._say(f'ER {self.model.error_format.format(code)}')

    def _status_text(self) -> str:
        text = f"{min(9, len(self.errors))}{'+' if self.stirring else '-'}{'+' if self.control else '-'}" \
               f"{'S' if self.stable else 'C'}"
        if self.show_ramp and self.model.ramp_rate_command:
            text += '+' if self._ramp_value is not None else ('W' if self.ramp_rate > 0 else '-')
        return text

    def _cmd_ID(self, args):
        if args == ('?',):
            self._say(f'ID {self.model.holder_id}')
            return True

    def _cmd_VN(self, args):
        if args == ('?',):
            self._say(f'VN {self.model.firmware}')
            return True

    def _cmd_MT(self, args):
        if args == ('?',) and self.model.limits:
            self._say(f'MT {self.model.highest:g}')
            return True

    def _cmd_LT(self, args):
        if args == ('?',) and self.model.limits:
            self._say(f'LT {self.model.lowest:g}')
            return True

    def _cmd_TC(self, args):
        if args in (('+',), ('-',)):
            if args == ('+',) and self.faulty:  # it shuts down again
                self.control = False
                self._error(8)
            else:
                self.control = args == ('+',)
                if not self.control:
                    self._ramp_value = None
            return True
        if args == ('?',) and self.model.control_query:
            self._say('TC ' + ('+' if self.control else '-'))
            return True
        return args in (('R+',), ('R-',)) and self.model.control_query

    def _cmd_TT(self, args):
        if len(args) == 2 and args[0] == 'S':
            try:
                value = float(args[1])
            except ValueError:
                return False
            if not self.model.lowest <= value <= self.model.highest:
                return False
            self.target = value
            self._ramp_value = self.temperature if self.ramp_rate > 0 else None
            if self.model.confirms:
                self._say(f'TT {value:.2f}')
            return True
        if args == ('?',):
            self._say(f'TT {self.target:.2f}')
            return True
        return args in (('+',), ('-',), ('R+',), ('R-',))

    def _cmd_IS(self, args):
        if args == ('?',):
            self._say(f'IS {self._status_text()}')
            return True
        if args in (('+',), ('R+',)):
            self.report_status = True
            return True
        if args in (('-',), ('R-',)):
            self.report_status = False
            return True
        if args in (('E+',), ('E-',)):
            self.show_ramp = args == ('E+',)
            return True
        return False

    def _cmd_ER(self, args):
        if args == ('?',) and self.model.error_query:
            if self.errors:
                self._say(f'ER {self.model.error_format.format(self.errors.pop(0))}')
            else:
                self._say('ER -1')
            return True
        if args in (('+',), ('-',)):
            self.report_errors = args == ('+',)
            return True
        return False

    def _periodic_command(self, code: str, args) -> bool:
        if args == ('?',):
            self._say(self._value_of(code))
            return True
        if len(args) == 1 and re.fullmatch(r'\+\d+', args[0]):
            self._periodic[code] = [float(args[0][1:]), self.simulated_time(), True]
            return True
        if args == ('+',):
            entry = self._periodic.setdefault(code, [3., self.simulated_time(), True])
            entry[2] = True
            return True
        if args == ('-',):
            if code in self._periodic:
                self._periodic[code][2] = False
            return True
        return False

    def _cmd_CT(self, args):
        return self._periodic_command('CT', args)

    def _cmd_PT(self, args):
        return self._periodic_command('PT', args)

    def _cmd_HT(self, args):
        return self.model.exchanger and self._periodic_command('HT', args)

    def _cmd_HL(self, args):
        if args == ('?',) and self.model.exchanger:
            self._say('HL 60')
            return True

    def _cmd_PS(self, args):
        if args == ('?',):
            self._say('PR ' + ('+' if self.has_probe else '-'))
            return True
        return args in (('+',), ('-',), ('R+',), ('R-',))

    def _cmd_PA(self, args):
        return bool(args) and (args[0] in ('+', '-') or (args[0] == 'S' and len(args) == 2))

    def _cmd_PX(self, args):
        return args in (('+',), ('-',))

    def _cmd_TL(self, args):
        return args in (('+',), ('-',), ('0',))

    def _cmd_SS(self, args):
        if args == ('+',):
            self.stirring = True
            return True
        if args == ('-',):
            self.stirring = False
            return True
        if not self.model.stirrer_speed:
            return False
        if len(args) == 2 and args[0] == 'S':
            try:
                speed = int(args[1])
            except ValueError:
                return False
            if speed == 0:
                self.stirring = False
                self.stir_speed = 0
            elif STIRRER_SPEEDS[0] <= speed <= STIRRER_SPEEDS[1]:
                self.stirring = True
                self.stir_speed = speed
            else:
                return False
            return True
        if args == ('?',):
            self._say(f'SS {self.stir_speed}')
            return True
        return args in (('R+',), ('R-',))

    def _cmd_MS(self, args):
        if args == ('?',) and self.model.stirrer_speed:
            self._say(f'MS {STIRRER_SPEEDS[1]}')
            return True

    def _cmd_LS(self, args):
        if args == ('?',) and self.model.stirrer_speed:
            self._say(f'LS {STIRRER_SPEEDS[0]}')
            return True

    def _set_ramp(self):
        if self.ramp_seconds > 0 and self.ramp_hundredths > 0:
            self.ramp_rate = ramp_rate_of(self.ramp_seconds, self.ramp_hundredths)
        else:
            self.ramp_rate = 0.
            self._ramp_value = None

    def _cmd_RS(self, args):
        if len(args) == 2 and args[0] == 'S' and args[1].isdigit():
            self.ramp_seconds = int(args[1])
            self._set_ramp()
            return True

    def _cmd_RT(self, args):
        if len(args) == 2 and args[0] == 'S' and args[1].isdigit():
            self.ramp_hundredths = int(args[1])
            self._set_ramp()
            return True

    def _cmd_RR(self, args):
        if not self.model.ramp_rate_command:
            return False
        if len(args) == 2 and args[0] == 'S':
            try:
                rate = float(args[1])
            except ValueError:
                return False
            if rate != 0 and not 0.01 <= rate <= 10:
                return False
            self.ramp_seconds = self.ramp_hundredths = 0
            self.ramp_rate = rate
            if rate == 0:
                self._ramp_value = None
            return True
        if args == ('?',):
            self._say(f'RR {self.ramp_rate:.2f}')
            return True
        if args == ('-',):
            self.ramp_rate = 0.
            self._ramp_value = None
            return True
        return args in (('+',), ('R+',), ('R-',))

    def _cmd_FP(self, args):
        return self.model.control_query and args in (('+',), ('-',))


class SimulatedSerial:
    """The part of ``serial.Serial`` the driver uses, on a :class:`SimulatedController`"""

    def __init__(self, controller: SimulatedController, port: str = 'sim://', baudrate: int = 19200,
                 timeout: float = 0.05):
        self.controller = controller
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._open = True

    @property
    def is_open(self) -> bool:
        return self._open

    def _check(self):
        if not self._open:
            raise OSError('The simulated port is closed')

    @property
    def in_waiting(self) -> int:
        self._check()
        return self.controller.available()

    def read(self, size: int = 1) -> bytes:
        self._check()
        deadline = time.monotonic() + (self.timeout or 0.)
        while True:
            data = self.controller.take(size)
            if data or time.monotonic() >= deadline:
                return data
            time.sleep(0.002)

    def write(self, data: bytes) -> int:
        self._check()
        self.controller.receive(data)
        return len(data)

    def flush(self):
        self._check()

    def reset_input_buffer(self):
        self._check()
        self.controller.drop_output()

    def close(self):
        self._open = False


def is_simulated(port: str) -> bool:
    """Whether a port is a simulated controller (``sim://...``)"""
    return str(port).strip().lower().startswith('sim:')


def parse_url(url: str) -> SimulatedController:
    """The simulated controller which a ``sim://model?option=value`` URL stands for"""
    parts = urlsplit(url.strip())
    name = (parts.netloc or parts.path.strip('/') or 'tc125').lower()
    if name not in MODELS:
        raise ValueError(f"No simulated model '{name}', choose among {', '.join(MODELS)}")
    options = {key: values[-1] for key, values in parse_qs(parts.query).items()}
    unknown = set(options) - {'speed', 'probe', 'latency', 'unknown'}
    if unknown:
        raise ValueError(f"Unknown option of the simulation: {', '.join(sorted(unknown))}")
    model = MODELS[name]
    if 'unknown' in options:
        if options['unknown'] not in ('quote', 'code', 'silent'):
            raise ValueError("The option 'unknown' is quote, code or silent")
        model = replace(model, unknown=options['unknown'])
    return SimulatedController(model, speed=float(options.get('speed', 1)),
                               probe=options.get('probe', '1') not in ('0', 'false', 'no'),
                               latency=float(options.get('latency', 0)))


def open_simulated(url: str, baudrate: int = 19200, timeout: float = 0.05) -> SimulatedSerial:
    """A simulated port, as :func:`serial.serial_for_url` would give it"""
    return SimulatedSerial(parse_url(url), url, baudrate, timeout)
