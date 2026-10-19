import numpy as np
import pytest

from seabreeze_models import MODELS, dark_pixel_indices

pytest.importorskip('seabreeze')  # the plugin needs python-seabreeze, but no spectrometer
from pymodaq_plugins_oceaninsight.daq_viewer_plugins.plugins_1D import daq_1Dviewer_Seabreeze as viewer  # noqa: E402
from pymodaq_plugins_oceaninsight.hardware import seabreeze_utils as sbu  # noqa: E402


class FakeDevice:
    """Stands for a seabreeze SeaBreezeDevice"""
    serial_number = 'FAKE0001'

    def __repr__(self):
        return '<SeaBreezeDevice FAKE:FAKE0001>'


class FakeFeature:
    """Stands for the spectrometer feature of a seabreeze device"""

    def __init__(self, dark_pixels):
        self.spectrometer = self
        self.nonlinearity_coefficients = None
        self._dark_pixels = dark_pixels

    def get_electric_dark_pixel_indices(self):
        return self._dark_pixels


class FakeSpectrometer:
    """Stands for seabreeze.spectrometers.Spectrometer

    The intensity of a pixel is its index: the pixels returned by the plugin can be identified
    """
    model = 'FAKE'
    serial_number = 'FAKE0001'
    integration_time_micros_limits = (1000, 10000000)

    def __init__(self, n_pixels, dark_pixels):
        self._n_pixels = n_pixels
        self.f = FakeFeature(dark_pixels)
        self.closed = False

    def close(self):
        self.closed = True

    def wavelengths(self):  # in nm
        return np.linspace(200, 1100, self._n_pixels)

    def intensities(self, correct_dark_counts=False, correct_nonlinearity=False):
        return np.arange(self._n_pixels, dtype=float)

    def integration_time_micros(self, integration_time):
        pass


@pytest.fixture
def make_plugin(monkeypatch):
    """Create the plugin connected to a fake spectrometer

    The data it emits are collected in plugin.emitted, the arguments of open_spectrometer in plugin.opened and the
    backends it released in plugin.released
    """

    def factory(n_pixels, dark_pixels, backend=None):
        device = FakeDevice()
        plugin = viewer.DAQ_1DViewer_Seabreeze()
        plugin.opened, plugin.released, plugin.emitted, plugin.titles = [], [], [], []

        def open_spectrometer(backend, selected_device=None, module_title=None, seen=None):
            plugin.opened.append((backend, selected_device))
            plugin.titles.append(module_title)
            if seen is not None:
                seen[:] = [device]
            return FakeSpectrometer(n_pixels, dark_pixels)

        monkeypatch.setattr(viewer, 'open_spectrometer', open_spectrometer)
        monkeypatch.setattr(viewer, 'release_backend', plugin.released.append)
        plugin.settings.child('device').setLimits([device])
        plugin.settings.child('device').setValue(device)
        assert plugin.settings['device'] is device
        if backend is not None:
            plugin.settings.child('backend').setValue(backend)
        plugin.dte_signal.connect(plugin.emitted.append)
        return plugin

    return factory


def grabbed_spectrum(plugin):
    return plugin.emitted[-1][0].data[0]


@pytest.mark.parametrize('model, n_pixels, ranges, kept', MODELS, ids=[model[0] for model in MODELS])
def test_spectrum_has_no_dark_pixel(make_plugin, model, n_pixels, ranges, kept):
    dark = dark_pixel_indices(ranges)
    plugin = make_plugin(n_pixels, dark)

    info, initialized = plugin.ini_detector()
    plugin.grab_data()

    start, stop = kept
    spectrum = grabbed_spectrum(plugin)
    assert initialized
    assert spectrum.size == plugin.x_axis.size == stop - start
    assert spectrum[0] == start and spectrum[-1] == stop - 1
    assert not set(spectrum.astype(int)) & set(dark)
    assert plugin.settings['advanced', 'dark_channels'] == n_pixels - (stop - start)


def test_wavelengths_are_in_nm(make_plugin):
    # seabreeze gives nm: the axis used to be multiplied by 1e9 and declared in m (6.5e11 m for 650 nm)
    plugin = make_plugin(2048, dark_pixel_indices([(6, 21)]))

    plugin.ini_detector()

    axis = plugin.x_axis
    assert axis.units == 'nm'
    assert axis.get_data().size == 2048 - 21
    assert 200. <= axis.get_data()[0] < axis.get_data()[-1] <= 1100.


def test_device_without_dark_pixels_is_initialized(make_plugin):
    # used to be an IndexError: STS, HDX, FX... do not have any electric dark pixel
    plugin = make_plugin(1024, [])
    info, initialized = plugin.ini_detector()
    assert initialized


def test_dark_pixels_wrapped_in_a_tuple(make_plugin):
    # what pyseabreeze returns for the USB4000
    plugin = make_plugin(3840, [tuple(range(5, 16))])
    plugin.ini_detector()
    plugin.grab_data()
    assert grabbed_spectrum(plugin)[0] == 16


def test_hardware_averaging(make_plugin):
    plugin = make_plugin(2048, dark_pixel_indices([(6, 21)]))
    plugin.ini_detector()
    plugin.grab_data(Naverage=3)
    spectrum = grabbed_spectrum(plugin)
    assert spectrum.size == 2048 - 21
    assert spectrum[0] == 21


class TestBackendList:

    def test_it_is_a_list_to_choose_from(self):
        plugin = viewer.DAQ_1DViewer_Seabreeze()
        backend = plugin.settings.child('backend')

        assert backend.opts['type'] == 'list'
        assert list(backend.opts['limits']) == list(sbu.BACKENDS)
        assert not backend.opts.get('readonly', False)  # the user can choose

    def test_pyseabreeze_is_the_backend_by_default(self):
        plugin = viewer.DAQ_1DViewer_Seabreeze()

        assert plugin.settings['backend'] == 'pyseabreeze' == sbu.DEFAULT_BACKEND

    @pytest.mark.parametrize('backend', sbu.BACKENDS)
    def test_the_chosen_backend_and_device_are_used(self, make_plugin, backend):
        plugin = make_plugin(1024, [], backend=backend)

        info, initialized = plugin.ini_detector()

        assert initialized
        assert [name for name, _ in plugin.opened] == [backend]
        assert plugin.opened[0][1] is plugin.settings['device']
        assert f"with the {backend} backend" in info

    def test_a_new_choice_is_used_by_the_next_initialization(self, make_plugin):
        plugin = make_plugin(1024, [], backend='cseabreeze')
        plugin.ini_detector()
        plugin.close()

        plugin.settings.child('backend').setValue('pyseabreeze')
        plugin.ini_detector()

        assert [name for name, _ in plugin.opened] == ['cseabreeze', 'pyseabreeze']

    def test_choice_while_initialized_is_announced(self, make_plugin, capsys):
        plugin = make_plugin(1024, [])
        plugin.ini_detector()

        plugin.commit_settings(plugin.settings.child('backend'))

        assert 'will be used when the spectrometer is initialized again' in capsys.readouterr().out

    def test_close_releases_the_backend(self, make_plugin):
        plugin = make_plugin(1024, [], backend='pyseabreeze')
        plugin.ini_detector()
        controller = plugin.controller

        plugin.close()
        plugin.close()

        assert controller.closed
        assert plugin.released == ['pyseabreeze']  # once

    def test_nothing_to_release_when_the_spectrometer_was_not_opened(self, make_plugin, monkeypatch):
        plugin = make_plugin(1024, [])

        def no_spectrometer(backend, selected_device=None, module_title=None, seen=None):
            raise RuntimeError("No spectrometer was found")

        monkeypatch.setattr(viewer, 'open_spectrometer', no_spectrometer)
        with pytest.raises(RuntimeError, match='No spectrometer'):
            plugin.ini_detector()
        plugin.close()

        assert plugin.released == []


class TestNothingIsTouchedBeforeTheUserConnects:
    """The spectrometers are looked for when a module is initialized, not when PyMoDAQ starts or a module is created"""

    @pytest.fixture
    def spy(self, monkeypatch):
        """Every access to the spectrometers: the listing of a backend, the opening of a spectrometer"""
        calls = []
        monkeypatch.setattr(sbu, 'list_devices', lambda name: calls.append(('list', name)) or [])
        monkeypatch.setattr(sbu, 'get_api', lambda name: calls.append(('api', name)))
        monkeypatch.setattr(sbu, 'open_spectrometer', lambda *args, **kwargs: calls.append(('open', args)))
        return calls

    def test_importing_the_plugin_does_not_list_the_spectrometers(self, spy):
        import importlib
        try:
            importlib.reload(viewer)  # (the class attributes, the settings, are made when the module is imported)
        finally:
            importlib.reload(viewer)

        assert spy == []
        assert not hasattr(viewer, 'list_connected_devices')

    def test_creating_the_plugin_does_not_open_a_spectrometer(self, spy):
        plugin = viewer.DAQ_1DViewer_Seabreeze()

        assert spy == [] and plugin.controller is None

    def test_the_device_list_only_has_auto_until_a_module_is_initialized(self):
        plugin = viewer.DAQ_1DViewer_Seabreeze()

        assert list(plugin.settings.child('device').opts['limits']) == [sbu.AUTO]


class TestDeviceChoice:

    def test_the_spectrometer_is_chosen_automatically_by_default(self):
        plugin = viewer.DAQ_1DViewer_Seabreeze()
        device = plugin.settings.child('device')

        assert device.value() == sbu.AUTO
        assert device.opts['limits'][0] == sbu.AUTO

    def test_the_connected_spectrometers_are_listed_when_the_module_is_initialized(self, make_plugin):
        plugin = make_plugin(1024, [])
        plugin.settings.child('device').setLimits([sbu.AUTO])
        plugin.settings.child('device').setValue(sbu.AUTO)

        plugin.ini_detector()

        device = plugin.settings.child('device')
        assert list(device.opts['limits']) == [sbu.AUTO, '<SeaBreezeDevice FAKE:FAKE0001>']
        assert device.value() == sbu.AUTO  # (what was selected stays)

    def test_a_listed_spectrometer_can_be_chosen_for_the_next_time(self, make_plugin):
        plugin = make_plugin(1024, [])
        plugin.settings.child('device').setLimits([sbu.AUTO])
        plugin.settings.child('device').setValue(sbu.AUTO)
        plugin.ini_detector()
        plugin.close()

        plugin.settings.child('device').setValue('<SeaBreezeDevice FAKE:FAKE0001>')
        plugin.ini_detector()

        assert plugin.opened[-1][1] == '<SeaBreezeDevice FAKE:FAKE0001>'

    def test_a_spectrometer_chosen_before_stays_selected_when_it_is_not_connected(self, make_plugin, monkeypatch):
        plugin = make_plugin(1024, [])
        absent = '<SeaBreezeDevice FAKE:ABSENT>'
        plugin.settings.child('device').setLimits([sbu.AUTO, absent])
        plugin.settings.child('device').setValue(absent)

        plugin.ini_detector()  # (the fake only knows FAKE0001)

        device = plugin.settings.child('device')
        assert device.value() == absent  # not replaced by Auto behind the back of the user
        assert list(device.opts['limits']) == [sbu.AUTO, '<SeaBreezeDevice FAKE:FAKE0001>', absent]

    def test_the_list_is_made_even_when_the_initialization_fails(self, make_plugin, monkeypatch):
        plugin = make_plugin(1024, [])
        plugin.settings.child('device').setLimits([sbu.AUTO])
        plugin.settings.child('device').setValue(sbu.AUTO)

        def not_open(backend, selected_device=None, module_title=None, seen=None):
            seen[:] = [FakeDevice()]
            raise RuntimeError('All the spectrometers found are already open.')

        monkeypatch.setattr(viewer, 'open_spectrometer', not_open)
        with pytest.raises(RuntimeError, match='already open'):
            plugin.ini_detector()

        assert list(plugin.settings.child('device').opts['limits']) == [sbu.AUTO, '<SeaBreezeDevice FAKE:FAKE0001>']

    def test_the_name_of_the_module_is_given_to_choose_the_spectrometer(self, make_plugin):
        plugin = make_plugin(1024, [])
        plugin._title = 'Spectro 2'
        plugin.settings.child('device').setLimits([sbu.AUTO])
        plugin.settings.child('device').setValue(sbu.AUTO)

        plugin.ini_detector()

        assert plugin.opened == [(plugin.settings['backend'], sbu.AUTO)]
        assert plugin.titles == ['Spectro 2']
