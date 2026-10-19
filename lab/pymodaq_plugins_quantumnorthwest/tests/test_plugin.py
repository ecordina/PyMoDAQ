"""The actuator plugin, on a simulated TC 125 / TC 1B"""
from dataclasses import replace

import pytest

from pymodaq.control_modules.thread_commands import ThreadStatus
from pymodaq.utils.data import DataActuator
from pymodaq.control_modules.move_utility_classes import DataActuatorType

from pymodaq_plugins_quantumnorthwest.daq_move_plugins import daq_move_QNW_TC as module
from pymodaq_plugins_quantumnorthwest.daq_move_plugins.daq_move_QNW_TC import DAQ_Move_QNW_TC
from pymodaq_plugins_quantumnorthwest.hardware import simulator
from pymodaq_plugins_quantumnorthwest.hardware.errors import QnwError, QnwLimitError
from pymodaq_plugins_quantumnorthwest.hardware.qnw_tc import QnwTC

from helpers import simulation


def temperature(value, units='°C'):
    return DataActuator('test', data=float(value), units=units)


class Bench:
    """The plugin, with what it says to PyMoDAQ kept"""

    def __init__(self, qtbot):
        self.plugin = DAQ_Move_QNW_TC()
        self.said = []
        self.plugin.emit_status = self.said.append

    def connect(self, url='sim://tc125', **settings):
        self.plugin.settings['connection', 'other_port'] = url
        for path, value in settings.items():
            self.plugin.settings[tuple(path.split('__'))] = value
        return self.plugin.ini_stage()

    @property
    def controller(self) -> QnwTC:
        return self.plugin.controller

    @property
    def sim(self):
        return simulation(self.plugin.controller)

    def messages(self):
        """The texts the plugin gave to PyMoDAQ to show"""
        return [s.attribute for s in self.said if s.command == ThreadStatus.UPDATE_STATUS]

    def commands(self):
        return self.sim.received


@pytest.fixture(autouse=True)
def fresh_readings(monkeypatch):
    """A temperature which was read less than 0.2 s ago is not read again: not in the tests, which change the
    temperature of the simulated holder and read it at once"""
    monkeypatch.setattr(module, 'MAX_AGE', 0.)


@pytest.fixture
def bench(qtbot):
    bench = Bench(qtbot)
    yield bench
    bench.plugin.close()


@pytest.fixture
def tc125(bench):
    assert bench.connect('sim://tc125')[1]
    return bench


@pytest.fixture
def tc1b(bench):
    assert bench.connect('sim://tc1b')[1]
    return bench


class TestDeclaration:
    def test_an_actuator_of_temperatures(self):
        assert DAQ_Move_QNW_TC._controller_units == '°C'
        assert DAQ_Move_QNW_TC._axis_names == ['Temperature']
        assert DAQ_Move_QNW_TC.data_actuator_type == DataActuatorType.DataActuator
        assert not DAQ_Move_QNW_TC.is_multiaxes

    def test_a_holder_takes_minutes_to_reach_its_target(self):
        params = {p['name']: p for p in DAQ_Move_QNW_TC.params}
        assert params['timeout']['value'] == 3600

    def test_the_simulated_controllers_are_in_the_ports_but_are_not_the_default(self):
        assert 'sim://tc125' in module.PORTS and 'sim://tc1b' in module.PORTS
        assert not module.DEFAULT_PORT.startswith('sim:') and module.DEFAULT_PORT in module.PORTS

    def test_the_port_choices(self, monkeypatch):
        monkeypatch.setattr(module, 'serial_ports', lambda: ['/dev/ttyUSB3', '/dev/ttyUSB4'])
        assert module.port_choices() == (['/dev/ttyUSB3', '/dev/ttyUSB4', 'sim://tc125', 'sim://tc1b'],
                                         '/dev/ttyUSB3')

    def test_a_port_is_proposed_when_there_is_none(self, monkeypatch):
        monkeypatch.setattr(module, 'serial_ports', lambda: [])
        ports, default = module.port_choices()
        assert default in ports and not default.startswith('sim:')

    def test_the_settings_exist(self, bench):
        settings = bench.plugin.settings
        for path in [('connection', 'port'), ('connection', 'baudrate'), ('temperature', 'reading'),
                     ('temperature', 'control'), ('temperature', 'ramp'), ('stirrer', 'stirrer_on'),
                     ('status', 'state'), ('closing', 'on_close'), ('bounds', 'is_bounds'), ('units',), ('epsilon',)]:
            settings.child(*path)
        assert bench.plugin.epsilon == 0.1 and settings['epsilon'] == 0.1


class TestInit:
    def test_tc125(self, bench):
        info, initialized = bench.connect('sim://tc125')
        assert initialized
        assert 'firmware 9.1' in info and 'SIMULATED CONTROLLER' in info
        settings = bench.plugin.settings
        assert settings['units'] == '°C'
        assert settings['connection', 'controller_info'] == info

    @pytest.mark.parametrize('url, low, high', [('sim://tc125', -30, 105), ('sim://tc1b', -40, 110)])
    def test_the_range_of_the_holder_is_in_the_bounds(self, bench, url, low, high):
        bench.connect(url)
        settings = bench.plugin.settings
        assert (settings['bounds', 'min_bound'], settings['bounds', 'max_bound']) == (low, high)
        assert settings['bounds', 'is_bounds'] is False  # (a target out of it is an error of the driver)

    def test_the_speed_of_the_stirrer_is_the_one_of_the_tc1b(self, bench):
        bench.connect('sim://tc125')
        speed = bench.plugin.settings.child('stirrer', 'stirrer_speed')
        assert speed.opts['readonly'] is True
        bench.plugin.close()
        bench.connect('sim://tc1b')
        assert speed.opts['readonly'] is False and (speed.opts['min'], speed.opts['max']) == (300, 2500)

    def test_the_state_is_read(self, tc125):
        settings = tc125.plugin.settings
        assert settings['status', 'state'] == 'off'
        assert settings['status', 'target'] == '25.00 °C'
        assert settings['status', 'probe'] == '22.00 °C'
        assert settings['temperature', 'control'] is False

    def test_the_ramp_is_switched_off_or_set(self, bench):
        bench.connect('sim://tc1b', temperature__ramp=1.5)
        assert bench.sim.ramp_rate == pytest.approx(1.5)
        bench.plugin.close()
        bench.connect('sim://tc1b', temperature__ramp=0.)
        assert bench.sim.ramp_rate == 0. and ['F1 RS S 0', 'F1 RT S 0'] == [
            c for c in bench.commands() if c.startswith(('F1 RS', 'F1 RT'))]

    def test_the_stirrer_and_the_control_are_left_as_they_are(self, tc125):
        assert not any(c.startswith(('F1 SS', 'F1 TC')) for c in tc125.commands())

    def test_a_port_which_is_not_there(self, bench):
        info, initialized = bench.connect('/dev/ttyNotThere')
        assert not initialized and 'cannot be opened' in info
        assert bench.plugin.controller is None

    def test_the_list_of_ports_is_made_again_after_a_failure(self, bench, monkeypatch):
        monkeypatch.setattr(module, 'serial_ports', lambda: ['/dev/ttyUSB7'])
        bench.plugin.settings['connection', 'port'] = module.DEFAULT_PORT
        bench.connect('/dev/ttyNotThere')
        port = bench.plugin.settings.child('connection', 'port')
        assert '/dev/ttyUSB7' in port.opts['limits'] and port.value() == module.DEFAULT_PORT

    def test_a_simulation_which_does_not_exist(self, bench):
        info, initialized = bench.connect('sim://tc999')
        assert not initialized and 'No simulated model' in info

    def test_nothing_on_the_port(self, bench, monkeypatch):
        monkeypatch.setattr(QnwTC, 'open', lambda self: (_ for _ in ()).throw(QnwError('nothing here')))
        assert bench.connect('sim://tc125') == ('nothing here', False)
        assert bench.plugin.controller is None

    def test_a_bug_does_not_leave_the_port_open(self, bench, monkeypatch):
        closed = []
        monkeypatch.setattr(QnwTC, 'open', lambda self: (_ for _ in ()).throw(RuntimeError('boom')))
        monkeypatch.setattr(QnwTC, 'close', lambda self: closed.append(self))
        assert bench.connect('sim://tc125') == ('boom', False)
        assert len(closed) == 1 and bench.plugin.controller is None

    def test_other_port_replaces_the_port(self, bench):
        bench.plugin.settings['connection', 'port'] = 'sim://tc125'
        bench.plugin.settings['connection', 'other_port'] = '  sim://tc1b  '
        assert bench.plugin._port() == 'sim://tc1b'
        bench.plugin.settings['connection', 'other_port'] = ''
        assert bench.plugin._port() == 'sim://tc125'

    def test_something_which_is_not_a_single_holder_is_told(self, bench, monkeypatch):
        monkeypatch.setitem(simulator.MODELS, 'tc125', replace(simulator.TC125, holder_id=22))
        assert bench.connect('sim://tc125')[1]
        assert any('only its sample holder (F1)' in m for m in bench.messages())

    def test_a_single_holder_is_not_told(self, tc125):
        assert not any('sample holder (F1)' in m for m in tc125.messages())


class TestReading:
    def test_the_temperature_of_the_holder(self, tc125):
        value = tc125.plugin.get_actuator_value()
        assert isinstance(value, DataActuator) and value.units == '°C'
        assert value.value() == pytest.approx(22.)

    def test_not_connected(self, bench):
        with pytest.raises(QnwError, match='not connected'):
            bench.plugin.get_actuator_value()

    def test_the_temperature_of_the_probe(self, tc1b):
        tc1b.sim.probe_temperature = 30.
        tc1b.plugin.settings['temperature', 'reading'] = 'Probe'
        assert tc1b.plugin.get_actuator_value().value() == pytest.approx(30., abs=0.01)

    @pytest.mark.parametrize('url', ['sim://tc125?probe=0', 'sim://tc1b?probe=0'])
    def test_no_probe_is_not_a_temperature(self, bench, url):
        bench.connect(url)
        bench.plugin.settings['temperature', 'reading'] = 'Probe'
        with pytest.raises(QnwError, match='No probe is connected'):
            bench.plugin.get_actuator_value()

    def test_a_recent_temperature_is_not_read_again(self, tc125, monkeypatch):
        monkeypatch.setattr(module, 'MAX_AGE', 10.)
        tc125.plugin.get_actuator_value()
        asked = len(tc125.commands())
        tc125.sim.temperature = 50.
        assert tc125.plugin.get_actuator_value().value() == pytest.approx(22.)
        assert tc125.commands()[asked:].count('F1 CT ?') == 0

    def test_the_status_is_not_read_each_time(self, tc125):
        tc125.plugin.get_actuator_value()
        asked = len(tc125.commands())
        for _ in range(5):
            tc125.plugin.get_actuator_value()
        new = tc125.commands()[asked:]
        assert new.count('F1 CT ?') == 5
        assert 'F1 IS ?' not in new and 'F1 PT ?' not in new and 'F1 TT ?' not in new

    def test_the_status_is_read_after_a_while(self, tc125):
        tc125.plugin.settings['status', 'refresh'] = 0.5
        tc125.plugin.get_actuator_value()
        tc125.plugin._status_time -= 1.
        asked = len(tc125.commands())
        tc125.plugin.get_actuator_value()
        assert 'F1 IS ?' in tc125.commands()[asked:]

    def test_what_the_status_cannot_read_does_not_hide_the_temperature(self, tc125, monkeypatch):
        monkeypatch.setattr(tc125.controller, 'status', lambda: (_ for _ in ()).throw(QnwError('no status')))
        tc125.plugin._status_time = 0.
        assert tc125.plugin.get_actuator_value().value() == pytest.approx(22.)

    def test_the_control_and_the_stirrer_follow_the_controller(self, tc125):
        tc125.sim.control = True
        tc125.sim.stirring = True
        tc125.plugin._refresh_status(force=True)
        settings = tc125.plugin.settings
        assert settings['temperature', 'control'] is True and settings['stirrer', 'stirrer_on'] is True
        assert settings['status', 'state'] == 'seeking'

    def test_the_values_which_are_shown(self, tc125):
        tc125.sim.temperature = 40.
        tc125.sim.probe_temperature = 39.
        tc125.plugin.get_actuator_value()
        tc125.plugin._refresh_status(force=True)
        settings = tc125.plugin.settings
        assert settings['status', 'holder'] == '40.00 °C' and settings['status', 'probe'] == '39.00 °C'
        assert settings['status', 'exchanger'].endswith('°C')

    def test_no_probe_is_shown(self, bench):
        bench.connect('sim://tc1b?probe=0')
        assert bench.plugin.settings['status', 'probe'] == 'not connected'

    def test_nothing_is_sent_to_the_settings_when_nothing_changes(self, tc125):
        tc125.plugin._refresh_status(force=True)
        tc125.said.clear()
        tc125.plugin._refresh_status(force=True)
        assert not [s for s in tc125.said if s.command == ThreadStatus.UPDATE_SETTINGS]


class TestMoving:
    def test_move_abs(self, tc125):
        tc125.plugin.move_abs(temperature(35))
        assert tc125.sim.target == 35. and tc125.sim.control
        assert tc125.plugin.target_value.value() == 35.

    def test_the_target_comes_first_then_the_control(self, tc125):
        tc125.plugin.move_abs(temperature(35))
        commands = tc125.commands()
        assert commands.index('F1 TT S 35.00') < commands.index('F1 TC +')

    def test_in_another_unit(self, tc125):
        tc125.plugin.move_abs(temperature(303.15, 'K'))
        assert tc125.sim.target == pytest.approx(30.)

    def test_a_target_out_of_the_range_of_the_holder_is_an_error(self, tc125):
        with pytest.raises(QnwLimitError, match='-30 to 105'):
            tc125.plugin.move_abs(temperature(150))
        with pytest.raises(QnwLimitError):
            tc125.plugin.move_abs(temperature(-100))
        assert tc125.sim.target == 25. and not any(c.startswith('F1 TT S') for c in tc125.commands())

    def test_the_bounds_of_pymodaq_can_be_used_too(self, tc125):
        tc125.plugin.settings['bounds', 'is_bounds'] = True
        tc125.plugin.move_abs(temperature(150))
        assert tc125.sim.target == 105.
        assert any(s.command == 'outofbounds' for s in tc125.said)

    def test_the_bounds_apply_to_the_unit_of_the_actuator(self, tc125):
        tc125.plugin.settings['bounds', 'is_bounds'] = True
        tc125.plugin.move_abs(temperature(303.15, 'K'))  # (30 °C, not 303 °C)
        assert tc125.sim.target == pytest.approx(30.)

    def test_the_control_can_be_left_off(self, tc125):
        tc125.plugin.settings['temperature', 'auto_on'] = False
        tc125.plugin.move_abs(temperature(35))
        assert tc125.sim.target == 35. and not tc125.sim.control

    def test_the_control_is_not_switched_on_again_and_again(self, tc125):
        for value in (30, 31, 32, 33):
            tc125.plugin.move_abs(temperature(value))
        assert tc125.commands().count('F1 TC +') == 1

    def test_a_ramp_which_the_program_makes(self, tc125):
        """Like ThermoSpectro: a new target every second, each one about 0.02 °C more"""
        sim = tc125.sim
        for step in range(40):
            tc125.plugin.move_abs(temperature(22 + 0.02 * step))
            sim.skip(1)
        assert 22.4 < sim.temperature < 22.9
        assert sim.target == pytest.approx(22.78)

    def test_move_rel_from_the_target(self, tc125):
        tc125.plugin.move_abs(temperature(30))
        tc125.plugin.move_rel(temperature(2.5))
        tc125.plugin.move_rel(temperature(2.5))
        assert tc125.sim.target == 35.

    def test_move_rel_before_any_move_from_the_temperature_of_the_holder(self, tc125):
        tc125.plugin.move_rel(temperature(5))
        assert tc125.sim.target == pytest.approx(27.)

    def test_move_home(self, tc125):
        tc125.plugin.settings['temperature', 'home'] = 20.
        tc125.plugin.move_home()
        assert tc125.sim.target == 20.

    def test_stop_motion_holds_the_temperature(self, tc1b, qtbot):
        tc1b.plugin.move_abs(temperature(60))
        tc1b.sim.skip(60)
        reached = []
        tc1b.plugin.move_done_signal.connect(reached.append)
        tc1b.plugin.stop_motion()
        assert 25 < tc1b.sim.target < 40
        assert tc1b.sim.ramp_rate == 0
        assert len(reached) == 1 and reached[0].value() == pytest.approx(tc1b.sim.temperature, abs=0.3)
        tc1b.sim.skip(300)
        assert tc1b.sim.temperature == pytest.approx(tc1b.sim.target, abs=0.1)

    def test_stop_motion_when_not_connected(self, bench, qtbot):
        bench.plugin.stop_motion()  # (nothing to stop: no error)

    def test_a_ramp_of_the_controller(self, bench):
        bench.connect('sim://tc125', temperature__ramp=1.)
        bench.plugin.move_abs(temperature(32))
        bench.sim.skip(120)
        assert bench.sim.temperature == pytest.approx(24., abs=0.6)


class TestMoveIsDone:
    def test_the_state_is_shown_when_a_move_is_over(self, tc125):
        tc125.plugin.move_abs(temperature(30))
        tc125.plugin.settings['temperature', 'control'] = False  # (as the GUI showed it before)
        tc125.plugin.move_done()
        assert tc125.plugin.settings['temperature', 'control'] is True

    def test_not_when_the_moves_are_not_waited_for(self, tc125):
        """Like ThermoSpectro, which sends a target every second: the controller is not asked more than needed"""
        tc125.plugin.ispolling = False
        tc125.plugin.move_abs(temperature(30))
        asked = len(tc125.commands())
        tc125.plugin.move_done()
        assert 'F1 IS ?' not in tc125.commands()[asked:]

    def test_the_control_is_shown_as_soon_as_it_is_switched_on(self, tc125):
        assert tc125.plugin.settings['temperature', 'control'] is False
        tc125.plugin.move_abs(temperature(30))
        assert tc125.plugin.settings['temperature', 'control'] is True

    def reach(self, bench, seconds):
        bench.plugin.move_abs(temperature(30))
        bench.sim.skip(seconds)
        bench.plugin.current_value = bench.plugin.get_actuator_value()

    def test_within_epsilon_and_stable(self, tc125):
        self.reach(tc125, 400)
        assert tc125.plugin.user_condition_to_reach_target() is True
        assert tc125.plugin._condition_to_reach_target() is True

    def test_within_epsilon_but_not_stable_yet(self, tc125):
        tc125.plugin.settings['temperature', 'auto_on'] = True
        tc125.plugin.move_abs(temperature(22.05))  # (the holder is at 22 °C: within epsilon from the start)
        tc125.sim.skip(10)
        tc125.plugin.current_value = tc125.plugin.get_actuator_value()
        tc125.plugin.target_value = temperature(22.05)
        assert tc125.plugin.absolute_difference_condition_to_reach_target()
        assert not tc125.plugin.user_condition_to_reach_target()
        assert not tc125.plugin._condition_to_reach_target()
        tc125.sim.skip(70)
        assert tc125.plugin._condition_to_reach_target()

    def test_epsilon_is_enough_if_asked(self, tc125):
        tc125.plugin.settings['temperature', 'wait_stable'] = False
        tc125.plugin.move_abs(temperature(22.05))
        tc125.plugin.current_value = tc125.plugin.get_actuator_value()
        assert tc125.plugin._condition_to_reach_target()

    def test_not_reached(self, tc125):
        self.reach(tc125, 5)
        assert not tc125.plugin._condition_to_reach_target()

    def test_nothing_to_wait_for_when_the_control_is_off(self, tc125):
        assert tc125.plugin.user_condition_to_reach_target() is True

    def test_a_status_which_cannot_be_read_is_not_stability(self, tc125, monkeypatch):
        tc125.plugin.settings['temperature', 'auto_on'] = True
        tc125.plugin.move_abs(temperature(30))
        monkeypatch.setattr(tc125.controller, 'status', lambda: (_ for _ in ()).throw(QnwError('no status')))
        assert tc125.plugin.user_condition_to_reach_target() is False


class TestCommittingSettings:
    def test_the_temperature_control(self, tc125):
        tc125.plugin.settings['temperature', 'control'] = True
        tc125.plugin.commit_settings(tc125.plugin.settings.child('temperature', 'control'))
        assert tc125.sim.control
        tc125.plugin.settings['temperature', 'control'] = False
        tc125.plugin.commit_settings(tc125.plugin.settings.child('temperature', 'control'))
        assert not tc125.sim.control

    def test_the_stirrer_of_the_tc125(self, tc125):
        stirrer = tc125.plugin.settings.child('stirrer')
        stirrer['stirrer_on'] = True
        tc125.plugin.commit_settings(stirrer.child('stirrer_on'))
        assert tc125.sim.stirring
        stirrer['stirrer_on'] = False
        tc125.plugin.commit_settings(stirrer.child('stirrer_on'))
        assert not tc125.sim.stirring

    def test_the_speed_of_the_stirrer_of_the_tc1b(self, tc1b):
        stirrer = tc1b.plugin.settings.child('stirrer')
        stirrer['stirrer_speed'] = 1800
        tc1b.plugin.commit_settings(stirrer.child('stirrer_speed'))
        assert not tc1b.sim.stirring  # (a speed is not a start)
        stirrer['stirrer_on'] = True
        tc1b.plugin.commit_settings(stirrer.child('stirrer_on'))
        assert tc1b.sim.stirring and tc1b.sim.stir_speed == 1800
        stirrer['stirrer_speed'] = 600
        tc1b.plugin.commit_settings(stirrer.child('stirrer_speed'))
        assert tc1b.sim.stir_speed == 600

    def test_the_ramp(self, tc125):
        tc125.plugin.settings['temperature', 'ramp'] = 0.5
        tc125.plugin.commit_settings(tc125.plugin.settings.child('temperature', 'ramp'))
        assert tc125.sim.ramp_rate == pytest.approx(0.5)

    def test_an_impossible_ramp_is_an_error(self, tc125):
        tc125.plugin.settings['temperature', 'ramp'] = 0.004
        with pytest.raises(QnwLimitError):
            tc125.plugin.commit_settings(tc125.plugin.settings.child('temperature', 'ramp'))

    def test_the_other_settings_send_nothing(self, tc125):
        asked = len(tc125.commands())
        for path in (('temperature', 'reading'), ('temperature', 'auto_on'), ('status', 'refresh'), ('epsilon',)):
            tc125.plugin.commit_settings(tc125.plugin.settings.child(*path))
        assert len(tc125.commands()) == asked

    def test_before_the_connection_nothing_happens(self, bench):
        bench.plugin.commit_settings(bench.plugin.settings.child('temperature', 'control'))

    def test_a_setting_which_the_controller_refuses_is_told_by_pymodaq(self, tc125):
        """``update_settings`` of PyMoDAQ catches what ``commit_settings`` raises and shows it"""
        tc125.sim.fail(8)
        tc125.plugin.settings['temperature', 'control'] = True
        tc125.said.clear()
        tc125.plugin.update_settings({'path': ['Settings', 'temperature', 'control'], 'change': 'value',
                                      'param': tc125.plugin.settings.child('temperature', 'control')})
        assert any('inadequate coolant' in m for m in tc125.messages())


class TestFaults:
    def test_a_fault_is_shown_and_told_once(self, tc125):
        tc125.plugin.move_abs(temperature(40))
        tc125.sim.fail(8)
        tc125.plugin._refresh_status(force=True)
        settings = tc125.plugin.settings
        assert 'inadequate coolant' in settings['status', 'fault']
        assert settings['status', 'state'] == 'off' and settings['temperature', 'control'] is False
        told = [m for m in tc125.messages() if 'inadequate coolant' in m]
        assert len(told) == 1
        tc125.plugin._refresh_status(force=True)
        assert len([m for m in tc125.messages() if 'inadequate coolant' in m]) == 1

    def test_the_fault_goes_when_the_control_is_on_again(self, tc125):
        tc125.sim.fail(8)
        tc125.plugin._refresh_status(force=True)
        tc125.sim.repair()
        tc125.plugin.settings['temperature', 'control'] = True
        tc125.plugin.commit_settings(tc125.plugin.settings.child('temperature', 'control'))
        assert tc125.plugin.settings['status', 'fault'] == ''
        assert tc125.plugin.settings['status', 'state'] in ('seeking', 'holding')

    def test_moving_with_a_fault_is_an_error(self, tc125):
        tc125.sim.fail(8)
        with pytest.raises(QnwError, match='inadequate coolant'):
            tc125.plugin.move_abs(temperature(30))

    def test_a_fault_which_was_repaired_at_the_front_panel_goes(self, tc125):
        tc125.sim.fail(8)
        tc125.plugin._refresh_status(force=True)
        assert 'inadequate coolant' in tc125.plugin.settings['status', 'fault']
        tc125.sim.repair()
        tc125.sim.control = True  # (the user pressed the button of the controller)
        tc125.plugin._refresh_status(force=True)
        assert tc125.plugin.settings['status', 'fault'] == ''
        assert tc125.plugin.settings['status', 'state'] in ('seeking', 'holding')

    def test_a_second_fault_is_told_again(self, tc125):
        tc125.sim.fail(8)
        tc125.plugin._refresh_status(force=True)
        tc125.sim.fail(5)
        tc125.plugin._refresh_status(force=True)
        assert any('error 5' in m for m in tc125.messages())


class TestClosing:
    def run(self, bench, mode):
        bench.connect('sim://tc1b')
        bench.plugin.settings['temperature', 'control'] = True
        bench.plugin.move_abs(temperature(30))
        bench.plugin.settings.child('stirrer', 'stirrer_on').setValue(True)
        bench.controller.set_stirrer(True, 1000)
        bench.plugin.settings['closing', 'on_close'] = mode
        sim = bench.sim
        port = bench.controller._port
        bench.plugin.close()
        return sim, port

    def test_it_is_left_as_it_is(self, bench):
        sim, port = self.run(bench, module.LEAVE)
        assert sim.control and sim.stirring and not port.is_open

    def test_the_control_is_switched_off(self, bench):
        sim, port = self.run(bench, module.CONTROL_OFF)
        assert not sim.control and sim.stirring and not port.is_open

    def test_all_is_switched_off(self, bench):
        sim, port = self.run(bench, module.ALL_OFF)
        assert not sim.control and not sim.stirring and not port.is_open

    def test_the_port_is_closed_even_if_the_controller_does_not_answer(self, bench):
        bench.connect('sim://tc1b')
        bench.plugin.settings['closing', 'on_close'] = module.ALL_OFF
        bench.sim.fail(8)  # (it cannot be switched off: it is)
        bench.sim.latency = 5.
        bench.controller.timeout = 0.05
        port = bench.controller._port
        bench.plugin.close()
        assert not port.is_open and bench.plugin.controller is None

    def test_a_move_which_is_waited_for_is_stopped_with_the_module(self, tc125):
        tc125.plugin.poll_timer.start()  # (what a move which is waited for does)
        tc125.plugin.close()
        assert not tc125.plugin.poll_timer.isActive()

    def test_closing_twice(self, tc125):
        tc125.plugin.close()
        tc125.plugin.close()

    def test_closing_without_a_connection(self, bench):
        bench.plugin.close()

    def test_connected_again(self, tc125):
        tc125.plugin.close()
        assert tc125.plugin.ini_stage()[1]
        assert tc125.plugin.get_actuator_value().value() == pytest.approx(22., abs=0.5)
