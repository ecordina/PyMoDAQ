"""The trigger mode of the Thorlabs cameras: one image per software trigger, the camera idle in between"""
import importlib.util
import sys
import threading
import time
import types
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest
from qtpy import QtCore

from pymodaq_data.data import DataRaw, DataToExport
from pymodaq_gui.parameter import Parameter

from pymodaq_plugins_thermospectro import trigger_mode as tm

pytestmark = pytest.mark.filterwarnings('ignore')

SHAPE = (6, 8)


class FakeTLCamera:
    """The parts of pylablib's ``ThorlabsTLCamera`` which the trigger mode and the buffer use, with the counters of its
    frames: a trigger makes one frame `exposure` seconds later, a camera armed in the continuous mode makes one every
    `exposure` seconds until it is cleared, ``wait_for_frame`` waits for a frame which was not read (``since='lastread'``) or
    not waited for (``'lastwait'``), ``read_multiple_images`` returns the unread frames, and a camera which is not armed does
    not answer"""

    def __init__(self, exposure=0.02):
        self.exposure = exposure
        self.armed = False
        self.continuous = False
        self.frames_per_trigger = self.auto_start = self.nframes = None
        self.acquired = self.read = self.waited = 0
        self.frames = []
        self.triggers = self.starts = self.clears = 0
        self.silent = False  # no frame comes
        self.generation = 0  # (a thread which makes frames stops when the camera is cleared)
        self.lock = threading.Lock()

    def get_exposure(self):
        return self.exposure

    def clear_acquisition(self):
        self.clears += 1
        self.armed = self.continuous = False
        self.generation += 1
        self.frames, self.acquired, self.read, self.waited = [], 0, 0, 0

    def start_acquisition(self, frames_per_trigger='default', auto_start=True, nframes=None):
        self.starts += 1
        self.armed = True
        self.frames_per_trigger, self.auto_start, self.nframes = frames_per_trigger, auto_start, nframes
        self.continuous = frames_per_trigger in ('default', None, 0)
        if self.continuous and auto_start:
            threading.Thread(target=self._free_run, args=(self.generation,), daemon=True).start()
        elif auto_start:
            self.send_software_trigger()

    def _free_run(self, generation):
        while generation == self.generation and self.armed:
            time.sleep(self.exposure)
            if generation == self.generation and not self.silent:
                self.make_frame()

    def acquisition_in_progress(self):
        return self.armed

    def send_software_trigger(self):
        if not self.armed:
            raise RuntimeError('the camera is not armed')
        self.triggers += 1
        if not self.silent:
            threading.Timer(self.exposure, self.make_frame).start()

    def make_frame(self):
        with self.lock:
            self.frames.append(np.full(SHAPE, self.acquired, dtype=np.uint16))
            self.acquired += 1

    def wait_for_frame(self, since='lastread', nframes=1, timeout=20., error_on_stopped=False):
        assert since in ('lastread', 'lastwait')  # ('now' would miss a frame which came before the wait began)
        if not self.armed:
            if error_on_stopped:
                raise RuntimeError('waiting for a frame while acquisition is stopped')
            return False
        deadline = time.monotonic() + timeout
        while True:
            if not self.armed:  # (stopped while it waited: pylablib raises, or returns False when it was not asked to)
                if error_on_stopped:
                    raise RuntimeError('waiting for a frame while acquisition is stopped')
                return False
            with self.lock:
                last = self.read if since == 'lastread' else self.waited
                if self.acquired >= last + nframes:
                    self.waited = self.acquired
                    return True
            if time.monotonic() > deadline:
                raise TimeoutError('no frame')
            time.sleep(0.001)

    def read_multiple_images(self, rng=None, peek=False, missing_frame='skip', return_info=False, return_rng=False):
        if not self.armed:
            return None
        with self.lock:
            new, self.read = self.frames[self.read:self.acquired], self.acquired
        return np.array(new) if new else np.zeros((0,) + SHAPE, dtype=np.uint16)


@pytest.fixture
def camera():
    return FakeTLCamera()


class TestTheDriverIsAskedForOneImagePerTrigger:
    def test_the_camera_is_armed_for_one_frame_per_trigger_and_idles(self, camera):
        tm.arm_one_frame_per_trigger(camera)
        time.sleep(0.15)

        assert camera.armed and camera.frames_per_trigger == 1 and camera.auto_start is False
        assert camera.triggers == 0 and camera.acquired == 0  # (nothing is exposed until it is triggered)

    def test_it_was_cleared_first(self, camera):
        camera.armed = True

        tm.arm_one_frame_per_trigger(camera)

        assert camera.clears == 1 and camera.starts == 1

    def test_a_trigger_gives_one_image(self, camera):
        tm.arm_one_frame_per_trigger(camera)

        image = tm.one_frame(camera)

        assert image.shape == SHAPE and image[0, 0] == 0
        assert camera.triggers == 1 and camera.acquired == 1

    def test_each_readout_is_a_trigger_and_an_image_of_its_own(self, camera):
        tm.arm_one_frame_per_trigger(camera)

        values = [int(tm.one_frame(camera)[0, 0]) for _ in range(4)]

        assert values == [0, 1, 2, 3] and camera.triggers == 4
        assert camera.starts == 1  # (armed once: nothing is armed or stopped for each readout)

    def test_the_camera_is_idle_between_two_readouts(self, camera):
        tm.arm_one_frame_per_trigger(camera)
        tm.one_frame(camera)

        time.sleep(0.2)  # (a free-running camera would have made ten images)

        assert camera.acquired == 1

    def test_a_late_image_of_a_trigger_which_was_given_up_is_not_taken_for_the_next_one(self, camera):
        tm.arm_one_frame_per_trigger(camera)
        camera.make_frame()  # (the image of an earlier trigger, which came after it was given up)

        image = tm.one_frame(camera)

        assert image[0, 0] == 1  # (the image of this trigger, not the stale one)

    def test_the_images_to_average(self, camera):
        tm.arm_one_frame_per_trigger(camera)

        frames = tm.grab_triggered(camera, 3)

        assert frames.shape == (3, 1) + SHAPE and frames.dtype == np.uint16
        assert frames[:, 0, 0, 0].tolist() == [0, 1, 2] and camera.triggers == 3

    def test_one_image_is_the_default(self, camera):
        tm.arm_one_frame_per_trigger(camera)

        assert tm.grab_triggered(camera).shape == (1, 1) + SHAPE

    def test_an_image_which_does_not_come_is_an_error(self, camera, monkeypatch):
        monkeypatch.setattr(tm, 'FRAME_TIMEOUT_MARGIN', 0.1)
        camera.exposure, camera.silent = 0., True
        tm.arm_one_frame_per_trigger(camera)

        with pytest.raises(TimeoutError):
            tm.one_frame(camera)

    def test_a_camera_which_is_not_armed_is_an_error(self, camera):
        with pytest.raises(RuntimeError, match='not armed'):
            tm.one_frame(camera)

    def test_a_camera_which_was_stopped_while_waiting_is_an_error_not_a_silent_nothing(self, camera):
        tm.arm_one_frame_per_trigger(camera)
        camera.silent = True
        threading.Timer(0.1, camera.clear_acquisition).start()

        with pytest.raises(RuntimeError, match='stopped'):
            tm.one_frame(camera)

    @pytest.mark.parametrize('exposure, expected', [(0.3, 10.6), (0., 10.), (4., 18.)])
    def test_the_time_an_image_is_waited_for(self, camera, exposure, expected):
        camera.exposure = exposure

        assert tm.frame_timeout(camera) == pytest.approx(expected)

    def test_a_driver_which_does_not_say_its_exposure_has_the_margin_only(self):
        class Mute:
            def get_exposure(self):
                raise RuntimeError

        assert tm.frame_timeout(Mute()) == tm.FRAME_TIMEOUT_MARGIN


class TestTheSettingOfTheModule:
    @staticmethod
    def module(value=tm.ACQ_TRIGGER, nested=True):
        param = dict(tm.TRIGGER_PARAMS[0], value=value)
        children = [{'name': 'detector_settings', 'type': 'group', 'children': [param]}] if nested else [param]
        return types.SimpleNamespace(settings=Parameter.create(name='settings', type='group', children=children))

    def test_the_default_is_the_trigger_mode(self):
        assert tm.TRIGGER_PARAMS[0]['value'] == tm.ACQ_TRIGGER
        assert tm.ACQ_TRIGGER in tm.TRIGGER_PARAMS[0]['limits'] and tm.ACQ_CONTINUOUS in tm.TRIGGER_PARAMS[0]['limits']

    @pytest.mark.parametrize('nested', [True, False])
    def test_a_module_in_the_trigger_mode(self, nested):
        assert tm.trigger_mode_of(self.module(nested=nested)) is True

    def test_a_module_which_free_runs(self):
        assert tm.trigger_mode_of(self.module(tm.ACQ_CONTINUOUS)) is False

    def test_a_module_without_the_setting_is_not_in_it(self):
        module = types.SimpleNamespace(settings=Parameter.create(name='settings', type='group', children=[]))

        assert tm.trigger_mode_of(module) is False
        assert tm.trigger_mode_of(None) is False
        assert tm.trigger_mode_of(types.SimpleNamespace()) is False


# ----------------------------------------------------------------------------------------------------------------------
# the plugin, with stand-ins for what it extends (the plugin of pymodaq_plugins_thorlabs needs the SDK of Thorlabs)
# ----------------------------------------------------------------------------------------------------------------------
@dataclass
class Grab:
    do_acquisition: bool = True
    snap: bool = False
    since: str = 'now'
    nframes: int = 1
    n_average: int = 1


class StockCallback(QtCore.QObject):
    """What the stock plugin does in its thread, for the tests: the continuous mode"""
    data_sig = QtCore.Signal(np.ndarray)
    error = QtCore.Signal()
    continuous_runs = []

    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.do_acquisition = True

    def set_do_grab(self, mode):
        self.do_acquisition = mode.do_acquisition
        if mode.do_acquisition:
            self.wait_for_acquisition(mode)

    def wait_for_acquisition(self, mode):
        StockCallback.continuous_runs.append(mode)


class StockPlugin(QtCore.QObject):
    """The plugin of pymodaq_plugins_thorlabs, as far as the trigger mode touches it"""
    params = [{'title': 'Exposure Time (ms)', 'name': 'exposure_time', 'type': 'int', 'value': 10},
              {'title': 'Buffer', 'name': 'buffer', 'type': 'group', 'children': [
                  {'name': 'mode', 'type': 'list', 'value': 'now', 'limits': ['now', 'lastread']}]}]
    callback_signal = QtCore.Signal(object)
    dte_signal = QtCore.Signal(object)

    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.settings = Parameter.create(name='settings', type='group', children=self.params)
        self.callback_thread = None
        self.frames, self.statuses, self.errors, self.committed, self.continuous_grabs = [], [], [], [], []
        self.emitted = []  # the data the plugin emitted

    def _prepare_view(self):
        pass

    def emit_data(self, frame=None):
        self.frames.append(frame)
        self.dte_signal.emit(DataToExport('Camera', data=[DataRaw('image', data=[np.asarray(frame)[0, 0]])]))

    def handle_error(self):
        self.errors.append(True)

    def emit_status(self, command):
        self.statuses.append(command)

    def grab_data(self, Naverage=1, **kwargs):
        self.continuous_grabs.append((Naverage, kwargs))

    def commit_settings(self, param):
        self.committed.append(param.name())

    def stop(self):
        self.callback_signal.emit(Grab(do_acquisition=False))
        QtCore.QCoreApplication.processEvents()
        self.controller.clear_acquisition()


@pytest.fixture
def plugin_class(monkeypatch):
    """The class of the plugin, loaded from its file with the stock plugin replaced by stand-ins"""
    def package(name, **attributes):
        module = types.ModuleType(name)
        module.__path__ = []
        for key, value in attributes.items():
            setattr(module, key, value)
        monkeypatch.setitem(sys.modules, name, module)
        return module

    package('pymodaq_plugins_thorlabs')
    package('pymodaq_plugins_thorlabs.daq_viewer_plugins')
    package('pymodaq_plugins_thorlabs.daq_viewer_plugins.plugins_2D')
    package('pymodaq_plugins_thorlabs.daq_viewer_plugins.plugins_2D.daq_2Dviewer_Thorlabs_TSI',
            DAQ_2DViewer_Thorlabs_TSI=StockPlugin)
    package('pymodaq_plugins_utils')
    package('pymodaq_plugins_utils.hardware')
    package('pymodaq_plugins_utils.hardware.camera_base_pylablib', CameraCallback=StockCallback, Grab=Grab)

    import pymodaq_plugins_thermospectro as root
    path = Path(root.__file__).parent / 'daq_viewer_plugins' / 'plugins_2D' / 'daq_2Dviewer_Thorlabs_TSI_Trigger.py'
    spec = importlib.util.spec_from_file_location('trigger_plugin_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    StockCallback.continuous_runs.clear()
    return module.DAQ_2DViewer_Thorlabs_TSI_Trigger


@pytest.fixture
def plugin(plugin_class, camera):
    plugin = plugin_class(camera)
    plugin.setup_callback_thread()
    plugin.dte_signal.connect(plugin.emitted.append)
    yield plugin
    plugin.callback_thread.callback.watching = False
    thread = plugin.callback_thread
    thread.quit()
    thread.wait(2000)
    camera.clear_acquisition()


def acquisition(plugin, value):
    plugin.settings.child(tm.ACQUISITION).setValue(value)
    return plugin.settings.child(tm.ACQUISITION)


class TestThePlugin:
    def test_it_has_the_settings_of_the_stock_plugin_and_the_acquisition(self, plugin_class):
        names = [param['name'] for param in plugin_class.params]

        assert names == ['exposure_time', 'buffer', tm.ACQUISITION]
        assert plugin_class.params[-1]['value'] == tm.ACQ_TRIGGER

    def test_an_image_is_a_trigger(self, qtbot, plugin, camera):
        plugin.grab_data(1, live=False)

        qtbot.waitUntil(lambda: bool(plugin.frames), timeout=5000)

        assert plugin.frames[0].shape == (1, 1) + SHAPE
        assert camera.triggers == 1 and camera.frames_per_trigger == 1 and camera.auto_start is False
        assert not plugin.continuous_grabs and not plugin.errors

    def test_the_camera_is_armed_once(self, qtbot, plugin, camera):
        for index in range(3):
            plugin.grab_data(1, live=False)
            qtbot.waitUntil(lambda: len(plugin.frames) == index + 1, timeout=5000)

        assert camera.starts == 1 and camera.triggers == 3
        assert [int(frame[0, 0, 0, 0]) for frame in plugin.frames] == [0, 1, 2]

    def test_the_images_to_average_are_triggers_too(self, qtbot, plugin, camera):
        plugin.grab_data(4, live=False)

        qtbot.waitUntil(lambda: bool(plugin.frames), timeout=5000)

        assert plugin.frames[0].shape == (4, 1) + SHAPE and camera.triggers == 4

    def test_the_live_view_triggers_again_and_again_until_it_is_stopped(self, qtbot, plugin, camera):
        plugin.grab_data(1, live=True)
        qtbot.waitUntil(lambda: len(plugin.frames) >= 3, timeout=5000)

        plugin.callback_thread.callback.do_acquisition = False  # (what stopping does)
        qtbot.wait(150)
        done = len(plugin.frames)
        qtbot.wait(150)

        assert done >= 3 and len(plugin.frames) == done  # (no more after it stopped)

    @pytest.mark.errors_expected
    def test_a_trigger_which_gets_no_image_is_an_error_of_the_plugin(self, qtbot, plugin, camera, monkeypatch):
        monkeypatch.setattr(tm, 'FRAME_TIMEOUT_MARGIN', 0.1)
        camera.exposure, camera.silent = 0., True

        plugin.grab_data(1, live=False)

        qtbot.waitUntil(lambda: bool(plugin.errors), timeout=5000)
        assert not plugin.frames

    def test_the_continuous_mode_is_what_the_stock_plugin_does(self, qtbot, plugin, camera):
        acquisition(plugin, tm.ACQ_CONTINUOUS)

        plugin.grab_data(1, live=False)

        assert plugin.continuous_grabs == [(1, {'live': False})]
        assert camera.starts == 0 and camera.triggers == 0
        assert plugin.callback_thread.callback.triggered is False

    def test_the_callback_follows_the_mode(self, qtbot, plugin, camera):
        plugin.grab_data(1, live=False)
        assert plugin.callback_thread.callback.triggered is True
        qtbot.waitUntil(lambda: bool(plugin.frames), timeout=5000)

        acquisition(plugin, tm.ACQ_CONTINUOUS)
        plugin.grab_data(1, live=False)
        qtbot.waitUntil(lambda: bool(StockCallback.continuous_runs or plugin.continuous_grabs), timeout=2000)

        assert plugin.callback_thread.callback.triggered is False

    def test_a_grab_which_cannot_arm_the_camera_is_told_not_raised(self, qtbot, plugin, camera):
        def refuse(*args, **kwargs):
            raise RuntimeError('cannot arm')
        camera.start_acquisition = refuse

        plugin.grab_data(1, live=False)

        assert len(plugin.statuses) == 1 and 'cannot arm' in plugin.statuses[0].attribute[0]


class TestChangingSettings:
    def test_a_new_mode_disarms_the_camera_so_that_the_next_image_arms_it_in_the_new_one(self, plugin, camera):
        tm.arm_one_frame_per_trigger(camera)
        camera.clears = 0

        plugin.commit_settings(acquisition(plugin, tm.ACQ_CONTINUOUS))

        assert not camera.armed and camera.clears == 1
        assert plugin.committed == []  # (the stock plugin has nothing to do with it)

    def test_an_exposure_time_changed_in_the_trigger_mode_is_set_on_a_camera_which_is_not_armed(self, plugin, camera):
        tm.arm_one_frame_per_trigger(camera)
        camera.clears = 0

        plugin.commit_settings(plugin.settings.child('exposure_time'))

        assert not camera.armed and camera.clears == 1
        assert plugin.committed == ['exposure_time']  # (then the stock plugin sets it)

    def test_an_exposure_time_changed_without_the_camera_armed_is_just_set(self, plugin, camera):
        plugin.commit_settings(plugin.settings.child('exposure_time'))

        assert camera.clears == 0 and plugin.committed == ['exposure_time']

    def test_in_the_continuous_mode_the_stock_plugin_does_it_all(self, plugin, camera):
        acquisition(plugin, tm.ACQ_CONTINUOUS)
        camera.armed = True

        plugin.commit_settings(plugin.settings.child('exposure_time'))

        assert camera.armed and camera.clears == 0 and plugin.committed == ['exposure_time']

    def test_the_other_settings_are_left_to_the_stock_plugin(self, plugin, camera):
        plugin.commit_settings(plugin.settings.child('buffer', 'mode'))

        assert plugin.committed == ['mode'] and camera.clears == 0


class TestTheUnitKnowsTheMode:
    """A camera in the trigger mode makes no image between its readouts: nothing to wait for when its exposure changes"""

    @pytest.fixture
    def bench(self, qtbot):
        from test_camera import Bench
        bench = Bench(qtbot)
        bench.camera.settings.addChild(dict(tm.TRIGGER_PARAMS[0], value=tm.ACQ_TRIGGER))
        yield bench
        from pymodaq_plugins_thermospectro.unit import UnitState
        if bench.unit.state != UnitState.IDLE:
            bench.unit._release()

    def test_it_does_not_run_between_readouts(self, bench):
        assert bench.unit._runs_between_readouts() is False

        bench.camera.settings.child(tm.ACQUISITION).setValue(tm.ACQ_CONTINUOUS)

        assert bench.unit._runs_between_readouts() is True

    def test_a_camera_which_is_disarmed_does_not_either(self, bench):
        bench.camera.settings.child(tm.ACQUISITION).setValue(tm.ACQ_CONTINUOUS)
        bench.settings.child('disarm').setValue(True)

        assert bench.unit._runs_between_readouts() is False

    def test_a_change_of_the_exposure_makes_no_readout_wait(self, bench):
        from test_camera_exposure import change, run, wait_run
        run(bench, readout=3.)
        wait_run(bench, 1)

        change(bench, 40.)

        assert bench.unit._settle_until == 0.0 and not bench.unit._must_wait()
        assert bench.camera.settings[('camera_settings', 'exposure_time')] == 40.  # (and it was given at once)

    def test_the_wait_is_there_in_the_continuous_mode(self, bench):
        from test_camera_exposure import change, run, wait_run
        bench.camera.settings.child(tm.ACQUISITION).setValue(tm.ACQ_CONTINUOUS)
        run(bench, readout=3.)
        wait_run(bench, 1)

        change(bench, 40.)

        assert bench.unit._settle_until > time.monotonic()

    def test_disarming_a_camera_which_is_triggered_is_advised_against(self, bench):
        bench.settings.child('disarm').setValue(True)

        message = bench.unit.disarm_warning()

        assert message and 'not needed' in message and 'trigger mode' in message

    def test_without_it_the_usual_warning_is_the_only_one(self, bench):
        bench.camera.settings.child(tm.ACQUISITION).setValue(tm.ACQ_CONTINUOUS)
        bench.settings.child('disarm').setValue(True)
        bench.settings.child('readout_time').setValue(0.1)

        message = bench.unit.disarm_warning()

        assert message and 'not needed' not in message and 'so close' in message

    def test_nothing_to_say_when_it_is_not_disarmed(self, bench):
        assert bench.unit.disarm_warning() is None


# ----------------------------------------------------------------------------------------------------------------------
# the newest image: a buffer of the last five images of a camera which runs
# ----------------------------------------------------------------------------------------------------------------------
class Clock:
    def __init__(self):
        self.now = 100.

    def __call__(self):
        self.now += 1.
        return self.now


class TestTheBuffer:
    @staticmethod
    def filled(n, size=5):
        fifo = tm.FrameFifo(size, Clock())
        for index in range(n):
            fifo.put(np.full(SHAPE, index))
        return fifo

    def test_the_newest_image_is_taken(self):
        fifo = self.filled(3)

        entry, = fifo.take_newest()

        assert entry.frame[0, 0] == 2 and entry.sequence == 3 and entry.time == 103.

    def test_an_image_is_never_taken_twice(self):
        fifo = self.filled(3)
        fifo.take_newest()

        assert fifo.take_newest() is None and fifo.unseen() == 0

    def test_the_next_one_which_comes_is_the_new_newest(self):
        fifo = self.filled(3)
        fifo.take_newest()
        fifo.put(np.full(SHAPE, 3))

        assert fifo.take_newest()[0].frame[0, 0] == 3

    def test_the_images_which_were_not_taken_are_dropped_when_a_newer_one_is(self):
        fifo = self.filled(4)

        fifo.take_newest()  # (1, 2 and 3 are older than 4: they are not taken any more)

        assert fifo.unseen() == 0 and fifo.take_newest() is None

    def test_it_keeps_the_last_five_first_in_first_out(self):
        fifo = self.filled(8)

        entries = fifo.take_newest(5)

        assert [int(e.frame[0, 0]) for e in entries] == [3, 4, 5, 6, 7]  # (oldest first, the first three are gone)
        assert fifo.unseen() == 0

    def test_it_cannot_give_more_than_it_holds(self):
        fifo = self.filled(8)

        assert fifo.unseen() == 5 and fifo.take_newest(6) is None

    def test_several_images_to_average(self):
        fifo = self.filled(4)

        assert fifo.take_newest(3) is not None
        assert self.filled(2).take_newest(3) is None  # (not enough new ones yet)

    def test_a_cleared_buffer_has_nothing_new(self):
        fifo = self.filled(3)

        fifo.clear()

        assert fifo.take_newest() is None
        fifo.put(np.zeros(SHAPE))
        assert fifo.unseen() == 1

    def test_an_image_has_the_time_it_arrived(self):
        fifo = tm.FrameFifo(5, Clock())
        fifo.put(np.zeros(SHAPE), when=42.)

        assert fifo.take_newest()[0].time == 42.

    def test_the_frames_to_emit(self):
        fifo = self.filled(3)

        when, frames = tm.newest_frames(fifo, 2)

        assert when == 103. and frames.shape == (2, 1) + SHAPE and frames[:, 0, 0, 0].tolist() == [1, 2]
        assert tm.newest_frames(fifo) is None


class TestWatchingTheCamera:
    def test_the_images_which_come_are_put_in_the_buffer_with_the_time_they_came(self):
        camera = FakeTLCamera(exposure=0.03)
        fifo = tm.FrameFifo()
        trigger_mode_arm = tm.arm_continuous
        trigger_mode_arm(camera)
        assert camera.armed and camera.frames_per_trigger == 'default' and camera.nframes == tm.FIFO_SIZE

        before = time.time()
        deadline = time.monotonic() + 3
        while fifo.sequence < 3 and time.monotonic() < deadline:
            tm.collect(camera, fifo)
        camera.clear_acquisition()

        entries = fifo.take_newest(3)
        assert [int(e.frame[0, 0]) for e in entries] == [0, 1, 2]
        assert all(before <= e.time <= time.time() for e in entries)
        assert entries[0].time < entries[2].time  # (each has the time it came)

    def test_a_wait_without_an_image_gives_nothing(self):
        camera = FakeTLCamera()
        camera.silent = True
        tm.arm_continuous(camera)

        assert tm.collect(camera, tm.FrameFifo(), timeout=0.05) == 0

    def test_no_image_is_missed_between_two_waits(self):
        camera = FakeTLCamera(exposure=0.02)
        fifo = tm.FrameFifo()
        tm.arm_continuous(camera)

        time.sleep(0.2)  # (several images come while nobody waits)
        tm.collect(camera, fifo)
        tm.collect(camera, fifo, timeout=0.5)
        camera.clear_acquisition()

        assert fifo.sequence >= 5

    def test_a_camera_which_is_not_armed_ends_the_watch(self):
        with pytest.raises(RuntimeError, match='stopped'):
            tm.collect(FakeTLCamera(), tm.FrameFifo())

    def test_what_the_driver_calls_a_timeout_is_only_a_wait_without_an_image(self):
        class MyTimeout(Exception):
            pass

        class Slow(FakeTLCamera):
            TimeoutError = MyTimeout

            def wait_for_frame(self, *args, **kwargs):
                raise MyTimeout

        assert tm.collect(Slow(), tm.FrameFifo()) == 0


class TestDatingTheData:
    def test_the_data_are_dated_with_the_time_the_image_arrived(self):
        sent = []
        signal = types.SimpleNamespace(emit=lambda dte: sent.append(dte), connect=lambda slot: 'connected')
        owner = types.SimpleNamespace(stamp=1234.5)
        dte = DataToExport('Camera', data=[DataRaw('image', data=[np.zeros(SHAPE)]), DataRaw('other', data=[np.zeros(3)])])

        tm.StampingSignal(signal, owner).emit(dte)

        assert sent == [dte] and [dwa.timestamp for dwa in dte.data] == [1234.5, 1234.5] and dte.timestamp == 1234.5

    def test_without_a_time_the_data_are_left_as_they_are(self):
        sent = []
        dte = DataToExport('Camera', data=[DataRaw('image', data=[np.zeros(SHAPE)])])
        made = dte.data[0].timestamp

        tm.StampingSignal(types.SimpleNamespace(emit=sent.append), types.SimpleNamespace(stamp=None)).emit(dte)

        assert sent == [dte] and dte.data[0].timestamp == made

    def test_the_rest_of_the_signal_is_the_one_it_wraps(self):
        signal = types.SimpleNamespace(emit=lambda dte: None, connect=lambda slot: 'connected')

        assert tm.StampingSignal(signal, types.SimpleNamespace()).connect(None) == 'connected'


class TestThePluginWithTheNewestImage:
    @pytest.fixture
    def newest(self, plugin, camera):
        camera.exposure = 0.2  # (a camera which makes an image every 0.2 s)
        acquisition(plugin, tm.ACQ_NEWEST)
        plugin.commit_settings(plugin.settings.child(tm.ACQUISITION))
        return plugin

    def test_the_camera_is_armed_once_to_run_and_watched(self, qtbot, newest, camera):
        newest.grab_data(1, live=False)

        qtbot.waitUntil(lambda: bool(newest.frames), timeout=5000)

        assert camera.continuous and camera.nframes == tm.FIFO_SIZE and camera.starts == 1 and camera.triggers == 0
        assert newest.callback_thread.callback.watching and newest.callback_thread.callback.mode == tm.ACQ_NEWEST
        camera.clear_acquisition()

    def test_a_readout_takes_the_image_which_is_there_without_waiting_for_the_next(self, qtbot, newest, camera):
        newest.grab_data(1, live=False)
        qtbot.waitUntil(lambda: len(newest.frames) == 1, timeout=5000)
        qtbot.wait(500)  # (two or three images came since: the one taken is older than the next one to come by 0.2 s)

        asked = time.monotonic()
        newest.grab_data(1, live=False)
        qtbot.waitUntil(lambda: len(newest.frames) == 2, timeout=5000)

        assert time.monotonic() - asked < 0.15  # (less than the 0.2 s an image takes: it did not wait for one)
        assert newest.frames[1][0, 0, 0, 0] > newest.frames[0][0, 0, 0, 0]  # (and it is a newer image than the first)
        camera.clear_acquisition()

    def test_the_data_are_dated_with_the_time_the_image_arrived_not_the_time_it_was_taken_out(self, qtbot, newest, camera):
        newest.grab_data(1, live=False)
        qtbot.waitUntil(lambda: len(newest.emitted) == 1, timeout=5000)
        qtbot.wait(450)  # (two images came since the first was taken)

        asked = time.time()
        newest.grab_data(1, live=False)
        qtbot.waitUntil(lambda: len(newest.emitted) == 2, timeout=5000)

        stamp = newest.emitted[1].data[0].timestamp
        assert stamp < asked  # (the image was already there when it was asked for)
        assert asked - stamp < 0.45 and newest.stamp is None  # (the newest of the two, not older)
        camera.clear_acquisition()

    def test_an_image_is_never_given_twice(self, qtbot, newest, camera):
        camera.exposure = 0.3
        newest.grab_data(1, live=False)
        qtbot.waitUntil(lambda: len(newest.frames) == 1, timeout=5000)

        newest.grab_data(1, live=False)  # (no new image yet: it is waited for)
        qtbot.waitUntil(lambda: len(newest.frames) == 2, timeout=5000)

        assert newest.frames[1][0, 0, 0, 0] != newest.frames[0][0, 0, 0, 0]
        camera.clear_acquisition()

    def test_the_images_to_average(self, qtbot, newest, camera):
        camera.exposure = 0.05
        newest.grab_data(3, live=False)

        qtbot.waitUntil(lambda: bool(newest.frames), timeout=5000)

        assert newest.frames[0].shape == (3, 1) + SHAPE
        assert len(set(int(v) for v in newest.frames[0][:, 0, 0, 0])) == 3  # (three different images)
        camera.clear_acquisition()

    def test_the_live_view_gets_each_new_image_until_it_is_stopped(self, qtbot, newest, camera):
        camera.exposure = 0.05
        newest.grab_data(1, live=True)
        qtbot.waitUntil(lambda: len(newest.frames) >= 3, timeout=5000)

        newest.stop()
        qtbot.wait(200)
        done = len(newest.frames)
        qtbot.wait(200)

        assert len(newest.frames) == done and not newest.callback_thread.callback.watching and not camera.armed

    @pytest.mark.errors_expected
    def test_an_image_which_does_not_come_is_an_error(self, qtbot, newest, camera, monkeypatch):
        monkeypatch.setattr(tm, 'FRAME_TIMEOUT_MARGIN', 0.1)
        camera.exposure, camera.silent = 0., True

        newest.grab_data(1, live=False)

        qtbot.waitUntil(lambda: bool(newest.errors), timeout=5000)
        assert not newest.frames and not newest.callback_thread.callback.watching

    def test_a_camera_which_was_stopped_ends_the_watch_quietly_and_the_next_image_starts_it_again(self, qtbot, newest, camera):
        newest.grab_data(1, live=False)
        qtbot.waitUntil(lambda: len(newest.frames) == 1, timeout=5000)

        camera.clear_acquisition()  # (what a change of the ROI does)
        qtbot.waitUntil(lambda: not newest.callback_thread.callback.watching, timeout=3000)
        assert not newest.errors

        newest.grab_data(1, live=False)
        qtbot.waitUntil(lambda: len(newest.frames) == 2, timeout=5000)
        assert camera.starts == 2 and newest.callback_thread.callback.watching
        camera.clear_acquisition()

    def test_going_back_to_the_trigger_mode(self, qtbot, newest, camera):
        newest.grab_data(1, live=False)
        qtbot.waitUntil(lambda: bool(newest.frames), timeout=5000)

        acquisition(newest, tm.ACQ_TRIGGER)
        newest.commit_settings(newest.settings.child(tm.ACQUISITION))
        qtbot.waitUntil(lambda: not newest.callback_thread.callback.watching, timeout=3000)
        newest.grab_data(1, live=False)
        qtbot.waitUntil(lambda: len(newest.frames) == 2, timeout=5000)

        assert camera.triggers == 1 and camera.frames_per_trigger == 1 and not camera.continuous
        camera.clear_acquisition()

    def test_the_data_of_the_other_modes_are_not_dated_by_the_plugin(self, qtbot, plugin, camera):
        plugin.grab_data(1, live=False)  # (the trigger mode: the data are made when the image comes)
        qtbot.waitUntil(lambda: len(plugin.emitted) == 1, timeout=5000)

        assert plugin.stamp is None and abs(plugin.emitted[0].data[0].timestamp - time.time()) < 1.
