"""The driver on real ports of pyserial (a pseudo-terminal, a TCP socket, a loopback), not on the simulated port

A thread plays the controller at the other end of the line: it feeds the bytes the driver writes to the simulated
controller, and writes its answers back. What this checks is the way the driver uses ``serial.Serial``
(``in_waiting``, ``read`` with a timeout, ``write``, ``flush``), which the simulated port only imitates.
"""
import os
import select
import socket
import threading

import pytest

serial = pytest.importorskip('serial')

from pymodaq_plugins_quantumnorthwest.hardware.errors import QnwConnectionError  # noqa: E402
from pymodaq_plugins_quantumnorthwest.hardware.qnw_tc import QnwTC  # noqa: E402
from pymodaq_plugins_quantumnorthwest.hardware.simulator import parse_url  # noqa: E402


class Line(threading.Thread):
    """The controller at the end of a line: ``receive()`` gives the bytes of the computer or None, ``send()`` writes"""

    def __init__(self, url, receive, send):
        super().__init__(daemon=True)
        self.controller = parse_url(url)
        self.muted = False
        self._receive, self._send = receive, send
        self._halt = threading.Event()

    def run(self):
        while not self._halt.is_set():
            data = self._receive()
            if data and not self.muted:
                self.controller.receive(data)
            out = self.controller.take(4096)
            if out and not self.muted:
                self._send(out)

    def stop(self):
        self._halt.set()
        self.join(5)


@pytest.fixture
def pty_line():
    pty = pytest.importorskip('pty')
    master, slave = pty.openpty()
    line = Line('sim://tc1b',
                lambda: os.read(master, 1024) if select.select([master], [], [], 0.005)[0] else None,
                lambda data: os.write(master, data))
    line.start()
    yield os.ttyname(slave), line
    line.stop()
    os.close(master)
    os.close(slave)


@pytest.fixture
def socket_line():
    server = socket.socket()
    server.bind(('127.0.0.1', 0))
    server.listen(1)

    class SocketLine(Line):
        connection = None

        def run(self):
            self.connection, _ = server.accept()
            self.connection.settimeout(0.005)
            super().run()

    def receive():
        try:
            data = line.connection.recv(1024)
        except OSError:  # (nothing yet: a timeout)
            return None
        if not data:
            line._halt.set()
        return data

    line = SocketLine('sim://tc1b', receive, lambda data: line.connection.sendall(data))
    line.start()
    yield f'socket://127.0.0.1:{server.getsockname()[1]}', line
    line.stop()
    if line.connection is not None:
        line.connection.close()
    server.close()


@pytest.fixture(params=['pty_line', 'socket_line'])
def real_line(request):
    return request.getfixturevalue(request.param)


class TestRealPorts:
    def test_it_is_a_real_port(self, real_line):
        url, line = real_line
        with QnwTC(url, timeout=1.) as tc:
            assert isinstance(tc._port, serial.SerialBase)
            assert tc.firmware == '2.6.04' and tc.holder_id == 114 and tc.has_probe

    def test_a_session(self, real_line):
        url, line = real_line
        with QnwTC(url, timeout=1.) as tc:
            assert tc.holder_temperature() == pytest.approx(22.)
            tc.set_target(31.5)
            tc.ensure_control()
            line.controller.skip(600)
            assert tc.target() == 31.5
            assert tc.holder_temperature() == pytest.approx(31.5, abs=0.05)
            assert tc.status().state == 'holding'
            tc.set_stirrer(True, 900)
            assert line.controller.stir_speed == 900
            tc.set_ramp_rate(0.5)  # (no answer to wait for: a question after it, which the controller answers
            tc.status()  # after having read the commands, makes sure it has read them)
            assert line.controller.ramp_rate == pytest.approx(0.5)

    def test_what_the_controller_says_on_its_own(self, real_line):
        url, line = real_line
        with QnwTC(url, timeout=1.) as tc:
            line.controller.inject(b'[F1 PT 27.40][F1 TT 40.00]')
            assert tc.holder_temperature() == pytest.approx(22.)
            assert tc.last['PT'].value == 27.4 and tc.last['TT'].value == 40.

    def test_a_controller_which_does_not_answer(self, real_line):
        url, line = real_line
        line.muted = True
        with pytest.raises(QnwConnectionError, match='does not answer'):
            QnwTC(url, timeout=0.2).open()

    def test_a_controller_which_stops_answering(self, real_line):
        from pymodaq_plugins_quantumnorthwest.hardware.errors import QnwTimeout
        url, line = real_line
        with QnwTC(url, timeout=0.2) as tc:
            line.muted = True
            with pytest.raises(QnwTimeout):
                tc.holder_temperature()
            line.muted = False
            assert tc.holder_temperature() == pytest.approx(22.)

    def test_the_port_is_closed(self, real_line):
        url, line = real_line
        tc = QnwTC(url, timeout=1.)
        tc.open()
        port = tc._port
        tc.close()
        assert not port.is_open


class TestWiring:
    def test_the_connection_which_is_closed_by_the_other_end(self, socket_line):
        url, line = socket_line
        tc = QnwTC(url, timeout=0.5)
        tc.open()
        line.stop()
        line.connection.close()
        with pytest.raises(QnwConnectionError):
            tc.holder_temperature()
        tc.close()

    def test_nothing_is_listening(self):
        server = socket.socket()
        server.bind(('127.0.0.1', 0))
        port = server.getsockname()[1]
        server.close()
        with pytest.raises(QnwConnectionError, match='cannot be opened'):
            QnwTC(f'socket://127.0.0.1:{port}', timeout=0.2).open()

    def test_a_loopback_is_not_a_controller(self):
        with pytest.raises(QnwConnectionError, match='does not answer'):
            QnwTC('loop://', timeout=0.2).open()

    def test_a_port_which_is_not_there(self):
        with pytest.raises(QnwConnectionError, match='cannot be opened'):
            QnwTC('/dev/ttyDoesNotExist', timeout=0.2).open()
