import math

import pytest

from pymodaq_plugins_quantumnorthwest.hardware import protocol
from pymodaq_plugins_quantumnorthwest.hardware.errors import QnwError
from pymodaq_plugins_quantumnorthwest.hardware.protocol import (ErrorReport, InstrumentStatus, Message,
                                                                MessageParser, QnwProtocolError)


class TestMessage:
    def test_parse(self):
        message = Message.parse('F1 CT 22.84')
        assert (message.unit, message.code, message.args, message.text) == ('F1', 'CT', ('22.84',), '22.84')
        assert message.raw == 'F1 CT 22.84'

    def test_several_arguments(self):
        message = Message.parse('F1 TT S 23.10')
        assert message.args == ('S', '23.10') and message.text == 'S 23.10'

    def test_no_argument(self):
        message = Message.parse('F1 NOPROBE')
        assert (message.code, message.args, message.text) == ('NOPROBE', (), '')

    def test_case_and_spaces(self):
        message = Message.parse('  f1   ct   22.84  ')
        assert (message.unit, message.code, message.args) == ('F1', 'CT', ('22.84',))

    def test_the_other_units(self):
        assert Message.parse('R1 CT 20.0').unit == 'R1'
        assert Message.parse('F2 DL 3').unit == 'F2'

    @pytest.mark.parametrize('content', ['', ' ', 'F1', '   F1  '])
    def test_not_a_message(self, content):
        assert Message.parse(content) is None


class TestParser:
    def test_one_message(self):
        assert [m.raw for m in MessageParser().feed(b'[F1 CT 22.84]')] == ['F1 CT 22.84']

    def test_several_messages_at_once(self):
        messages = MessageParser().feed(b'[F1 CT 22.84][F1 TT 25.00]\r\n[F1 IS 0-+S]')
        assert [m.code for m in messages] == ['CT', 'TT', 'IS']

    def test_text_between_messages_is_ignored(self):
        messages = MessageParser().feed(b'hello\r\n[F1 CT 1.00] noise \x00\xff [F1 CT 2.00] bye')
        assert [m.args for m in messages] == [('1.00',), ('2.00',)]

    STREAM = b'junk[F1 ID 11]\r\n[F1 VN 9.1][F1 ER 9 <<[F1 XX ?]>>]\x00[F1 IS 0-+S]'

    @pytest.mark.parametrize('cut', range(1, len(STREAM)))
    def test_a_stream_cut_anywhere(self, cut):
        parser = MessageParser()
        messages = parser.feed(self.STREAM[:cut]) + parser.feed(self.STREAM[cut:])
        assert [m.code for m in messages] == ['ID', 'VN', 'ER', 'IS']
        assert messages[2].raw == 'F1 ER 9 <<[F1 XX ?]>>'

    def test_a_stream_one_byte_at_a_time(self):
        parser = MessageParser()
        messages = [m for byte in self.STREAM for m in parser.feed(bytes([byte]))]
        assert [m.code for m in messages] == ['ID', 'VN', 'ER', 'IS']

    def test_the_part_of_a_message_is_kept(self):
        parser = MessageParser()
        assert parser.feed(b'[F1 CT 22') == []
        assert parser.partial
        assert [m.args for m in parser.feed(b'.84]')] == [('22.84',)]
        assert not parser.partial

    def test_a_message_which_is_cut_by_another_one_is_dropped(self):
        messages = MessageParser().feed(b'[F1 CT 22[F1 TT 25.00]')
        assert [m.raw for m in messages] == ['F1 TT 25.00']

    def test_the_quoted_command_keeps_its_brackets(self):
        message, = MessageParser().feed(b'[F1 ER 9 <<[F1 TT S 5000]>>]')
        assert message.code == 'ER' and message.raw == 'F1 ER 9 <<[F1 TT S 5000]>>'

    def test_a_quotation_which_is_never_closed_does_not_swallow_the_stream(self):
        parser = MessageParser()
        assert parser.feed(b'[F1 ER 9 <<' + b'x' * 300) == []
        assert not parser.partial  # too long: garbage
        assert [m.code for m in parser.feed(b'[F1 CT 1.00]')] == ['CT']

    def test_garbage_which_is_too_long_is_dropped(self):
        parser = MessageParser()
        assert parser.feed(b'[' + b'a' * 500) == []
        assert [m.code for m in parser.feed(b'[F1 CT 1.00]')] == ['CT']

    def test_bytes_which_are_not_text(self):
        messages = MessageParser().feed(b'\xff\xfe[F1 CT 1.00]\x80')
        assert [m.code for m in messages] == ['CT']

    def test_a_message_without_code_is_skipped(self):
        assert [m.code for m in MessageParser().feed(b'[F1][F1 CT 1.00]')] == ['CT']

    def test_reset(self):
        parser = MessageParser()
        parser.feed(b'[F1 CT 2')
        parser.reset()
        assert [m.args for m in parser.feed(b'0.00]')] == []


class TestCommand:
    def test_command(self):
        assert protocol.command('TT', 'S', '23.10') == b'[F1 TT S 23.10]'

    def test_question(self):
        assert protocol.command('CT', '?') == b'[F1 CT ?]'

    def test_numbers_and_unit(self):
        assert protocol.command('RS', 'S', 3, unit='R1') == b'[R1 RS S 3]'

    def test_a_command_is_a_message(self):
        message, = MessageParser().feed(protocol.command('SS', 'S', 1000))
        assert (message.unit, message.code, message.args) == ('F1', 'SS', ('S', '1000'))

    def test_normalized(self):
        assert protocol.normalized('[F1 ct ?]') == protocol.normalized('<<F1  CT ?>>') == 'F1CT?'


class TestNumbers:
    @pytest.mark.parametrize('value, text', [(23.1, '23.10'), (-5, '-5.00'), (105, '105.00'), (-0.004, '0.00'),
                                             (0, '0.00'), (71.321, '71.32')])
    def test_format_temperature(self, value, text):
        assert protocol.format_temperature(value) == text

    @pytest.mark.parametrize('text, value', [('22.84', 22.84), (' -1 ', -1.), ('110', 110.), ('1e1', 10.)])
    def test_parse_float(self, text, value):
        assert protocol.parse_float(text) == value

    @pytest.mark.parametrize('text', ['NA', 'na', 'N/A', '', '  '])
    def test_not_available(self, text):
        assert protocol.parse_float(text) is None

    @pytest.mark.parametrize('text', ['abc', 'nan', 'inf', '-inf', '22.8.4', '?'])
    def test_not_a_number(self, text):
        with pytest.raises(QnwProtocolError):
            protocol.parse_float(text)

    def test_protocol_errors_are_errors_of_the_controller(self):
        assert issubclass(QnwProtocolError, QnwError) and issubclass(QnwProtocolError, ValueError)


class TestInstrumentStatus:
    @pytest.mark.parametrize('text, expected', [
        ('0-+S', InstrumentStatus(0, False, True, True)),
        ('0++C', InstrumentStatus(0, True, True, False)),
        ('1---'.replace('---', '--C'), InstrumentStatus(1, False, False, False)),
        ('0-+SW', InstrumentStatus(0, False, True, True, 'W')),
        ('0++C+', InstrumentStatus(0, True, True, False, '+')),
        ('0--S-', InstrumentStatus(0, False, False, True, '-')),
        ('9-+s', InstrumentStatus(9, False, True, True)),
        ('0 - + S', InstrumentStatus(0, False, True, True)),
    ])
    def test_parse(self, text, expected):
        assert protocol.parse_instrument_status(text) == expected

    @pytest.mark.parametrize('text', ['', '?', 'R', '0-+', '0-+X', 'a-+S', '0*+S', '0-+SWW', '10-+S'])
    def test_not_a_status(self, text):
        with pytest.raises(QnwProtocolError):
            protocol.parse_instrument_status(text)

    @pytest.mark.parametrize('status, state', [(InstrumentStatus(0, False, False, False), 'off'),
                                               (InstrumentStatus(0, False, False, True), 'off'),
                                               (InstrumentStatus(0, False, True, False), 'seeking'),
                                               (InstrumentStatus(0, False, True, True), 'holding')])
    def test_state(self, status, state):
        assert status.state == state


class TestErrors:
    @pytest.mark.parametrize('text, code', [('-1', -1), ('0', 0), ('05', 5), ('5', 5), ('08', 8), ('9', 9)])
    def test_codes(self, text, code):
        assert protocol.parse_error(text).code == code

    def test_no_error(self):
        for text in ('-1', '0'):
            report = protocol.parse_error(text)
            assert not report.is_error and report.text == 'no error'

    def test_a_fault(self):
        report = protocol.parse_error('08')
        assert report.is_error and 'inadequate coolant' in report.text and 'error 8' in report.text

    def test_the_command_which_was_not_understood(self):
        report = protocol.parse_error('9 <<[F1 XX ?]>>')
        assert report == ErrorReport(9, '[F1 XX ?]')
        assert '[F1 XX ?]' in report.text

    def test_an_unknown_code(self):
        assert 'unknown error' in protocol.parse_error('42').text

    @pytest.mark.parametrize('text', ['', 'abc', '?'])
    def test_not_an_error(self, text):
        with pytest.raises(QnwProtocolError):
            protocol.parse_error(text)


class TestRamp:
    # the pairs of the table of the manual of the TC 125: rate (°C/min), RS (s), RT (hundredths of a degree)
    TABLE = [(0.05, 12, 1), (0.1, 12, 2), (0.2, 6, 2), (0.5, 6, 5), (1, 3, 5), (2, 3, 10), (5, 3, 25), (10, 3, 50)]

    @pytest.mark.parametrize('rate, seconds, hundredths', TABLE)
    def test_the_table_of_the_manual(self, rate, seconds, hundredths):
        assert protocol.ramp_rate_of(seconds, hundredths) == pytest.approx(rate)

    @pytest.mark.parametrize('rate', [row[0] for row in TABLE])
    def test_the_table_rates_are_exact(self, rate):
        seconds, hundredths = protocol.ramp_increments(rate)
        assert protocol.ramp_rate_of(seconds, hundredths) == pytest.approx(rate, rel=1e-9)

    def test_the_steps_are_small(self):
        assert protocol.ramp_increments(1.) == (3, 5)  # not (6, 10)

    @pytest.mark.parametrize('rate', [0.01, 0.0123, 0.05, 0.07, 0.3, 0.77, 1.5, 3.3, 7.1, 9.99, 10.])
    def test_any_rate(self, rate):
        seconds, hundredths = protocol.ramp_increments(rate)
        assert 1 <= seconds <= protocol.MAX_RAMP_SECONDS and hundredths >= 1
        assert protocol.ramp_rate_of(seconds, hundredths) == pytest.approx(rate, rel=0.03)

    def test_the_whole_range(self):
        for i in range(400):
            rate = 0.01 * 1000 ** (i / 399)  # 0.01 to 10, logarithmic
            seconds, hundredths = protocol.ramp_increments(rate)
            assert abs(protocol.ramp_rate_of(seconds, hundredths) - rate) / rate < 0.03, rate

    @pytest.mark.parametrize('rate', [0, -1, 0.005, 10.5, 100, math.nan])
    def test_a_rate_which_is_out_of_range(self, rate):
        with pytest.raises(ValueError):
            protocol.ramp_increments(rate)
