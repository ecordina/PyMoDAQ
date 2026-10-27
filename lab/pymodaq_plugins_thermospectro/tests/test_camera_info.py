"""camera_info.py: what the driver of a camera says about it (the model, the size of the pixels, the binning...)

Pure python: the drivers are replaced by small objects which answer as the ones of pylablib do.
"""
import collections
import json

import pytest

from pymodaq_plugins_thermospectro import camera_info
from pymodaq_plugins_thermospectro.camera_info import CameraInfo, read_camera_info, table_pitch

TLInfo = collections.namedtuple('TLInfo', ['model', 'name', 'serial_number', 'firmware_version'])
UCInfo = collections.namedtuple('UCInfo', ['cam_id', 'model', 'manufacturer', 'serial_number'])


class ThorlabsCamera:
    """What a ``ThorlabsTLCamera`` of pylablib answers (the pitch is asked to the SDK: see ``said_by_sdk``)"""
    handle = object()

    def __init__(self, model='CS165MU1', roi=(0, 1440, 0, 1080, 1, 1), bits=10):
        self.model, self._roi, self.bits = model, roi, bits

    def get_device_info(self):
        return TLInfo(self.model, 'My camera', '12345', '1.2.3')

    def get_sensor_info(self):
        return ('mono', self.bits)

    def get_roi(self):
        return self._roi


class Uc480Camera:
    def __init__(self, pixel_size=520):
        self.pixel_size = pixel_size

    def get_device_info(self):
        return UCInfo(1, 'DCC1545M', 'Thorlabs', 'SN1')

    def _get_sensor_info(self):
        class Sensor:
            wPixelSize = self.pixel_size

        return Sensor()

    def get_roi(self):
        return (0, 1280, 0, 1024, 1, 1)


@pytest.fixture
def said_by_sdk(monkeypatch):
    """The SDK of Thorlabs gives the pitch (its library is not on this computer)"""
    monkeypatch.setattr(camera_info, '_thorlabs_pitch', lambda controller: (3.45, 3.45))


class TestTheTable:
    @pytest.mark.parametrize('model, pitch', [
        ('CS165MU1', 3.45), ('CS165CU/M', 3.45), ('cs165mu', 3.45), ('CS135MUN', 4.8), ('CS235MU', 5.86),
        ('CS505MU1', 3.45), ('CS895CU', 3.45), ('CS2100M-USB', 5.04), (' CS2100M USB ', 5.04)])
    def test_the_models_of_thorlabs(self, model, pitch):
        assert table_pitch(model) == pitch

    @pytest.mark.parametrize('model', ['', 'DCC1545M', 'Mock', 'CS16', None])
    def test_what_is_not_there_is_not_guessed(self, model):
        assert table_pitch(model) is None


class TestReadingACamera:
    def test_a_thorlabs_camera_says_everything(self, said_by_sdk):
        info = read_camera_info(ThorlabsCamera())

        assert (info.model, info.name, info.serial) == ('CS165MU1', 'My camera', '12345')
        assert info.pixel_um == (3.45, 3.45) and info.pixel_source == 'camera'
        assert info.bit_depth == 10 and info.sensor_type == 'mono'
        assert info.binning == (1, 1) and info.window == (0, 1440, 0, 1080)

    def test_the_binning_makes_the_pixels_larger(self, said_by_sdk):
        info = read_camera_info(ThorlabsCamera(roi=(100, 500, 200, 600, 2, 4)))

        assert info.binning == (2, 4) and info.window == (100, 500, 200, 600)
        assert info.effective_pixel_um == pytest.approx((6.9, 13.8))

    def test_the_table_is_the_fallback(self, monkeypatch):
        def refuses(controller):
            raise OSError('the SDK does not know it')

        monkeypatch.setattr(camera_info, '_thorlabs_pitch', refuses)

        info = read_camera_info(ThorlabsCamera('CS235MU'))

        assert info.pixel_um == (5.86, 5.86) and info.pixel_source == 'table'

    def test_what_the_camera_says_is_better_than_the_table(self, monkeypatch):
        monkeypatch.setattr(camera_info, '_thorlabs_pitch', lambda controller: (3.0, 3.0))

        info = read_camera_info(ThorlabsCamera('CS165MU1'))

        assert info.pixel_um == (3.0, 3.0) and info.pixel_source == 'camera'

    def test_a_uc480_camera_says_its_pixel_size_in_10_nm(self, monkeypatch):
        monkeypatch.setattr(camera_info, '_thorlabs_pitch', lambda controller: None)

        info = read_camera_info(Uc480Camera(520))

        assert info.model == 'DCC1545M' and info.manufacturer == 'Thorlabs'
        assert info.pixel_um == pytest.approx((5.2, 5.2)) and info.pixel_source == 'camera'

    def test_a_pitch_which_is_not_one_is_not_believed(self, monkeypatch):
        monkeypatch.setattr(camera_info, '_thorlabs_pitch', lambda controller: None)

        info = read_camera_info(Uc480Camera(52000))  # (52 mm)

        assert info.pixel_um is None and info.pixel_source == ''

    @pytest.mark.parametrize('controller', [None, object(), 'a string'])
    def test_what_says_nothing_gives_an_empty_info(self, controller):
        info = read_camera_info(controller)

        assert not info and info.pixel_um is None and info.to_json() == '{}'

    def test_it_never_raises(self):
        class Broken:
            def __getattr__(self, name):
                raise RuntimeError(name)

        assert not read_camera_info(Broken())


class TestTheFile:
    def test_the_json_gives_the_info_back(self, said_by_sdk):
        info = read_camera_info(ThorlabsCamera(roi=(0, 720, 0, 540, 2, 2)))

        again = CameraInfo.from_json(info.to_json())

        assert again == info and again.pixel_um == (3.45, 3.45) and again.binning == (2, 2)

    def test_the_json_is_plain_text_of_what_is_known(self, said_by_sdk):
        content = json.loads(read_camera_info(ThorlabsCamera()).to_json())

        assert content['model'] == 'CS165MU1' and content['pixel_um'] == [3.45, 3.45]
        assert 'binning' not in content and 'manufacturer' not in content  # (nothing to say)

    @pytest.mark.parametrize('text', ['', 'nonsense', '[1, 2]', '{"unknown": 1}', None])
    def test_a_text_which_is_not_an_info_gives_an_empty_one(self, text):
        assert not CameraInfo.from_json(text)
