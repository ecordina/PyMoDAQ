"""Look at a controller without PyMoDAQ, to check the connection and see what the controller really says

    qnw_tc_check /dev/ttyUSB0 --trace

(or ``python -m pymodaq_plugins_quantumnorthwest.hardware.diagnose COM3``). Without a port, the serial ports of the
computer are listed. ``sim://tc125`` (or ``sim://tc1b``) is a simulated controller.

The controller is connected as the plugin does it (it is asked what it is and what it can do, and told to stop the
periodic reports), then the temperatures and the status are read, with the time each answer took. Nothing is set: the
temperature control, the target and the stirrer are left as they are.
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Callable, Optional, Sequence

from pymodaq_plugins_quantumnorthwest.hardware.errors import QnwError
from pymodaq_plugins_quantumnorthwest.hardware.protocol import ErrorReport, InstrumentStatus
from pymodaq_plugins_quantumnorthwest.hardware.qnw_tc import DEFAULT_BAUDRATE, QnwTC, open_port, serial_ports


class TracedPort:
    """A port which shows every byte which goes through it (``>>`` sent, ``<<`` received) with the time"""

    def __init__(self, port, show: Callable[[str], None], clock: Callable[[], float] = time.monotonic):
        self._port = port
        self._show = show
        self._clock = clock
        self._start = clock()

    def _trace(self, arrow: str, data: bytes):
        self._show(f'  {self._clock() - self._start:8.3f} s {arrow} {data!r}')

    def write(self, data):
        self._trace('>>', bytes(data))
        return self._port.write(data)

    def read(self, size=1):
        data = self._port.read(size)
        if data:
            self._trace('<<', data)
        return data

    def __getattr__(self, name):  # in_waiting, flush, reset_input_buffer, close...
        return getattr(self._port, name)


def parser() -> argparse.ArgumentParser:
    arguments = argparse.ArgumentParser(
        prog='qnw_tc_check', description='Connect a Quantum Northwest temperature controller (TC 125, TC 1, TC 1B) and '
                                         'show what it says. Nothing is set on it.')
    arguments.add_argument('port', nargs='?',
                           help='the serial port (/dev/ttyUSB0, COM3, socket://host:port, sim://tc125): the serial '
                                'ports are listed without it')
    arguments.add_argument('--baudrate', type=int, default=DEFAULT_BAUDRATE, help='19200 for these controllers')
    arguments.add_argument('--timeout', type=float, default=1., help='s, how long an answer is waited for (1)')
    arguments.add_argument('--trace', action='store_true', help='show every byte sent and received')
    arguments.add_argument('--watch', type=float, default=0., metavar='SECONDS',
                           help='then read the temperatures every second for this long')
    return arguments


def describe(value) -> str:
    """What a reading of the controller is, for a person"""
    if value is None:
        return 'not available'
    if isinstance(value, float):
        return f'{value:.2f} °C'
    if isinstance(value, InstrumentStatus):
        return (f"{value.state}: control {'on' if value.control else 'off'}, stirrer "
                f"{'on' if value.stirring else 'off'}, temperature {'stable' if value.stable else 'changing'}, "
                f"{value.errors} error(s) not reported")
    if isinstance(value, ErrorReport):
        return value.text
    return str(value)


def timed(call: Callable):
    """The result of a call (or its error, as a text) and its duration (s)"""
    start = time.perf_counter()
    try:
        value = call()
    except QnwError as e:
        value = f'ERROR: {e}'
    return value, time.perf_counter() - start


def main(argv: Optional[Sequence[str]] = None, out: Callable[[str], None] = print) -> int:
    """Run the check, ``out`` shows each line. Returns the exit code: 0, or 1 if the controller cannot be connected"""
    args = parser().parse_args(argv)
    if not args.port:
        ports = serial_ports()
        out('Serial ports: ' + (', '.join(ports) if ports else 'none found'))
        out('Give one as argument (qnw_tc_check PORT), or sim://tc125 for a simulated controller.')
        return 0

    def factory(port, baudrate):
        opened = open_port(port, baudrate)
        return TracedPort(opened, out) if args.trace else opened

    tc = QnwTC(args.port, baudrate=args.baudrate, timeout=args.timeout, port_factory=factory)
    out(f'Connecting {args.port} at {args.baudrate} baud (an answer is waited for {args.timeout:g} s)...')
    start = time.perf_counter()
    try:
        tc.open()
    except QnwError as e:
        out(f'FAILED: {e}')
        return 1
    out(f'Connected in {time.perf_counter() - start:.2f} s: {tc.summary()}')
    out(f'  targets from {tc.lowest_target} to {tc.highest_target} °C, stirrer speeds {tc.stirrer_speeds}')
    durations = []
    try:
        for label, call in (('holder', tc.holder_temperature), ('probe', tc.probe_temperature),
                            ('heat exchanger', tc.heat_exchanger_temperature), ('target', tc.target),
                            ('status', tc.status), ('error', tc.error)):
            if label == 'error' and not tc.can_report_errors:
                out(f'  {label:15} the controller does not answer [F1 ER ?]: it only counts its errors (status)')
                continue
            value, duration = timed(call)
            durations.append(duration)
            out(f'  {label:15} {describe(value)}   ({duration * 1000:.0f} ms)')
        if args.watch > 0:
            out(f'Reading the temperatures every second for {args.watch:g} s (Ctrl-C to stop)...')
            end = time.monotonic() + args.watch
            try:
                while time.monotonic() < end:
                    holder, duration = timed(tc.holder_temperature)
                    durations.append(duration)
                    probe, duration = timed(tc.probe_temperature)
                    durations.append(duration)
                    status, _ = timed(tc.status)
                    out(f'  holder {describe(holder)}   probe {describe(probe)}   '
                        f'{getattr(status, "state", status)}')
                    time.sleep(1.)
            except KeyboardInterrupt:
                out('Stopped.')
        out(f'Answers took {min(durations) * 1000:.0f} to {max(durations) * 1000:.0f} ms '
            f'(the plugin waits {args.timeout:g} s: raise "Reply timeout" if it is close).')
    finally:
        tc.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
