"""Try the plugin without hardware: a Dashboard with a Mock spectrometer and a simulated Quantum Northwest controller

    qnw_tc_demo

opens the ThermoSpectro extension (``pymodaq_plugins_thermospectro``) on a Mock 1D detector (*Spectro1*) and the
``QNW_TC`` actuator (*Thermo1*) on a simulated TC 125, and connects them. ``qnw_tc_demo --port sim://tc1b?speed=60``
chooses another simulated controller (the simulated time runs ``speed`` times faster than the real one).

With the real instruments (a Seabreeze spectrometer and a controller on a serial port)::

    qnw_tc_start /dev/ttyUSB0

Nothing is initialized by ``qnw_tc_start`` before the extension is open: the modules are connected by it, as by the
*Connect the modules* button.
"""
from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

DEFAULT_PORT = 'sim://tc125?speed=30'


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = argparse.ArgumentParser(prog='qnw_tc_demo', description=__doc__.split('\n\n')[0])
    arguments.add_argument('--port', default=DEFAULT_PORT, help=f'a simulated controller ({DEFAULT_PORT}) or a real '
                                                                'serial port (/dev/ttyUSB0, COM3)')
    arguments.add_argument('--detector', default='Mock', help='the 1D detector plugin: Mock (default) or Seabreeze')
    args = arguments.parse_args(argv)
    sys.argv = sys.argv[:1]  # (PyMoDAQ reads the command line too)

    from pymodaq_gui.qt_utils import mkQApp
    from pymodaq.dashboard import create_load_dashboard  # (first: the order of the imports of PyMoDAQ matters)
    from pymodaq.control_modules.enums import DAQTypesEnum
    from pymodaq.extensions import ExtensionEnum
    from pymodaq.utils.managers.modules import ModuleType
    from pymodaq.utils.managers.modules.loader import PluginInfo
    from qtpy import QtCore

    app = mkQApp('Dashboard')
    shared_ui, dashboard = create_load_dashboard(show_dashboard=True)
    if 'THERMOSPECTRO' not in ExtensionEnum.names():
        print('The ThermoSpectro extension is not installed: python -m pip install -e '
              '/path/to/pymodaq_plugins_thermospectro')
        return 1
    manager = dashboard.modules_manager
    infos = [PluginInfo(id=manager.get_random_id(), name='Thermo1', class_name='QNW_TC', type=ModuleType.Actuator,
                        is_master=True, do_init=False, ui=None, daq_type=None, axis_name='Temperature'),
             PluginInfo(id=manager.get_random_id(), name='Spectro1', class_name=args.detector, type=ModuleType.Detector,
                        is_master=True, do_init=False, ui=None, daq_type=DAQTypesEnum.DAQ1D)]
    dashboard.module_creator.load_created_modules([[info] for info in infos])
    state = {}

    def step():
        if len(manager.modules_all) < len(infos):
            return
        timer.stop()
        thermostat = manager.get_mod_from_name('Thermo1', mod=ModuleType.Actuator)
        thermostat.settings.child(thermostat._hw_settings_name, 'connection', 'other_port').setValue(args.port)
        state['extension'] = dashboard.load_extension(ExtensionEnum['THERMOSPECTRO'])
        state['extension'].get_action('initialize').trigger()  # Connect the modules

    def close_everything():
        # Qt aborts ("QThread: Destroyed while thread is still running") if it quits with modules alive: close them
        app.aboutToQuit.disconnect(close_everything)
        timer.stop()
        dashboard.quit_fun()

    timer = QtCore.QTimer()
    timer.setInterval(200)
    timer.timeout.connect(step)
    timer.start()
    app.aboutToQuit.connect(close_everything)
    return app.exec()


def start(argv: Optional[Sequence[str]] = None) -> int:
    """The real instruments: ``qnw_tc_start PORT`` is a Seabreeze spectrometer and the controller on PORT"""
    arguments = argparse.ArgumentParser(prog='qnw_tc_start', description='ThermoSpectro with a Seabreeze spectrometer '
                                                                         'and a Quantum Northwest controller.')
    arguments.add_argument('port', help='the serial port of the controller (/dev/ttyUSB0, COM3)')
    args = arguments.parse_args(argv)
    return main(['--detector', 'Seabreeze', '--port', args.port])


if __name__ == '__main__':
    sys.exit(main())
