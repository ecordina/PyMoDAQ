import sys
import types
from pathlib import Path

import numpy as np
import pytest
import toml

from pymodaq_plugins_oceaninsight.hardware import seabreeze_utils as sbu
from seabreeze_models import MODELS, dark_pixel_indices


class TestActivePixelSlice:

    @pytest.mark.parametrize('model, n_pixels, ranges, kept', MODELS, ids=[model[0] for model in MODELS])
    def test_models(self, model, n_pixels, ranges, kept):
        assert sbu.active_pixel_slice(n_pixels, dark_pixel_indices(ranges)) == slice(*kept)

    @pytest.mark.parametrize('model, n_pixels, ranges, kept', MODELS, ids=[model[0] for model in MODELS])
    def test_no_dark_pixel_is_kept(self, model, n_pixels, ranges, kept):
        dark = dark_pixel_indices(ranges)
        kept_pixels = np.arange(n_pixels)[sbu.active_pixel_slice(n_pixels, dark)]
        assert kept_pixels.size > 0
        assert not set(kept_pixels) & set(dark)

    def test_no_dark_pixel_keeps_the_whole_spectrum(self):
        assert sbu.active_pixel_slice(1024, []) == slice(0, 1024)

    def test_indices_wrapped_in_a_tuple(self):
        # what pyseabreeze returns for the USB4000 and the NIRQuest
        assert sbu.active_pixel_slice(3840, [tuple(range(5, 16))]) == slice(16, 3840)
        assert sbu.active_pixel_slice(512, [()]) == slice(0, 512)

    def test_indices_as_array_or_tuple(self):
        assert sbu.active_pixel_slice(2048, np.arange(2, 24)) == slice(24, 2048)
        assert sbu.active_pixel_slice(2048, tuple(range(2, 24))) == slice(24, 2048)

    def test_indices_out_of_the_detector_are_ignored(self):
        assert sbu.active_pixel_slice(100, [-1, 3, 100, 5000]) == slice(4, 100)

    def test_all_pixels_dark_keeps_the_whole_spectrum(self):
        assert sbu.active_pixel_slice(10, range(10)) == slice(0, 10)


class TestCheckBackend:

    @pytest.mark.parametrize('name, expected', [('cseabreeze', 'cseabreeze'),
                                                ('pyseabreeze', 'pyseabreeze'),
                                                ('PySeaBreeze', 'pyseabreeze'),
                                                ('  cseabreeze\n', 'cseabreeze')])
    def test_known_backends(self, name, expected):
        assert sbu.check_backend(name) == expected

    @pytest.mark.parametrize('name', ['', 'libseabreeze', 'cseabreeze,pyseabreeze', None])
    def test_unknown_backend(self, name):
        with pytest.raises(ValueError, match='pyseabreeze, cseabreeze'):
            sbu.check_backend(name)

    def test_the_default_backend_is_pyseabreeze(self):
        assert sbu.DEFAULT_BACKEND == 'pyseabreeze'
        assert sbu.BACKENDS == ('pyseabreeze', 'cseabreeze')  # (the default first)

    def test_the_default_does_not_depend_on_the_configuration(self):
        # an entry of the configuration (an older version had one: seabreeze_backend) must not set the default
        template = toml.load(Path(sbu.__file__).parents[1] / 'resources' / 'config_template.toml')
        assert 'seabreeze_backend' not in template


class TestLoadBackend:

    def test_missing_module_is_explained(self, monkeypatch):
        monkeypatch.setitem(sys.modules, 'seabreeze.pyseabreeze', None)  # makes the import fail
        with pytest.raises(ImportError, match="'pyseabreeze'.*pyusb"):
            sbu.load_backend('pyseabreeze')

    def test_unknown_backend(self):
        with pytest.raises(ValueError):
            sbu.load_backend('libseabreeze')

    def test_cseabreeze(self):
        pytest.importorskip('seabreeze')
        assert sbu.load_backend('cseabreeze')._backend_ == 'cseabreeze'

    def test_pyseabreeze(self):
        pytest.importorskip('usb')  # pyusb
        assert sbu.load_backend('pyseabreeze')._backend_ == 'pyseabreeze'


class TestDeviceSerial:

    class Device:
        def __init__(self, serial):
            self.serial_number = serial

    @pytest.mark.parametrize('device, expected', [(Device('FLMS12345'), 'FLMS12345'),
                                                  (Device(' 42 '), '42'),
                                                  (Device('?'), None),
                                                  (Device(''), None),
                                                  ('<SeaBreezeDevice FLAMES:FLMS12345>', 'FLMS12345'),
                                                  ('<SeaBreezeDevice FLAMES:?>', None),
                                                  ('', None),
                                                  (None, None),
                                                  ('something else', None)])
    def test_device_serial(self, device, expected):
        assert sbu.device_serial(device) == expected


# ---------------------------------------------------------------------------------------------------------------------
# The backends are replaced by fakes that follow the interface of the python-seabreeze ones: the Spectrometer class of
# seabreeze is the real one
# ---------------------------------------------------------------------------------------------------------------------

N_PIXELS = 100


class FakeSpectrometerFeature:
    _spectrum_length = N_PIXELS

    def get_electric_dark_pixel_indices(self):
        return list(range(0, 4))

    def get_wavelengths(self):
        return np.linspace(200, 1100, N_PIXELS)

    def get_intensities(self):
        return np.arange(N_PIXELS, dtype=float)

    def get_integration_time_micros_limits(self):
        return 1000, 10000000

    def set_integration_time_micros(self, integration_time):
        pass


class FakeFeatures:
    spectrometer = FakeSpectrometerFeature()
    nonlinearity_coefficients = None


def make_fake_backend(name, serials):
    """A module with the interface of a python-seabreeze backend, seeing one device for each serial number"""
    module = types.ModuleType(f'seabreeze.{name}')
    module._backend_ = name

    class SeaBreezeError(Exception):
        pass

    class SeaBreezeDevice:
        model = 'FAKE'

        def __init__(self, serial):
            self.serial_number = serial
            self.is_open = False
            self.f = FakeFeatures()

        def open(self):
            self.is_open = True

        def close(self):
            self.is_open = False

        def __repr__(self):
            return f'<SeaBreezeDevice FAKE:{self.serial_number}>'

    class SeaBreezeAPI:
        created = 0

        def __init__(self, initialize=True):
            SeaBreezeAPI.created += 1
            self._devices = [SeaBreezeDevice(serial) for serial in serials]  # as seabreeze, always the same objects

        def list_devices(self):
            return list(self._devices)

    module.SeaBreezeError, module.SeaBreezeDevice, module.SeaBreezeAPI = SeaBreezeError, SeaBreezeDevice, SeaBreezeAPI
    return module


@pytest.fixture
def fake_backends(monkeypatch):
    """Fake backends and a clean state of the module: the dict of the backends can be changed by the test"""
    pytest.importorskip('seabreeze')
    from seabreeze.spectrometers import Spectrometer
    if '_backend' not in vars(Spectrometer):
        pytest.skip('seabreeze older than 2.9.0')
    backends = dict(cseabreeze=make_fake_backend('cseabreeze', ['S1', 'S2']),
                    pyseabreeze=make_fake_backend('pyseabreeze', ['S1', 'S2']))
    monkeypatch.setattr(sbu, 'load_backend', lambda name: backends[sbu.check_backend(name)])
    for state in ('_apis', '_spectrometer_classes', '_open_spectrometers'):
        monkeypatch.setattr(sbu, state, {})
    return backends


class TestOpenSpectrometer:

    @pytest.mark.parametrize('backend', sbu.BACKENDS)
    def test_the_spectrometer_uses_the_chosen_backend(self, fake_backends, backend):
        spectrometer = sbu.open_spectrometer(backend)

        assert isinstance(spectrometer._dev, fake_backends[backend].SeaBreezeDevice)
        assert spectrometer._backend is fake_backends[backend]
        assert spectrometer.serial_number == 'S1'
        assert len(spectrometer.wavelengths()) == N_PIXELS
        assert spectrometer._dev.is_open

    def test_seabreeze_own_backend_is_not_touched(self, fake_backends):
        from seabreeze.spectrometers import Spectrometer
        sbu.open_spectrometer('pyseabreeze')
        assert vars(Spectrometer)['_backend'].__class__.__name__ == '_SeabreezeBackendDescriptor'

    def test_serial_of_the_selected_device(self, fake_backends):
        spectrometer = sbu.open_spectrometer('cseabreeze', '<SeaBreezeDevice FAKE:S2>')
        assert spectrometer.serial_number == 'S2'

    def test_device_listed_by_the_other_backend(self, fake_backends):
        device_of_cseabreeze = sbu.list_devices('cseabreeze')[1]  # S2, as listed when PyMoDAQ starts
        spectrometer = sbu.open_spectrometer('pyseabreeze', device_of_cseabreeze)
        assert spectrometer.serial_number == 'S2'
        assert isinstance(spectrometer._dev, fake_backends['pyseabreeze'].SeaBreezeDevice)

    def test_without_selection_the_first_one_not_open_is_used(self, fake_backends):
        assert sbu.open_spectrometer('cseabreeze', '').serial_number == 'S1'
        assert sbu.open_spectrometer('cseabreeze', None).serial_number == 'S2'
        with pytest.raises(RuntimeError, match='already open'):
            sbu.open_spectrometer('cseabreeze')

    def test_no_spectrometer(self, fake_backends):
        fake_backends['pyseabreeze'] = make_fake_backend('pyseabreeze', [])
        with pytest.raises(RuntimeError, match="No spectrometer was found by the 'pyseabreeze'.*other backend"):
            sbu.open_spectrometer('pyseabreeze')

    def test_the_devices_listed_are_told(self, fake_backends):
        seen = []

        sbu.open_spectrometer('cseabreeze', '<SeaBreezeDevice FAKE:S2>', seen=seen)

        assert [device.serial_number for device in seen] == ['S1', 'S2']  # (all of them, in order)

    def test_the_devices_listed_are_told_even_if_the_one_asked_for_is_not_there(self, fake_backends):
        seen = ['something old']

        with pytest.raises(RuntimeError, match='serial number S9'):
            sbu.open_spectrometer('cseabreeze', '<SeaBreezeDevice FAKE:S9>', seen=seen)

        assert [device.serial_number for device in seen] == ['S1', 'S2']

    def test_unknown_serial_number(self, fake_backends):
        with pytest.raises(RuntimeError, match="serial number S9.*'cseabreeze'.*other backend"):
            sbu.open_spectrometer('cseabreeze', '<SeaBreezeDevice FAKE:S9>')

    def test_unknown_backend(self, fake_backends):
        with pytest.raises(ValueError):
            sbu.open_spectrometer('libseabreeze')

    def test_api_is_created_once_by_backend(self, fake_backends):
        # creating the API of pyseabreeze resets the USB devices, the open ones would be disturbed
        sbu.list_devices('pyseabreeze')
        sbu.open_spectrometer('pyseabreeze')
        sbu.open_spectrometer('pyseabreeze')
        sbu.list_devices('pyseabreeze')
        assert fake_backends['pyseabreeze'].SeaBreezeAPI.created == 1
        assert fake_backends['cseabreeze'].SeaBreezeAPI.created == 0

    def test_old_seabreeze(self, fake_backends, monkeypatch):
        class Spectrometer:  # without the _backend of seabreeze >= 2.9.0
            pass

        module = types.ModuleType('seabreeze.spectrometers')
        module.Spectrometer = Spectrometer
        monkeypatch.setitem(sys.modules, 'seabreeze.spectrometers', module)
        with pytest.raises(ImportError, match='2.9.0'):
            sbu.open_spectrometer('cseabreeze')


class TestBackendsAreNotMixed:

    def test_other_backend_is_refused_while_a_spectrometer_is_open(self, fake_backends):
        sbu.open_spectrometer('cseabreeze')

        with pytest.raises(sbu.BackendInUseError, match="'cseabreeze'.*cannot be mixed"):
            sbu.open_spectrometer('pyseabreeze')
        assert fake_backends['pyseabreeze'].SeaBreezeAPI.created == 0  # nothing of it was started

        sbu.open_spectrometer('cseabreeze')  # the same one is fine

    def test_other_backend_is_possible_once_the_spectrometers_are_closed(self, fake_backends):
        first = sbu.open_spectrometer('cseabreeze')
        second = sbu.open_spectrometer('cseabreeze')
        first.close()
        sbu.release_backend('cseabreeze')
        with pytest.raises(sbu.BackendInUseError):
            sbu.open_spectrometer('pyseabreeze')
        second.close()
        sbu.release_backend('cseabreeze')

        assert sbu.open_spectrometer('pyseabreeze').serial_number == 'S1'

    def test_failed_opening_does_not_hold_the_backend(self, fake_backends):
        fake_backends['cseabreeze'] = make_fake_backend('cseabreeze', [])
        with pytest.raises(RuntimeError, match='No spectrometer'):
            sbu.open_spectrometer('cseabreeze')

        assert sbu.open_spectrometer('pyseabreeze').serial_number == 'S1'

    def test_release_without_opening(self, fake_backends):
        sbu.release_backend('cseabreeze')  # does not go below zero
        sbu.open_spectrometer('cseabreeze')
        with pytest.raises(sbu.BackendInUseError):
            sbu.open_spectrometer('pyseabreeze')


class TestRealSeabreeze:
    """No spectrometer is needed: what is run when PyMoDAQ starts, with the real backends"""

    def test_listing(self):
        pytest.importorskip('seabreeze')
        assert isinstance(sbu.list_devices('cseabreeze'), list)

    def test_listing_with_pyseabreeze(self):
        pytest.importorskip('usb')
        assert isinstance(sbu.list_devices('pyseabreeze'), list)

    @pytest.mark.parametrize('backend', sbu.BACKENDS)
    def test_spectrometer_class(self, backend):
        pytest.importorskip('seabreeze')
        from seabreeze.spectrometers import Spectrometer
        if '_backend' not in vars(Spectrometer):
            pytest.skip('seabreeze older than 2.9.0')
        if backend == 'pyseabreeze':
            pytest.importorskip('usb')

        spectrometer = sbu.spectrometer_class(backend)

        assert issubclass(spectrometer, Spectrometer)
        assert spectrometer._backend is sbu.load_backend(backend)
        assert sbu.spectrometer_class(backend) is spectrometer


class TestAutomaticDevice:
    """The spectrometers are sorted, and Spectro k opens the k-th one: the same one each time"""

    @pytest.fixture
    def three(self, fake_backends):
        # not in the order of the serial numbers: the order the USB bus gives
        fake_backends['cseabreeze'] = make_fake_backend('cseabreeze', ['S3', 'S1', 'S2'])
        return fake_backends

    def test_the_list_is_sorted_by_serial_number(self, three):
        assert [d.serial_number for d in sbu.list_devices('cseabreeze')] == ['S1', 'S2', 'S3']

    def test_the_sort_does_not_depend_on_the_order_of_the_bus(self, fake_backends):
        fake_backends['cseabreeze'] = make_fake_backend('cseabreeze', ['S2', 'S3', 'S1'])
        assert [d.serial_number for d in sbu.list_devices('cseabreeze')] == ['S1', 'S2', 'S3']

    @pytest.mark.parametrize('title, expected', [
        ('Spectro 1', 1), ('Spectro2', 2), ('Spectrometer 12', 12), ('detector 00', None), ('Spectro', None),
        ('', None), (None, None), ('1 spectro', None), ('Spectro 2 ', 2)])
    def test_module_number(self, title, expected):
        assert sbu.module_number(title) == expected

    def test_spectro_k_opens_the_kth_spectrometer(self, three):
        second = sbu.open_spectrometer('cseabreeze', sbu.AUTO, module_title='Spectro 2')
        first = sbu.open_spectrometer('cseabreeze', sbu.AUTO, module_title='Spectro 1')

        assert (first.serial_number, second.serial_number) == ('S1', 'S2')  # whatever the order of the initializations

    def test_the_same_ones_each_time(self, three):
        for _ in range(2):
            spectrometers = [sbu.open_spectrometer('cseabreeze', None, module_title=f'Spectro {k}') for k in (1, 2, 3)]
            assert [s.serial_number for s in spectrometers] == ['S1', 'S2', 'S3']
            for spectrometer in spectrometers:
                spectrometer.close()
                sbu.release_backend('cseabreeze')

    def test_modules_without_number_take_the_first_free_ones_in_order(self, three):
        assert [sbu.open_spectrometer('cseabreeze', sbu.AUTO, module_title='Spectro').serial_number
                for _ in range(3)] == ['S1', 'S2', 'S3']

    def test_zero_based_names_do_not_collide(self, three):
        # detector 00 takes the first free one, detector 01 would like the first one but it is open: it takes the next
        assert sbu.open_spectrometer('cseabreeze', sbu.AUTO, module_title='detector 00').serial_number == 'S1'
        assert sbu.open_spectrometer('cseabreeze', sbu.AUTO, module_title='detector 01').serial_number == 'S2'

    def test_a_number_beyond_the_spectrometers_takes_the_first_free_one(self, fake_backends):
        assert sbu.open_spectrometer('cseabreeze', sbu.AUTO, module_title='Spectro 7').serial_number == 'S1'

    def test_a_given_spectrometer_wins_over_the_number(self, three):
        spectrometer = sbu.open_spectrometer('cseabreeze', '<SeaBreezeDevice FAKE:S3>', module_title='Spectro 1')

        assert spectrometer.serial_number == 'S3'

    def test_nothing_left_to_open(self, fake_backends):
        sbu.open_spectrometer('cseabreeze', sbu.AUTO, module_title='Spectro 1')
        sbu.open_spectrometer('cseabreeze', sbu.AUTO, module_title='Spectro 2')

        with pytest.raises(RuntimeError, match='already open'):
            sbu.open_spectrometer('cseabreeze', sbu.AUTO, module_title='Spectro 3')


class TestPrepareUsb:
    """On Windows pyusb finds the libusb DLL of the libusb-package package, and pyseabreeze is the backend"""

    @pytest.fixture
    def windows(self, monkeypatch, tmp_path):
        import os
        folder = tmp_path / 'libusb_package'
        folder.mkdir()
        fake = types.ModuleType('libusb_package')
        fake.get_library_path = lambda: str(folder / 'libusb-1.0.dll')
        monkeypatch.setitem(sys.modules, 'libusb_package', fake)
        monkeypatch.setattr(sys, 'platform', 'win32')
        added = []
        monkeypatch.setattr(os, 'add_dll_directory', added.append, raising=False)
        monkeypatch.setenv('PATH', '/usr/bin')
        monkeypatch.delenv('SEABREEZE_BACKEND', raising=False)
        return folder, added

    def test_the_dll_folder_is_made_available(self, windows):
        import os
        folder, added = windows

        assert sbu.prepare_usb() == str(folder)

        assert added == [str(folder)]
        assert os.environ['PATH'].split(os.pathsep) == [str(folder), '/usr/bin']
        assert os.environ['SEABREEZE_BACKEND'] == 'pyseabreeze'

    def test_it_can_be_called_again(self, windows):
        import os
        folder, _ = windows

        sbu.prepare_usb()
        sbu.prepare_usb()

        assert os.environ['PATH'].split(os.pathsep).count(str(folder)) == 1

    def test_the_backend_chosen_by_the_user_is_kept(self, windows, monkeypatch):
        import os
        monkeypatch.setenv('SEABREEZE_BACKEND', 'cseabreeze')

        sbu.prepare_usb()

        assert os.environ['SEABREEZE_BACKEND'] == 'cseabreeze'

    def test_nothing_is_done_without_the_package(self, windows, monkeypatch):
        import os
        monkeypatch.setitem(sys.modules, 'libusb_package', None)  # (not installed)

        assert sbu.prepare_usb() is None

        assert os.environ['PATH'] == '/usr/bin' and 'SEABREEZE_BACKEND' not in os.environ

    def test_nothing_is_done_on_linux(self, windows, monkeypatch):
        import os
        monkeypatch.setattr(sys, 'platform', 'linux')

        assert sbu.prepare_usb() is None

        assert os.environ['PATH'] == '/usr/bin' and 'SEABREEZE_BACKEND' not in os.environ

    def test_pyseabreeze_is_prepared_before_it_is_imported_and_not_the_other_one(self, monkeypatch):
        calls = []
        monkeypatch.setattr(sbu, 'prepare_usb', lambda: calls.append('prepared'))
        monkeypatch.setattr(sbu.importlib, 'import_module', lambda name: calls.append(name) or name)

        sbu.load_backend('pyseabreeze')
        sbu.load_backend('cseabreeze')

        assert calls == ['prepared', 'seabreeze.pyseabreeze', 'seabreeze.cseabreeze']


class TestUsbDevicesAreOpenedOneAtATime:
    """Two spectrometers initialized together (a thread each) must not open the same USB devices at the same time:
    they corrupt each other's answers"""

    def test_the_listing_is_serialized(self, monkeypatch):
        import threading
        import time
        state = {'now': 0, 'max': 0}

        class Api:
            def list_devices(self):
                state['now'] += 1
                state['max'] = max(state['max'], state['now'])
                time.sleep(0.05)
                state['now'] -= 1
                return []

        monkeypatch.setattr(sbu, 'get_api', lambda name: Api())
        threads = [threading.Thread(target=sbu.list_devices, args=('pyseabreeze',)) for _ in range(4)]

        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert state['max'] == 1

    def test_opening_holds_the_lock_while_it_lists_and_opens(self, monkeypatch):
        """open_spectrometer takes the lock around the listing and the opening: another thread waits for both"""
        source = Path(sbu.__file__).read_text()
        body = source[source.index('with _open_lock:  # (one spectrometer at a time'):]
        assert 'devices = list_devices(backend)' in body.split('except BaseException')[0]
        assert 'return spectrometer(find_device' in body.split('except BaseException')[0]
