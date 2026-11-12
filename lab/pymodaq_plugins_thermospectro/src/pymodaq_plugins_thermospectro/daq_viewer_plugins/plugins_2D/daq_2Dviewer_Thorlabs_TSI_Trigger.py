"""The Thorlabs scientific cameras (Zelux, Kiralux...) with more ways to be read than the stock plugin

It is the plugin ``Thorlabs_TSI`` (of ``pymodaq_plugins_thorlabs``: everything it has, ROI, binning, exposure, color
conversion, is the same) with one more setting, *Acquisition*:

* **One frame per trigger** (the default): the camera is armed once, with *Frames per Trigger = 1*, and waits. Each readout
  sends a software trigger: the camera makes one exposure, the image is read out, and it idles until the next readout.
  It does not free-run between readouts (the stock plugin leaves the camera in *Frames per Trigger = continuous*, where
  it makes images as fast as it can whether or not they are taken), and nothing is armed or stopped for each readout
  (*Disarm between readouts*). An exposure time which is changed is the one of the next image. For readouts far apart.
* **Continuous, newest image**: the camera runs, a thread watches it and puts its images **in a buffer of the last five as
  they arrive**, each with the time it arrived, first in, first out; a readout takes the newest image nobody took yet, at once.
  The image is dated by the time it arrived (it is in the data, see :class:`~.trigger_mode.StampingSignal`), not by the time it
  was asked for. For readouts a few seconds apart, where the camera has to run: nothing waits for a future image, so no image
  is late because of the exposure it waited for.
* **Continuous, next image**: what the stock plugin does: a readout waits for the first image which comes after it.

See :mod:`pymodaq_plugins_thermospectro.trigger_mode` for what the camera does in each. Choose ``Thorlabs_TSI_Trigger`` as the
plugin of the camera in the Dashboard (or ``--camera-plugin Thorlabs_TSI_Trigger`` of the applications of this package); it
needs ``pymodaq_plugins_thorlabs`` and ``pylablib`` (``python lab/install_lab.py --camera``), and is not listed without them.
"""
import time
from collections import deque

from qtpy import QtCore, QtWidgets

from pymodaq_utils.logger import set_logger, get_module_name
from pymodaq_utils.utils import ThreadCommand
from pymodaq.control_modules.viewer_utility_classes import main

from pymodaq_plugins_thorlabs.daq_viewer_plugins.plugins_2D.daq_2Dviewer_Thorlabs_TSI import DAQ_2DViewer_Thorlabs_TSI
from pymodaq_plugins_utils.hardware.camera_base_pylablib import CameraCallback, Grab

from pymodaq_plugins_thermospectro import trigger_mode

logger = set_logger(get_module_name(__file__))


class TriggeredCallback(CameraCallback):
    """What waits for the images in its own thread

    In the trigger mode it triggers the camera for each image. In the newest image mode it watches the camera all the time
    (:meth:`watch`), puts what comes in the buffer, and gives the newest image to a readout which asks for it. In the
    continuous mode, next image, it does what the stock plugin does (waits for the first image which comes).
    """
    mode = trigger_mode.ACQ_TRIGGER
    watching = False  # the camera is being watched (newest image mode)
    request = None  # the readout which asks for an image (newest image mode): its Grab
    requested_at = 0.0

    def __init__(self, controller):
        super().__init__(controller)
        self.fifo = trigger_mode.FrameFifo()
        self.times = deque()  # when each image which was given to the plugin arrived, in the order it was given

    @property
    def triggered(self) -> bool:
        return self.mode == trigger_mode.ACQ_TRIGGER

    def submit(self, mode: Grab):
        """A readout asks for an image (newest image mode): the watch gives it the newest one

        It is handed over directly, not by a signal: the watch is a slot which runs all the time, and a signal sent to the
        thread while it runs is not delivered when it is asked for (the events of the thread are not processed in it).
        """
        self.requested_at = time.monotonic()
        self.request = mode

    def cancel(self):
        """Stop watching, forget the request, and what is in the buffer (it is not new when the camera is armed again)"""
        self.watching = False
        self.request = None
        self.fifo.clear()

    def wait_for_acquisition(self, mode: Grab):
        if self.mode == trigger_mode.ACQ_NEWEST:
            return self.submit(mode)
        if self.mode != trigger_mode.ACQ_TRIGGER:
            return super().wait_for_acquisition(mode)
        while self.do_acquisition:
            try:
                self.data_sig.emit(trigger_mode.grab_triggered(self.controller, mode.n_average))
                QtCore.QThread.msleep(10)
            except Exception as e:
                logger.exception(str(e))
                self.error.emit()
                break
            QtWidgets.QApplication.processEvents()
            if not self.do_acquisition or mode.snap:
                break

    def watch(self):
        """Put the images of the camera in the buffer as they arrive until it is stopped (:meth:`cancel`), and give the
        newest one to the readout which asks for it (:meth:`submit`)"""
        if self.watching:
            return
        self.watching = True
        try:
            while self.watching:
                trigger_mode.collect(self.controller, self.fifo)
                self.serve()
        except Exception as e:  # (the camera was stopped, or it does not answer)
            if self.watching and self.request is not None:  # (nothing to say if it was stopped on purpose, or no readout waited)
                logger.exception(str(e))
                self.request = None
                self.error.emit()
        finally:
            self.watching = False

    def serve(self):
        """Give the newest image to the readout which waits for one (several to average if it asks for them)"""
        mode = self.request
        if mode is None:
            return
        found = trigger_mode.newest_frames(self.fifo, mode.n_average)
        if found is None:
            if time.monotonic() - self.requested_at > trigger_mode.frame_timeout(self.controller):
                raise TimeoutError('no new image from the camera')
            return
        stamp, frames = found
        self.times.append(stamp)  # (before the images: the plugin dates them with it as it emits them)
        if mode.snap:
            self.request = None
        else:
            self.requested_at = time.monotonic()
        self.data_sig.emit(frames)


class DAQ_2DViewer_Thorlabs_TSI_Trigger(DAQ_2DViewer_Thorlabs_TSI):
    """Plugin for the Thorlabs TSI scientific cameras, with one frame per trigger or the newest image (see the module)"""
    params = DAQ_2DViewer_Thorlabs_TSI.params + trigger_mode.TRIGGER_PARAMS
    watch_signal = QtCore.Signal()
    stamp = None  # the time the data which are emitted now are dated with (the image of the buffer arrived then), or None

    def acquisition(self) -> str:
        """The setting *Acquisition*"""
        return self.settings[trigger_mode.ACQUISITION]

    def triggered(self) -> bool:
        """The camera makes one image per trigger"""
        return self.acquisition() == trigger_mode.ACQ_TRIGGER

    def setup_callback_thread(self):
        """As the stock plugin does, with a callback which can trigger the camera or watch it"""
        callback = TriggeredCallback(self.controller)
        callback.mode = self.acquisition()
        self.settings.child('buffer', 'mode').setReadonly(True)

        self.callback_thread = QtCore.QThread(self)
        callback.moveToThread(self.callback_thread)
        callback.data_sig.connect(self.emit_data)
        callback.error.connect(self.handle_error)
        self.callback_signal.connect(callback.set_do_grab)
        self.watch_signal.connect(callback.watch)
        self.callback_thread.callback = callback
        self.callback_thread.start()
        if not isinstance(self.dte_signal, trigger_mode.StampingSignal):
            self.dte_signal = trigger_mode.StampingSignal(self.dte_signal, self)

        self._prepare_view()

    def emit_data(self, frame=None):
        """The stock plugin emits the data: dated by the time the image arrived when it came from the buffer"""
        callback = self.callback_thread.callback
        self.stamp = callback.times.popleft() if callback.mode == trigger_mode.ACQ_NEWEST and callback.times else None
        try:
            super().emit_data(frame)
        finally:
            self.stamp = None

    def stop(self):
        """Stop the camera: the watch of its images ends first (an image which is waited for is not an error)"""
        self.callback_thread.callback.cancel()
        return super().stop()

    def grab_data(self, Naverage=1, **kwargs):
        """Ask for an image: the camera is armed (once) and triggered, or watched and its newest image taken, or what the
        stock plugin does, depending on the setting *Acquisition*"""
        mode = self.acquisition()
        callback = self.callback_thread.callback
        callback.mode = mode
        if mode == trigger_mode.ACQ_CONTINUOUS:
            return super().grab_data(Naverage, **kwargs)
        try:
            self.is_live = kwargs.get('live', False)
            self.Naverage = Naverage
            self.n_frames = 1
            if mode == trigger_mode.ACQ_TRIGGER:
                if not self.controller.acquisition_in_progress():
                    trigger_mode.arm_one_frame_per_trigger(self.controller)
            else:
                if not self.controller.acquisition_in_progress():
                    trigger_mode.arm_continuous(self.controller)
                    callback.fifo.clear()
                callback.submit(Grab(do_acquisition=True, snap=not self.is_live, n_average=Naverage, nframes=self.n_frames,
                                     since=self.settings['buffer', 'mode']))
                if not callback.watching:
                    self.watch_signal.emit()
                return
            self.callback_signal.emit(Grab(do_acquisition=True, snap=not self.is_live, n_average=Naverage,
                                           nframes=self.n_frames, since=self.settings['buffer', 'mode']))
        except Exception as e:
            self.emit_status(ThreadCommand('Update_Status', [str(e), 'log']))

    def commit_settings(self, param):
        """The mode is changed: the camera is stopped and the next image arms it in the new one. An exposure time which is
        changed in the trigger mode is set on a camera which is not armed: the next image is the first one made with it"""
        if param.name() == trigger_mode.ACQUISITION:
            self.stop()
            self.callback_thread.callback.mode = self.acquisition()
            return
        if param.name() == 'exposure_time' and self.triggered() and self.controller.acquisition_in_progress():
            self.controller.clear_acquisition()
        super().commit_settings(param)


if __name__ == '__main__':
    main(__file__, init=False)
