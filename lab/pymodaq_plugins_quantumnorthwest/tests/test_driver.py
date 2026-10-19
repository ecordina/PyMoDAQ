"""The driver against the simulated TC 125 and TC 1B, with the things which go wrong on a real line"""
import threading
import time

import pytest

from pymodaq_plugins_quantumnorthwest.hardware.errors import (QnwConnectionError, QnwError, QnwLimitError,
                                                              QnwRejected, QnwTimeout)
from pymodaq_plugins_quantumnorthwest.hardware.protocol import (InstrumentStatus, Message, QnwProtocolError,
                                                                UNREADABLE_ERROR)
from pymodaq_plugins_quantumnorthwest.hardware.qnw_tc import QnwTC, open_port, serial_ports
from pymodaq_plugins_quantumnorthwest.hardware.simulator import SimulatedSerial, parse_url

from helpers import simulation


class FakeClock:
    def __init__(self):
        self.now = 1000.

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Dribble(SimulatedSerial):
    """A port which gives one byte at a time"""

    @property
    def in_waiting(self):
        return min(1, super().in_waiting)

    def read(self, size=1):
        return super().read(1)


class Dead(SimulatedSerial):
    """A port with nothing behind it"""

    def write(self, data):
        return len(data)


class Echo(SimulatedSerial):
    """A port which sends back what it receives (a loopback plug)"""

    def write(self, data):
        self.controller.inject(data)
        return len(data)


def factory(serial_class, url='sim://tc125'):
    def make(port, baudrate):
        return serial_class(parse_url(url), port, baudrate)
    return make


def sent(tc):
    """The commands the controller received"""
    return simulation(tc).received


class TestOpening:
    def test_what_is_found(self, make_tc, model_url):
        tc = make_tc(model_url)
        sim = simulation(tc).model
        assert tc.is_open
        assert tc.firmware == sim.firmware
        assert tc.holder_id == sim.holder_id
        assert (tc.lowest_target, tc.highest_target) == (sim.lowest, sim.highest)
        assert tc.has_probe is True
        assert (tc.stirrer_speeds is not None) == sim.stirrer_speed

    def test_the_names(self, make_tc):
        assert make_tc('sim://tc125').holder_name == 'single cuvette holder with probe capability'
        assert 'TC 1B' not in make_tc('sim://tc1b').summary()
        assert make_tc('sim://tc1b').summary() == 'firmware 2.6.04, single cuvette holder (ID 114), probe connected'

    def test_stirrer_speeds(self, make_tc):
        assert make_tc('sim://tc1b').stirrer_speeds == (300., 2500.)
        assert make_tc('sim://tc125').stirrer_speeds is None

    def test_no_error_is_left_by_what_the_driver_asked(self, make_tc, model_url):
        """The commands which a controller does not know are errors it counts: they are read when it is opened"""
        tc = make_tc(model_url)
        assert tc.status().errors == 0

    @pytest.mark.parametrize('unknown', ['quote', 'code', 'silent'])
    def test_whatever_a_controller_does_with_what_it_does_not_know(self, make_tc, unknown):
        start = time.perf_counter()
        tc = make_tc(f'sim://tc125?unknown={unknown}')
        assert tc.firmware == '9.1' and tc.stirrer_speeds is None
        assert tc.status().errors == 0
        if unknown != 'silent':  # (it says so: no need to wait for the answers which do not come)
            assert time.perf_counter() - start < 0.4

    def test_the_reports_which_were_left_on_are_stopped(self, make_tc):
        tc = make_tc('sim://tc125')
        assert {'CT -', 'PT -', 'HT -', 'PA -'} <= {m.split(' ', 1)[1] for m in sent(tc) if m.startswith('F1 ')}

    def test_the_reports_of_the_status_are_stopped_too(self, make_tc):
        assert 'F1 IS -' in sent(make_tc('sim://tc125')) and 'F1 IS -' in sent(make_tc('sim://tc1b'))

    def test_only_commands_of_the_manual_are_sent_to_a_tc125(self, make_tc):
        """Firmware 9 (TC 125, 225, 425): the speed of the stirrer is set by hand, its manual has no command for it"""
        commands = sent(make_tc('sim://tc125'))
        assert 'F1 LS ?' not in commands and 'F1 MS ?' not in commands
        assert all(command.split(' ')[1] in {'VN', 'ID', 'MT', 'LT', 'PS', 'CT', 'PT', 'HT', 'IS', 'PA', 'PX', 'ER'}
                   for command in commands), commands
        assert 'F1 LS ?' in sent(make_tc('sim://tc1b')) and 'F1 MS ?' in sent(make_tc('sim://tc1b'))

    def test_a_controller_of_another_firmware_which_does_not_know_the_speed(self, make_tc, monkeypatch):
        """Firmware 1.0 without the speed of the stirrer: the questions are asked, not understood, and their errors
        are read when opening"""
        from dataclasses import replace
        from pymodaq_plugins_quantumnorthwest.hardware import simulator
        monkeypatch.setitem(simulator.MODELS, 'old', replace(simulator.TC125, firmware='1.0'))
        tc = make_tc('sim://old')
        assert 'F1 LS ?' in sent(tc) and tc.stirrer_speeds is None
        assert tc.status().errors == 0

    def test_the_probe_precision_is_asked_for_only_with_a_probe(self, make_tc):
        assert 'F1 PX +' in sent(make_tc('sim://tc125'))
        assert 'F1 PX +' not in sent(make_tc('sim://tc125?probe=0'))

    def test_no_probe(self, make_tc, model_url):
        tc = make_tc(model_url + '?probe=0')
        assert tc.has_probe is False and tc.probe_temperature() is None

    def test_open_twice(self, make_tc):
        tc = make_tc()
        tc.open()
        assert tc.is_open

    def test_context_manager(self):
        with QnwTC('sim://tc125', timeout=0.5) as tc:
            assert tc.is_open
        assert not tc.is_open

    def test_close_and_open_again(self, make_tc):
        tc = make_tc('sim://tc1b')
        tc.close()
        assert not tc.is_open
        tc.open()
        assert tc.firmware == '2.6.04'

    def test_nothing_can_be_done_when_it_is_closed(self, make_tc):
        tc = make_tc()
        tc.close()
        with pytest.raises(QnwConnectionError):
            tc.holder_temperature()
        tc.close()  # (again: nothing)

    def test_a_controller_which_does_not_answer(self):
        tc = QnwTC('sim://dead', timeout=0.2, port_factory=factory(Dead))
        start = time.perf_counter()
        with pytest.raises(QnwConnectionError, match=r'does not answer on sim://dead at 19200 baud.*serial'):
            tc.open()
        assert 0.35 < time.perf_counter() - start < 2
        assert not tc.is_open

    def test_a_controller_which_does_not_know_the_version(self, make_tc):
        class NoVersion(SimulatedSerial):
            def write(self, data):
                if data == b'[F1 VN ?]':
                    return len(data)  # (silent)
                return super().write(data)

        tc = QnwTC('sim://old', timeout=0.2, port_factory=factory(NoVersion))
        tc.open()
        assert tc.is_open and tc.firmware == '' and tc.holder_id == 11
        assert 'firmware unknown' in tc.summary()
        assert tc.holder_temperature() == pytest.approx(22.)

    def test_a_controller_which_refuses_the_version(self):
        class NoVersion(SimulatedSerial):
            def write(self, data):
                if data == b'[F1 VN ?]':
                    self.controller.inject(b'[F1 ER 09]')
                    return len(data)
                return super().write(data)

        tc = QnwTC('sim://old', timeout=0.2, port_factory=factory(NoVersion))
        tc.open()
        assert tc.is_open and tc.firmware == ''

    def test_a_controller_which_knows_nothing_but_the_temperature(self):
        class Minimal(SimulatedSerial):
            def write(self, data):
                if data in (b'[F1 VN ?]', b'[F1 ID ?]'):
                    return len(data)
                return super().write(data)

        tc = QnwTC('sim://minimal', timeout=0.2, port_factory=factory(Minimal))
        tc.open()
        assert tc.is_open and tc.holder_id is None and tc.holder_name == 'unknown sample holder'

    def test_a_line_which_sends_back_what_it_receives_is_not_a_controller(self):
        tc = QnwTC('sim://echo', timeout=0.2, port_factory=factory(Echo))
        with pytest.raises(QnwConnectionError, match='does not answer'):
            tc.open()
        assert not tc.is_open

    def test_a_port_which_cannot_be_opened(self):
        def refuse(port, baudrate):
            raise OSError(2, 'No such file or directory')

        tc = QnwTC('/dev/ttyNOWHERE', port_factory=refuse)
        with pytest.raises(QnwConnectionError, match='/dev/ttyNOWHERE cannot be opened.*No such file'):
            tc.open()
        assert not tc.is_open

    def test_a_real_port_which_is_not_there(self):
        with pytest.raises(QnwConnectionError, match='cannot be opened'):
            QnwTC('/dev/ttyNOWHERE-0').open()

    def test_the_first_command_may_be_lost(self):
        """The controller was in the middle of a message: the first question gets no answer, the second does"""
        class Late(SimulatedSerial):
            first = True

            def write(self, data):
                if self.first:
                    self.first = False
                    return len(data)
                return super().write(data)

        tc = QnwTC('sim://late', timeout=0.2, port_factory=factory(Late))
        tc.open()
        assert tc.firmware == '9.1'

    def test_the_serial_ports(self):
        ports = serial_ports()
        assert isinstance(ports, list) and all(isinstance(port, str) for port in ports)


class TestTheQpod:
    """The TC 125 of a qpod, which was connected for the first time: it refuses MT, LT and HT, and its errors do not say
    which command they are about (``[F1 ER 9]``)"""

    @pytest.mark.parametrize('latency', [0., 0.03, 0.2])
    def test_it_opens(self, make_tc, latency):
        tc = make_tc(f'sim://qpod?probe=0&latency={latency}', timeout=1.)
        assert tc.firmware == '9.1'
        assert tc.holder_id == 11
        assert tc.lowest_target is None and tc.highest_target is None
        assert tc.has_probe is False
        assert tc.status() == InstrumentStatus(0, False, False, False, None)

    def test_the_refusal_of_a_report_is_not_the_refusal_of_the_next_question(self, make_tc):
        tc = make_tc('sim://qpod?latency=0.03', timeout=1.)
        assert not tc._tell_checked('HT', '-')  # (it says so, without saying which command)
        assert tc._tell_checked('CT', '-')
        assert tc.status().errors == 0  # (the answer was not taken for a refusal of this question)

    def test_the_answer_which_comes_after_a_refusal_of_another_command_is_the_answer(self, make_tc):
        tc = make_tc('sim://qpod?latency=0.1', timeout=1.)
        simulation(tc).inject(b'[F1 ER 9]')  # (the refusal of something which was sent before)
        assert tc.status().control is False

    def test_a_refused_question_takes_its_grace_once(self, make_tc):
        tc = make_tc('sim://qpod', timeout=1.)
        begin = time.monotonic()
        assert tc._ask('MT', '?', optional=True) is None
        assert tc._ask('MT', '?', optional=True) is None
        assert time.monotonic() - begin < 0.6

    def test_a_refusal_is_an_error_for_a_question_which_is_needed(self, make_tc):
        tc = make_tc('sim://qpod', timeout=1.)
        with pytest.raises(QnwRejected, match=r'MT \?'):
            tc._ask('MT', '?')

    def test_the_heat_exchanger_is_not_told(self, make_tc):
        tc = make_tc('sim://qpod')
        assert tc.heat_exchanger_temperature() is None

    def test_it_does_not_answer_which_error_it_counts(self, make_tc):
        assert make_tc('sim://qpod').can_report_errors is False
        assert make_tc('sim://tc125').can_report_errors is True
        assert make_tc('sim://tc1b').can_report_errors is True

    def test_a_fault_is_told_once_even_if_the_controller_cannot_say_which(self, make_tc):
        tc = make_tc('sim://qpod')
        simulation(tc).faulty = True
        tc._tell('TC', '+')  # (it shuts down again: an error which it counts)
        faults = tc.check_faults()
        assert [fault.code for fault in faults] == [UNREADABLE_ERROR]
        assert 'cannot say which' in faults[0].text
        assert tc.check_faults() == []  # (the count is still there: it is not told again)
        tc._tell('TC', '+')
        assert [fault.code for fault in tc.check_faults()] == [UNREADABLE_ERROR]  # (a new one is)

    def test_it_can_be_driven(self, make_tc):
        tc = make_tc('sim://qpod?speed=60')
        tc.set_target(30.)
        assert tc.target() == pytest.approx(30.)
        assert tc.ensure_control() is True
        assert tc.status().control is True
        assert tc.holder_temperature() == pytest.approx(22., abs=1.)
        tc.set_control(False)
        assert tc.status().control is False


class TestTheLine:
    """Replies like they come on a real line"""

    def test_one_byte_at_a_time(self, model_url):
        tc = QnwTC(model_url, timeout=1., port_factory=factory(Dribble, model_url))
        tc.open()
        assert tc.holder_temperature() == pytest.approx(22.)
        assert tc.status().errors == 0

    def test_garbage_before_the_answer(self, make_tc):
        tc = make_tc()
        simulation(tc).inject(b'\x00\xff garbage [F1 CT 1')  # (a message which is cut)
        assert tc.holder_temperature() == pytest.approx(22.)

    def test_messages_which_nobody_asked_for(self, make_tc):
        tc = make_tc()
        sim = simulation(tc)
        sim.inject(b'[F1 CT 31.10][F1 PT 30.00][F1 TT 40.00][F1 TC +][F1 IS 0++S]')
        assert tc.holder_temperature() == pytest.approx(22.)  # (the one which was asked for)
        assert tc.last['PT'].value == 30. and tc.last['TT'].value == 40. and tc.last['control'].value is True

    def test_periodic_reports_left_on_by_another_program(self, make_tc):
        tc = make_tc('sim://tc1b?speed=50')
        records = []
        record = tc._record
        tc._record = lambda message: (records.append(message.code), record(message))[1]
        tc._port.write(b'[F1 CT +1]')  # (what the program which was run before left)
        for _ in range(5):
            time.sleep(0.1)
            assert 20. < tc.holder_temperature() < 40.
        assert records.count('CT') > 5  # (more than the answers: the reports were read too)

    def test_a_reply_which_came_late_is_not_taken_for_the_next_one(self, make_tc):
        tc = make_tc('sim://tc125')
        simulation(tc).latency = 0.3  # (the line gets slow)
        tc.timeout = 0.1
        with pytest.raises(QnwTimeout, match=r'\[F1 CT \?\].*0\.1 s'):
            tc.holder_temperature()
        time.sleep(0.3)  # (its answer is there now)
        tc.timeout = 1.
        assert tc.holder_temperature() == pytest.approx(22.)
        assert tc.holder_temperature() == pytest.approx(22.)  # (and nothing is left over)
        assert tc._parser.partial is False

    def test_a_timeout_does_not_break_the_driver(self, make_tc):
        tc = make_tc('sim://tc125')
        simulation(tc).latency = 0.3
        tc.timeout = 0.1
        with pytest.raises(QnwTimeout):
            tc.status()
        time.sleep(0.35)
        tc.timeout = 1.
        assert tc.status().errors == 0

    def test_the_echo_of_a_question_is_not_its_answer(self, make_tc):
        tc = make_tc()
        simulation(tc).inject(b'[F1 CT ?]')
        assert tc.holder_temperature() == pytest.approx(22.)

    def test_a_status_which_says_the_controller_was_switched_on_again(self, make_tc):
        tc = make_tc()
        simulation(tc).inject(b'[F1 IS R]')
        assert tc.status().control is False

    def test_a_message_which_cannot_be_read_is_skipped(self, make_tc):
        tc = make_tc()
        simulation(tc).inject(b'[F1 CT abc][F1 IS ###][F1 ER xyz][F1 PT ]')
        assert tc.holder_temperature() == pytest.approx(22.)

    def test_a_reply_which_is_not_a_number_is_an_error(self):
        class Garbled(SimulatedSerial):
            def write(self, data):
                if data == b'[F1 CT ?]':
                    self.controller.inject(b'[F1 CT abc]')
                    return len(data)
                return super().write(data)

        tc = QnwTC('sim://garbled', timeout=0.5, port_factory=factory(Garbled))
        tc.open()
        with pytest.raises(QnwProtocolError, match='abc'):
            tc.holder_temperature()

    def test_something_else_on_the_port(self):
        """A device which says things in brackets, but not what a controller says"""
        class Other(SimulatedSerial):
            def write(self, data):
                if data == b'[F1 IS ?]':
                    self.controller.inject(b'[F1 IS #$%]')
                    return len(data)
                return super().write(data)

        tc = QnwTC('sim://other', timeout=0.5, port_factory=factory(Other))
        with pytest.raises(QnwConnectionError, match='does not talk like a Quantum Northwest controller.*#\\$%'):
            tc.open()
        assert not tc.is_open

    def test_what_belongs_to_another_unit_is_not_ours(self, make_tc):
        tc = make_tc()
        simulation(tc).inject(b'[R1 CT 99.00]')
        assert tc.holder_temperature() == pytest.approx(22.)
        assert tc.last['CT'].value == pytest.approx(22.)

    def test_the_connection_is_lost(self, make_tc):
        tc = make_tc()
        tc._port.close()
        with pytest.raises(QnwConnectionError, match='is lost'):
            tc.holder_temperature()

    def test_the_rejection_of_a_command(self, make_tc):
        tc = make_tc('sim://tc1b')
        with pytest.raises(QnwRejected, match=r'XX \?'):
            tc._ask('XX', '?')

    @pytest.mark.parametrize('content, rejects', [
        ('F1 ER 9 <<[F1 CT ?]>>', True),
        ('F1 ER 09 <<f1  ct ?>>', True),
        ('F1 ER 9 <<[F1 ZZ ?]>>', False),  # (the rejection of another command)
        ('F1 ER 9', True),  # (not told which)
        ('F1 ER 8', False),
        ('F1 CT 22.00', False),
    ])
    def test_which_rejections_are_ours(self, content, rejects):
        assert QnwTC._rejects(Message.parse(content), '[F1 CT ?]') is rejects

    def test_an_unknown_question_is_asked_once(self, make_tc):
        tc = make_tc('sim://tc125?unknown=silent', timeout=0.2)
        assert tc._ask('XX', '?', optional=True, timeout=0.05) is None
        count = len(sent(tc))
        assert tc._ask('XX', '?', optional=True, timeout=0.05) is None
        assert len(sent(tc)) == count


class TestReading:
    def test_holder_temperature(self, make_tc, model_url):
        tc = make_tc(model_url)
        simulation(tc).temperature = 37.25
        assert tc.holder_temperature() == pytest.approx(37.25, abs=0.01)

    def test_a_recent_reading_is_not_read_again(self, model_url):
        clock = FakeClock()
        tc = QnwTC(model_url, timeout=0.5, clock=clock)
        tc.open()
        first = tc.holder_temperature()
        asked = len(sent(tc))
        simulation(tc).temperature = 50.
        assert tc.holder_temperature(max_age=0.5) == first
        assert len(sent(tc)) == asked
        clock.advance(0.6)
        assert tc.holder_temperature(max_age=0.5) == pytest.approx(50., abs=0.01)
        assert tc.holder_temperature() == pytest.approx(50., abs=0.01)  # (no max_age: always read)

    def test_probe_temperature(self, make_tc, model_url):
        tc = make_tc(model_url)
        simulation(tc).probe_temperature = 31.5
        assert tc.probe_temperature() == pytest.approx(31.5, abs=0.01)

    def test_a_probe_which_is_removed(self, make_tc, model_url):
        tc = make_tc(model_url)
        simulation(tc).has_probe = False
        assert tc.probe_temperature() is None
        assert tc.last['PT'].value is None

    def test_heat_exchanger_temperature(self, make_tc, model_url):
        tc = make_tc(model_url)
        assert tc.heat_exchanger_temperature() == 22.

    def test_target(self, make_tc, model_url):
        assert make_tc(model_url).target() == 25.

    def test_status(self, make_tc, model_url):
        tc = make_tc(model_url)
        assert tc.status() == InstrumentStatus(0, False, False, False)
        simulation(tc).control = True
        simulation(tc).stirring = True
        assert tc.status() == InstrumentStatus(0, True, True, False)
        assert tc.status().state == 'seeking'

    def test_the_status_of_the_ramp_when_it_is_in_the_reply(self, make_tc):
        tc = make_tc('sim://tc1b')
        simulation(tc).show_ramp = True
        assert tc.status().ramp == '-'

    def test_the_last_things_the_controller_said(self, make_tc, model_url):
        tc = make_tc(model_url)
        tc.holder_temperature()
        tc.status()
        assert tc.last['CT'].value == pytest.approx(22.) and tc.last['IS'].value.errors == 0
        assert tc.last['VN'].value == simulation(tc).model.firmware


class TestTarget:
    def test_set_target(self, make_tc, model_url):
        tc = make_tc(model_url)
        tc.set_target(30.256)
        assert 'F1 TT S 30.26' in sent(tc)
        assert simulation(tc).target == pytest.approx(30.26)
        assert tc.target() == pytest.approx(30.26)

    def test_it_does_not_switch_the_control_on(self, make_tc, model_url):
        tc = make_tc(model_url)
        tc.set_target(30.)
        assert tc.status().control is False

    @pytest.mark.parametrize('value', [-31, 105.5, 1000, float('nan'), float('inf')])
    def test_a_target_which_the_holder_does_not_take(self, make_tc, value):
        tc = make_tc('sim://tc125')
        with pytest.raises(QnwLimitError):
            tc.set_target(value)
        assert not any(m.startswith('F1 TT S') for m in sent(tc))

    def test_the_limits_are_the_ones_of_the_controller(self, make_tc):
        make_tc('sim://tc1b').set_target(-40.)  # (-30 for the TC 125)
        with pytest.raises(QnwLimitError, match=r'-30 to 105'):
            make_tc('sim://tc125').set_target(-40.)

    def test_the_limits_can_be_unknown(self, make_tc):
        tc = make_tc('sim://tc125')
        tc.lowest_target = tc.highest_target = None
        tc.set_target(500.)  # (it is the controller which will refuse)
        assert 'F1 TT S 500.00' in sent(tc)

    def test_the_same_target_is_not_sent_again_at_once(self):
        clock = FakeClock()
        tc = QnwTC('sim://tc125', timeout=0.5, clock=clock)
        tc.open()
        tc.set_target(30.)
        tc.set_target(30.001)  # (the same, to the hundredth)
        tc.set_target(30.004)
        assert sent(tc).count('F1 TT S 30.00') == 1
        tc.set_target(30.01)
        assert 'F1 TT S 30.01' in sent(tc)
        clock.advance(6.)  # (a command may be lost: it is sent again after a while)
        tc.set_target(30.01)
        assert sent(tc).count('F1 TT S 30.01') == 2

    def test_a_target_which_was_changed_at_the_front_panel_is_sent_again(self):
        tc = QnwTC('sim://tc1b', timeout=0.5)
        tc.open()
        tc.set_target(30.)
        simulation(tc).inject(b'[F1 TT 35.00]')  # (said by the controller: somebody turned the knob)
        tc.holder_temperature()
        tc.set_target(30.)
        assert sent(tc).count('F1 TT S 30.00') == 2

    def test_negative_targets(self, make_tc):
        tc = make_tc('sim://tc1b')
        tc.set_target(-12.5)
        assert 'F1 TT S -12.50' in sent(tc) and tc.target() == -12.5

    def test_zero_is_not_negative_zero(self, make_tc):
        tc = make_tc()
        tc.set_target(-0.0001)
        assert 'F1 TT S 0.00' in sent(tc)


class TestControl:
    def test_switch_on_and_off(self, make_tc, model_url):
        tc = make_tc(model_url)
        assert tc.set_control(True).control is True and simulation(tc).control
        assert tc.set_control(False).control is False and not simulation(tc).control

    def test_ensure_control(self, model_url):
        clock = FakeClock()
        tc = QnwTC(model_url, timeout=0.5, clock=clock)
        tc.open()
        assert tc.ensure_control() is True
        asked = len(sent(tc))
        assert tc.ensure_control() is False  # (it is known to be on)
        assert len(sent(tc)) == asked
        simulation(tc).control = False  # (switched off by hand)
        clock.advance(6.)
        assert tc.ensure_control() is True and simulation(tc).control

    def test_ensure_control_when_it_is_already_on(self, make_tc):
        tc = make_tc()
        simulation(tc).control = True
        assert tc.ensure_control() is False
        assert not any(m == 'F1 TC +' for m in sent(tc))

    def test_a_fault_keeps_it_off(self, make_tc, model_url):
        tc = make_tc(model_url)
        simulation(tc).fail(8)
        with pytest.raises(QnwError, match='inadequate coolant'):
            tc.set_control(True)
        assert tc.fault is not None and tc.fault.code == 8

    def test_switched_on_again_after_the_repair(self, make_tc, model_url):
        tc = make_tc(model_url)
        simulation(tc).fail(8)
        assert [r.code for r in tc.check_faults()] == [8]
        assert tc.fault.code == 8
        simulation(tc).repair()
        tc.set_control(True)
        assert tc.fault is None

    def test_a_fault_is_reported_once(self, make_tc, model_url):
        tc = make_tc(model_url)
        simulation(tc).fail(8)
        assert [r.code for r in tc.check_faults()] == [8]
        assert tc.check_faults() == []
        assert tc.fault.code == 8  # (but it is remembered)

    def test_faults_are_not_the_commands_which_were_not_understood(self, make_tc):
        tc = make_tc('sim://tc1b')
        with pytest.raises(QnwRejected):
            tc._ask('XX', '?')
        assert tc.check_faults() == [] and tc.fault is None

    def test_the_errors_are_read_until_there_is_none(self):
        """A status which says 9 errors (a corrupted one) is not followed by 9 questions if the first says none"""
        class Nine(SimulatedSerial):
            def write(self, data):
                if data == b'[F1 IS ?]':
                    self.controller.inject(b'[F1 IS 9--C]')
                    return len(data)
                return super().write(data)

        tc = QnwTC('sim://nine', timeout=0.5, port_factory=factory(Nine))
        tc.open()
        before = sent(tc).count('F1 ER ?')
        assert tc.pending_errors() == []
        assert sent(tc).count('F1 ER ?') == before + 1

    def test_a_fault_which_was_repaired_is_forgotten(self, make_tc):
        tc = make_tc('sim://tc125')
        simulation(tc).control = True
        simulation(tc).fail(5)  # (the control goes on)
        assert [r.code for r in tc.check_faults()] == [5] and tc.fault.code == 5
        assert tc.check_faults() == [] and tc.fault is None

    def test_a_fault_which_keeps_the_control_off_is_kept(self, make_tc):
        tc = make_tc('sim://tc125')
        simulation(tc).fail(8)
        tc.check_faults()
        assert tc.check_faults() == [] and tc.fault.code == 8
        simulation(tc).repair()
        simulation(tc).control = True  # (switched on at the front panel)
        assert tc.check_faults() == [] and tc.fault is None

    def test_every_error_is_read(self, make_tc):
        tc = make_tc('sim://tc1b')
        simulation(tc).fail(5)
        simulation(tc).fail(7)
        assert [r.code for r in tc.pending_errors()] == [5, 7]
        assert tc.status().errors == 0

    def test_errors_which_are_reported_on_their_own(self, make_tc):
        tc = make_tc('sim://tc1b')
        tc._port.write(b'[F1 ER +]')
        simulation(tc).fail(6)
        tc.holder_temperature()  # (the report is read on the way)
        assert tc.fault is not None and tc.fault.code == 6


class TestStirrer:
    def test_the_stirrer_of_the_tc125_has_no_speed(self, make_tc):
        tc = make_tc('sim://tc125')
        assert tc.set_stirrer(True, 1200).stirring
        assert 'F1 SS +' in sent(tc) and not any(m.startswith('F1 SS S') for m in sent(tc))
        assert not tc.set_stirrer(False).stirring

    def test_the_stirrer_of_the_tc1b_has_one(self, make_tc):
        tc = make_tc('sim://tc1b')
        assert tc.set_stirrer(True, 1200).stirring
        assert 'F1 SS S 1200' in sent(tc) and simulation(tc).stir_speed == 1200

    def test_without_a_speed(self, make_tc):
        tc = make_tc('sim://tc1b')
        tc.set_stirrer(True)
        assert 'F1 SS +' in sent(tc)

    @pytest.mark.parametrize('speed, expected', [(10, 300), (5000, 2500), (1234.6, 1235)])
    def test_the_speed_is_the_one_the_controller_takes(self, make_tc, speed, expected):
        tc = make_tc('sim://tc1b')
        tc.set_stirrer(True, speed)
        assert simulation(tc).stir_speed == expected

    def test_a_stirrer_which_does_not_start(self, make_tc):
        tc = make_tc('sim://tc125')
        simulation(tc)._cmd_SS = lambda args: True  # (a holder without stirrer: nothing happens)
        with pytest.raises(QnwError, match='did not switch the stirrer on'):
            tc.set_stirrer(True)


class TestRamp:
    @pytest.mark.parametrize('rate, increments', [(1., (3, 5)), (0.5, (6, 5)), (0.05, (12, 1)), (10., (3, 50))])
    def test_increments(self, make_tc, model_url, rate, increments):
        tc = make_tc(model_url)
        tc.set_ramp_rate(rate)
        assert 'F1 RS S %d' % increments[0] in sent(tc) and 'F1 RT S %d' % increments[1] in sent(tc)
        assert simulation(tc).ramp_rate == pytest.approx(rate)

    def test_off(self, make_tc, model_url):
        tc = make_tc(model_url)
        tc.set_ramp_rate(1.)
        tc.set_ramp_rate(0.)
        assert simulation(tc).ramp_rate == 0. and sent(tc)[-2:] == ['F1 RS S 0', 'F1 RT S 0']

    @pytest.mark.parametrize('rate', [0.001, 11., 100.])
    def test_a_rate_out_of_range(self, make_tc, rate):
        tc = make_tc()
        with pytest.raises(QnwLimitError, match='ramp rate'):
            tc.set_ramp_rate(rate)

    def test_the_holder_follows_the_ramp(self, make_tc, model_url):
        tc = make_tc(model_url)
        tc.set_control(True)
        tc.set_target(22.)
        simulation(tc).skip(60)
        tc.set_ramp_rate(1.)
        tc.set_target(30.)
        simulation(tc).skip(180)
        assert tc.holder_temperature() == pytest.approx(25., abs=0.6)

    def test_hold(self, make_tc, model_url):
        tc = make_tc(model_url)
        tc.set_control(True)
        tc.set_ramp_rate(1.)
        tc.set_target(60.)
        simulation(tc).skip(120)
        temperature = tc.hold()
        assert 23. < temperature < 26.
        assert simulation(tc).target == pytest.approx(temperature, abs=0.006) and simulation(tc).ramp_rate == 0.
        simulation(tc).skip(300)
        assert tc.holder_temperature() == pytest.approx(temperature, abs=0.1)


class TestThreads:
    def test_the_conversations_do_not_mix(self, make_tc, model_url):
        tc = make_tc(model_url, timeout=2.)
        simulation(tc).temperature = 33.
        errors = []

        def hammer(call, check):
            try:
                for _ in range(60):
                    check(call())
            except Exception as e:  # noqa
                errors.append(e)

        threads = [
            threading.Thread(target=hammer, args=(tc.holder_temperature, lambda v: pytest.approx(33., abs=0.1) == v)),
            threading.Thread(target=hammer, args=(tc.status, lambda s: isinstance(s, InstrumentStatus) or 1 / 0)),
            threading.Thread(target=hammer, args=(tc.target, lambda v: v == 25. or 1 / 0)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert not errors, errors


class TestOpenPort:
    def test_the_simulated_port(self):
        port = open_port('sim://tc1b')
        assert isinstance(port, SimulatedSerial) and port.controller.model.name == 'TC 1B'

    def test_a_wrong_simulation_is_a_value_error(self):
        with pytest.raises(ValueError):
            open_port('sim://nothing')

    def test_the_driver_says_what_is_wrong_with_a_simulation(self):
        with pytest.raises(QnwConnectionError, match='cannot be opened.*No simulated model'):
            QnwTC('sim://nothing').open()
