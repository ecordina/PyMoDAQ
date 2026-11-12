"""The trigger mode of a Thorlabs scientific camera: one image per software trigger, the camera idle in between

The Thorlabs cameras (Zelux, Kiralux...) have two ways to be asked for images (manual of the CS165, ITN004347-D02, section 4.2):

* *Frames per Trigger = continuous* (the default, and what the plugin ``Thorlabs_TSI`` of PyMoDAQ does): once it is started the
  camera exposes and reads out image after image, as fast as the exposure allows, until it is stopped, whether or not
  anybody takes them. A readout takes the first image which comes after it was asked for: when the readouts are far
  apart the camera makes hundreds of images for each one which is used, the image has been exposed partly before the
  readout was asked, and what changes when the exposure is changed is only seen a frame later.
* *Frames per Trigger = 1*, with a software trigger: the camera is armed once and waits. Each trigger makes **one** exposure,
  the image is read out, and the camera idles again until the next trigger. The exposure begins when the readout is
  asked for, the image is the one of that exposure, and the camera does not run in between. Nothing is armed or stopped
  for each readout (which is what *Disarm between readouts* does, and what the driver says may fail 0.1 to 1 % of
  the times).

The plugin ``Thorlabs_TSI_Trigger`` of this package has a third way, for readouts which are only a few seconds apart, where the
camera has to run: *Continuous, newest image*. The camera free-runs, and its images are **put in a buffer of the last five
as they arrive, each with the time it arrived**; a readout takes the newest one nobody took yet, at once, instead of waiting
for the next one to come (the stock plugin: the readout waits up to one exposure, then the transfer and the display add to
it, which is how an image can be later than the readout time). The time of the image is the time it arrived.

This module is what the plugin does with the driver of the camera (``pylablib``'s ``ThorlabsTLCamera``, or what stands for it
in the tests), without importing anything of the plugins: see
:mod:`pymodaq_plugins_thermospectro.daq_viewer_plugins.plugins_2D.daq_2Dviewer_Thorlabs_TSI_Trigger`.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

import numpy as np

#: The values of the setting *Acquisition* of the plugin
ACQ_TRIGGER = 'One frame per trigger'
ACQ_NEWEST = 'Continuous, newest image'
ACQ_CONTINUOUS = 'Continuous, next image'  # (what the stock plugin does)
ACQUISITION = 'acquisition'  # its name
#: How many images the buffer of a camera which runs holds (the oldest goes when a sixth comes)
FIFO_SIZE = 5

#: The setting of the plugin (added to the ones of ``Thorlabs_TSI``)
TRIGGER_PARAMS = [
    {'title': 'Acquisition:', 'name': ACQUISITION, 'type': 'list', 'limits': [ACQ_TRIGGER, ACQ_NEWEST, ACQ_CONTINUOUS],
     'value': ACQ_TRIGGER,
     'tip': 'One frame per trigger: the camera is armed once and idle, each readout triggers one exposure (the camera does '
            'not run between the readouts, an exposure time which is changed is the one of the next image): for readouts far '
            'apart. Continuous, newest image: the camera runs, the last 5 images are kept as they arrive and a readout takes '
            'the newest one at once, the time of the image is the time it arrived: for readouts only a few seconds apart. '
            'Continuous, next image: the camera runs and a readout waits for the first image which comes after it (what the '
            'plugin Thorlabs_TSI does)'},
]

#: A frame is waited for after a trigger at most twice the exposure time plus this (s)
FRAME_TIMEOUT_MARGIN = 10.0


def frame_timeout(controller) -> float:
    """How long (s) the image of a trigger is waited for: twice the exposure time, and ten seconds more"""
    try:
        exposure = float(controller.get_exposure())
    except Exception:
        exposure = 0.0
    return 2.0 * max(exposure, 0.0) + FRAME_TIMEOUT_MARGIN


def arm_one_frame_per_trigger(controller):
    """Arm the camera so that each software trigger gives one image, and it idles in between

    The camera exposes only when it is triggered (:func:`one_frame`): ``auto_start=False`` does not trigger it when it is
    armed, ``frames_per_trigger=1`` makes each trigger give one image.
    """
    controller.clear_acquisition()
    controller.start_acquisition(frames_per_trigger=1, auto_start=False, nframes=1)


def one_frame(controller) -> np.ndarray:
    """Trigger the armed camera and return the image of that exposure

    The images which were not read (one of a trigger which was given up, which came late) are dropped first: the image
    waited for is the first one which comes after the trigger, counted from the last one which was read, so that it is not
    a late one of the trigger before. The camera was armed by :func:`arm_one_frame_per_trigger`.

    Raises
    ------
    Exception
        What the driver raises if the camera is not armed, or does not give the image in time
    """
    controller.read_multiple_images(missing_frame='skip')
    controller.send_software_trigger()
    controller.wait_for_frame(since='lastread', timeout=frame_timeout(controller), error_on_stopped=True)
    frames = controller.read_multiple_images(missing_frame='skip')
    if frames is None or len(frames) == 0:
        raise RuntimeError('the camera gave no image after the trigger')
    return np.asarray(frames[-1])


def grab_triggered(controller, n_average: int = 1) -> np.ndarray:
    """`n_average` triggered images, in the array ``(n_average, 1, rows, columns[, colors])`` the plugin emits"""
    n = max(1, int(n_average))
    frames = None
    for index in range(n):
        frame = one_frame(controller)
        if frames is None:
            frames = np.zeros((n, 1) + frame.shape, dtype=frame.dtype)
        frames[index, 0] = frame
    return frames


def arm_continuous(controller):
    """Arm the camera to run (what the stock plugin does), with a buffer of :data:`FIFO_SIZE` images"""
    controller.clear_acquisition()
    controller.start_acquisition(nframes=FIFO_SIZE)


@dataclass
class Entry:
    """An image of the buffer: the time it arrived (epoch seconds), its number (in the order of arrival) and its pixels"""
    time: float
    sequence: int
    frame: np.ndarray


class FrameFifo:
    """The last images of a camera which runs, put in as they arrive, first in, first out

    A readout takes the newest image which was not taken before (:meth:`take_newest`): there is always one when the readout time is
    longer than the exposure, and none until the next one comes when it is not (the same image is never taken twice).
    The thread which watches the camera puts the images in (:func:`collect`), the thread of the readouts takes them out.
    """

    def __init__(self, size: int = FIFO_SIZE, clock=time.time):
        self.size = size
        self.clock = clock
        self._entries: deque = deque(maxlen=size)
        self._lock = threading.Lock()
        self.sequence = 0  # how many images came
        self.taken = 0  # the number of the newest one which was taken

    def clear(self):
        """Forget the images (the camera was armed again): what came before is not new any more"""
        with self._lock:
            self._entries.clear()
            self.taken = self.sequence

    def put(self, frame: np.ndarray, when: float | None = None):
        with self._lock:
            self.sequence += 1
            self._entries.append(Entry(self.clock() if when is None else when, self.sequence, frame))

    def unseen(self) -> int:
        """How many images came since the last one was taken (at most the size of the buffer: the others are gone)"""
        with self._lock:
            return min(self.sequence - self.taken, len(self._entries))

    def take_newest(self, n: int = 1) -> list[Entry] | None:
        """The `n` newest images which were not taken (oldest first), None if there are not that many yet"""
        with self._lock:
            if min(self.sequence - self.taken, len(self._entries)) < max(1, n):
                return None
            entries = list(self._entries)[-max(1, n):]
            self.taken = self.sequence
            return entries


def collect(controller, fifo: FrameFifo, timeout: float = 0.05) -> int:
    """Wait up to `timeout` seconds for the camera to make an image, and put what came in the buffer (with the time of now)

    It is what the thread which watches the camera does all the time. The wait is counted from the last time an image was
    waited for successfully, so that none is missed between two calls.

    Returns
    -------
    int
        How many images were put in the buffer (0 if none came in time)

    Raises
    ------
    Exception
        What the driver raises when the camera is not armed (any more): the watch is over
    """
    try:
        controller.wait_for_frame(since='lastwait', timeout=timeout, error_on_stopped=True)
    except (getattr(controller, 'TimeoutError', TimeoutError), TimeoutError):
        return 0
    when = fifo.clock()  # (the images were made a moment ago: the wait polls every millisecond)
    frames = controller.read_multiple_images(missing_frame='skip')
    if frames is None or len(frames) == 0:
        return 0
    for frame in list(frames)[-fifo.size:]:
        fifo.put(np.array(frame), when)
    return len(frames)


def newest_frames(fifo: FrameFifo, n_average: int = 1):
    """What a readout takes from the buffer: ``(time, frames)`` with the time the newest image arrived and the array
    ``(n_average, 1, rows, columns[, colors])`` the plugin emits, None if the images which are new are not enough yet"""
    entries = fifo.take_newest(max(1, int(n_average)))
    if entries is None:
        return None
    return entries[-1].time, np.stack([entry.frame for entry in entries])[:, None]


class StampingSignal:
    """The signal a plugin emits its data with, which dates them first: what the stock plugin does not do, because it makes
    the data when it emits them, and an image of the buffer was made earlier

    `owner` has the attribute ``stamp``: the time (epoch) to date the data with, or None to leave them as they are.
    """

    def __init__(self, signal, owner):
        self._signal = signal
        self._owner = owner

    def emit(self, dte, *args):
        stamp = getattr(self._owner, 'stamp', None)
        if stamp is not None:
            for dwa in dte.data:
                dwa.timestamp = stamp
            dte.timestamp = stamp
        return self._signal.emit(dte, *args)

    def __getattr__(self, name):
        return getattr(self._signal, name)


def _walk(param):
    for child in param.children():
        yield child
        yield from _walk(child)


def trigger_mode_of(module) -> bool:
    """Whether a detector module of the Dashboard is a camera in the trigger mode (its setting *Acquisition* says so)"""
    settings = getattr(module, 'settings', None)
    if settings is None:
        return False
    try:
        return any(param.name() == ACQUISITION and param.value() == ACQ_TRIGGER for param in _walk(settings))
    except Exception:
        return False
