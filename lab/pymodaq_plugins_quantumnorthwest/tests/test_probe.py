"""probe_port(): is there a controller on a port? One question, nothing which sets anything, the port is closed again

The thermospectro commands use it on the USB-serial ports of the computer to find the temperature controllers.
"""
import os
import select
import time

import pytest

from pymodaq_plugins_quantumnorthwest.hardware import qnw_tc
from pymodaq_plugins_quantumnorthwest.hardware.qnw_tc import open_port, probe_port

QUERY = 'F1 VN ?'


@pytest.fixture
def opened():
    """What the factory opened, to look at it afterwards"""
    return []


@pytest.fixture
def factory(opened):
    def make(port, baudrate=19200, *args):
        port = open_port(port, baudrate, *args)
        opened.append(port)
        return port
    return make


class TestFound:
    @pytest.mark.parametrize('url, firmware', [('sim://tc125', '9.1'), ('sim://tc1b', '2.6.04'), ('sim://qpod', '9.1'),
                                               ('sim://', '9.1')])
    def test_the_firmware_of_the_controller(self, url, firmware, factory):
        assert probe_port(url, port_factory=factory) == firmware

    @pytest.mark.parametrize('url', ['sim://tc125', 'sim://tc1b', 'sim://qpod'])
    def test_one_question_and_nothing_else(self, url, factory, opened):
        probe_port(url, port_factory=factory)

        assert [port.controller.received for port in opened] == [[QUERY]]

    @pytest.mark.parametrize('url', ['sim://tc125', 'sim://tc1b', 'sim://qpod'])
    def test_the_controller_is_left_as_it_was(self, url, factory, opened):
        probe_port(url, port_factory=factory)

        controller = opened[0].controller
        assert not controller.control and not controller.stirring and controller.target == 25.
        assert controller.ramp_seconds == controller.ramp_hundredths == 0 and controller.errors == []

    def test_the_port_is_closed(self, factory, opened):
        probe_port('sim://tc125', port_factory=factory)

        assert not opened[0].is_open

    def test_it_is_quick_when_the_controller_answers(self):
        start = time.monotonic()

        probe_port('sim://tc1b')

        assert time.monotonic() - start < 0.3

    def test_what_the_controller_said_before_is_not_an_answer(self, factory):
        def with_old_news(port, baudrate):
            opened = factory(port, baudrate)
            opened.controller.inject(b'[F1 VN 0.0.0]')  # (a stale answer: dropped with the input buffer)
            return opened

        assert probe_port('sim://tc125', port_factory=with_old_news) == '9.1'

    def test_a_controller_which_talks_on_its_own(self, factory, opened):
        def chatty(port, baudrate):
            port = factory(port, baudrate)
            real_write = port.write

            def write(data):
                # (periodic reports come before the answer)
                port.controller.inject(b'[F1 CT 22.84][F1 PT 23.10][F1 IS 0-+S]')
                return real_write(data)
            port.write = write
            return port

        assert probe_port('sim://tc1b', port_factory=chatty) == '2.6.04'


class TestNotFound:
    def test_a_controller_which_does_not_answer(self, factory, opened):
        start = time.monotonic()

        assert probe_port('sim://tc125?latency=30', timeout=0.1, port_factory=factory) is None

        assert time.monotonic() - start < 1.
        assert opened[0].controller.received == [QUERY, QUERY]  # (the question was asked twice, and nothing else)
        assert not opened[0].is_open

    def test_the_default_timeout_is_short(self, factory):
        start = time.monotonic()

        assert probe_port('sim://tc125?latency=30', port_factory=factory) is None

        assert time.monotonic() - start < 1.5

    def test_the_first_question_may_be_lost(self, factory, opened):
        """The first byte of a freshly opened USB-serial port can be lost: the question is asked once more"""
        def losing(port, baudrate):
            port = factory(port, baudrate)
            real_write = port.write
            sent = []

            def write(data):
                sent.append(data)
                return len(data) if len(sent) == 1 else real_write(data)  # (the first one never arrives)
            port.write = write
            port.sent = sent
            return port

        assert probe_port('sim://tc1b', port_factory=losing) == '2.6.04'

        assert opened[0].sent == [b'[F1 VN ?]', b'[F1 VN ?]']

    def test_an_answer_which_is_late_is_taken(self, factory):
        assert probe_port('sim://tc125?latency=0.25', timeout=0.2, port_factory=factory) == '9.1'

    def test_a_line_which_sends_back_what_it_gets_is_not_a_controller(self):
        pytest.importorskip('serial')

        assert probe_port('loop://', timeout=0.1) is None

    def test_something_else_which_talks_in_brackets(self, factory):
        def other(port, baudrate):
            port = factory(port, baudrate)
            port.controller.receive = lambda data: port.controller.inject(  # (it does not answer the question)
                b'[HELLO WORLD][F1 XX 1][F2 VN 5.5][F1 VN]')
            return port

        assert probe_port('sim://tc125', timeout=0.1, port_factory=other) is None

    def test_the_other_units_are_not_the_controller(self, factory):
        def other(port, baudrate):
            port = factory(port, baudrate)
            port.controller.receive = lambda data: port.controller.inject(b'[R1 VN 3.0]')  # (the reference holder)
            return port

        assert probe_port('sim://tc125', timeout=0.1, port_factory=other) is None


class TestNeverRaises:
    def test_a_port_which_cannot_be_opened(self):
        problems = []

        def held(port, baudrate):
            raise PermissionError(13, 'Access is denied.')

        assert probe_port('COM5', port_factory=held, problems=problems) is None

        assert len(problems) == 1 and 'COM5' in problems[0] and 'cannot be opened' in problems[0]
        assert 'Access is denied' in problems[0]

    def test_a_port_which_is_not_there(self):
        problems = []

        assert probe_port('/dev/ttyDoesNotExist', problems=problems) is None

        assert 'cannot be opened' in problems[0]

    def test_a_port_which_breaks_when_it_is_written_to(self, factory, opened):
        problems = []

        def broken(port, baudrate):
            port = factory(port, baudrate)

            def write(data):
                raise OSError('The device does not recognize the command')
            port.write = write
            return port

        assert probe_port('sim://tc125', port_factory=broken, problems=problems) is None

        assert 'cannot be used' in problems[0] and not opened[0].is_open  # (and it was closed)

    def test_a_port_which_breaks_when_it_is_read(self, factory, opened):
        def broken(port, baudrate):
            port = factory(port, baudrate)
            port.read = lambda size=1: (_ for _ in ()).throw(OSError('unplugged'))
            return port

        assert probe_port('sim://tc125', port_factory=broken) is None
        assert not opened[0].is_open

    def test_a_port_which_cannot_be_closed(self, factory):
        def stuck(port, baudrate):
            port = factory(port, baudrate)

            def close():
                raise OSError('stuck')
            port.close = close
            return port

        assert probe_port('sim://tc125', port_factory=stuck) == '9.1'

    def test_no_problems_list_is_needed(self):
        def held(port, baudrate):
            raise PermissionError('busy')

        assert probe_port('COM5', port_factory=held) is None

    def test_a_port_which_is_not_a_port(self):
        assert probe_port('sim://nothing') is None  # (no such simulated model)


class TestOnARealPort:
    """A real pyserial port: the other end is a pseudo-terminal where a simulated controller answers"""

    @pytest.fixture
    def pty_end(self):
        pty = pytest.importorskip('pty')
        import threading
        from pymodaq_plugins_quantumnorthwest.hardware.simulator import parse_url

        master, slave = pty.openpty()
        controller = parse_url('sim://tc1b')
        halt = threading.Event()
        state = {'muted': False}

        def run():
            while not halt.is_set():
                if select.select([master], [], [], 0.005)[0]:
                    data = os.read(master, 1024)
                    if data and not state['muted']:
                        controller.receive(data)
                out = controller.take(4096)
                if out and not state['muted']:
                    os.write(master, out)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        yield os.ttyname(slave), controller, state
        halt.set()
        thread.join(5)
        os.close(master)
        os.close(slave)

    def test_it_finds_the_controller(self, pty_end):
        name, controller, _ = pty_end

        assert probe_port(name) == '2.6.04'

        assert controller.received == [QUERY]

    def test_it_finds_nothing_when_nobody_answers(self, pty_end):
        name, controller, state = pty_end
        state['muted'] = True
        start = time.monotonic()

        assert probe_port(name, timeout=0.2) is None

        assert time.monotonic() - start < 1.5

    def test_the_port_is_free_again_afterwards(self, pty_end):
        name, _, _ = pty_end

        probe_port(name)

        with qnw_tc.QnwTC(name, timeout=1.) as tc:  # (the probe held the port exclusively: it has let go of it)
            assert tc.firmware == '2.6.04'

    def test_a_port_which_another_program_holds(self, pty_end):
        serial = pytest.importorskip('serial')
        if os.name != 'posix':
            pytest.skip('an exclusive lock is a posix feature')
        name, _, _ = pty_end
        problems = []
        holder = serial.serial_for_url(name, exclusive=True)
        try:
            assert probe_port(name, problems=problems) is None
        finally:
            holder.close()

        assert len(problems) == 1 and 'cannot be opened' in problems[0]
