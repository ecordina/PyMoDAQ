"""Small things the tests share"""
from pymodaq_plugins_quantumnorthwest.hardware.qnw_tc import QnwTC


def simulation(tc: QnwTC):
    """The simulated controller behind an open :class:`QnwTC`"""
    return tc._port.controller
