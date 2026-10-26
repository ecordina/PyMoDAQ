"""tools/thermospectro_export.py with the files of a long log: the whole spectrum (image) is saved only every so often, a
temperature is held (phase hold)"""
import math

import numpy as np
import pytest

from pymodaq_data.h5modules.data_saving import DataBundle

from pymodaq_plugins_thermospectro.ramp import PHASE_CODES, Phase
from pymodaq_plugins_thermospectro.rois import Roi
from pymodaq_plugins_thermospectro.saving import build_dtes
from pymodaq_plugins_thermospectro.unit_settings import CONTROL_HOLD

from test_export import camera_measurement, export, make_camera_unit, read_csv, run_export
from test_saving import Experiment, h5saver, make_measurement, make_unit  # noqa: F401  (h5saver is a fixture)

pytestmark = pytest.mark.filterwarnings('ignore')


def write_log(h5saver, n=9, spectrum_every=3, image_every=4, channels=1, hold=True):
    """A spectrometer and a camera read out 9 times: the whole spectrum is saved at the readouts 0, 3, 6, the image at
    the readouts 0 and 4 and 8 (the ROIs and the temperatures at each one)"""
    spectrometer = make_unit(1, rois=[Roi('A', 500., 510.)], run_ramp=hold)  # (a held controller is driven by it)
    camera = make_camera_unit()
    if hold:
        for unit in (spectrometer, camera):
            unit.settings.param['ramp', 'control'] = CONTROL_HOLD
    experiment = Experiment(h5saver, [spectrometer, camera])
    recordings = {r.number: r for r in experiment.recordings}
    for index in range(n):
        measurements = [make_measurement(spectrometer, index, t0=1000., phase=Phase.HOLD if hold else None),
                        camera_measurement(camera, index, 1000.4, channels)]
        measurements[0].full_due = index % spectrum_every == 0
        measurements[1].full_due = index % image_every == 0
        for unit, measurement in zip((spectrometer, camera), measurements):
            for dte in build_dtes(recordings[unit.number], measurement):
                experiment.saver.add_data_bundle(DataBundle(dte=dte))
    experiment.finish()
    return experiment.path


class TestSpectrumNowAndThen:
    def test_one_row_per_readout_for_the_values(self, h5saver, qapp, tmp_path):
        path = write_log(h5saver)

        out = run_export(path, tmp_path)

        header, rows = read_csv(out / f'{path.stem}_spectrometer1.csv')
        assert header == ['time (s)', 'temperature (°C)', 'A mean (500-510 nm) [counts]']
        assert len(rows) == 9
        assert [row[2] for row in rows] == pytest.approx([100. * i for i in range(9)])  # (the ROIs: every readout)

    def test_one_row_per_spectrum_which_was_saved(self, h5saver, qapp, tmp_path):
        path = write_log(h5saver)

        out = run_export(path, tmp_path)

        header, rows = read_csv(out / f'{path.stem}_spectrometer1_spectra.csv')
        assert len(rows) == 3 and header[:3] == ['time (s)', 'temperature (°C)', '400.000 nm']  # (the readouts 0, 3, 6)
        assert [row[0] for row in rows] == pytest.approx([0., 3., 6.])
        assert [row[1] for row in rows] == pytest.approx([20., 23., 26.])  # (the temperature of those readouts)
        assert [row[2] for row in rows] == pytest.approx([0., 3., 6.])  # (the first pixel: x - 400 + readout)
        assert rows[2][-1] == pytest.approx(506.)

    def test_a_spectrum_at_each_readout_gives_a_row_for_each(self, h5saver, qapp, tmp_path):
        path = write_log(h5saver, spectrum_every=1)

        out = run_export(path, tmp_path)

        assert len(read_csv(out / f'{path.stem}_spectrometer1_spectra.csv')[1]) == 9

    def test_the_held_temperature_is_a_column_of_the_program(self, h5saver, qapp, tmp_path):
        path = write_log(h5saver)

        out = run_export(path, tmp_path, '--program', '--no-spectra')

        header, rows = read_csv(out / f'{path.stem}_spectrometer1.csv')
        assert header[-3:] == ['set-point', 'cycle', 'phase code']
        assert {row[-1] for row in rows} == {float(PHASE_CODES[Phase.HOLD])}


class TestImagesNowAndThen:
    @pytest.fixture(autouse=True)
    def tifffile(self):
        return pytest.importorskip('tifffile')

    def test_the_tif_has_the_images_which_were_saved(self, h5saver, qapp, tmp_path, tifffile):
        path = write_log(h5saver)

        out = run_export(path, tmp_path)

        with tifffile.TiffFile(out / f'{path.stem}_camera.tif') as tif:
            images = tif.asarray()
        assert images.shape[0] == 3  # (the readouts 0, 4 and 8)
        rows, columns = np.mgrid[0:6, 0:8]
        for page, readout in enumerate((0, 4, 8)):
            assert np.array_equal(images[page], 10 * rows + columns + readout)

    def test_the_csv_of_the_camera_has_a_row_per_readout_and_says_where_the_images_are(self, h5saver, qapp, tmp_path):
        path = write_log(h5saver)

        out = run_export(path, tmp_path)

        header, rows = read_csv(out / f'{path.stem}_camera.csv')
        assert header[0] == 'time (s)' and header[-1] == 'tif frame' and len(rows) == 9
        assert [row[1] for row in rows] == pytest.approx([30. + i for i in range(9)])  # (every readout)
        assert [row[-1] for row in rows] == [1., '', '', '', 2., '', '', '', 3.]  # (the frame in ImageJ, from 1)
        assert 'tif file' not in header  # (one tif holds them all)
        assert not (out / f'{path.stem}_camera_log.csv').exists()  # (the readouts are the rows of the csv)

    def test_with_an_image_at_each_readout_every_row_has_its_frame(self, h5saver, qapp, tmp_path):
        path = write_log(h5saver, image_every=1)

        out = run_export(path, tmp_path)

        assert [row[-1] for row in read_csv(out / f'{path.stem}_camera.csv')[1]] == [float(i) for i in range(1, 10)]

    def test_the_frames_of_the_csv_are_the_pages_of_the_tif(self, h5saver, qapp, tmp_path, tifffile):
        path = write_log(h5saver)

        out = run_export(path, tmp_path)

        with tifffile.TiffFile(out / f'{path.stem}_camera.tif') as tif:
            labels = tif.imagej_metadata['Labels']
        _, rows = read_csv(out / f'{path.stem}_camera.csv')
        assert len(labels) == len([row for row in rows if row[-1] != '']) == 3


class TestTheTifOfAnImageJLimit:
    """ImageJ does not read BigTIFF: a long series of images is split in several classic tif files"""

    @pytest.fixture(autouse=True)
    def tifffile(self, monkeypatch):
        tifffile = pytest.importorskip('tifffile')
        monkeypatch.setattr(export, 'TIF_PART_BYTES', 2 * 6 * 8 * 2 + 5)  # (two images of 6 x 8 pixels of 16 bits)
        return tifffile

    def test_the_images_are_in_several_files(self, h5saver, qapp, tmp_path, tifffile):
        path = write_log(h5saver, image_every=1)

        out = run_export(path, tmp_path)

        names = sorted(file.name for file in out.glob('*_camera_part*.tif'))
        assert names == [f'{path.stem}_camera_part0{n}.tif' for n in range(1, 6)] and not (out / f'{path.stem}_camera.tif').exists()
        frames = []
        for name in names:
            with tifffile.TiffFile(out / name) as tif:
                assert not tif.is_bigtiff
                frames.append(tif.asarray().reshape(-1, 6, 8))
        assert [len(part) for part in frames] == [2, 2, 2, 2, 1]
        rows, columns = np.mgrid[0:6, 0:8]
        for index, image in enumerate(np.concatenate(frames)):
            assert np.array_equal(image, 10 * rows + columns + index)

    def test_the_csv_files_say_which_file_and_frame(self, h5saver, qapp, tmp_path):
        path = write_log(h5saver, image_every=1)

        out = run_export(path, tmp_path)

        header, rows = read_csv(out / f'{path.stem}_camera.csv')
        assert header[-2:] == ['tif file', 'tif frame'] and len(rows) == 9
        assert [(row[-2], row[-1]) for row in rows[:5]] == [
            (f'{path.stem}_camera_part01.tif', 1.), (f'{path.stem}_camera_part01.tif', 2.),
            (f'{path.stem}_camera_part02.tif', 1.), (f'{path.stem}_camera_part02.tif', 2.),
            (f'{path.stem}_camera_part03.tif', 1.)]

    def test_the_readouts_without_an_image_have_no_file_and_no_frame(self, h5saver, qapp, tmp_path):
        path = write_log(h5saver, image_every=4)  # (3 images: two files)

        out = run_export(path, tmp_path)

        header, rows = read_csv(out / f'{path.stem}_camera.csv')
        assert header[-2:] == ['tif file', 'tif frame'] and len(rows) == 9
        assert [row[-2:] for row in rows] == [
            [f'{path.stem}_camera_part01.tif', 1.], ['', ''], ['', ''], ['', ''],
            [f'{path.stem}_camera_part01.tif', 2.], ['', ''], ['', ''], ['', ''],
            [f'{path.stem}_camera_part02.tif', 1.]]

    def test_a_small_series_is_one_file(self, h5saver, qapp, tmp_path, monkeypatch):
        monkeypatch.setattr(export, 'TIF_PART_BYTES', 10 ** 9)
        path = write_log(h5saver, image_every=1)

        out = run_export(path, tmp_path)

        assert (out / f'{path.stem}_camera.tif').exists() and not list(out.glob('*_part*'))
        header, _ = read_csv(out / f'{path.stem}_camera.csv')
        assert 'tif file' not in header


class TestLongLogs:
    def test_a_big_epoch_time_keeps_its_fractions_of_a_second(self):
        options = type('Options', (), {'absolute_time': True})()

        name, values = export.time_column(np.array([1_760_000_000.25, 1_760_000_001.5]), 0., options)

        assert name == 'time (epoch s)' and values == ['1760000000.250000', '1760000001.500000']
        options.absolute_time = False
        assert export.time_column(np.array([1_760_000_000.25, 1_760_000_001.5]), 1_760_000_000., options)[1] == [
            '0.250000', '1.500000']

    def test_the_time_of_two_days_is_exact_to_the_microsecond(self):
        options = type('Options', (), {'absolute_time': False})()

        assert export.time_column(np.array([172800.123456]), 0., options)[1] == ['172800.123456']

    def test_a_value_far_from_any_sample_is_left_empty(self):
        time, values = np.arange(10.), np.arange(10.) * 10
        time = np.concatenate([time, time + 1000.])  # (nothing was measured between 10 and 1000)
        values = np.concatenate([values, values])

        got = export.nearest(time, values, np.array([2., 500., 1002.]), max_gap=export.MAX_GAP)

        assert got[0] == 20. and np.isnan(got[1]) and got[2] == 20.
        interpolated = export.nearest(time, values, np.array([2.5, 500.]), interpolate=True, max_gap=export.MAX_GAP)
        assert interpolated[0] == 25. and np.isnan(interpolated[1])

    def test_without_a_limit_the_closest_sample_is_always_taken(self):
        assert export.nearest(np.array([0., 1.]), np.array([5., 6.]), np.array([500.])).tolist() == [6.]

    def test_a_cell_with_no_value_is_empty(self):
        assert [export.cell(value) for value in (1.5, math.nan, math.inf, 'text', '')] == ['1.5', '', '', 'text', '']

    def test_the_first_group_of_the_readouts_gives_the_rows(self):
        def series(kind, n, step):
            return export.Series(kind, f'Unit1_{kind}', 1000. + step * np.arange(n), [np.zeros(n)], ['x'])

        unit = export.UnitData(1, {'Spectrum': series('Spectrum', 3, 60.), 'Temperature': series('Temperature', 180, 1.)})
        assert len(export.master_series(unit)) == 180  # (the temperature of every readout, not the sparse spectra)
        unit.series['ROIs'] = series('ROIs', 180, 1.)
        assert export.master_series(unit).kind == 'ROIs'

    @pytest.mark.parametrize('label, expected', [('Peak (main) (450-460 nm)', 'Peak (main)'), ('A (1)', 'A'),
                                                 ('(1-2 nm)', ''), ('no parentheses', 'no parentheses')])
    def test_only_the_last_group_in_parentheses_is_cut(self, label, expected):
        assert export.short(label) == expected

    def test_a_unit_without_thermostat_is_described_so(self):
        unit = export.UnitData(1, {'ROIs': export.Series('ROIs', 'Unit1_ROIs', np.zeros(2), [np.zeros(2)], ['x'],
                                                           None, {'thermostat': '', 'spectrometer': 'Spectro1'})})

        assert 'with no thermostat' in export.describe({1: unit})

    def test_the_range_of_big_images_is_found_a_few_at_a_time(self):
        class Images:
            def __init__(self, array):
                self.array, self.dtype, self.asked = array, array.dtype, []

            def __len__(self):
                return len(self.array)

            def __getitem__(self, key):
                if isinstance(key, slice):
                    self.asked.append(key.stop - key.start)
                return self.array[key]

        images = Images(np.arange(100 * 6 * 8, dtype=np.int32).reshape(100, 6, 8) % 1000)

        assert export.min_max(images, block_bytes=6 * 8 * 4 * 10) == (0., 999.)
        assert max(images.asked) == 10 and len(images.asked) == 10  # (never all of it at once)
        assert export.imagej_dtype([images]) == np.uint16

    def test_the_spectra_are_read_a_block_of_rows_at_a_time(self, h5saver, qapp, tmp_path, monkeypatch):
        path = write_log(h5saver, spectrum_every=1)
        whole = run_export(path, tmp_path / 'whole')

        monkeypatch.setattr(export.SpectraRows, 'BLOCK', 2)
        sizes = []
        blocks_of = export.SpectraRows.blocks

        def blocks(self):
            for start, block in blocks_of(self):
                sizes.append(len(block))
                yield start, block

        monkeypatch.setattr(export.SpectraRows, 'blocks', blocks)
        by_blocks = run_export(path, tmp_path / 'blocks')

        assert (whole / f'{path.stem}_spectrometer1_spectra.csv').read_bytes() == (
            by_blocks / f'{path.stem}_spectrometer1_spectra.csv').read_bytes()
        assert sizes == [2, 2, 2, 2, 1] and len(read_csv(by_blocks / f'{path.stem}_spectrometer1_spectra.csv')[1]) == 9
