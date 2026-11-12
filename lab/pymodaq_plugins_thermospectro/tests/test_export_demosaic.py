"""lab_export: the colors of the images of a color camera which were recorded as the Bayer mosaic

The mosaic is the raw counts of the sensor, one channel. The tif of the exporter holds the colors made from it, by bilinear
interpolation unless told otherwise (``--demosaic``): the mosaic itself (``raw``), nearest, cubic, malvar, superpixel.
"""
import json

import numpy as np
import pytest

from pymodaq_plugins_thermospectro import bayer, export, saving
from pymodaq_plugins_thermospectro.camera import RectRoi

from test_export import HEIGHT, WIDTH, run_export, write_experiment
from test_saving import h5saver  # noqa: F401  (a fixture)

pytestmark = pytest.mark.filterwarnings('ignore')

OFFSETS = [(0, 0), (0, 1), (1, 0), (1, 1)]
METHODS = [m for m in export.DEMOSAIC_METHODS if m != 'raw']
BORDER = export.BAYER_PAD


def scene(shape, offset, colors=(100., 200., 300.), gradient=None):
    """The mosaic of a scene whose red, green and blue are these (flat), or a linear gradient `gradient` = (y, x) slopes"""
    found = export.bayer_masks(shape, offset)
    mosaic = sum(color * found[index] for index, color in enumerate(colors)).astype(float)
    if gradient is not None:
        rows, columns = np.indices(shape)
        mosaic = mosaic + gradient[0] * rows + gradient[1] * columns
    return mosaic


def interior(array, border=BORDER):
    return array[..., border:-border, border:-border]


class TestTheMethods:
    @pytest.mark.parametrize('method', METHODS)
    @pytest.mark.parametrize('offset', OFFSETS)
    def test_a_flat_scene_gives_flat_colors_whatever_the_phase(self, method, offset):
        colors = export.demosaic(scene((24, 28), offset), offset, method)

        if method == 'superpixel':
            assert colors.shape[0] == 3
            expected = (100., 200., 300.)
            for index in range(3):
                assert np.allclose(colors[index], expected[index])
        else:
            assert colors.shape == (3, 24, 28)
            for index, value in enumerate((100., 200., 300.)):
                assert np.allclose(interior(colors[index]), value), (method, offset, index)

    @pytest.mark.parametrize('method', ['bilinear', 'cubic', 'malvar'])
    @pytest.mark.parametrize('offset', OFFSETS)
    def test_a_linear_gradient_is_rebuilt_exactly(self, method, offset):
        mosaic = scene((24, 28), offset, colors=(0., 0., 0.), gradient=(10., 1.))
        rows, columns = np.indices(mosaic.shape)

        colors = export.demosaic(mosaic, offset, method)

        for index in range(3):
            assert np.allclose(interior(colors[index]), interior(10. * rows + columns), atol=1e-3), (method, offset)

    @pytest.mark.parametrize('method', ['bilinear', 'nearest', 'cubic', 'malvar'])
    def test_the_pixels_which_measured_a_color_keep_their_count(self, method):
        mosaic = np.random.default_rng(3).integers(0, 1024, (20, 22)).astype(np.float32)
        found = export.bayer_masks(mosaic.shape, (1, 0))

        colors = export.demosaic(mosaic, (1, 0), method)

        for index in range(3):
            assert np.array_equal(colors[index][found[index]], mosaic[found[index]]), method

    @pytest.mark.parametrize('offset', OFFSETS)
    def test_the_bilinear_one_is_the_one_of_the_extension(self, offset):
        mosaic = np.random.default_rng(5).integers(0, 1024, (11, 13)).astype(np.uint16)

        assert np.array_equal(export.demosaic(mosaic, offset, 'bilinear'), bayer.debayer(mosaic, offset))

    def test_nearest_copies_the_pixel_of_the_block(self):
        mosaic = np.array([[1, 2, 5, 6], [3, 4, 7, 8], [9, 10, 13, 14], [11, 12, 15, 16]], dtype=float)  # (red at 0, 0)

        colors = export.demosaic(mosaic, (0, 0), 'nearest')

        assert np.array_equal(colors[0][:2, :2], np.full((2, 2), 1.))  # (the red of the block)
        assert np.array_equal(colors[2][:2, :2], np.full((2, 2), 4.))  # (the blue of the block)
        assert colors[1][0, 0] == 2 and colors[1][1, 1] == 3  # (the green right of the red, the one left of the blue)
        assert colors[1][0, 1] == 2 and colors[1][1, 0] == 3  # (the greens themselves)

    @pytest.mark.parametrize('offset', OFFSETS)
    def test_the_superpixel_is_a_block_of_2_by_2(self, offset):
        mosaic = np.random.default_rng(1).integers(0, 1024, (9, 11)).astype(np.float32)
        r0, c0 = offset

        colors = export.demosaic(mosaic, offset, 'superpixel')

        assert colors.shape == (3, (9 - r0) // 2, (11 - c0) // 2)
        assert colors[0][0, 0] == mosaic[r0, c0] and colors[2][0, 0] == mosaic[r0 + 1, c0 + 1]
        assert colors[1][0, 0] == (mosaic[r0, c0 + 1] + mosaic[r0 + 1, c0]) / 2

    def test_a_method_which_is_not_one_is_refused(self):
        with pytest.raises(ValueError, match='not a way'):
            export.demosaic(np.zeros((4, 4)), (0, 0), 'magic')
        with pytest.raises(ValueError, match='2D'):
            export.demosaic(np.zeros((2, 4, 4)), (0, 0))

    def test_a_tiny_image_does_not_break(self):
        for method in METHODS:
            assert export.demosaic(np.ones((3, 3)), (0, 0), method).ndim == 3


class TestTheSeries:
    def test_the_counts_stay_counts_and_cannot_go_below_zero(self):
        mosaic = np.zeros((1, 16, 16), dtype=np.uint16)
        mosaic[0, :, 8:] = 1000  # (a sharp edge: a cubic interpolation overshoots on both sides)
        series = export.Series('Image', 'Unit3_Image', np.zeros(1), [mosaic], ['Intensity'], units=['A.U.'])

        colors = export.color_series(series, (0, 0), 'cubic')

        assert len(colors.arrays) == 3 and colors.labels == ['Red', 'Green', 'Blue'] and colors.units == ['A.U.'] * 3
        assert colors.arrays[0].dtype == np.uint16 and colors.arrays[0].shape == (1, 16, 16)
        assert all(array[0].min() >= 0 for array in colors.arrays)

    def test_floats_on_request_or_from_floats(self):
        integer = np.ones((2, 8, 8), dtype=np.uint16)
        series = export.Series('Image', 't', np.zeros(2), [integer], ['I'])

        assert export.color_series(series, (0, 0), 'bilinear').arrays[0].dtype == np.uint16
        assert export.color_series(series, (0, 0), 'bilinear', as_float=True).arrays[0].dtype == np.float32
        series.arrays = [integer.astype(float)]
        assert export.color_series(series, (0, 0), 'bilinear').arrays[0].dtype == np.float32

    def test_a_channel_is_read_like_an_array_of_images(self):
        mosaic = np.random.default_rng(2).integers(0, 1024, (4, 8, 8)).astype(np.uint16)
        series = export.Series('Image', 't', np.arange(4.), [mosaic], ['I'])
        red = export.color_series(series, (0, 0), 'bilinear').arrays[0]

        assert len(red) == 4 and red.shape == (4, 8, 8)
        assert np.array_equal(red[2], np.rint(export.demosaic(mosaic[2], (0, 0))[0]).astype(np.uint16))
        assert red[-1].shape == (8, 8) and red[1:3].shape == (2, 8, 8)
        with pytest.raises(IndexError):
            red[4]
        with pytest.raises(TypeError):
            red['x']


def said(monkeypatch, **changes):
    info = {'manufacturer': 'Thorlabs', 'model': 'CS165CU', 'pixel_um': [3.45, 3.45], 'pixel_source': 'camera',
            'bit_depth': 10, 'sensor_type': 'bayer', 'raw_counts': True, 'bayer_phase': 'red', 'image_format': 'bayer',
            'window': [0, 1440, 0, 1080], **changes}
    monkeypatch.setattr(saving, 'camera_info_of', lambda unit: json.dumps(info) if unit.settings.KIND == 'camera' else '')


def mosaic_of(index):
    rows, columns = np.mgrid[0:HEIGHT, 0:WIDTH]
    return (10 * rows + columns + index).astype(np.uint16)


@pytest.fixture
def tifffile():
    return pytest.importorskip('tifffile')


class TestTheTif:
    def test_by_default_the_colors_are_made_by_bilinear_interpolation(self, h5saver, qapp, tmp_path, monkeypatch,
                                                                       tifffile):
        said(monkeypatch)
        path = write_experiment(h5saver, spectrometer=False)

        out = run_export(path, tmp_path)

        with tifffile.TiffFile(out / f'{path.stem}_camera.tif') as tif:
            images, metadata = tif.asarray(), tif.imagej_metadata
        assert images.shape == (5, 3, HEIGHT, WIDTH) and images.dtype == np.uint16
        for index in range(5):
            assert np.array_equal(images[index], np.rint(bayer.debayer(mosaic_of(index), (0, 0))).astype(np.uint16))
        assert metadata['mode'] == 'composite' and metadata['Ranges'][:2] == (0.0, 1023.0)
        assert 'bilinear interpolation' in metadata['Info'] and 'Bayer phase red' in metadata['Info']

    def test_raw_is_the_mosaic_as_it_was_recorded(self, h5saver, qapp, tmp_path, monkeypatch, tifffile):
        said(monkeypatch)
        path = write_experiment(h5saver, spectrometer=False)

        out = run_export(path, tmp_path, '--demosaic', 'raw')

        with tifffile.TiffFile(out / f'{path.stem}_camera.tif') as tif:
            images, metadata = tif.asarray(), tif.imagej_metadata
        assert images.shape == (5, HEIGHT, WIDTH) and images.dtype == np.uint16
        for index in range(5):
            assert np.array_equal(images[index], mosaic_of(index))
        assert 'raw Bayer mosaic' in metadata['Info']

    @pytest.mark.parametrize('method', ['nearest', 'cubic', 'malvar'])
    def test_the_other_methods(self, h5saver, qapp, tmp_path, monkeypatch, tifffile, method):
        said(monkeypatch)
        path = write_experiment(h5saver, spectrometer=False)

        out = run_export(path, tmp_path, '--demosaic', method)

        with tifffile.TiffFile(out / f'{path.stem}_camera.tif') as tif:
            images, info = tif.asarray(), tif.imagej_metadata['Info']
        expected = np.clip(np.rint(export.demosaic(mosaic_of(3), (0, 0), method)), 0, 65535).astype(np.uint16)
        assert np.array_equal(images[3], expected)
        assert export.DEMOSAIC_WORDS[method][:20] in info

    def test_the_superpixel_halves_the_image_and_doubles_the_pixel(self, h5saver, qapp, tmp_path, monkeypatch, tifffile):
        said(monkeypatch)
        path = write_experiment(h5saver, spectrometer=False)

        out = run_export(path, tmp_path, '--demosaic', 'superpixel')

        with tifffile.TiffFile(out / f'{path.stem}_camera.tif') as tif:
            images = tif.asarray()
            x = tif.pages[0].tags['XResolution'].value
        assert images.shape == (5, 3, HEIGHT // 2, WIDTH // 2)
        assert x[0] / x[1] == pytest.approx(1 / 6.9, rel=1e-3)  # (a pixel is a block of 2 x 2 of 3.45 um)

    def test_floats_on_request(self, h5saver, qapp, tmp_path, monkeypatch, tifffile):
        said(monkeypatch)
        path = write_experiment(h5saver, spectrometer=False)

        out = run_export(path, tmp_path, '--demosaic-float')

        with tifffile.TiffFile(out / f'{path.stem}_camera.tif') as tif:
            images, info = tif.asarray(), tif.imagej_metadata['Info']
        assert images.dtype == np.float32 and '32 bits floats' in info
        assert np.allclose(images[1], bayer.debayer(mosaic_of(1), (0, 0)))

    def test_the_phase_of_the_sensor_is_used(self, h5saver, qapp, tmp_path, monkeypatch, tifffile):
        said(monkeypatch, bayer_phase='green_left_or_red')
        path = write_experiment(h5saver, spectrometer=False)

        out = run_export(path, tmp_path)

        with tifffile.TiffFile(out / f'{path.stem}_camera.tif') as tif:
            images = tif.asarray()
        assert np.array_equal(images[0], np.rint(bayer.debayer(mosaic_of(0), (0, 1))).astype(np.uint16))

    def test_a_window_which_starts_at_an_odd_pixel_moves_the_pattern(self, h5saver, qapp, tmp_path, monkeypatch, tifffile):
        said(monkeypatch, window=[1, 9, 0, 6])
        path = write_experiment(h5saver, spectrometer=False)

        out = run_export(path, tmp_path)

        with tifffile.TiffFile(out / f'{path.stem}_camera.tif') as tif:
            images = tif.asarray()
        assert np.array_equal(images[0], np.rint(bayer.debayer(mosaic_of(0), (0, 1))).astype(np.uint16))

    def test_a_crop_has_the_pattern_of_its_place_in_the_image(self, h5saver, qapp, tmp_path, monkeypatch, tifffile):
        said(monkeypatch)
        path = write_experiment(h5saver, spectrometer=False, camera_rois=[RectRoi('Window', 1, 6, 1, 5, aggregation='crop')])

        out = run_export(path, tmp_path, '--demosaic', 'bilinear')

        with tifffile.TiffFile(out / f'{path.stem}_camera_crop_Window.tif') as tif:
            images = tif.asarray()
        crop = mosaic_of(2)[1:5, 1:6]
        assert images.shape == (5, 3, 4, 5)
        assert np.array_equal(images[2], np.rint(bayer.debayer(crop, (1, 1))).astype(np.uint16))  # (red at an odd place)

    def test_an_option_which_cannot_apply_is_said(self, h5saver, qapp, tmp_path, monkeypatch, tifffile, capsys):
        said(monkeypatch, image_format='rgb')
        path = write_experiment(h5saver, spectrometer=False, channels=3)

        out = run_export(path, tmp_path, '--demosaic', 'cubic')

        with tifffile.TiffFile(out / f'{path.stem}_camera.tif') as tif:
            assert tif.asarray().shape == (5, 3, HEIGHT, WIDTH)  # (as they were)
        assert '--demosaic cubic is ignored' in capsys.readouterr().err

    def test_binned_images_are_exported_as_they_are(self, h5saver, qapp, tmp_path, monkeypatch, tifffile, capsys):
        said(monkeypatch, binning=[2, 2])
        path = write_experiment(h5saver, spectrometer=False)

        out = run_export(path, tmp_path)

        with tifffile.TiffFile(out / f'{path.stem}_camera.tif') as tif:
            assert tif.asarray().shape == (5, HEIGHT, WIDTH)
        assert 'binned' in capsys.readouterr().err

    def test_a_monochrome_camera_is_left_alone_without_a_word(self, h5saver, qapp, tmp_path, monkeypatch, tifffile, capsys):
        said(monkeypatch, image_format='', sensor_type='mono', bayer_phase='')
        path = write_experiment(h5saver, spectrometer=False)

        out = run_export(path, tmp_path)

        with tifffile.TiffFile(out / f'{path.stem}_camera.tif') as tif:
            assert tif.asarray().shape == (5, HEIGHT, WIDTH)
        assert 'ignored' not in capsys.readouterr().err

    def test_the_methods_are_in_the_help(self):
        text = export.parser().format_help()

        assert '--demosaic' in text and all(method in text for method in export.DEMOSAIC_METHODS)
