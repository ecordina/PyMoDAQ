import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))  # (helpers.py)

# the tests of the plugin build real Qt objects: no display needed
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pymodaq_plugins_quantumnorthwest.hardware import qnw_tc  # noqa: E402
from pymodaq_plugins_quantumnorthwest.hardware.qnw_tc import QnwTC  # noqa: E402


@pytest.fixture(params=['sim://tc125', 'sim://tc1b'], ids=['tc125', 'tc1b'])
def model_url(request):
    """Every test which uses it runs for the TC 125 and for the TC 1B"""
    return request.param


@pytest.fixture(autouse=True)
def quick_quiet_commands(monkeypatch, request):
    """A controller is not given the time to refuse a command which has no answer, but in the tests which are about it"""
    if request.cls is None or request.cls.__name__ != 'TestTheQpod':
        monkeypatch.setattr(qnw_tc, 'QUIET_WAIT', 0.)


@pytest.fixture
def make_tc():
    """Opens controllers (``make_tc('sim://tc1b', timeout=1)``) and closes them at the end of the test"""
    opened = []

    def make(url='sim://tc125', **kwargs):
        kwargs.setdefault('timeout', 0.5)
        tc = QnwTC(url, **kwargs)
        tc.open()
        opened.append(tc)
        return tc

    yield make
    for tc in opened:
        tc.close()
