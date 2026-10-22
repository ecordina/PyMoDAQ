"""tools/thermospectro_export.py with what the user wrote: the observations (csv) and the protocol (text)"""
import datetime

import h5py
import numpy as np
import pytest

from pymodaq_data import DataToExport
from pymodaq_data.h5modules.data_saving import DataBundle

from pymodaq_plugins_thermospectro.rois import Roi
from pymodaq_plugins_thermospectro.saving import NoteBundle, build_dtes

from test_export import export, read_csv, run_export
from test_saving import Experiment, h5saver, make_measurement, make_unit  # noqa: F401  (h5saver is a fixture)

pytestmark = pytest.mark.filterwarnings('ignore')

METADATA = {'experiment_title': 'Ramp 3', 'sample': 'Pomme 12', 'operator': 'Émile', 'application': 'thermo_spectro',
            'protocol': '1. Heat to 80 °C at 2 °C/min\n2. Hold 10 min'}


def write_file(h5saver, tmp_path, notes=(), metadata=None, readouts=6):
    """A file of a spectrometer read out `readouts` times from the epoch second 1000, with these observations (epoch,
    text) and what was said about the experiment"""
    unit = make_unit(1, rois=[Roi('A', 500., 510.)], run_ramp=False)
    experiment = Experiment(h5saver, [unit], path=tmp_path / 'Sample12.h5', metadata=metadata)
    recording = experiment.recordings[0]
    for index in range(readouts):
        for dte in build_dtes(recording, make_measurement(unit, index, t0=1000., phase=None)):
            experiment.saver.add_data_bundle(DataBundle(dte=dte))
    for epoch, text in notes:
        experiment.saver.add_data_bundle(NoteBundle(dte=DataToExport('Note'), epoch=epoch, text=text))
    experiment.finish()
    return experiment.path


def clock(epoch):
    return datetime.datetime.fromtimestamp(epoch).strftime('%Y-%m-%d %H:%M:%S')


def read_text(path):
    return path.read_text(encoding='utf-8-sig')


class TestTheObservations:
    NOTES = [(1002.5, 'Door opened'), (1004., 'The sample turned yellow')]

    def test_one_row_each_with_the_time_of_the_experiment_and_the_clock(self, h5saver, qapp, tmp_path):
        path = write_file(h5saver, tmp_path, self.NOTES)

        out = run_export(path, tmp_path)

        header, rows = read_csv(out / 'Sample12_notes.csv')
        assert header == ['time (s)', 'clock time', 'observation']
        assert rows == [[2.5, clock(1002.5), 'Door opened'], [4.0, clock(1004.), 'The sample turned yellow']]

    def test_the_time_is_the_one_of_the_other_files(self, h5saver, qapp, tmp_path):
        path = write_file(h5saver, tmp_path, self.NOTES)

        out = run_export(path, tmp_path)

        _, data = read_csv(out / 'Sample12_spectrometer1.csv')
        _, notes = read_csv(out / 'Sample12_notes.csv')
        assert data[0][0] == 0. and notes[0][0] == pytest.approx(1002.5 - 1000.)  # (the zero is the first measurement)

    def test_in_epoch_seconds_if_asked(self, h5saver, qapp, tmp_path):
        path = write_file(h5saver, tmp_path, self.NOTES)

        out = run_export(path, tmp_path, '--absolute-time')

        header, rows = read_csv(out / 'Sample12_notes.csv')
        assert header[0] == 'time (epoch s)' and [row[0] for row in rows] == pytest.approx([1002.5, 1004.])

    def test_commas_quotes_lines_and_any_language_are_kept(self, h5saver, qapp, tmp_path):
        texts = ['yellow, then "brown"', 'two\nlines', 'Échantillon n°3 → brun 😀 (湿度 40 %)', ';semicolon;']
        path = write_file(h5saver, tmp_path, [(1001. + index, text) for index, text in enumerate(texts)])

        out = run_export(path, tmp_path)

        assert [row[2] for row in read_csv(out / 'Sample12_notes.csv')[1]] == texts

    def test_the_delimiter_is_the_one_of_the_other_files(self, h5saver, qapp, tmp_path):
        path = write_file(h5saver, tmp_path, self.NOTES)

        out = run_export(path, tmp_path, '--delimiter', ';')

        assert read_text(out / 'Sample12_notes.csv').splitlines()[0] == 'time (s);clock time;observation'

    def test_no_observation_no_file(self, h5saver, qapp, tmp_path):
        path = write_file(h5saver, tmp_path)

        out = run_export(path, tmp_path)

        assert not (out / 'Sample12_notes.csv').exists()

    def test_the_list_tells_how_many_without_writing_anything(self, h5saver, qapp, tmp_path, capsys):
        path = write_file(h5saver, tmp_path, self.NOTES, METADATA)

        assert export.main([str(path), '--list', '-o', str(tmp_path / 'out')]) == 0

        told = capsys.readouterr().out
        assert '2 observations' in told and 'experiment Ramp 3, sample Pomme 12, operator Émile' in told
        assert 'a protocol of 43 characters' in told
        assert not (tmp_path / 'out').exists() or not list((tmp_path / 'out').iterdir())

    def test_one_observation_is_not_plural(self, h5saver, qapp, tmp_path, capsys):
        path = write_file(h5saver, tmp_path, self.NOTES[:1])

        export.main([str(path), '--list'])

        assert '1 observation\n' in capsys.readouterr().out

    def test_the_observations_of_a_file_being_written_are_read(self, h5saver, qapp, tmp_path):
        """(the arrays are enlarged one after the other: a row may be half written)"""
        path = tmp_path / 'half.h5'
        with h5py.File(path, 'w') as file:
            group = file.create_group('RawData/Datalogger000/Notes')
            group.create_dataset('time', data=[1., 2., 3.])
            group.create_dataset('text', data=np.array([b'one', b'two'], dtype='S10'))

        with h5py.File(path, 'r') as file:
            times, texts = export.read_notes(file['RawData/Datalogger000'])

        assert times.tolist() == [1., 2.] and texts == ['one', 'two']


class TestTheProtocol:
    def test_it_is_a_text_with_what_was_said(self, h5saver, qapp, tmp_path):
        path = write_file(h5saver, tmp_path, metadata=METADATA)

        out = run_export(path, tmp_path)

        lines = read_text(out / 'Sample12_protocol.txt').splitlines()
        assert lines[:4] == ['Experiment: Ramp 3', 'Sample: Pomme 12', 'Operator: Émile', 'Application: thermo_spectro']
        assert lines[4].startswith('Started: ') and lines[5] == 'File: Sample12.h5'
        assert lines[6:] == ['', 'Protocol', '--------', '1. Heat to 80 °C at 2 °C/min', '2. Hold 10 min']

    def test_it_says_when_the_file_was_made(self, h5saver, qapp, tmp_path):
        before = datetime.datetime.now().replace(microsecond=0)
        path = write_file(h5saver, tmp_path, metadata=METADATA)

        out = run_export(path, tmp_path)

        started = [line for line in read_text(out / 'Sample12_protocol.txt').splitlines() if line.startswith('Started')][0]
        assert before <= datetime.datetime.strptime(started, 'Started: %Y-%m-%d %H:%M:%S') <= datetime.datetime.now()

    def test_what_was_not_said_is_not_listed(self, h5saver, qapp, tmp_path):
        path = write_file(h5saver, tmp_path, metadata={'experiment_title': '', 'sample': 'Pomme 12', 'operator': '',
                                                       'protocol': '', 'application': 'thermo_spectro'})

        out = run_export(path, tmp_path)

        text = read_text(out / 'Sample12_protocol.txt')
        assert 'Sample: Pomme 12' in text and 'Experiment' not in text and 'Operator' not in text
        assert 'Protocol' not in text

    def test_a_protocol_alone_is_enough(self, h5saver, qapp, tmp_path):
        path = write_file(h5saver, tmp_path, metadata={'protocol': 'Just this'})

        out = run_export(path, tmp_path)

        assert read_text(out / 'Sample12_protocol.txt').endswith('Protocol\n--------\nJust this\n')

    def test_nothing_said_no_file(self, h5saver, qapp, tmp_path):
        """The application is not something the user said"""
        path = write_file(h5saver, tmp_path, metadata={'experiment_title': '', 'sample': '', 'operator': '',
                                                       'protocol': '', 'application': 'thermo_spectro'})

        out = run_export(path, tmp_path)

        assert not (out / 'Sample12_protocol.txt').exists()

    def test_a_long_protocol_in_any_language_is_whole(self, h5saver, qapp, tmp_path):
        protocol = 'Rampe 20→80 °C à 2 °C/min — échantillon n°3 😀\n' * 6000
        path = write_file(h5saver, tmp_path, metadata={'protocol': protocol})

        out = run_export(path, tmp_path)

        assert read_text(out / 'Sample12_protocol.txt').endswith('--------\n' + protocol.rstrip('\n') + '\n')

    def test_the_file_is_in_utf8_with_a_signature_like_the_csv_files(self, h5saver, qapp, tmp_path):
        path = write_file(h5saver, tmp_path, metadata=METADATA)

        out = run_export(path, tmp_path)

        assert (out / 'Sample12_protocol.txt').read_bytes().startswith(b'\xef\xbb\xbf')


class TestAFileMadeBefore:
    def test_it_is_converted_as_it_was(self, h5saver, qapp, tmp_path):
        path = write_file(h5saver, tmp_path)
        with h5py.File(path, 'a') as file:
            del file['RawData/Datalogger000/Notes']

        out = run_export(path, tmp_path)

        assert sorted(file.name for file in out.iterdir()) == ['Sample12_spectrometer1.csv',
                                                               'Sample12_spectrometer1_wavelengths.csv']

    def test_nothing_is_read_where_there_is_nothing(self, h5saver, qapp, tmp_path):
        path = write_file(h5saver, tmp_path)
        with h5py.File(path, 'a') as file:
            del file['RawData/Datalogger000/Notes']

        with h5py.File(path, 'r') as file:
            assert export.read_description(file) == {}
            times, texts = export.read_notes(file['RawData/Datalogger000'])

        assert len(times) == 0 and texts == []

    def test_a_file_with_the_application_only(self, h5saver, qapp, tmp_path):
        path = write_file(h5saver, tmp_path, metadata={'application': 'thermo_spectro'})

        with h5py.File(path, 'r') as file:
            assert export.read_description(file) == {'application': 'thermo_spectro'}
