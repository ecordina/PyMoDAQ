"""The language of the Quantum Northwest temperature controllers (TC 125, TC 1, TC 1B)

The controller and the computer exchange *messages* between square brackets. The computer sends commands, for
instance ``[F1 CT ?]`` (*what is the temperature of the sample holder?*) or ``[F1 TT S 23.10]`` (*set the target
temperature to 23.10 °C*). The controller answers (``[F1 CT 22.84]``) and, depending on its settings, reports on its own
what is changed by hand on its front panel, or what it was asked to report periodically. ``F1`` is the sample holder
(``R1`` is the reference holder of the dual controllers and ``F2`` the cell changer of the multi-position ones: they are
not used here). The text which is not between brackets is ignored by the controller, and here.

This module knows how to cut a stream of bytes into messages and how to read the values in them, nothing about ports.
It follows the *serial communications* appendix of the manuals: version 9.1 of the firmware of the TC 125 / 225 / 425
(appendix of the manual of the qpod and of the CD 250), and version 1.0 and 2.x of the TC 1 and TC 1B (appendix of the
manuals of the Luma 40 and of the TC 1B).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Optional

from pymodaq_plugins_quantumnorthwest.hardware.errors import QnwError

MAX_MESSAGE_LENGTH = 200  # characters: a message which is longer is garbage


class QnwProtocolError(QnwError, ValueError):
    """A message which was understood as a message, but not as the one it should be"""


@dataclass(frozen=True)
class Message:
    """A message of the controller, without its brackets

    ``[F1 CT 22.84]`` gives ``Message(unit='F1', code='CT', args=('22.84',), text='22.84')``.
    """
    unit: str
    code: str
    args: tuple[str, ...] = ()
    text: str = ''  # everything which follows the code, as it was written
    raw: str = ''  # the whole content

    @classmethod
    def parse(cls, content: str) -> Optional['Message']:
        """The message of the content of a pair of brackets, None if there is no command code in it"""
        parts = content.split(None, 2)
        if len(parts) < 2:
            return None
        text = parts[2].strip() if len(parts) > 2 else ''
        return cls(parts[0].upper(), parts[1].upper(), tuple(text.split()), text, content.strip())


class MessageParser:
    """Cut a stream of bytes into messages

    The bytes may come in any number of pieces, with anything between the messages. A message which is cut by another
    one (a ``[`` before the closing ``]``) is dropped. The controller quotes the command it did not understand
    between ``<<`` and ``>>``, and this command has its own brackets: the quotation is kept in the message.
    """

    def __init__(self):
        self._text: Optional[str] = None  # None: not inside a message

    def reset(self):
        """Forget the part of a message which was received"""
        self._text = None

    @property
    def partial(self) -> bool:
        """Whether the beginning of a message was received"""
        return self._text is not None

    def _quoted(self) -> bool:
        return self._text.rfind('<<') > self._text.rfind('>>')

    def feed(self, data: bytes) -> list[Message]:
        """The messages which are complete after these bytes (the rest is kept for the next ones)"""
        messages = []
        for char in data.decode('latin-1'):
            if self._text is None:
                if char == '[':
                    self._text = ''
            elif char == '[' and not self._quoted():
                self._text = ''  # the message before was cut
            elif char == ']' and not self._quoted():
                message = Message.parse(self._text)
                self._text = None
                if message is not None:
                    messages.append(message)
            else:
                self._text += char
                if len(self._text) > MAX_MESSAGE_LENGTH:
                    self._text = None
        return messages


def command(code: str, *args, unit: str = 'F1') -> bytes:
    """The bytes of a command: ``command('TT', 'S', '23.10')`` is ``b'[F1 TT S 23.10]'``"""
    return f"[{' '.join([unit, code, *(str(arg) for arg in args)])}]".encode('ascii')


def normalized(text: str) -> str:
    """A command or what is quoted from it, to compare them: no brackets, spaces or case"""
    return re.sub(r'[\s\[\]<>]', '', text).upper()


def format_temperature(value: float) -> str:
    """A temperature as the controller takes it: hundredths of a degree"""
    text = f'{value:.2f}'
    return '0.00' if text == '-0.00' else text


def parse_float(text: str) -> Optional[float]:
    """A number of the controller, None if it says it is not available (NA)"""
    text = text.strip()
    if text.upper() in ('NA', 'N/A', ''):
        return None
    try:
        value = float(text)
    except ValueError:
        raise QnwProtocolError(f"'{text}' is not a number") from None
    if not math.isfinite(value):
        raise QnwProtocolError(f"'{text}' is not a finite number")
    return value


@dataclass(frozen=True)
class InstrumentStatus:
    """The answer to ``[F1 IS ?]``, for instance ``0-+S``: no unreported error, stirrer off, control on, stable"""
    errors: int  # number of errors the controller did not report yet (0 to 9)
    stirring: bool
    control: bool  # the temperature control is on
    stable: bool  # the temperature of the holder is stable, False: it is changing
    ramp: Optional[str] = None  # '-' no ramp, 'W' it waits for a new target, '+' it ramps. None: not told

    @property
    def state(self) -> str:
        """``off``, ``seeking`` (the temperature is changing) or ``holding`` (it is stable)"""
        if not self.control:
            return 'off'
        return 'holding' if self.stable else 'seeking'


_STATUS = re.compile(r'^(\d)([+-])([+-])([SC])([-+W])?$', re.IGNORECASE)


def parse_instrument_status(text: str) -> InstrumentStatus:
    """The status in the text which follows ``IS``: ``0-+S``, with the status of the ramp at the end if asked for"""
    match = _STATUS.match(''.join(text.split()))
    if match is None:
        raise QnwProtocolError(f"'{text}' is not an instrument status")
    errors, stirring, control, stable, ramp = match.groups()
    return InstrumentStatus(int(errors), stirring == '+', control == '+', stable.upper() == 'S',
                            None if ramp is None else ramp.upper())


UNREADABLE_ERROR = 99  # (not a code of the controller) an error is counted by a controller which cannot tell it

ERROR_TEXTS = {
    5: 'the temperature of the sample holder is out of range (loose cable? sensor failure?)',
    6: 'the temperatures of the sample holder and of the heat exchanger are out of range (loose cable?)',
    7: 'the temperature of the heat exchanger is out of range (loose cable? sensor failure?)',
    8: 'inadequate coolant (check the flow of water): the temperature control has shut down',
    9: 'the controller did not understand a command',
    UNREADABLE_ERROR: 'the controller counts an error and cannot say which (it does not answer [F1 ER ?])',
}


@dataclass(frozen=True)
class ErrorReport:
    """The answer to ``[F1 ER ?]``: -1 (or 0) is no error, 5 to 8 are faults, 9 a command which was not understood"""
    code: int
    detail: str = ''  # the command which was not understood (code 9)

    @property
    def is_error(self) -> bool:
        return self.code > 0

    @property
    def text(self) -> str:
        if not self.is_error:
            return 'no error'
        text = ERROR_TEXTS.get(self.code, 'unknown error')
        return f'error {self.code}, {text}' + (f' ({self.detail})' if self.detail else '')


def parse_error(text: str) -> ErrorReport:
    """The error in the text which follows ``ER``: ``-1``, ``05`` or ``9 <<[F1 XX ?]>>``"""
    parts = text.split(None, 1)
    if not parts:
        raise QnwProtocolError('an error report without a code')
    try:
        code = int(parts[0])
    except ValueError:
        raise QnwProtocolError(f"'{parts[0]}' is not an error code") from None
    quoted = re.search(r'<<(.*)>>', text, re.DOTALL)
    return ErrorReport(code, quoted.group(1).strip() if quoted else '')


# The ramp of the temperature is made by the controller from two integers, the time increment RS (seconds) and the
# temperature increment RT (hundredths of a degree): rate (°C/min) = (RT / 100) / (RS / 60). Both are 0 when there is no
# ramp. The TC 1 and the TC 1B accept them too (they have a ramp rate of their own, RR, as well).
MIN_RAMP_RATE = 0.01  # °C/min
MAX_RAMP_RATE = 10.  # °C/min
MAX_RAMP_SECONDS = 60


def ramp_rate_of(seconds: int, hundredths: int) -> float:
    """The ramp rate (°C/min) of a time increment (s) and of a temperature increment (hundredths of a degree)"""
    return 0.6 * hundredths / seconds


def ramp_increments(rate: float) -> tuple[int, int]:
    """The time (RS, s) and temperature (RT, hundredths of a degree) increments which give a ramp rate (°C/min)

    The steps of the ramp are the smallest which give it: the shortest time increment (of 12 s or less, like in the
    manual) which gives exactly the rate, else the shortest which is within 1 % of it, else the closest one.
    """
    if not MIN_RAMP_RATE <= rate <= MAX_RAMP_RATE:
        raise ValueError(f'A ramp rate is between {MIN_RAMP_RATE:g} and {MAX_RAMP_RATE:g} °C/min, not {rate:g}')

    def candidate(seconds):
        hundredths = max(1, round(rate * seconds / 0.6))
        return abs(ramp_rate_of(seconds, hundredths) - rate) / rate, seconds, hundredths

    candidates = [candidate(seconds) for seconds in range(1, MAX_RAMP_SECONDS + 1)]
    for tolerance, longest in ((1e-9, 12), (0.01, MAX_RAMP_SECONDS)):
        for error, seconds, hundredths in candidates[:longest]:
            if error <= tolerance:
                return seconds, hundredths
    _, seconds, hundredths = min(candidates)
    return seconds, hundredths
