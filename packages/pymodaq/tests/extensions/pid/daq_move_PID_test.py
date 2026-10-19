# -*- coding: utf-8 -*-
from collections import deque

from qtpy import QtCore

from pymodaq.extensions.pid.actuator_controller import PIDController
from pymodaq.extensions.pid.daq_move_PID import DAQ_Move_PID


class FakeDAQPID(QtCore.QObject):
    """What the PIDController of the actuator needs from the PID extension"""
    curr_points_signal = QtCore.Signal(dict)
    setpoints_signal = QtCore.Signal(dict)
    emit_curr_points_sig = QtCore.Signal()

    def __init__(self):
        super().__init__()
        self.queue_points = {'setpoint': deque(maxlen=20)}


class TestIniStage:

    def test_without_controller(self):
        # what the Dashboard does with a PID actuator that is part of an experiment: there is no controller, it is
        # only given by the PID extension, when it creates the setpoint actuators
        actuator = DAQ_Move_PID()

        info, initialized = actuator.ini_stage(None)

        assert not initialized
        assert 'PID extension' in info
        assert actuator.controller is None

    def test_with_controller(self):
        daq_pid = FakeDAQPID()  # has to be kept alive as long as its signals are used
        controller = PIDController(daq_pid, 'setpoint')
        actuator = DAQ_Move_PID()

        info, initialized = actuator.ini_stage(controller)

        assert initialized
        assert actuator.controller is controller
        assert actuator.settings['check_stab', 'queue_length'] == 20


def test_settings_change_without_controller():
    # the settings of an actuator that could not be initialized can still be edited
    actuator = DAQ_Move_PID()
    actuator.commit_settings(actuator.settings.child('check_stab', 'queue_length'))
