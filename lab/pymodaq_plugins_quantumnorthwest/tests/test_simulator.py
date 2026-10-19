"""The simulated controller follows the manuals: what the tests of the driver and of the plugin rely on"""
import re
import time

import pytest

from pymodaq_plugins_quantumnorthwest.hardware import simulator
from pymodaq_plugins_quantumnorthwest.hardware.protocol import MessageParser
from pymodaq_plugins_quantumnorthwest.hardware.simulator import open_simulated, parse_url


class Bench:
    """A computer which talks to a simulated controller by hand"""

    def __init__(self, url='sim://tc125'):
        self.serial = open_simulated(url)
        self.sim = self.serial.controller
        self.parser = MessageParser()

    def listen(self):
        messages = []
        while self.serial.in_waiting:
            messages += self.parser.feed(self.serial.read(self.serial.in_waiting))
        return messages

    def send(self, data):
        self.serial.write(data if isinstance(data, bytes) else data.encode())
        return self.listen()

    def say(self, text, unit='F1'):
        """Send [F1 text], what the controller says as text, like ``['F1 CT 22.00']``"""
        return [m.raw for m in self.send(f'[{unit} {text}]')]


@pytest.fixture
def tc125():
    return Bench('sim://tc125')


@pytest.fixture
def tc1b():
    return Bench('sim://tc1b')


@pytest.fixture(params=['sim://tc125', 'sim://tc1b'], ids=['tc125', 'tc1b'])
def bench(request):
    return Bench(request.param)


class TestIdentity:
    def test_tc125(self, tc125):
        assert tc125.say('VN ?') == ['F1 VN 9.1']
        assert tc125.say('ID ?') == ['F1 ID 11']
        assert tc125.say('MT ?') == ['F1 MT 105']
        assert tc125.say('LT ?') == ['F1 LT -30']

    def test_tc1b(self, tc1b):
        assert tc1b.say('VN ?') == ['F1 VN 2.6.04']
        assert tc1b.say('ID ?') == ['F1 ID 114']
        assert tc1b.say('MT ?') == ['F1 MT 110']
        assert tc1b.say('LT ?') == ['F1 LT -40']

    def test_the_text_which_is_not_between_brackets_is_ignored(self, bench):
        assert [m.raw for m in bench.send(b'hello\r\n [F1 VN ?] and\x00 bye')] == [
            f'F1 VN {bench.sim.model.firmware}']

    def test_the_other_units_are_not_answered(self, bench):
        assert bench.say('CT ?', unit='R1') == []
        assert bench.say('PL ?', unit='F2') == []

    def test_commands_in_pieces(self, bench):
        bench.serial.write(b'[F1 V')
        assert bench.listen() == []
        bench.serial.write(b'N ?]')
        assert len(bench.listen()) == 1


class TestTemperatures:
    def test_current_temperature_has_hundredths(self, bench):
        assert re.fullmatch(r'F1 CT 22\.00', bench.say('CT ?')[0])

    def test_target(self, bench):
        bench.say('TT S 30.5')
        assert bench.say('TT ?') == ['F1 TT 30.50']

    def test_the_target_is_confirmed_by_the_tc1b_only(self, tc125, tc1b):
        assert tc125.say('TT S 30') == []
        assert tc1b.say('TT S 30') == ['F1 TT 30.00']

    def test_a_target_out_of_range_is_refused(self, bench):
        bench.say('TT S 500')
        bench.say('TT S -273')
        assert bench.sim.target == 25.
        assert bench.sim.errors == [9, 9]

    def test_instrument_status(self, bench):
        status, = bench.say('IS ?')
        assert re.fullmatch(r'F1 IS 0-\-C', status)  # no error, stirrer off, control off, changing

    def test_the_status_follows_the_commands(self, bench):
        bench.say('TC +')
        bench.say('SS +')
        assert bench.say('IS ?') == ['F1 IS 0++C']
        bench.say('TC -')
        assert bench.say('IS ?') == ['F1 IS 0+-C']

    def test_the_status_of_the_ramp_is_the_fifth_parameter_if_asked_for(self, tc1b):
        tc1b.say('IS E+')
        assert tc1b.say('IS ?') == ['F1 IS 0--C-']
        tc1b.say('RR S 1')
        assert tc1b.say('IS ?') == ['F1 IS 0--CW']

    def test_the_probe(self, bench):
        assert bench.say('PS ?') == ['F1 PR +']
        assert re.fullmatch(r'F1 PT 22\.00', bench.say('PT ?')[0])

    def test_no_probe(self):
        tc125, tc1b = Bench('sim://tc125?probe=0'), Bench('sim://tc1b?probe=0')
        assert tc125.say('PS ?') == tc1b.say('PS ?') == ['F1 PR -']
        assert tc125.say('PT ?') == ['F1 PT NA']  # firmware 9.1
        assert tc1b.say('PT ?') == ['F1 NOPROBE']  # firmware 2.x

    def test_the_heat_exchanger(self, bench):
        assert bench.say('HT ?') == ['F1 HT 22']
        assert bench.say('HL ?') == ['F1 HL 60']


class TestUnknownCommands:
    def test_quoted_by_the_tc1b(self, tc1b):
        assert tc1b.say('XX ?') == ['F1 ER 9 <<[F1 XX ?]>>']

    def test_code_of_the_tc125(self, tc125):
        assert tc125.say('XX ?') == ['F1 ER 09']

    def test_silent_but_counted(self):
        bench = Bench('sim://tc125?unknown=silent')
        assert bench.say('XX ?') == []
        assert bench.say('IS ?') == ['F1 IS 1--C']

    def test_errors_are_reported_one_after_the_other(self, tc1b):
        tc1b.say('XX ?')
        tc1b.say('YY ?')
        assert tc1b.say('IS ?') == ['F1 IS 2--C']
        assert tc1b.say('ER ?') == ['F1 ER 9']
        assert tc1b.say('ER ?') == ['F1 ER 9']
        assert tc1b.say('ER ?') == ['F1 ER -1']
        assert tc1b.say('IS ?') == ['F1 IS 0--C']

    @pytest.mark.parametrize('command', ['TC ?', 'SS S 1000', 'SS ?', 'MS ?', 'LS ?', 'RR ?', 'RR S 1'])
    def test_what_only_the_tc1b_knows(self, tc125, tc1b, command):
        assert tc125.say(command) == ['F1 ER 09']
        assert 'ER 9' not in ' '.join(tc1b.say(command))

    @pytest.mark.parametrize('command', ['TC +', 'TC -', 'SS +', 'SS -', 'RS S 3', 'RT S 5', 'CT -', 'PT -', 'HT -',
                                         'PA -', 'PX +', 'TT -', 'IS -'])
    def test_what_both_know(self, bench, command):
        assert bench.say(command) == []
        assert not bench.sim.errors


class TestStirrer:
    def test_on_and_off(self, bench):
        bench.say('SS +')
        assert bench.sim.stirring
        bench.say('SS -')
        assert not bench.sim.stirring

    def test_speed_of_the_tc1b(self, tc1b):
        tc1b.say('SS S 1500')
        assert tc1b.sim.stirring and tc1b.say('SS ?') == ['F1 SS 1500']
        assert tc1b.say('MS ?') == ['F1 MS 2500'] and tc1b.say('LS ?') == ['F1 LS 300']
        tc1b.say('SS S 0')
        assert not tc1b.sim.stirring
        tc1b.say('SS S 100')  # too slow
        assert tc1b.sim.errors == [9]


class TestDynamics:
    def test_the_holder_goes_to_the_target_and_gets_stable(self, bench):
        bench.say('TT S 30')
        bench.say('TC +')
        bench.sim.skip(30)
        assert 22.5 < bench.sim.temperature < 29
        assert bench.say('IS ?')[0].endswith('C')  # changing
        bench.sim.skip(400)
        assert bench.sim.temperature == pytest.approx(30., abs=0.05)
        assert bench.say('IS ?') == ['F1 IS 0-+S']

    def test_stable_only_after_a_minute_in_the_band(self, bench):
        bench.say('TT S 22')
        bench.say('TC +')
        bench.sim.skip(30)
        assert bench.say('IS ?') == ['F1 IS 0-+C']
        bench.sim.skip(40)
        assert bench.say('IS ?') == ['F1 IS 0-+S']

    def test_heating_is_limited(self, bench):
        bench.say('TT S 100')
        bench.say('TC +')
        bench.sim.skip(60)
        assert 22 + 3 < bench.sim.temperature <= 22 + 0.12 * 60 + 0.01

    def test_cooling_is_limited(self, bench):
        bench.sim.temperature = 60.
        bench.say('TT S 0')
        bench.say('TC +')
        bench.sim.skip(60)
        assert 60 - 0.08 * 60 - 0.01 <= bench.sim.temperature < 60 - 3

    def test_without_control_it_goes_back_to_the_room_temperature(self, bench):
        bench.sim.temperature = 50.
        bench.sim.skip(2000)
        assert 22 < bench.sim.temperature < 24

    def test_the_probe_follows_the_holder_with_a_delay(self, bench):
        bench.say('TT S 40')
        bench.say('TC +')
        bench.sim.skip(60)
        assert bench.sim.probe_temperature < bench.sim.temperature - 1

    def test_ramp_of_the_tc125(self, tc125):
        tc125.say('TC +')
        tc125.say('TT S 22')
        tc125.sim.skip(10)
        tc125.say('RS S 3')
        tc125.say('RT S 5')  # 1 °C/min
        assert tc125.sim.ramp_rate == pytest.approx(1.)
        tc125.say('TT S 30')
        tc125.sim.skip(180)
        assert tc125.sim.temperature == pytest.approx(25., abs=0.6)
        tc125.sim.skip(600)
        assert tc125.sim.temperature == pytest.approx(30., abs=0.1)

    def test_ramp_of_the_tc1b(self, tc1b):
        tc1b.say('TC +')
        tc1b.say('RR S 0.5')
        assert tc1b.say('RR ?') == ['F1 RR 0.50']
        tc1b.say('TT S 30')
        tc1b.sim.skip(240)
        assert tc1b.sim.temperature == pytest.approx(24., abs=0.6)

    def test_no_ramp_when_both_increments_are_zero(self, tc125):
        tc125.say('RS S 3')
        tc125.say('RT S 5')
        tc125.say('RS S 0')
        tc125.say('RT S 0')
        assert tc125.sim.ramp_rate == 0.
        tc125.say('TC +')
        tc125.say('TT S 30')
        tc125.sim.skip(100)
        assert tc125.sim.temperature > 26  # not a ramp of 1 °C/min

    def test_the_simulated_time_can_run_faster(self):
        bench = Bench('sim://tc125?speed=3600')
        bench.say('TT S 30')
        bench.say('TC +')
        time.sleep(0.15)
        assert bench.sim.simulated_time() > 300
        reply, = bench.say('CT ?')
        assert float(reply.split()[-1]) == pytest.approx(30., abs=0.5)


class TestFaults:
    def test_inadequate_coolant_shuts_the_control_down(self, tc125):
        tc125.say('TC +')
        tc125.sim.fail(8)
        assert tc125.say('IS ?') == ['F1 IS 1--C']
        assert tc125.say('ER ?') == ['F1 ER 08']
        assert tc125.say('IS ?') == ['F1 IS 0--C']
        assert tc125.say('ER ?') == ['F1 ER -1']

    def test_it_cannot_be_switched_on_again_before_it_is_repaired(self, tc1b):
        tc1b.sim.fail(8)
        tc1b.say('ER ?')
        tc1b.say('TC +')
        assert tc1b.say('IS ?') == ['F1 IS 1--C']
        assert tc1b.say('ER ?') == ['F1 ER 8']
        tc1b.sim.repair()
        tc1b.say('TC +')
        assert tc1b.say('IS ?') == ['F1 IS 0-+C']

    def test_the_heat_exchanger_gets_hot(self, tc125):
        tc125.sim.fail(8)
        assert int(tc125.say('HT ?')[0].split()[-1]) > 55

    def test_errors_which_are_reported_when_they_happen(self, tc1b):
        tc1b.say('ER +')
        tc1b.sim.fail(5)
        assert [m.raw for m in tc1b.listen()] == ['F1 ER 5']


class TestReports:
    def test_periodic_reports(self, bench):
        bench.say('CT +3')
        assert bench.listen() == []
        bench.sim.skip(4)
        assert [m.code for m in bench.listen()] == ['CT']
        bench.say('CT -')
        bench.sim.skip(10)
        assert bench.listen() == []

    def test_the_probe_and_heat_exchanger_reports(self, bench):
        bench.say('PT +3')
        bench.say('HT +3')
        bench.sim.skip(5)
        assert sorted(m.code for m in bench.listen()) == ['HT', 'PT']

    def test_a_report_can_be_started_again(self, bench):
        bench.say('CT +2')
        bench.say('CT -')
        bench.say('CT +')
        bench.sim.skip(3)
        assert [m.code for m in bench.listen()] == ['CT']

    def test_injected_bytes_come_out_as_they_are(self, bench):
        bench.sim.inject(b'\x00junk[F1 CT 1')
        bench.sim.inject(b'0.00]')
        assert [m.raw for m in bench.listen()] == ['F1 CT 10.00']

    def test_latency(self):
        bench = Bench('sim://tc125?latency=0.15')
        bench.serial.write(b'[F1 VN ?]')
        assert bench.serial.in_waiting == 0
        time.sleep(0.2)
        assert bench.serial.in_waiting > 0


class TestSerialPort:
    def test_read_waits_for_the_timeout(self):
        serial = open_simulated('sim://', timeout=0.1)
        start = time.perf_counter()
        assert serial.read(1) == b''
        assert 0.08 < time.perf_counter() - start < 0.5

    def test_a_closed_port_cannot_be_used(self):
        serial = open_simulated('sim://')
        serial.close()
        assert not serial.is_open
        for call in (lambda: serial.write(b'[F1 VN ?]'), lambda: serial.in_waiting, lambda: serial.read(1)):
            with pytest.raises(OSError):
                call()

    def test_reset_input_buffer(self):
        bench = Bench()
        bench.serial.write(b'[F1 VN ?]')
        bench.serial.reset_input_buffer()
        assert bench.serial.in_waiting == 0

    def test_read_gives_what_is_asked_for(self):
        bench = Bench()
        bench.serial.write(b'[F1 VN ?]')
        assert bench.serial.read(3) == b'[F1'
        assert bench.serial.read(100) == b' VN 9.1]'


class TestUrl:
    def test_the_models(self):
        assert parse_url('sim://').model is simulator.TC125
        assert parse_url('sim://tc125').model is simulator.TC125
        assert parse_url('SIM://TC1B').model is simulator.TC1B
        assert parse_url('sim:tc1b').model is simulator.TC1B

    def test_options(self):
        sim = parse_url('sim://tc1b?speed=60&probe=0&latency=0.25&unknown=silent')
        assert sim.speed == 60 and not sim.has_probe and sim.latency == 0.25 and sim.model.unknown == 'silent'
        assert sim.model.name == 'TC 1B' and sim.model.firmware == '2.6.04'

    def test_defaults(self):
        sim = parse_url('sim://tc125')
        assert sim.speed == 1 and sim.has_probe and sim.latency == 0 and sim.model.unknown == 'code'

    @pytest.mark.parametrize('url', ['sim://tc999', 'sim://tc125?color=red', 'sim://tc125?unknown=shout',
                                     'sim://tc125?speed=fast'])
    def test_wrong_urls(self, url):
        with pytest.raises(ValueError):
            parse_url(url)

    def test_is_simulated(self):
        assert simulator.is_simulated('sim://tc125') and simulator.is_simulated(' SIM://')
        assert not simulator.is_simulated('/dev/ttyUSB0') and not simulator.is_simulated('COM3')
