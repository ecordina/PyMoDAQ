"""What the h5 file says about the camera which took the images: its model, the size of its pixels, its binning...

The images of a camera were saved without a word about the camera. This asks the driver of the camera once, when an
experiment starts, and the answer is saved with the images (the attribute ``camera_info`` of the groups of the camera), so
that ``lab_export`` can write what the pixels are in the tif files (the size of a pixel, the bit depth, the model...).

The size of a pixel (the pitch of the sensor, in um) is asked to the camera where its driver tells it:

* a Thorlabs scientific camera (Zelux, Kiralux, Quantalux: the plugin ``Thorlabs_TSI``) says it itself
  (``tl_camera_get_sensor_pixel_width`` of the SDK of Thorlabs),
* a uc480 / uEye camera (``UC480``) says it in the information of its sensor (``wPixelSize``, in 10 nm).

:data:`SENSOR_PITCH_UM` is the fallback for a Thorlabs camera which does not say it (an old SDK): the pitch of the models
Thorlabs documents. It is **the pitch of the sensor**: with a lens or a microscope between the camera and the sample the
size of a pixel *on the sample* is this divided by the magnification, which the camera cannot know (``lab_export
--magnification`` or ``--pixel-size``).

Pure python: no Qt, no PyMoDAQ. Nothing here raises: a camera which says nothing gives an empty :class:`CameraInfo`.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Optional

#: Pitch of the pixels of the sensor (um) of the scientific cameras of Thorlabs, by the beginning of the model: the end of
#: a model only tells the variant (M monochrome, C color, U USB, N NIR enhanced, P polarization, 1 with external trigger,
#: /M metric taps). From the data sheets and manuals of Thorlabs (thorlabs.com), checked in October 2026 against these
#: pages (the models of the same family share one sensor size; the color ones were not looked up one by one):
#:   CS165 (Zelux 1.6 MP, 1440 x 1080): Thorlabs manual of the CS165 series, "Pixel Size 3.45 um x 3.45 um"
#:   CS135 (Kiralux 1.3 MP, 1280 x 1024): Thorlabs specification table of Kiralux, "4.8 um" square pixels, 10 bits
#:   CS235 (Kiralux 2.3 MP, 1920 x 1200): CS235MU / CS235CU, "5.86 um x 5.86 um", 12 bits
#:   CS505 (Kiralux 5 MP, 2448 x 2048): Sony IMX250 sensor, 3.45 um pixels (Thorlabs press release, 2018)
#:   CS895 (Kiralux 8.9 MP, 4096 x 2160): CS895MU, 3.45 um x 3.45 um, 12 bits
#:   CS2100 (Quantalux sCMOS 2.1 MP, 1920 x 1080): 5.04 um pixels, 16 bits (Thorlabs announcement, 2017)
SENSOR_PITCH_UM: dict[str, float] = {
    'CS165': 3.45,
    'CS135': 4.8,
    'CS235': 5.86,
    'CS505': 3.45,
    'CS895': 3.45,
    'CS2100': 5.04,
}
#: A pitch outside of these limits (um) is not a pitch: the driver is not asked again, the table is used
PITCH_LIMITS_UM = (0.5, 30.0)
#: The uc480 driver gives the pixel size in units of 10 nm
UC480_PITCH_UNIT_UM = 0.01


@dataclass
class CameraInfo:
    """What a camera says about itself (an empty text or None is "not said")"""
    model: str = ''
    name: str = ''  # what the user called the camera (Thorlabs), may be empty
    serial: str = ''
    manufacturer: str = ''
    pixel_um: Optional[tuple[float, float]] = None  # pitch of the sensor along x and y
    pixel_source: str = ''  # 'camera' (the driver said it), 'table' (SENSOR_PITCH_UM), or '' (not known)
    bit_depth: int = 0
    sensor_type: str = ''  # 'mono', 'bayer', 'mono_pol' (Thorlabs)
    binning: tuple[int, int] = (1, 1)  # along x and y
    window: Optional[tuple[int, int, int, int]] = None  # of the sensor, in its pixels: x start, x end, y start, y end

    def __bool__(self) -> bool:
        """Whether the camera said anything at all"""
        return bool(self.model or self.name or self.serial or self.pixel_um or self.bit_depth)

    def to_json(self) -> str:
        data = {key: (list(value) if isinstance(value, tuple) else value) for key, value in asdict(self).items()
                if value not in ('', None, 0) and not (key == 'binning' and tuple(value) == (1, 1))}
        return json.dumps(data, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> 'CameraInfo':
        """The info written by :meth:`to_json` (an empty one if the text is not that)"""
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        info = cls()
        for key, value in data.items():
            if not hasattr(info, key):
                continue
            if key in ('pixel_um', 'binning', 'window') and isinstance(value, (list, tuple)):
                value = tuple(value)
            setattr(info, key, value)
        return info

    @property
    def effective_pixel_um(self) -> Optional[tuple[float, float]]:
        """The size of a pixel of the image on the sensor: the pitch times the binning (None if the pitch is not known)"""
        if not self.pixel_um:
            return None
        return self.pixel_um[0] * max(1, int(self.binning[0])), self.pixel_um[1] * max(1, int(self.binning[1]))


def _text(value) -> str:
    if isinstance(value, bytes):
        value = value.decode('utf-8', errors='replace')
    return '' if value is None else str(value).strip()


def table_pitch(model: str) -> Optional[float]:
    """The pitch (um) of a Thorlabs scientific camera of this model in :data:`SENSOR_PITCH_UM`, None if it is not there"""
    key = re.sub(r'[^A-Z0-9]', '', _text(model).upper())
    for prefix in sorted(SENSOR_PITCH_UM, key=len, reverse=True):
        if key.startswith(prefix):
            return SENSOR_PITCH_UM[prefix]
    return None


def _valid(pitch: float) -> bool:
    return PITCH_LIMITS_UM[0] <= pitch <= PITCH_LIMITS_UM[1]


def _thorlabs_pitch(controller) -> Optional[tuple[float, float]]:
    """The pitch the SDK of Thorlabs gives (the controller of the plugin ``Thorlabs_TSI``: it has a ``handle``)"""
    handle = getattr(controller, 'handle', None)
    if handle is None:
        return None
    from pylablib.devices.Thorlabs.TLCamera import lib
    pitch = (float(lib.tl_camera_get_sensor_pixel_width(handle)), float(lib.tl_camera_get_sensor_pixel_height(handle)))
    return pitch if all(_valid(value) for value in pitch) else None


def _uc480_pitch(controller) -> Optional[tuple[float, float]]:
    """The pixel size in the information of the sensor of a uc480 / uEye camera (square pixels)"""
    sensor = controller._get_sensor_info()
    pitch = float(sensor.wPixelSize) * UC480_PITCH_UNIT_UM
    return (pitch, pitch) if _valid(pitch) else None


def read_camera_info(controller) -> CameraInfo:
    """Ask the driver of a camera (the pylablib object of the plugin) what it is: never raises

    Parameters
    ----------
    controller: object, optional
        The ``controller`` of the plugin of the camera: a pylablib camera (``Thorlabs_TSI``, ``UC480``). Anything else (a
        Mock camera, None) gives an empty info.
    """
    info = CameraInfo()
    if controller is None:
        return info
    try:
        device = controller.get_device_info()
        info.model = _text(getattr(device, 'model', ''))
        info.name = _text(getattr(device, 'name', ''))
        info.serial = _text(getattr(device, 'serial_number', ''))
        info.manufacturer = _text(getattr(device, 'manufacturer', ''))
    except Exception:
        pass
    for source in (_thorlabs_pitch, _uc480_pitch):
        try:
            pitch = source(controller)
        except Exception:
            pitch = None
        if pitch:
            info.pixel_um, info.pixel_source = pitch, 'camera'
            break
    if info.pixel_um is None:
        pitch = table_pitch(info.model)
        if pitch is not None:
            info.pixel_um, info.pixel_source = (pitch, pitch), 'table'
    try:
        sensor = controller.get_sensor_info()  # (Thorlabs: the type of the sensor and its bit depth)
        info.sensor_type, info.bit_depth = _text(sensor[0]), int(sensor[1])
    except Exception:
        pass
    try:
        x0, x1, y0, y1, x_bin, y_bin = controller.get_roi()[:6]
        info.window, info.binning = (int(x0), int(x1), int(y0), int(y1)), (max(1, int(x_bin)), max(1, int(y_bin)))
    except Exception:
        pass
    return info
