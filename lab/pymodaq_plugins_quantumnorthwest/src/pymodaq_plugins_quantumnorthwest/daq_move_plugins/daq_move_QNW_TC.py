import sys
import time
from typing import Optional

from pymodaq_utils.logger import set_logger, get_module_name
from pymodaq_utils.utils import ThreadCommand

from pymodaq.control_modules.move_utility_classes import (DAQ_Move_base, comon_parameters_fun, main,
                                                          DataActuatorType)
from pymodaq.control_modules.thread_commands import ThreadStatus
from pymodaq.utils.data import DataActuator

from pymodaq_plugins_quantumnorthwest.hardware.errors import QnwError
from pymodaq_plugins_quantumnorthwest.hardware.qnw_tc import QnwTC, DEFAULT_BAUDRATE, SINGLE_HOLDERS, serial_ports
from pymodaq_plugins_quantumnorthwest.hardware.simulator import is_simulated

logger = set_logger(get_module_name(__file__))

SIMULATED_PORTS = ['sim://tc125', 'sim://tc1b']
READINGS = ['Holder', 'Probe']
LEAVE, CONTROL_OFF, ALL_OFF = 'leave it as it is', 'switch the temperature control off', \
                              'switch the temperature control and the stirrer off'
MAX_AGE = 0.2  # s: the temperature of the holder which was read less than this ago is not read again


def port_choices() -> tuple[list[str], str]:
    """The serial ports of the computer, then the simulated controllers, and the one to choose by default"""
    ports = serial_ports()
    if not ports:  # (a port which is not there yet is told when the controller is connected)
        ports = ['COM1' if sys.platform.startswith('win') else '/dev/ttyUSB0']
    return ports + SIMULATED_PORTS, ports[0]


def with_timeout(params: list, seconds: int) -> list:
    """The common parameters of an actuator, with a longer time to reach the target: a holder takes minutes"""
    for param in params:
        if param['name'] == 'timeout':
            param['value'] = seconds
    return params


PORTS, DEFAULT_PORT = port_choices()


class DAQ_Move_QNW_TC(DAQ_Move_base):
    """The temperature controller of a Quantum Northwest Peltier cuvette holder: the position is a temperature (°C)

    Controls the TC 125, the TC 1 and the TC 1B (qpod, Luma 40, CD 250, qX2, qX3, t2 Sport...) through their serial
    port (USB or RS-232, 19200 baud). Moving the actuator sets the target temperature of the sample holder and switches
    the temperature control on; its value is the temperature of the holder, or of the external probe in the cuvette.
    The probe, the heat exchanger, the stability of the temperature and the faults of the controller are shown in the
    settings, as well as the stirrer, which can be switched on and off (and its speed set with a TC 1 or a TC 1B).

    The serial option of the TC 125 is needed (a USB or RS-232 connector at the back). It was written from the manuals
    of the serial communications of these controllers and tested against a simulated controller (the port ``sim://``),
    not against a real one.
    """
    _controller_units = '°C'
    _axis_names = ['Temperature']
    _epsilons = [0.1]
    is_multiaxes = False
    data_actuator_type = DataActuatorType.DataActuator

    params = [
        {'title': 'Connection:', 'name': 'connection', 'type': 'group', 'children': [
            {'title': 'Port:', 'name': 'port', 'type': 'list', 'limits': PORTS, 'value': DEFAULT_PORT,
             'tip': 'The serial port of the controller. sim://tc125 and sim://tc1b are simulated controllers (a '
                    'real one is not needed)'},
            {'title': 'Other port:', 'name': 'other_port', 'type': 'str', 'value': '',
             'tip': 'A port which is not in the list or a URL of pyserial (socket://host:port, rfc2217://...). '
                    'It replaces the port above when it is filled'},
            {'title': 'Baud rate:', 'name': 'baudrate', 'type': 'int', 'value': DEFAULT_BAUDRATE,
             'tip': 'The controllers talk at 19200 baud'},
            {'title': 'Reply timeout (s):', 'name': 'reply_timeout', 'type': 'float', 'value': 1., 'min': 0.1,
             'max': 30., 'tip': 'How long the controller is given to answer a question'},
            {'title': 'Controller:', 'name': 'controller_info', 'type': 'str', 'value': '', 'readonly': True},
        ]},
        {'title': 'Temperature:', 'name': 'temperature', 'type': 'group', 'children': [
            {'title': 'Measured by:', 'name': 'reading', 'type': 'list', 'limits': READINGS, 'value': READINGS[0],
             'tip': 'The value of the actuator: the temperature of the metal body of the sample holder, or the '
                    'one of the external probe in the cuvette (the probe has to be connected)'},
            {'title': 'Control on:', 'name': 'control', 'type': 'bool', 'value': False,
             'tip': 'The temperature control: the holder goes to the target temperature and stays there. It is '
                    'the state of the controller, which it can be switched on and off from here'},
            {'title': 'Switch on when moving:', 'name': 'auto_on', 'type': 'bool', 'value': True,
             'tip': 'Switch the temperature control on when a target temperature is set (the controller does '
                    'not do it by itself when the computer sets the target)'},
            {'title': 'Wait for stability:', 'name': 'wait_stable', 'type': 'bool', 'value': True,
             'tip': 'A move is over when the temperature is within epsilon of the target AND the controller says '
                    'it is stable (it stayed within 0.05 °C for a minute). Untick it to stop at epsilon'},
            {'title': 'Ramp (°C/min):', 'name': 'ramp', 'type': 'float', 'value': 0., 'min': 0., 'max': 10.,
             'tip': 'The controller reaches a new target at this rate. 0: as fast as it can (to be used when the '
                    'program which moves the actuator makes its own ramp)'},
            {'title': 'Home (°C):', 'name': 'home', 'type': 'float', 'value': 25.,
             'tip': 'The temperature of the home button'},
        ]},
        {'title': 'Stirrer:', 'name': 'stirrer', 'type': 'group', 'children': [
            {'title': 'Stirrer on:', 'name': 'stirrer_on', 'type': 'bool', 'value': False,
             'tip': 'The magnetic stirrer of the holder'},
            {'title': 'Speed (rpm):', 'name': 'stirrer_speed', 'type': 'int', 'value': 1000, 'min': 200,
             'max': 2500, 'tip': 'The speed of the stirrer, with a TC 1 or a TC 1B. The stirrer of a TC 125 turns '
                                 'at the speed of the knob of its front panel'},
        ]},
        {'title': 'Status:', 'name': 'status', 'type': 'group', 'children': [
            {'title': 'State:', 'name': 'state', 'type': 'str', 'value': '', 'readonly': True,
             'tip': 'off, seeking (the temperature is changing) or holding (it is stable)'},
            {'title': 'Holder:', 'name': 'holder', 'type': 'str', 'value': '', 'readonly': True},
            {'title': 'Probe:', 'name': 'probe', 'type': 'str', 'value': '', 'readonly': True},
            {'title': 'Heat exchanger:', 'name': 'exchanger', 'type': 'str', 'value': '', 'readonly': True,
             'tip': 'The temperature of the heat exchanger of the Peltier element: the controller shuts down above '
                    '60 °C (not enough water flows)'},
            {'title': 'Target of the controller:', 'name': 'target', 'type': 'str', 'value': '', 'readonly': True},
            {'title': 'Fault:', 'name': 'fault', 'type': 'str', 'value': '', 'readonly': True},
            {'title': 'Refresh (s):', 'name': 'refresh', 'type': 'float', 'value': 3., 'min': 0.5,
             'tip': 'How often the status of the controller is read when the temperature is'},
        ]},
        {'title': 'When closed:', 'name': 'closing', 'type': 'group', 'children': [
            {'title': 'The controller:', 'name': 'on_close', 'type': 'list', 'limits': [LEAVE, CONTROL_OFF, ALL_OFF],
             'value': LEAVE, 'tip': 'What is done when the module is closed (the controller goes on by itself)'},
        ]},
    ] + with_timeout(comon_parameters_fun(axis_names=_axis_names), 3600)

    def ini_attributes(self):
        self.controller: Optional[QnwTC] = None
        self._wanted: Optional[DataActuator] = None  # the last target which was asked (as the user gave it)
        self._status_time = 0.
        self._known_faults: set[int] = set()

    # ----- connection ------------------------------------------------------------------------------------------

    def _port(self) -> str:
        other = str(self.settings['connection', 'other_port']).strip()
        return other or self.settings['connection', 'port']

    def _refresh_ports(self):
        """The ports of the computer now: a controller which was plugged in since PyMoDAQ started is in the list the
        next time the user looks"""
        ports, _ = port_choices()
        port = self.settings.child('connection', 'port')
        current = port.value()
        if current not in ports:
            ports.append(current)
        port.setLimits(ports)
        port.setValue(current)

    def ini_stage(self, controller=None):
        """Connect the controller and read its state

        Parameters
        ----------
        controller: object
            not used (one controller for one actuator)

        Returns
        -------
        info: str
        initialized: bool
            False if there is no controller on the port
        """
        settings = self.settings
        port = self._port()
        self.controller = QnwTC(port, baudrate=settings['connection', 'baudrate'],
                                timeout=settings['connection', 'reply_timeout'])
        try:
            self.controller.open()
            self._set_up()
        except Exception as e:
            if isinstance(e, QnwError):
                logger.error(str(e))
            else:
                logger.exception(str(e))
            self.controller.close()
            self.controller = None
            self._refresh_ports()
            return str(e), False
        info = self.controller.summary() + (' (SIMULATED CONTROLLER)' if is_simulated(port) else '')
        settings.child('connection', 'controller_info').setValue(info)
        if self.controller.holder_id not in SINGLE_HOLDERS:
            self.emit_status(ThreadCommand(ThreadStatus.UPDATE_STATUS,
                                           f'This is a {self.controller.holder_name}: only its sample holder (F1) '
                                           f'is controlled here'))
        return info, True

    def _set_up(self):
        """Settings which follow from the controller: the units, the allowed targets, the ramp, the state"""
        settings, controller = self.settings, self.controller
        settings.child('units').setValue(self._controller_units)
        low, high = controller.lowest_target, controller.highest_target
        if low is not None and high is not None:  # (not switched on: a target out of it is an error of the driver)
            settings.child('bounds', 'min_bound').setValue(low)
            settings.child('bounds', 'max_bound').setValue(high)
        has_speed = controller.stirrer_speeds is not None
        speed = settings.child('stirrer', 'stirrer_speed')
        speed.setOpts(readonly=not has_speed)
        if has_speed:
            speed.setOpts(min=int(controller.stirrer_speeds[0]), max=int(controller.stirrer_speeds[1]))
        controller.set_ramp_rate(settings['temperature', 'ramp'])  # (the controller may have been left with a ramp)
        self._known_faults.clear()
        self._wanted = None
        self._refresh_status(force=True)

    # ----- status ----------------------------------------------------------------------------------------------

    def _show(self, group: str, name: str, value):
        param = self.settings.child(group, name)
        if param.value() != value:
            param.setValue(value)

    def _refresh_status(self, force: bool = False):
        """Read what the controller says of itself (not at each reading of the temperature) and show it"""
        now = time.monotonic()
        if not force and now - self._status_time < self.settings['status', 'refresh']:
            return
        self._status_time = now
        controller = self.controller
        status = controller.status()
        for report in controller.check_faults(status):
            self._report_fault(report)
        self._show('temperature', 'control', status.control)
        self._show('stirrer', 'stirrer_on', status.stirring)
        self._show('status', 'state', status.state)
        self._show('status', 'fault', controller.fault.text if controller.fault is not None else '')
        self._show('status', 'target', f'{controller.target():.2f} °C')
        self._show('status', 'holder', f'{controller.holder_temperature(max_age=MAX_AGE):.2f} °C')
        probe = controller.probe_temperature()
        self._show('status', 'probe', 'not connected' if probe is None else f'{probe:.2f} °C')
        exchanger = controller.heat_exchanger_temperature()
        self._show('status', 'exchanger', 'not told' if exchanger is None else f'{exchanger:.0f} °C')

    def _report_fault(self, report):
        """Tell once that the controller reported a fault"""
        message = f'Quantum Northwest controller: {report.text}'
        logger.error(message)
        if report.code not in self._known_faults:
            self._known_faults.add(report.code)
            self.emit_status(ThreadCommand(ThreadStatus.UPDATE_STATUS, message))

    # ----- the actuator ----------------------------------------------------------------------------------------

    def get_actuator_value(self) -> DataActuator:
        """The temperature (°C) of the holder or of the probe"""
        if self.controller is None:
            raise QnwError('The controller is not connected')
        if self.settings['temperature', 'reading'] == 'Probe':
            temperature = self.controller.probe_temperature()
            if temperature is None:
                raise QnwError('No probe is connected: choose to measure the temperature of the holder')
        else:
            temperature = self.controller.holder_temperature(max_age=MAX_AGE)
        try:
            self._refresh_status()
        except QnwError as e:  # the temperature is the point: what the status says can wait
            logger.warning(f'The status of the controller cannot be read: {e}')
        return self.get_position_with_scaling(DataActuator(data=float(temperature), units=self.axis_unit))

    def user_condition_to_reach_target(self) -> bool:
        """The temperature is stable (said by the controller), if this is asked for"""
        if not self.settings['temperature', 'wait_stable']:
            return True
        try:
            status = self.controller.status()
        except QnwError as e:
            logger.warning(f'The stability of the temperature cannot be read: {e}')
            return False
        return status.stable or not status.control

    def _go(self, temperature: float):
        """Set the target temperature of the controller and switch its temperature control on"""
        self.controller.set_target(temperature)
        if self.settings['temperature', 'auto_on'] and self.controller.ensure_control():
            self._show('temperature', 'control', True)

    def _in_axis_unit(self, value: DataActuator) -> DataActuator:
        """The value in °C: the bounds of PyMoDAQ are numbers, which are the ones of the unit of the actuator"""
        if value.units == self.axis_unit:
            return value
        try:
            magnitude = value.value(self.axis_unit)
        except Exception:  # (no unit: it is meant in °C)
            magnitude = value.value()
        return DataActuator(self._title, data=float(magnitude), units=self.axis_unit)

    def move_abs(self, value: DataActuator):
        """Set the target temperature

        Parameters
        ----------
        value: DataActuator
            the target temperature
        """
        value = self.check_bound(self._in_axis_unit(value))
        self.target_value = value
        self._wanted = value
        self._go(self.set_position_with_scaling(value).value(self.axis_unit))

    def move_done(self, position: Optional[DataActuator] = None):
        """The move is over: show the state of the controller (not read while it went on). A program which sends a
        target every second and does not wait for the moves would make the controller busy with it"""
        if self.ispolling and self.controller is not None:
            try:
                self._refresh_status(force=True)
            except QnwError as e:
                logger.warning(f'The status of the controller cannot be read: {e}')
        super().move_done(position)

    def move_rel(self, value: DataActuator):
        """Change the target temperature by ``value`` (and not the temperature of the holder, which takes minutes to
        reach it)

        Parameters
        ----------
        value: DataActuator
            the difference (°C)
        """
        reference = self._wanted if self._wanted is not None else self.get_actuator_value()
        target = DataActuator(self._title, data=float(reference.value(self.axis_unit)) + float(value.value()),
                              units=self.axis_unit)
        self.move_abs(target)

    def move_home(self):
        """Go to the home temperature of the settings"""
        self.move_abs(DataActuator(self._title, data=float(self.settings['temperature', 'home']),
                                   units=self.axis_unit))

    def stop_motion(self):
        """Keep the temperature the holder has now (its target is set to it, and the ramp is stopped)"""
        if self.controller is None:
            return
        temperature = self.controller.hold()
        self._wanted = DataActuator(self._title, data=temperature, units=self.axis_unit)
        self.move_done()

    def commit_settings(self, param):
        """Send the settings which are the state of the controller to it"""
        if self.controller is None:
            return
        name, value = param.name(), param.value()
        if name == 'control':
            self.controller.set_control(bool(value))
            self._refresh_status(force=True)
        elif name in ('stirrer_on', 'stirrer_speed'):
            stirrer = self.settings.child('stirrer')
            if stirrer['stirrer_on']:
                self.controller.set_stirrer(True, stirrer['stirrer_speed'])
            elif name == 'stirrer_on':
                self.controller.set_stirrer(False)
        elif name == 'ramp':
            self.controller.set_ramp_rate(float(value))

    def close(self):
        """Leave the controller as the settings say, and close the port"""
        self.poll_timer.stop()  # (a move to a temperature takes minutes: it may be waited for when the module is closed)
        controller, self.controller = self.controller, None
        if controller is None:
            return
        try:
            mode = self.settings['closing', 'on_close']
            if mode in (CONTROL_OFF, ALL_OFF):
                controller.set_control(False)
            if mode == ALL_OFF:
                controller.set_stirrer(False)
        except QnwError as e:
            logger.warning(f'The controller was not left as asked: {e}')
        finally:
            controller.close()


if __name__ == '__main__':
    main(__file__)
