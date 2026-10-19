"""tools/thermospectro_export.py: the h5 file of an experiment becomes csv and tif files

The files are written by the saver of the extension itself, in a real h5 file, as during an experiment.
"""
import csv
import importlib.util
import math
import sys
import time
from pathlib import Path

import h5py
import numpy as np
import pytest

from pymodaq_gui.parameter import Parameter

from pymodaq_data.data import DataRaw
from pymodaq_data.h5modules.data_saving import DataBundle

from fakes import FakeCamera, FakeManager, FakeThermostat
from pymodaq_plugins_thermospectro.camera import Frame, RectRoi
from pymodaq_plugins_thermospectro.saving import build_dtes
from pymodaq_plugins_thermospectro.unit import CameraUnit, Measurement
from pymodaq_plugins_thermospectro.unit_settings import CAMERA, CameraSettings, unit_definition
from pymodaq_plugins_thermospectro.rois import Roi

from test_saving import Experiment, h5saver, make_measurement, make_unit  # noqa: F401  (h5saver is a fixture)

pytestmark = pytest.mark.filterwarnings('ignore')

TOOL = Path(__file__).resolve().parents[1] / 'tools' / 'thermospectro_export.py'
SPEC = importlib.util.spec_from_file_location('thermospectro_export', TOOL)
export = importlib.util.module_from_spec(SPEC)
sys.modules['thermospectro_export'] = export  # (the dataclasses of the tool look their module up)
SPEC.loader.exec_module(export)

HEIGHT, WIDTH = 6, 8


def make_camera_unit(thermostat=True):
    camera = FakeCamera('Camera1')
    thermo = FakeThermostat('Thermo1') if thermostat else None
    settings = CameraSettings(Parameter.create(**unit_definition(3, [RectRoi('Spot', 1, 4, 1, 3)], kind=CAMERA)), 3)
    settings.set_modules(['Camera1'], ['Thermo1'] if thermostat else [], 'Camera1', 'Thermo1' if thermostat else None)
    settings.child('record', 'image').setValue(True)
    settings.child('ramp', 'run').setValue(False)
    unit = CameraUnit(settings, FakeManager(*[m for m in (camera, thermo) if m is not None]))
    unit.attach()
    return unit


def camera_measurement(unit, index, t0, channels=1, dtype=np.uint16, temperature=30.):
    """An image of `channels` colors: the pixel (row y, column x) of the channel c is 1000 c + 10 y + x + index"""
    rows, columns = np.mgrid[0:HEIGHT, 0:WIDTH]
    arrays = [(1000 * channel + 10 * rows + columns + index).astype(dtype) for channel in range(channels)]
    frame = Frame(arrays[0].astype(float), time.monotonic(), DataRaw('image', data=arrays))
    rois = unit.settings.rois
    values = [rois[0].value(arrays[0].astype(float))]
    return Measurement(unit.number, index, t0 + index * 1.0, frame, rois, values, temperature + index, math.nan,
                       None, 0)


def write_experiment(h5saver, spectrometer_times=(1000., 1.0, 6), camera_times=(1000.4, 1.0, 5), channels=1,
                     dtype=np.uint16, spectrometer=True, camera=True, camera_thermostat=True, spectrum=True,
                     image=True):
    """A file with a spectrometer (unit 1) and a camera (unit 3) which are not read out at the same instants"""
    units = []
    if spectrometer:
        units.append(make_unit(1, rois=[Roi('A', 500., 510.), Roi('B', 600., 610.)], run_ramp=False))
        units[-1].settings.child('record', 'spectrum').setValue(spectrum)
    if camera:
        units.append(make_camera_unit(camera_thermostat))
        units[-1].settings.child('record', 'image').setValue(image)
    experiment = Experiment(h5saver, units)
    for unit in units:
        recording = next(r for r in experiment.recordings if r.number == unit.number)
        t0, step, n = spectrometer_times if unit.number == 1 else camera_times
        for index in range(n):
            if unit.number == 1:
                measurement = make_measurement(unit, index, t0=t0 + index * (step - 1.0), phase=None)
            else:
                measurement = camera_measurement(unit, index, t0 + index * (step - 1.0), channels, dtype)
            for dte in build_dtes(recording, measurement):
                experiment.saver.add_data_bundle(DataBundle(dte=dte))
    experiment.finish()
    return experiment.path


def number(text: str):
    try:
        return float(text)
    except ValueError:
        return text


def read_csv(path: Path):
    """(header, rows): the numbers are read as floats, the text (the full spectra) is left as it is"""
    with open(path, newline='', encoding='utf-8-sig') as stream:
        rows = list(csv.reader(stream))
    return rows[0], [[number(value) for value in row] for row in rows[1:]]


def run_export(path, tmp_path, *arguments):
    out = tmp_path / 'out'
    assert export.main([str(path), '-o', str(out), *arguments]) == 0
    return out


class TestReading:
    @pytest.mark.parametrize('value, expected', [
        ('{"module": "builtins", "type": "str", "data": "\'Spectro1\'"}', 'Spectro1'),
        ('{"module": "builtins", "type": "float", "data": "20.0"}', 20.0),
        ('{"module": "builtins", "type": "int", "data": "3"}', 3),
        (b'{"module": "builtins", "type": "str", "data": "\'\\u00b0C\'"}', '°C'),
        ('plain', 'plain'),
        ('{"not": "pymodaq"}', '{"not": "pymodaq"}'),
        (7, 7),
    ])
    def test_the_attributes_of_pymodaq(self, value, expected):
        assert export.attribute(value) == expected

    def test_the_groups_of_a_file(self, h5saver, qapp):
        path = write_experiment(h5saver)

        with h5py.File(path, 'r') as file:
            units = export.read_experiment(file)

            assert sorted(units) == [1, 3]
            assert sorted(units[1].series) == ['ROIs', 'Spectrum', 'Temperature']
            assert sorted(units[3].series) == ['Image', 'ROIs', 'Temperature']
            assert units[1].kind == 'spectrometer' and units[3].kind == 'camera'
            assert units[1].attr('spectrometer') == 'Spectro1' and units[3].attr('camera') == 'Camera1'
            assert units[1].temperature_units == '°C'
            assert len(units[1].series['Spectrum']) == 6 and len(units[3].series['Image']) == 5

    def test_a_file_without_an_experiment(self, tmp_path):
        path = tmp_path / 'other.h5'
        with h5py.File(path, 'w') as file:
            file.create_group('RawData')

        assert export.main([str(path)]) == 1

    def test_a_file_which_is_not_h5(self, tmp_path):
        path = tmp_path / 'text.h5'
        path.write_text('not an h5 file')

        assert export.main([str(path)]) == 1

    def test_an_experiment_which_is_not_there(self, h5saver, qapp, capsys):
        path = write_experiment(h5saver)

        assert export.main([str(path), '--experiment', '3', '--list']) == 1
        assert 'no experiment 3' in capsys.readouterr().err

    def test_list(self, h5saver, qapp, capsys, tmp_path):
        path = write_experiment(h5saver)

        assert export.main([str(path), '--list', '-o', str(tmp_path / 'out')]) == 0

        text = capsys.readouterr().out
        assert 'unit 1: spectrometer 1 Spectro1 with Thermo1' in text and 'unit 3: camera Camera1' in text
        assert not (tmp_path / 'out').exists() or not list((tmp_path / 'out').iterdir())  # (nothing is written)


class TestSpectrometerCsv:
    def test_time_temperature_rois_and_full_spectrum(self, h5saver, qapp, tmp_path):
        path = write_experiment(h5saver)

        out = run_export(path, tmp_path)

        header, rows = read_csv(out / f'{path.stem}_spectrometer1.csv')
        assert header == ['time (s)', 'temperature (°C)', 'A (500-510 nm)', 'B (600-610 nm)', 'full spectrum']
        assert len(rows) == 6
        assert [row[0] for row in rows] == pytest.approx([0., 1., 2., 3., 4., 5.])  # (from the start of the experiment)
        assert [row[1] for row in rows] == pytest.approx([20., 21., 22., 23., 24., 25.])
        assert [row[2] for row in rows] == pytest.approx([0., 100., 200., 300., 400., 500.])
        assert [row[3] for row in rows] == pytest.approx([1., 101., 201., 301., 401., 501.])

    def test_the_full_spectrum_is_one_column_of_intensities(self, h5saver, qapp, tmp_path):
        path = write_experiment(h5saver)

        out = run_export(path, tmp_path)

        _, rows = read_csv(out / f'{path.stem}_spectrometer1.csv')
        spectrum = np.array(rows[3][-1].split(), dtype=float)
        assert spectrum.size == 1001
        assert spectrum[0] == pytest.approx(3.) and spectrum[-1] == pytest.approx(503.)  # (x - 400 + index)

    def test_the_wavelengths_are_in_a_file_of_their_own(self, h5saver, qapp, tmp_path):
        path = write_experiment(h5saver)

        out = run_export(path, tmp_path)

        header, rows = read_csv(out / f'{path.stem}_spectrometer1_wavelengths.csv')
        assert header == ['pixel', 'wavelength (nm)'] and len(rows) == 1001
        assert rows[0][1] == pytest.approx(400.) and rows[-1][1] == pytest.approx(900.)

    def test_one_column_per_wavelength(self, h5saver, qapp, tmp_path):
        path = write_experiment(h5saver)

        out = run_export(path, tmp_path, '--spectrum', 'columns')

        header, rows = read_csv(out / f'{path.stem}_spectrometer1.csv')
        assert header[:4] == ['time (s)', 'temperature (°C)', 'A (500-510 nm)', 'B (600-610 nm)']
        assert header[4] == '400.000 nm' and header[-1] == '900.000 nm' and len(header) == 4 + 1001
        assert rows[2][4] == pytest.approx(2.) and rows[2][-1] == pytest.approx(502.)
        assert not (out / f'{path.stem}_spectrometer1_wavelengths.csv').exists()

    def test_no_full_spectrum(self, h5saver, qapp, tmp_path):
        path = write_experiment(h5saver)

        out = run_export(path, tmp_path, '--spectrum', 'none')

        header, _ = read_csv(out / f'{path.stem}_spectrometer1.csv')
        assert header == ['time (s)', 'temperature (°C)', 'A (500-510 nm)', 'B (600-610 nm)']

    def test_a_spectrum_which_was_not_recorded_is_left_out(self, h5saver, qapp, tmp_path):
        path = write_experiment(h5saver, spectrum=False, camera=False)

        out = run_export(path, tmp_path)

        header, _ = read_csv(out / f'{path.stem}_spectrometer1.csv')
        assert header[-1] == 'B (600-610 nm)'
        assert not list(out.glob('*wavelengths*'))

    def test_short_names_absolute_time_and_the_ramp(self, h5saver, qapp, tmp_path):
        path = write_experiment(h5saver, camera=False)

        out = run_export(path, tmp_path, '--short-names', '--absolute-time', '--program', '--spectrum', 'none')

        header, rows = read_csv(out / f'{path.stem}_spectrometer1.csv')
        assert header[:4] == ['time (epoch s)', 'temperature (°C)', 'A', 'B']
        assert rows[0][0] == pytest.approx(1000.) and rows[5][0] == pytest.approx(1005.)

    def test_the_program_columns(self, h5saver, qapp, tmp_path):
        unit = make_unit(1, rois=[Roi('A', 500., 510.)], run_ramp=True)
        experiment = Experiment(h5saver, [unit])
        for index in range(3):
            experiment.write(unit, index)
        experiment.finish()

        out = run_export(experiment.path, tmp_path, '--program', '--spectrum', 'none')

        header, rows = read_csv(out / f'{experiment.path.stem}_spectrometer1.csv')
        assert header[-3:] == ['set-point', 'cycle', 'phase code']
        assert [row[-3] for row in rows] == pytest.approx([21., 22., 23.])

    def test_a_spectrometer_without_thermostat_has_no_temperature_column(self, h5saver, qapp, tmp_path):
        unit = make_unit(1, rois=[Roi('A', 500., 510.)], run_ramp=False, thermostat=False)
        experiment = Experiment(h5saver, [unit])
        for index in range(3):
            experiment.write(unit, index, phase=None)
        experiment.finish()

        out = run_export(experiment.path, tmp_path, '--spectrum', 'none')

        header, _ = read_csv(out / f'{experiment.path.stem}_spectrometer1.csv')
        assert header == ['time (s)', 'A (500-510 nm)']

    def test_two_spectrometers_two_files(self, h5saver, qapp, tmp_path):
        units = [make_unit(1, rois=[Roi('A', 500., 510.)]), make_unit(2, rois=[Roi('B', 600., 610.)])]
        experiment = Experiment(h5saver, units)
        for index in range(3):
            for unit in units:
                experiment.write(unit, index)
        experiment.finish()

        out = run_export(experiment.path, tmp_path, '--spectrum', 'none')

        assert read_csv(out / f'{experiment.path.stem}_spectrometer1.csv')[0][2] == 'A (500-510 nm)'
        assert read_csv(out / f'{experiment.path.stem}_spectrometer2.csv')[0][2] == 'B (600-610 nm)'

    def test_the_csv_opens_in_excel_with_the_degree_sign(self, h5saver, qapp, tmp_path):
        path = write_experiment(h5saver, camera=False)

        out = run_export(path, tmp_path)

        assert (out / f'{path.stem}_spectrometer1.csv').read_bytes().startswith(b'\xef\xbb\xbf')

    def test_another_delimiter(self, h5saver, qapp, tmp_path):
        path = write_experiment(h5saver, camera=False)

        out = run_export(path, tmp_path, '--delimiter', ';', '--spectrum', 'none')

        first = (out / f'{path.stem}_spectrometer1.csv').read_text(encoding='utf-8-sig').splitlines()[0]
        assert first == 'time (s);temperature (°C);A (500-510 nm);B (600-610 nm)'

    def test_the_files_go_next_to_the_h5_file_by_default(self, h5saver, qapp):
        path = write_experiment(h5saver, camera=False)

        assert export.main([str(path), '--spectrum', 'none']) == 0

        assert (path.parent / f'{path.stem}_spectrometer1.csv').exists()


class TestCamera:
    @pytest.fixture(autouse=True)
    def tifffile(self):
        return pytest.importorskip('tifffile')

    def test_the_images_are_stitched_in_a_tif(self, h5saver, qapp, tmp_path, tifffile):
        path = write_experiment(h5saver, spectrometer=False)

        out = run_export(path, tmp_path)

        with tifffile.TiffFile(out / f'{path.stem}_camera.tif') as tif:
            assert tif.is_imagej and tif.series[0].axes == 'TYX'  # (one channel: ImageJ has no channel axis then)
            images = tif.asarray()
            metadata = tif.imagej_metadata
        assert images.shape == (5, HEIGHT, WIDTH) and images.dtype == np.uint16
        rows, columns = np.mgrid[0:HEIGHT, 0:WIDTH]
        for index in range(5):
            assert np.array_equal(images[index], 10 * rows + columns + index)
        assert metadata['frames'] == 5 and metadata['finterval'] == pytest.approx(1.0)
        assert metadata['Labels'][0].startswith('t=0.000 s, T=30.000')
        assert 'Camera1' in metadata['Info']

    def test_the_colors_are_the_channels(self, h5saver, qapp, tmp_path, tifffile):
        path = write_experiment(h5saver, spectrometer=False, channels=3)

        out = run_export(path, tmp_path)

        with tifffile.TiffFile(out / f'{path.stem}_camera.tif') as tif:
            assert tif.series[0].axes == 'TCYX' and tif.series[0].shape == (5, 3, HEIGHT, WIDTH)
            images = tif.asarray()
            metadata = tif.imagej_metadata
        rows, columns = np.mgrid[0:HEIGHT, 0:WIDTH]
        for index, channel in ((0, 0), (2, 1), (4, 2)):
            assert np.array_equal(images[index, channel], 1000 * channel + 10 * rows + columns + index)
        assert metadata['channels'] == 3 and metadata['mode'] == 'composite'
        assert metadata['Labels'][:3] == ['t=0.000 s, T=30.000, R', 't=0.000 s, T=30.000, G', 't=0.000 s, T=30.000, B']

    @pytest.mark.parametrize('stored, written', [
        (np.uint8, np.uint8), (np.uint16, np.uint16), (np.float32, np.float32), (np.float64, np.float32),
        (np.int32, np.uint16), (np.int16, np.uint16)])
    def test_the_pixels_are_stored_in_a_type_imagej_reads(self, h5saver, qapp, tmp_path, tifffile, stored, written):
        path = write_experiment(h5saver, spectrometer=False, dtype=stored)

        out = run_export(path, tmp_path)

        with tifffile.TiffFile(out / f'{path.stem}_camera.tif') as tif:
            assert tif.asarray().dtype == written

    def test_values_too_big_for_16_bits_become_floats(self, tmp_path, tifffile):
        class FakeImages:
            def __init__(self, array):
                self.array = array
                self.dtype = array.dtype

            def __len__(self):
                return len(self.array)

            def __getitem__(self, key):
                return self.array[key]

        assert export.imagej_dtype([FakeImages(np.array([[1, 70000]], dtype=np.int32))]) == np.float32
        assert export.imagej_dtype([FakeImages(np.array([[-5, 7]], dtype=np.int32))]) == np.float32
        assert export.imagej_dtype([FakeImages(np.array([[5, 7]], dtype=np.int64))]) == np.uint16

    def test_the_csv_of_the_camera(self, h5saver, qapp, tmp_path):
        path = write_experiment(h5saver)

        out = run_export(path, tmp_path)

        header, rows = read_csv(out / f'{path.stem}_camera.csv')
        assert header == ['time (s)', 'temperature (°C)', 'spectrometer 1 A (500-510 nm)',
                          'spectrometer 1 B (600-610 nm)', 'camera Spot (x 1-4, y 1-3 pixel)']
        assert len(rows) == 5  # (one row per image)
        # the images are 0.4 s after the readouts of the spectrometer: time from the start of the experiment
        assert [row[0] for row in rows] == pytest.approx([0.4, 1.4, 2.4, 3.4, 4.4])
        assert [row[1] for row in rows] == pytest.approx([30., 31., 32., 33., 34.])  # (the temperature of the camera)
        # the closest readout of the spectrometer: 0.4 s before (the image at 0.4 is closest to the readout at 0)
        assert [row[2] for row in rows] == pytest.approx([0., 100., 200., 300., 400.])
        assert [row[3] for row in rows] == pytest.approx([1., 101., 201., 301., 401.])
        # the mean of the pixels of the ROI on the image (columns 1 to 3, rows 1 and 2)
        assert [row[4] for row in rows] == pytest.approx([np.mean([10 * y + x + index for y in (1, 2) for x in (1, 2, 3)])
                                                          for index in range(5)])

    def test_the_closest_readout_or_an_interpolation(self, h5saver, qapp, tmp_path):
        path = write_experiment(h5saver, camera_times=(1000.6, 1.0, 5))  # (the images between the readouts)

        nearest_out = run_export(path, tmp_path)
        _, nearest_rows = read_csv(nearest_out / f'{path.stem}_camera.csv')
        interpolated_out = tmp_path / 'out_interpolated'
        assert export.main([str(path), '-o', str(interpolated_out), '--interpolate']) == 0
        _, interpolated_rows = read_csv(interpolated_out / f'{path.stem}_camera.csv')

        assert [row[2] for row in nearest_rows] == pytest.approx([100., 200., 300., 400., 500.])  # (0.4 s after)
        assert [row[2] for row in interpolated_rows] == pytest.approx([60., 160., 260., 360., 460.])

    def test_the_temperature_of_the_spectrometer_when_the_camera_has_none(self, h5saver, qapp, tmp_path):
        path = write_experiment(h5saver, camera_thermostat=False)

        out = run_export(path, tmp_path)

        header, rows = read_csv(out / f'{path.stem}_camera.csv')
        assert header[1] == 'temperature (°C)'
        assert [row[1] for row in rows] == pytest.approx([20., 21., 22., 23., 24.])  # (of the spectrometer, at 0.4 s)

    def test_a_camera_alone(self, h5saver, qapp, tmp_path):
        path = write_experiment(h5saver, spectrometer=False, camera_thermostat=False)

        out = run_export(path, tmp_path)

        header, rows = read_csv(out / f'{path.stem}_camera.csv')
        assert header == ['time (s)', 'camera Spot (x 1-4, y 1-3 pixel)'] and len(rows) == 5

    def test_the_images_which_were_not_recorded_make_a_csv_and_no_tif(self, h5saver, qapp, tmp_path, capsys):
        path = write_experiment(h5saver, image=False)

        out = run_export(path, tmp_path)

        assert (out / f'{path.stem}_camera.csv').exists() and not (out / f'{path.stem}_camera.tif').exists()
        assert 'were not recorded' in capsys.readouterr().err

    def test_no_tif_on_request(self, h5saver, qapp, tmp_path):
        path = write_experiment(h5saver)

        out = run_export(path, tmp_path, '--no-tif')

        assert (out / f'{path.stem}_camera.csv').exists() and not (out / f'{path.stem}_camera.tif').exists()

    def test_a_file_which_is_still_being_written_is_read_up_to_its_last_image(self, h5saver, qapp, tmp_path, tifffile):
        """The time axis of a group may be a row ahead of its data: the shortest wins"""
        path = write_experiment(h5saver, spectrometer=False)
        with h5py.File(path, 'r+') as file:
            group = next(g for g in file['RawData/Datalogger000'].values() if 'Data2D' in g)
            data = group['Data2D/CH00/EnlData00']
            data.resize(4, axis=0)

        out = run_export(path, tmp_path)

        with tifffile.TiffFile(out / f'{path.stem}_camera.tif') as tif:
            assert tif.asarray().shape[0] == 4
        assert len(read_csv(out / f'{path.stem}_camera.csv')[1]) == 4

    def test_the_names_of_the_new_files_are_those_of_the_h5_file(self, h5saver, qapp, tmp_path):
        path = write_experiment(h5saver)

        out = run_export(path, tmp_path)

        assert sorted(file.name for file in out.iterdir()) == sorted(
            f'{path.stem}{suffix}' for suffix in ('_spectrometer1.csv', '_spectrometer1_wavelengths.csv',
                                                  '_camera.csv', '_camera.tif'))

    def test_without_tifffile_the_message_says_what_to_do(self, h5saver, qapp, tmp_path, monkeypatch):
        path = write_experiment(h5saver, spectrometer=False)
        monkeypatch.setitem(sys.modules, 'tifffile', None)

        with pytest.raises(SystemExit, match='pip install tifffile'):
            export.main([str(path), '-o', str(tmp_path / 'out')])


class TestNearest:
    def test_the_closest_sample(self):
        time, values = np.array([0., 1., 2., 3.]), np.array([10., 20., 30., 40.])

        assert export.nearest(time, values, np.array([-5., 0.4, 0.6, 2.4, 9.])).tolist() == [10., 10., 20., 30., 40.]

    def test_interpolated(self):
        time, values = np.array([0., 1., 2.]), np.array([0., 10., 30.])

        assert export.nearest(time, values, np.array([0.5, 1.5]), interpolate=True).tolist() == [5., 20.]

    def test_one_sample(self):
        assert export.nearest(np.array([5.]), np.array([7.]), np.array([0., 9.])).tolist() == [7., 7.]

    def test_no_sample(self):
        assert np.isnan(export.nearest(np.zeros(0), np.zeros(0), np.array([0., 1.]))).all()

    @pytest.mark.parametrize('label, expected', [('ROI 1 (454.2-464.6 nm)', 'ROI 1'), ('ROI 1', 'ROI 1'),
                                                 ('Spot (x 1-4, y 1-3 pixel)', 'Spot')])
    def test_short_names(self, label, expected):
        assert export.short(label) == expected
