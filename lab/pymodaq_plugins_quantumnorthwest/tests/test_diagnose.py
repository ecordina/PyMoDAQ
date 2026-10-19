import pytest

from pymodaq_plugins_quantumnorthwest.hardware import diagnose
from pymodaq_plugins_quantumnorthwest.hardware.simulator import open_simulated


def run(*argv):
    lines = []
    code = diagnose.main(list(argv), out=lines.append)
    return code, '\n'.join(lines)


class TestCheck:
    @pytest.mark.parametrize('model, firmware', [('tc125', 'firmware 9.1'), ('tc1b', 'firmware 2.6.04')])
    def test_a_controller(self, model, firmware):
        code, text = run(f'sim://{model}', '--timeout', '0.5')
        assert code == 0
        assert 'Connected' in text and firmware in text
        for label in ('holder', 'probe', 'heat exchanger', 'target', 'status', 'error'):
            assert label in text
        assert 'holder          22.00 °C' in text and 'no error' in text and 'Answers took' in text
        assert 'off: control off, stirrer off, temperature changing, 0 error(s) not reported' in text

    def test_the_raw_traffic(self):
        code, text = run('sim://tc125', '--trace', '--timeout', '0.5')
        assert code == 0
        assert ">> b'[F1 VN ?]'" in text and "<< b'[F1 VN 9.1]'" in text

    def test_nothing_is_set(self):
        sent = []
        original = diagnose.open_port

        def spy(port, baudrate):
            opened = original(port, baudrate)
            write = opened.write
            opened.write = lambda data: (sent.append(bytes(data)), write(data))[1]
            return opened

        diagnose.open_port = spy
        try:
            run('sim://tc1b', '--timeout', '0.5')
        finally:
            diagnose.open_port = original
        assert sent
        assert not [command for command in sent if command.startswith((b'[F1 TT S', b'[F1 TC', b'[F1 SS', b'[F1 RS',
                                                                       b'[F1 RT', b'[F1 RR'))]

    def test_watching(self):
        code, text = run('sim://tc125', '--timeout', '0.5', '--watch', '1.5')
        assert code == 0 and text.count('holder 22.00 °C') >= 1 and 'Reading the temperatures' in text

    def test_a_controller_which_does_not_answer(self, monkeypatch):
        class Dead:
            def __init__(self, port, baudrate):
                self.port = open_simulated(port, baudrate)

            def __getattr__(self, name):
                return getattr(self.port, name)

            def write(self, data):
                return len(data)

        monkeypatch.setattr(diagnose, 'open_port', Dead)
        code, text = run('sim://tc125', '--timeout', '0.1')
        assert code == 1 and text.count('FAILED') == 1 and 'does not answer' in text

    def test_a_port_which_is_not_there(self):
        code, text = run('/dev/ttyDoesNotExist')
        assert code == 1 and 'FAILED' in text and 'cannot be opened' in text

    def test_without_a_port_the_ports_are_listed(self, monkeypatch):
        monkeypatch.setattr(diagnose, 'serial_ports', lambda: ['/dev/ttyUSB0', '/dev/ttyUSB1'])
        code, text = run()
        assert code == 0 and '/dev/ttyUSB0, /dev/ttyUSB1' in text and 'sim://tc125' in text

    def test_no_port_at_all(self, monkeypatch):
        monkeypatch.setattr(diagnose, 'serial_ports', lambda: [])
        assert 'none found' in run()[1]


class TestDescribe:
    def test_values(self):
        from pymodaq_plugins_quantumnorthwest.hardware.protocol import ErrorReport, InstrumentStatus
        assert diagnose.describe(22.5) == '22.50 °C'
        assert 'not available' in diagnose.describe(None)
        assert diagnose.describe(InstrumentStatus(1, True, True, True)).startswith('holding: control on, stirrer on')
        assert diagnose.describe(ErrorReport(8)).startswith('error 8, inadequate coolant')
        assert diagnose.describe('ERROR: no answer') == 'ERROR: no answer'


class TestTracedPort:
    def test_it_forwards_everything(self):
        lines = []
        port = diagnose.TracedPort(open_simulated('sim://tc125'), lines.append)
        port.write(b'[F1 VN ?]')
        assert port.in_waiting > 0
        assert port.read(100) == b'[F1 VN 9.1]'
        assert port.read(1) == b''  # (nothing: not shown)
        port.reset_input_buffer()
        port.close()
        assert not port.is_open
        assert len(lines) == 2 and lines[0].endswith(">> b'[F1 VN ?]'") and lines[1].endswith("<< b'[F1 VN 9.1]'")
