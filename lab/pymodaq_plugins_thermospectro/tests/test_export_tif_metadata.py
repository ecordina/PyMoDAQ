"""What the tif files of lab_export say besides the pixels: the size of a pixel, the camera, the date, the exposure times...

The camera information is saved by the extension with the images (see camera_info.py), in a real h5 file, and read back by
the exporter.
"""
import datetime
import json

import numpy as np
import pytest

from pymodaq_plugins_thermospectro import export, saving
from pymodaq_plugins_thermospectro.camera import RectRoi
from pymodaq_plugins_thermospectro.rois import Roi

from test_export import make_camera_unit, run_export, write_experiment
from test_saving import h5saver, make_unit  # noqa: F401  (h5saver is a fixture)

pytestmark = pytest.mark.filterwarnings('ignore')


class ThorlabsLike:
    """A driver which answers like the one of a Thorlabs camera"""

    def get_device_info(self):
        return type('Info', (), {'model': 'CS165MU1', 'name': '', 'serial_number': '1'})()

ZELUX = {'manufacturer': 'Thorlabs', 'model': 'CS165MU1', 'serial': '12345', 'pixel_um': [3.45, 3.45],
         'pixel_source': 'camera', 'bit_depth': 10, 'sensor_type': 'mono', 'window': [0, 1440, 0, 1080]}


def said(monkeypatch, **changes):
    """The camera says this when the experiment starts"""
    info = {**ZELUX, **changes}
    monkeypatch.setattr(saving, 'camera_info_of', lambda unit: json.dumps(info) if unit.settings.KIND == 'camera' else '')


def page_of(tif):
    return tif.pages[0]


class TestRecording:
    def test_the_driver_of_the_camera_is_asked(self, qapp, monkeypatch):
        unit = make_camera_unit()
        unit.detector.controller = type('Controller', (), {
            'get_device_info': lambda self: type('Info', (), {'model': 'CS235MU', 'name': '', 'serial_number': '7'})(),
            'get_roi': lambda self: (0, 960, 0, 600, 2, 2)})()

        info = json.loads(saving.camera_info_of(unit))

        assert info['model'] == 'CS235MU' and info['pixel_um'] == [5.86, 5.86]  # (from the table: no SDK here)
        assert info['binning'] == [2, 2] and info['window'] == [0, 960, 0, 600]

    def test_the_controller_of_the_plugin_is_the_one_asked(self, qapp):
        unit = make_camera_unit()
        unit.detector.plugin = type('Plugin', (), {'controller': type('Controller', (), {
            'get_device_info': lambda self: type('Info', (), {'model': 'CS135MU'})()})()})()

        assert json.loads(saving.camera_info_of(unit))['model'] == 'CS135MU'

    def test_a_camera_which_says_nothing_saves_nothing(self, qapp):
        assert saving.camera_info_of(make_camera_unit()) == ''

    def test_a_spectrometer_has_no_camera_info(self, qapp):
        unit = make_unit(1, rois=[Roi('A', 500., 510.)], run_ramp=False)
        unit.detector.controller = ThorlabsLike()  # (it would answer, if it were asked)

        assert saving.camera_info_of(unit) == ''

    def test_it_is_in_the_h5_file(self, h5saver, qapp, monkeypatch):
        said(monkeypatch)
        path = write_experiment(h5saver, spectrometer=False)

        import h5py
        with h5py.File(path, 'r') as file:
            found = []
            file.visititems(lambda name, node: found.append(export.attribute(node.attrs['camera_info']))
                            if 'camera_info' in node.attrs else None)

        assert found and all(json.loads(text)['model'] == 'CS165MU1' for text in found)


class TestThePixelSize:
    def test_a_tif_is_calibrated_in_um(self, h5saver, qapp, tmp_path, monkeypatch):
        tifffile = pytest.importorskip('tifffile')
        said(monkeypatch)
        path = write_experiment(h5saver, spectrometer=False)

        out = run_export(path, tmp_path)

        with tifffile.TiffFile(out / f'{path.stem}_camera.tif') as tif:
            assert tif.imagej_metadata['unit'] == 'um'
            x, y = (tif.pages[0].tags[name].value for name in ('XResolution', 'YResolution'))
        assert x[0] / x[1] == pytest.approx(1 / 3.45, rel=1e-3) and y[0] / y[1] == pytest.approx(1 / 3.45, rel=1e-3)

    def test_the_binning_is_in_the_pixel_size(self, h5saver, qapp, tmp_path, monkeypatch):
        tifffile = pytest.importorskip('tifffile')
        said(monkeypatch, binning=[2, 4])
        path = write_experiment(h5saver, spectrometer=False)

        out = run_export(path, tmp_path)

        with tifffile.TiffFile(out / f'{path.stem}_camera.tif') as tif:
            x, y = (tif.pages[0].tags[name].value for name in ('XResolution', 'YResolution'))
        assert x[0] / x[1] == pytest.approx(1 / 6.9, rel=1e-3) and y[0] / y[1] == pytest.approx(1 / 13.8, rel=1e-3)

    def test_the_magnification_gives_the_pixel_on_the_sample(self, h5saver, qapp, tmp_path, monkeypatch):
        tifffile = pytest.importorskip('tifffile')
        said(monkeypatch)
        path = write_experiment(h5saver, spectrometer=False)

        out = run_export(path, tmp_path, '--magnification', '10')

        with tifffile.TiffFile(out / f'{path.stem}_camera.tif') as tif:
            x = tif.pages[0].tags['XResolution'].value
            info = tif.imagej_metadata['Info']
        assert x[0] / x[1] == pytest.approx(10 / 3.45, rel=1e-3)
        assert 'magnification 10' in info

    def test_the_sensor_plane_is_said_to_be_that(self, h5saver, qapp, tmp_path, monkeypatch):
        tifffile = pytest.importorskip('tifffile')
        said(monkeypatch)
        path = write_experiment(h5saver, spectrometer=False)

        out = run_export(path, tmp_path)

        with tifffile.TiffFile(out / f'{path.stem}_camera.tif') as tif:
            assert 'SENSOR PLANE' in tif.imagej_metadata['Info']

    def test_a_pixel_size_which_was_measured_wins(self, h5saver, qapp, tmp_path, monkeypatch):
        tifffile = pytest.importorskip('tifffile')
        said(monkeypatch)
        path = write_experiment(h5saver, spectrometer=False)

        out = run_export(path, tmp_path, '--pixel-size', '0.325', '--magnification', '10')

        with tifffile.TiffFile(out / f'{path.stem}_camera.tif') as tif:
            x = tif.pages[0].tags['XResolution'].value
            info = tif.imagej_metadata['Info']
        assert x[0] / x[1] == pytest.approx(1 / 0.325, rel=1e-3)
        assert '0.325 um per pixel, given with --pixel-size' in info and 'SENSOR PLANE' not in info

    def test_a_file_without_camera_info_stays_in_pixels(self, h5saver, qapp, tmp_path):
        tifffile = pytest.importorskip('tifffile')
        path = write_experiment(h5saver, spectrometer=False)

        out = run_export(path, tmp_path)

        with tifffile.TiffFile(out / f'{path.stem}_camera.tif') as tif:
            assert tif.imagej_metadata['unit'] == 'pixel'

    def test_a_file_without_camera_info_can_still_be_calibrated(self, h5saver, qapp, tmp_path):
        tifffile = pytest.importorskip('tifffile')
        path = write_experiment(h5saver, spectrometer=False)

        out = run_export(path, tmp_path, '--pixel-size', '0.5')

        with tifffile.TiffFile(out / f'{path.stem}_camera.tif') as tif:
            assert tif.imagej_metadata['unit'] == 'um'

    def test_a_camera_which_did_not_say_its_pixels_is_told_so(self, h5saver, qapp, tmp_path, monkeypatch, capsys):
        tifffile = pytest.importorskip('tifffile')
        said(monkeypatch, pixel_um=None, pixel_source='')
        path = write_experiment(h5saver, spectrometer=False)

        out = run_export(path, tmp_path)

        assert '--pixel-size' in capsys.readouterr().err
        with tifffile.TiffFile(out / f'{path.stem}_camera.tif') as tif:
            assert tif.imagej_metadata['unit'] == 'pixel'

    @pytest.mark.parametrize('option', ['--magnification', '--pixel-size'])
    @pytest.mark.parametrize('value', ['0', '-2', 'many'])
    def test_a_number_which_is_not_positive_is_refused(self, option, value):
        with pytest.raises(SystemExit):
            export.parser().parse_args(['file.h5', option, value])

    def test_the_crops_are_calibrated_too(self, h5saver, qapp, tmp_path, monkeypatch):
        tifffile = pytest.importorskip('tifffile')
        said(monkeypatch)
        path = write_experiment(h5saver, spectrometer=False, camera_rois=[RectRoi('Window', 2, 6, 0, 3, aggregation='crop')])

        out = run_export(path, tmp_path)

        with tifffile.TiffFile(out / f'{path.stem}_camera_crop_Window.tif') as tif:
            assert tif.imagej_metadata['unit'] == 'um'
            assert 'CS165MU1' in tif.imagej_metadata['Info']


class TestTheOtherMetadata:
    def test_the_camera_and_the_software_are_in_the_tags(self, h5saver, qapp, tmp_path, monkeypatch):
        tifffile = pytest.importorskip('tifffile')
        said(monkeypatch)
        path = write_experiment(h5saver, spectrometer=False)

        out = run_export(path, tmp_path)

        with tifffile.TiffFile(out / f'{path.stem}_camera.tif') as tif:
            tags = page_of(tif).tags
            assert (tags['Make'].value, tags['Model'].value) == ('Thorlabs', 'CS165MU1')
            assert 'lab_export' in tags['Software'].value

    def test_the_date_is_the_one_of_the_first_image(self, h5saver, qapp, tmp_path, monkeypatch):
        tifffile = pytest.importorskip('tifffile')
        said(monkeypatch)
        path = write_experiment(h5saver, spectrometer=False)  # (the first image is at 1000.4 s of the epoch)

        out = run_export(path, tmp_path)

        with tifffile.TiffFile(out / f'{path.stem}_camera.tif') as tif:
            written = page_of(tif).tags['DateTime'].value
        assert written == datetime.datetime.fromtimestamp(1000.4).strftime('%Y:%m:%d %H:%M:%S')

    def test_the_display_range_is_the_one_of_the_camera(self, h5saver, qapp, tmp_path, monkeypatch):
        tifffile = pytest.importorskip('tifffile')
        said(monkeypatch)
        path = write_experiment(h5saver, spectrometer=False)  # (16 bits pixels, a 10 bits camera)

        out = run_export(path, tmp_path)

        with tifffile.TiffFile(out / f'{path.stem}_camera.tif') as tif:
            assert tuple(tif.imagej_metadata['Ranges']) == (0.0, 1023.0)

    def test_a_range_larger_than_the_pixels_is_not_written(self, h5saver, qapp, tmp_path, monkeypatch):
        tifffile = pytest.importorskip('tifffile')
        said(monkeypatch, bit_depth=16)
        path = write_experiment(h5saver, spectrometer=False, dtype=np.uint8)  # (8 bits pixels, a 16 bits camera)

        out = run_export(path, tmp_path)

        with tifffile.TiffFile(out / f'{path.stem}_camera.tif') as tif:
            assert 'Ranges' not in tif.imagej_metadata

    def test_each_channel_has_its_range(self, h5saver, qapp, tmp_path, monkeypatch):
        tifffile = pytest.importorskip('tifffile')
        said(monkeypatch)
        path = write_experiment(h5saver, spectrometer=False, channels=3)

        out = run_export(path, tmp_path)

        with tifffile.TiffFile(out / f'{path.stem}_camera.tif') as tif:
            assert tuple(tif.imagej_metadata['Ranges']) == (0.0, 1023.0) * 3
            assert tif.imagej_metadata['mode'] == 'composite'  # (the colors stay what they were)

    def test_the_exposure_time_of_each_image_is_in_its_label(self, h5saver, qapp, tmp_path):
        tifffile = pytest.importorskip('tifffile')
        path = write_experiment(h5saver, spectrometer=False, camera_integration=lambda index: 10. + index)

        out = run_export(path, tmp_path)

        with tifffile.TiffFile(out / f'{path.stem}_camera.tif') as tif:
            labels = tif.imagej_metadata['Labels']
        assert labels[0].startswith('t=0.000 s, T=30.000, exp=10.000 ms')
        assert [label.split('exp=')[1].split(' ms')[0] for label in labels] == ['10.000', '11.000', '12.000', '13.000', '14.000']

    def test_the_time_between_the_images_is_still_there(self, h5saver, qapp, tmp_path, monkeypatch):
        tifffile = pytest.importorskip('tifffile')
        said(monkeypatch)
        path = write_experiment(h5saver, spectrometer=False)

        out = run_export(path, tmp_path)

        with tifffile.TiffFile(out / f'{path.stem}_camera.tif') as tif:
            assert tif.imagej_metadata['finterval'] == pytest.approx(1.0) and tif.imagej_metadata['frames'] == 5

    def test_the_description_says_what_the_camera_is(self, h5saver, qapp, tmp_path, monkeypatch):
        tifffile = pytest.importorskip('tifffile')
        said(monkeypatch, binning=[2, 2], window=[100, 740, 50, 590])
        path = write_experiment(h5saver, spectrometer=False)

        out = run_export(path, tmp_path)

        with tifffile.TiffFile(out / f'{path.stem}_camera.tif') as tif:
            info = tif.imagej_metadata['Info']
        for word in ('Thorlabs CS165MU1', 'serial 12345', 'sensor pixels 3.45 x 3.45 um', '10 bits', 'binning 2 x 2',
                     'sensor window x 100-740, y 50-590 pixel'):
            assert word in info

    def test_text_which_is_not_ascii_does_not_stop_the_export(self, h5saver, qapp, tmp_path, monkeypatch):
        tifffile = pytest.importorskip('tifffile')
        said(monkeypatch, model='Caméra µ', manufacturer='Thorlabs')
        path = write_experiment(h5saver, spectrometer=False)

        out = run_export(path, tmp_path)

        with tifffile.TiffFile(out / f'{path.stem}_camera.tif') as tif:
            assert 'Cam?ra ?' in tif.imagej_metadata['Info'] and page_of(tif).tags['Model'].value == 'Cam?ra ?'


class TestALongSeriesOfCrops:
    def test_the_crops_of_a_long_series_are_one_bigtiff_with_the_same_metadata(self, h5saver, qapp, tmp_path, monkeypatch):
        tifffile = pytest.importorskip('tifffile')
        monkeypatch.setattr(export, 'CLASSIC_TIF_BYTES', 2 * 3 * 4 * 2 + 5)  # (two crops of 4 x 3 pixels of 16 bits)
        said(monkeypatch)
        path = write_experiment(h5saver, spectrometer=False, camera_rois=[RectRoi('Window', 2, 6, 0, 3, aggregation='crop')])

        out = run_export(path, tmp_path)

        assert not list(out.glob('*_part*')) and len(list(out.glob('*_camera_crop_*.tif'))) == 1
        with tifffile.TiffFile(out / f'{path.stem}_camera_crop_Window.tif') as tif:
            assert tif.is_bigtiff and tif.imagej_metadata['unit'] == 'um' and tif.asarray().shape == (5, 3, 4)
            assert tif.pages[0].tags['Model'].value == 'CS165MU1'
