import json
import math
import subprocess
import sys
import textwrap
import time
from types import SimpleNamespace

import h5py
import numpy as np
import pytest

from pymodaq_gui.h5modules.saving import H5Saver
from pymodaq_gui.parameter import Parameter

from pymodaq_data.data import Axis, DataRaw
from pymodaq_data.h5modules.data_saving import DataBundle, DataLoader

from fakes import FakeDetector, FakeManager, FakeThermostat
from pymodaq_plugins_thermospectro import saving
from pymodaq_plugins_thermospectro.ramp import PHASE_CODES, Phase
from pymodaq_plugins_thermospectro.rois import Roi, default_rois
from pymodaq_plugins_thermospectro.saving import ThermoSpectroSaver, UnitRecording, build_dtes
from pymodaq_plugins_thermospectro.spectrometers import Spectrum
from pymodaq_plugins_thermospectro.unit import Measurement, Unit
from pymodaq_plugins_thermospectro.unit_settings import UnitSettings, unit_definition

pytestmark = pytest.mark.filterwarnings('ignore')

X = np.linspace(400., 900., 1001)


def make_unit(number=1, rois=None, run_ramp=True, thermostat=True, integration=False, units=None,
              per_integration=False, spectrum_per_integration=False):
    """A unit (not running) with fake modules, as the extension would hand it to the saver (its integration time is
    saved only if asked: it has its own tests; `units` is the intensity unit, `per_integration` the division of the
    values by the integration time, `spectrum_per_integration` the one of the full spectra and crops)"""
    detector = FakeDetector(f'Spectro{number}')
    thermo = FakeThermostat(f'Thermo{number}') if thermostat else None
    settings = UnitSettings(Parameter.create(**unit_definition(number, rois if rois is not None else
                                                               default_rois(number))), number)
    settings.set_modules([detector.title], [thermo.title] if thermo else [], detector.title,
                         thermo.title if thermo else None)
    settings.child('ramp', 'run').setValue(run_ramp)
    settings.child('record', 'integration_time').setValue(integration)
    if units is not None:
        settings.child('intensity_units').setValue(units)
    settings.child('per_integration').setValue(per_integration)
    settings.child('spectrum_per_integration').setValue(spectrum_per_integration)
    unit = Unit(settings, FakeManager(*[m for m in (detector, thermo) if m is not None]))
    unit.attach()
    return unit


def make_measurement(unit, index, t0=1000., phase=Phase.RAMP, integration=math.nan):
    y = X - 400. + index
    axis = Axis(label='wavelength', units='nm', data=X, index=0)
    dwa = DataRaw('spectrum', data=[y], axes=[axis], labels=['intensity'])
    spectrum = Spectrum(axis, y, time.monotonic(), dwa, X, 'nm')
    rois = unit.settings.rois
    return Measurement(unit.number, index, t0 + index, spectrum, rois, [100. * index + k for k in range(len(rois))],
                       20. + index, 21. + index if phase is not None else math.nan, phase,
                       1 if phase is not None else 0, integration_time=integration)


class TestRecording:
    def test_everything_is_recorded_by_default(self, qapp):
        unit = make_unit(integration=True)

        recording = UnitRecording.from_unit(unit)

        assert recording.titles == ['Unit1_Spectrum', 'Unit1_ROIs', 'Unit1_Integration', 'Unit1_Temperature',
                                   'Unit1_Program']
        assert [roi.name for roi in recording.rois] == [f'ROI {i}' for i in range(1, 7)]
        assert recording.roi_indices == [0, 1, 2, 3, 4, 5]
        assert recording.is_saved and recording.program

    def test_the_spectrum_can_be_left_out(self, qapp):
        unit = make_unit()
        unit.settings.child('record', 'spectrum').setValue(False)

        assert 'Unit1_Spectrum' not in UnitRecording.from_unit(unit).titles

    def test_the_temperature_can_be_left_out_and_the_ramp_with_it(self, qapp):
        unit = make_unit()
        unit.settings.child('record', 'temperature').setValue(False)

        titles = UnitRecording.from_unit(unit).titles

        assert titles == ['Unit1_Spectrum', 'Unit1_ROIs']

    def test_each_roi_can_be_left_out_by_itself(self, qapp):
        unit = make_unit()
        for key in ('roi01', 'roi04'):
            unit.settings.child('rois', key, 'record').setValue(False)

        recording = UnitRecording.from_unit(unit)

        assert [roi.name for roi in recording.rois] == ['ROI 1', 'ROI 3', 'ROI 4', 'ROI 6']
        assert recording.roi_indices == [0, 2, 3, 5]

    def test_no_roi_recorded_no_group(self, qapp):
        unit = make_unit()
        for key in unit.settings.roi_keys:
            unit.settings.child('rois', key, 'record').setValue(False)

        assert 'Unit1_ROIs' not in UnitRecording.from_unit(unit).titles

    def test_nothing_at_all(self, qapp):
        unit = make_unit()
        unit.settings.child('record', 'spectrum').setValue(False)
        unit.settings.child('record', 'temperature').setValue(False)
        unit.settings.set_rois([])

        recording = UnitRecording.from_unit(unit)

        assert recording.titles == [] and not recording.is_saved

    def test_without_ramp_there_is_no_program_group(self, qapp):
        unit = make_unit(run_ramp=False)

        assert UnitRecording.from_unit(unit).titles == ['Unit1_Spectrum', 'Unit1_ROIs', 'Unit1_Temperature']

    def test_without_thermostat_there_is_no_temperature(self, qapp):
        unit = make_unit(thermostat=False)

        assert UnitRecording.from_unit(unit).titles == ['Unit1_Spectrum', 'Unit1_ROIs']

    def test_rois_with_the_same_name_are_told_apart(self, qapp):
        unit = make_unit(rois=[Roi('peak', 500., 510.), Roi('peak', 600., 610.)])

        assert [roi.name for roi in UnitRecording.from_unit(unit).rois] == ['peak', 'peak (2)']

    def test_the_second_unit_has_its_own_titles(self, qapp):
        assert UnitRecording.from_unit(make_unit(2)).titles[0] == 'Unit2_Spectrum'

    def test_the_settings_are_a_copy(self, qapp):
        unit = make_unit()
        recording = UnitRecording.from_unit(unit)

        unit.settings.child('integration_time').setValue(123.)

        assert recording.settings['integration_time'] == 500.
        assert recording.integration_time == 500.


class TestTimestampedSaver:
    def test_it_filters_like_the_standard_saver_and_keeps_the_time(self, h5saver, qapp):
        from pymodaq.utils.h5modules.module_saving import DetectorTimeSaver
        unit = make_unit()
        experiment = Experiment(h5saver, [unit])
        dte = build_dtes(experiment.recordings[0], make_measurement(unit, 3, t0=700.))[0]
        mine = experiment.saver.detectors['Unit1_Spectrum']
        standard = DetectorTimeSaver(mine.module)
        standard.h5saver = h5saver

        ours, theirs = mine.filter_data(dte), standard.filter_data(dte)

        assert [dwa.name for dwa in ours] == [dwa.name for dwa in theirs] == [dte[0].name]
        assert ours.timestamp == 703.
        experiment.finish()

    def test_data_which_is_not_to_be_saved_is_left_out(self, h5saver, qapp):
        unit = make_unit()
        experiment = Experiment(h5saver, [unit])
        dte = build_dtes(experiment.recordings[0], make_measurement(unit, 0))[0]
        dte[0].add_extra_attribute(do_save=False)

        assert len(experiment.saver.detectors['Unit1_Spectrum'].filter_data(dte)) == 0
        experiment.finish()

    def test_it_does_not_use_data_to_export_append(self, h5saver, qapp, monkeypatch):
        """DataToExport.append is not thread-safe, and this runs in the thread which writes the file"""
        from pymodaq_data import DataToExport
        calls = []
        original = DataToExport.__dict__['append']

        def spy(self, *args):
            calls.append(args)
            original.__get__(self, type(self))
            return original(*args)

        unit = make_unit()
        experiment = Experiment(h5saver, [unit])
        monkeypatch.setattr(DataToExport, 'append', spy)

        for index in range(3):
            experiment.write(unit, index)

        assert calls == []
        monkeypatch.undo()
        experiment.finish()


class TestBuild:
    def test_one_data_to_export_per_group(self, qapp):
        unit = make_unit()
        dtes = build_dtes(UnitRecording.from_unit(unit), make_measurement(unit, 3))

        assert [dte.name for dte in dtes] == ['Unit1_Spectrum', 'Unit1_ROIs', 'Unit1_Temperature', 'Unit1_Program']

    def test_what_is_not_recorded_is_not_built(self, qapp):
        unit = make_unit()
        unit.settings.child('record', 'spectrum').setValue(False)
        unit.settings.child('rois', 'roi00', 'record').setValue(False)

        dtes = build_dtes(UnitRecording.from_unit(unit), make_measurement(unit, 0))

        assert [dte.name for dte in dtes] == ['Unit1_ROIs', 'Unit1_Temperature', 'Unit1_Program']
        assert len(dtes[0][0]) == 5  # five ROIs

    def test_all_are_stamped_with_the_time_of_the_readout(self, qapp):
        unit = make_unit()

        dtes = build_dtes(UnitRecording.from_unit(unit), make_measurement(unit, 7, t0=5000.))

        assert [dte.timestamp for dte in dtes] == [5007.] * 4

    def test_the_spectrum(self, qapp):
        unit = make_unit()
        dte = build_dtes(UnitRecording.from_unit(unit), make_measurement(unit, 2))[0]

        dwa = dte[0]
        assert dwa.origin == 'Spectro1'
        assert dwa.data[0][0] == 2. and dwa.data[0].size == 1001
        assert dwa.get_axis_from_index(0)[0].units == 'nm'

    def test_a_spectrum_without_axis_gets_the_one_which_is_known(self, qapp):
        unit = make_unit()
        measurement = make_measurement(unit, 0)
        measurement.spectrum.dwa.axes = []  # (a plugin which sends its axis once)

        dwa = build_dtes(UnitRecording.from_unit(unit), measurement)[0][0]

        assert dwa.get_axis_from_index(0)[0].size == 1001

    def test_the_rois_are_one_value_each_with_their_window_as_label(self, qapp):
        unit = make_unit(rois=[Roi('a', 500., 510.), Roi('b', 600., 610.), Roi('c', 700., 710., record=False)])
        measurement = make_measurement(unit, 1)

        dwa = build_dtes(UnitRecording.from_unit(unit), measurement)[1][0]

        assert [float(array[0]) for array in dwa.data] == [100., 101.]
        assert dwa.labels == ['a mean (500-510 nm)', 'b mean (600-610 nm)']

    def test_the_temperature_and_the_program(self, qapp):
        unit = make_unit()
        dtes = build_dtes(UnitRecording.from_unit(unit), make_measurement(unit, 4))

        temperature, program = dtes[2][0], dtes[3][0]

        assert float(temperature.data[0][0]) == 24.
        assert str(temperature.units) == '°C'
        assert [float(array[0]) for array in program.data] == [25., 1., PHASE_CODES[Phase.RAMP]]
        assert program.labels == ['set-point', 'cycle', 'phase code']

    def test_a_missing_temperature_is_saved_as_nan_to_keep_the_arrays_aligned(self, qapp):
        unit = make_unit()
        measurement = make_measurement(unit, 0)
        measurement.temperature = math.nan

        temperature = build_dtes(UnitRecording.from_unit(unit), measurement)[2][0]

        assert math.isnan(float(temperature.data[0][0]))

    def test_the_program_is_not_built_before_the_ramp_has_a_state(self, qapp):
        unit = make_unit()

        dtes = build_dtes(UnitRecording.from_unit(unit), make_measurement(unit, 0, phase=None))

        assert [dte.name for dte in dtes] == ['Unit1_Spectrum', 'Unit1_ROIs', 'Unit1_Temperature']


@pytest.fixture
def h5saver(qtbot, tmp_path):
    saver = H5Saver()
    saver.settings['base_path'] = str(tmp_path)
    saver.settings['base_name'] = 'ThermoSpectro'
    yield saver
    saver.close_file()


class Experiment:
    """A saver writing in a real file, as the saver thread of the extension does"""

    def __init__(self, h5saver, units, **prepare):
        self.h5saver = h5saver
        self.units = units
        self.recordings = [UnitRecording.from_unit(unit) for unit in units]
        self.swmr = saving.prepare_file(h5saver, **prepare)
        extension = SimpleNamespace(title='ThermoSpectro', modules_manager=None,
                                    settings=Parameter.create(name='settings', type='group', children=[]))
        self.saver = ThermoSpectroSaver(extension)
        self.saver.configure(self.recordings)
        self.saver.h5saver = h5saver
        self.saver.get_set_node(new=True)

    @property
    def path(self):
        return self.h5saver.h5_file_path.joinpath(self.h5saver.h5_file_name)

    def write(self, unit, index, **kwargs):
        recording = next(r for r in self.recordings if r.number == unit.number)
        for dte in build_dtes(recording, make_measurement(unit, index, **kwargs)):
            self.saver.add_data_bundle(DataBundle(dte=dte))

    def finish(self):
        self.h5saver.flush()
        self.h5saver.close_file()
        return saving.finalize_file(self.h5saver)


def titles(group):
    """The groups of the data of an experiment, by title (not the one of the observations)"""
    return {_text(grp.attrs['TITLE']): grp for name, grp in group.items()
            if isinstance(grp, h5py.Group) and 'TITLE' in grp.attrs and name != saving.NOTES}


def _text(value):
    text = value.decode() if isinstance(value, bytes) else str(value)
    try:
        import ast
        text = ast.literal_eval(json.loads(text)['data'])
    except (ValueError, KeyError, SyntaxError, TypeError):
        pass
    return text


def attribute(node, name):
    value = node.attrs[name]
    try:
        import ast
        return ast.literal_eval(json.loads(value)['data'])
    except (ValueError, KeyError, SyntaxError, TypeError):
        return value


class TestFile:
    def test_a_new_file_in_swmr_mode_for_each_experiment(self, h5saver):
        first = saving.prepare_file(h5saver)
        first_path = h5saver.h5_file_path.joinpath(h5saver.h5_file_name)
        h5saver.close_file()

        second = saving.prepare_file(h5saver)
        second_path = h5saver.h5_file_path.joinpath(h5saver.h5_file_name)

        assert first and second and h5saver.swmr_mode and h5saver.backend == 'h5py'
        assert first_path != second_path and first_path.is_file() and second_path.is_file()
        with h5py.File(first_path, 'r') as f:
            assert _text(f.attrs['swmr_compatible']) in (True, 'True')

    def test_the_h5py_backend_is_used_even_if_the_saver_was_set_for_another(self, h5saver):
        pytest.importorskip('tables')
        h5saver.settings['backend', 'backend_type'] = 'tables'

        assert saving.prepare_file(h5saver)

        assert h5saver.backend == 'h5py'
        assert h5saver.settings['backend', 'swmr_options', 'enable_swmr']

    def test_swmr_is_switched_on_even_if_it_was_off(self, h5saver):
        h5saver.settings['backend', 'swmr_options', 'enable_swmr'] = False

        assert saving.prepare_file(h5saver)

    def test_the_flush_interval(self, h5saver):
        saving.prepare_file(h5saver, flush_interval=5)

        assert h5saver.settings['backend', 'swmr_options', 'flush_interval'] == 5
        assert h5saver._flush_interval == 5

    def test_an_experiment_in_a_file_which_is_not_swmr_cannot_be_finalized(self, h5saver):
        assert not saving.finalize_file(h5saver)  # nothing was opened


class TestSaver:
    def test_everything_is_saved_in_the_groups_of_the_unit(self, h5saver, qapp):
        unit = make_unit()
        experiment = Experiment(h5saver, [unit])
        for index in range(5):
            experiment.write(unit, index)
        assert experiment.finish()

        with h5py.File(experiment.path, 'r') as f:
            groups = titles(f['RawData/Datalogger000'])
            assert sorted(groups) == ['Unit1_Program', 'Unit1_ROIs', 'Unit1_Spectrum', 'Unit1_Temperature']
            spectra = groups['Unit1_Spectrum']['Data1D/CH00/EnlData00']
            assert spectra.shape == (5, 1001)
            assert spectra[3, 0] == 3.
            rois = groups['Unit1_ROIs']['Data0D/CH00']
            assert [rois[f'EnlData{i:02d}'][:, 0].tolist() for i in (0, 5)] == [
                [0., 100., 200., 300., 400.], [5., 105., 205., 305., 405.]]
            assert groups['Unit1_Temperature']['Data0D/CH00/EnlData00'][:, 0].tolist() == [20., 21., 22., 23., 24.]
            program = groups['Unit1_Program']['Data0D/CH00']
            assert program['EnlData00'][:, 0].tolist() == [21., 22., 23., 24., 25.]
            assert program['EnlData02'][:, 0].tolist() == [PHASE_CODES[Phase.RAMP]] * 5

    def test_the_four_groups_of_a_unit_share_the_same_time_axis(self, h5saver, qapp):
        unit = make_unit()
        experiment = Experiment(h5saver, [unit])
        for index in range(4):
            experiment.write(unit, index, t0=5000.)
        experiment.finish()

        with h5py.File(experiment.path, 'r') as f:
            axes = [group['NavAxes/Axis00'][:].tolist() for group in titles(f['RawData/Datalogger000']).values()]

        assert axes == [[5000., 5001., 5002., 5003.]] * 4

    def test_the_wavelengths_are_the_axis_of_the_spectrum(self, h5saver, qapp):
        unit = make_unit()
        experiment = Experiment(h5saver, [unit])
        experiment.write(unit, 0)
        experiment.finish()

        with h5py.File(experiment.path, 'r') as f:
            group = titles(f['RawData/Datalogger000'])['Unit1_Spectrum']
            assert group['Data1D/CH00/Axis00'][:].tolist() == X.tolist()

    def test_what_a_reader_needs_to_know_is_in_the_attributes(self, h5saver, qapp):
        unit = make_unit()
        unit.settings.child('rois', 'roi02', 'record').setValue(False)
        experiment = Experiment(h5saver, [unit])
        experiment.write(unit, 0)
        experiment.finish()

        with h5py.File(experiment.path, 'r') as f:
            groups = titles(f['RawData/Datalogger000'])
            rois = groups['Unit1_ROIs']
            assert attribute(rois, 'unit') == 1
            assert attribute(rois, 'spectrometer') == 'Spectro1' and attribute(rois, 'thermostat') == 'Thermo1'
            assert attribute(rois, 'integration_time_ms') == 500. and attribute(rois, 'readout_time_s') == 1.
            recorded = json.loads(attribute(rois, 'rois'))
            assert [roi['name'] for roi in recorded] == ['ROI 1', 'ROI 2', 'ROI 4', 'ROI 5', 'ROI 6']
            assert recorded[0]['start'] == default_rois(1)[0].start
            assert attribute(groups['Unit1_Temperature'], 'temperature_units') == '°C'
            codes = json.loads(attribute(groups['Unit1_Program'], 'phase_codes'))
            assert codes['ramp'] == PHASE_CODES[Phase.RAMP] and codes['hold final'] == PHASE_CODES[Phase.HOLD_FINAL]

    def test_each_kind_of_data_can_be_left_out_independently(self, h5saver, qapp):
        unit = make_unit()
        unit.settings.child('record', 'temperature').setValue(False)
        for key in ('roi00', 'roi01', 'roi02'):
            unit.settings.child('rois', key, 'record').setValue(False)
        experiment = Experiment(h5saver, [unit])
        for index in range(3):
            experiment.write(unit, index)
        experiment.finish()

        with h5py.File(experiment.path, 'r') as f:
            groups = titles(f['RawData/Datalogger000'])
            assert sorted(groups) == ['Unit1_ROIs', 'Unit1_Spectrum']
            rois = groups['Unit1_ROIs']['Data0D/CH00']
            assert sorted(rois) == ['EnlData00', 'EnlData01', 'EnlData02']
            assert rois['EnlData00'][:, 0].tolist() == [3., 103., 203.]  # the fourth ROI is the first recorded

    def test_only_the_temperature(self, h5saver, qapp):
        unit = make_unit()
        unit.settings.child('record', 'spectrum').setValue(False)
        unit.settings.set_rois([])
        experiment = Experiment(h5saver, [unit])
        for index in range(3):
            experiment.write(unit, index)
        experiment.finish()

        with h5py.File(experiment.path, 'r') as f:
            assert sorted(titles(f['RawData/Datalogger000'])) == ['Unit1_Program', 'Unit1_Temperature']

    def test_two_units_in_one_file(self, h5saver, qapp):
        one, two = make_unit(1), make_unit(2)
        two.settings.child('record', 'spectrum').setValue(False)
        experiment = Experiment(h5saver, [one, two])
        for index in range(3):
            experiment.write(one, index, t0=1000.)
            experiment.write(two, index, t0=2000.)
        experiment.finish()

        with h5py.File(experiment.path, 'r') as f:
            groups = titles(f['RawData/Datalogger000'])
            assert sorted(groups) == ['Unit1_Program', 'Unit1_ROIs', 'Unit1_Spectrum', 'Unit1_Temperature',
                                      'Unit2_Program', 'Unit2_ROIs', 'Unit2_Temperature']
            assert groups['Unit1_ROIs']['NavAxes/Axis00'][:].tolist() == [1000., 1001., 1002.]
            assert groups['Unit2_ROIs']['NavAxes/Axis00'][:].tolist() == [2000., 2001., 2002.]
            assert attribute(groups['Unit2_ROIs'], 'spectrometer') == 'Spectro2'

    def test_the_file_is_readable_with_the_pymodaq_loader(self, h5saver, qapp):
        unit = make_unit()
        experiment = Experiment(h5saver, [unit])
        for index in range(4):
            experiment.write(unit, index, t0=3000.)
        experiment.finish()

        loader = DataLoader(experiment.path)
        with h5py.File(experiment.path, 'r') as f:
            node = [name for name, grp in f['RawData/Datalogger000'].items()
                    if _text(grp.attrs['TITLE']) == 'Unit1_Spectrum'][0]
        data = loader.load_data(f'/RawData/Datalogger000/{node}/Data1D/CH00/EnlData00', load_all=True)

        assert data.shape == (4, 1001)
        assert [axis.label for axis in data.axes] == ['wavelength', 'time']
        assert data.axes[1].get_data().tolist() == [3000., 3001., 3002., 3003.]

    def test_nothing_is_saved_when_nothing_is_chosen(self, h5saver, qapp):
        unit = make_unit()
        unit.settings.child('record', 'spectrum').setValue(False)
        unit.settings.child('record', 'temperature').setValue(False)
        unit.settings.set_rois([])
        experiment = Experiment(h5saver, [unit])

        assert experiment.saver.expected == set()
        assert experiment.finish() in (True, False)

    @pytest.mark.errors_expected
    def test_data_which_cannot_be_saved_is_counted_and_does_not_raise(self, h5saver, qapp):
        unit = make_unit()
        experiment = Experiment(h5saver, [unit])
        other = build_dtes(UnitRecording.from_unit(make_unit(2)), make_measurement(make_unit(2), 0))[0]

        experiment.saver.add_data_bundle(DataBundle(dte=other))  # a group which is not saved

        assert experiment.saver.n_errors == 1
        experiment.finish()

    def test_the_counter_of_errors_starts_again_with_each_experiment(self, h5saver, qapp):
        unit = make_unit()
        experiment = Experiment(h5saver, [unit])
        experiment.saver.n_errors = 3

        experiment.saver.configure(experiment.recordings)

        assert experiment.saver.n_errors == 0
        experiment.finish()


class TestSwmr:
    READER = textwrap.dedent('''
        import h5py, json, sys
        file = h5py.File(sys.argv[1], 'r', swmr=True)
        sizes = {}
        def visit(name, obj):
            if isinstance(obj, h5py.Dataset) and name.endswith('EnlData00'):
                obj.refresh()
                sizes[name] = obj.shape[0]
        file['RawData/Datalogger000'].visititems(visit)
        print(json.dumps(sorted(set(sizes.values()))))
    ''')

    def read_from_another_process(self, path):
        result = subprocess.run([sys.executable, '-c', self.READER, str(path)], capture_output=True, text=True,
                                timeout=120)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    def test_swmr_starts_when_every_group_has_its_first_data(self, h5saver, qapp):
        unit = make_unit()
        experiment = Experiment(h5saver, [unit])
        recording = experiment.recordings[0]
        dtes = build_dtes(recording, make_measurement(unit, 0))
        assert len(dtes) == 4

        for dte in dtes[:-1]:
            experiment.saver.add_data_bundle(DataBundle(dte=dte))
            assert not experiment.saver.swmr_started and not h5saver.is_swmr_active
        experiment.saver.add_data_bundle(DataBundle(dte=dtes[-1]))

        assert experiment.saver.swmr_started and h5saver.is_swmr_active
        experiment.finish()

    def test_the_file_is_readable_by_another_process_during_the_experiment(self, h5saver, qapp):
        unit = make_unit()
        experiment = Experiment(h5saver, [unit])
        for index in range(3):
            experiment.write(unit, index)

        assert self.read_from_another_process(experiment.path) == [3]

        for index in range(3, 8):
            experiment.write(unit, index)

        assert self.read_from_another_process(experiment.path) == [8]
        experiment.finish()

    def test_two_units_swmr_starts_when_the_slowest_has_delivered(self, h5saver, qapp):
        one, two = make_unit(1), make_unit(2)
        experiment = Experiment(h5saver, [one, two])
        for index in range(5):  # the first unit is five readouts ahead
            experiment.write(one, index)
        assert not experiment.saver.swmr_started

        experiment.write(two, 0)

        assert experiment.saver.swmr_started
        assert self.read_from_another_process(experiment.path) == [1, 5]
        experiment.finish()

    def test_the_end_of_swmr_updates_the_shape_attributes(self, h5saver, qapp):
        unit = make_unit()
        experiment = Experiment(h5saver, [unit])
        for index in range(6):
            experiment.write(unit, index)
        experiment.finish()

        with h5py.File(experiment.path, 'r') as f:
            assert attribute(f, 'swmr_active') in (False, np.False_)
            for group in titles(f['RawData/Datalogger000']).values():
                for name, node in group['Data0D/CH00'].items() if 'Data0D' in group else []:
                    assert attribute(node, 'shape') == (6, 1)

    def test_without_swmr_the_file_is_simply_written(self, h5saver, qapp):
        pytest.importorskip('tables')
        unit = make_unit()
        h5saver.settings['backend', 'backend_type'] = 'tables'
        recordings = [UnitRecording.from_unit(unit)]
        h5saver.init_file(update_h5=True)  # (not prepare_file: no SWMR, pytables)
        extension = SimpleNamespace(title='ThermoSpectro', modules_manager=None,
                                    settings=Parameter.create(name='settings', type='group', children=[]))
        saver = ThermoSpectroSaver(extension)
        saver.configure(recordings)
        saver.h5saver = h5saver
        saver.get_set_node(new=True)

        for index in range(3):
            for dte in build_dtes(recordings[0], make_measurement(unit, index)):
                saver.add_data_bundle(DataBundle(dte=dte))

        assert not saver.swmr_started and saver.n_errors == 0
        assert not saving.finalize_file(h5saver)


def test_data_marked_to_be_saved_is_kept(h5saver, qapp):
    unit = make_unit()
    experiment = Experiment(h5saver, [unit])
    dte = build_dtes(experiment.recordings[0], make_measurement(unit, 0))[0]
    dte[0].add_extra_attribute(do_save=True)

    assert len(experiment.saver.detectors['Unit1_Spectrum'].filter_data(dte)) == 1
    experiment.finish()


class TestWhenSwmrCannotBeStarted:
    """SWMR is what lets another program (lab_export) read the file while it is written: the data do not depend on it"""

    def test_the_data_are_saved_and_it_is_told_once(self, h5saver, qapp, monkeypatch):
        calls = []

        def refuse():
            calls.append(1)
            raise OSError('refused by this system')

        monkeypatch.setattr(h5saver, 'enable_swmr', refuse)
        unit = make_unit()
        experiment = Experiment(h5saver, [unit])

        for index in range(4):
            experiment.write(unit, index)

        saver = experiment.saver
        assert not saver.swmr_started and 'refused by this system' in saver.swmr_error
        assert saver.n_errors == 0  # (nothing was lost: it is not an error of the data)
        assert len(calls) == 1  # (and it is not tried again at each measurement)
        experiment.finish()
        with h5py.File(experiment.path, 'r') as f:
            assert titles(f['RawData/Datalogger000'])['Unit1_ROIs']['Data0D/CH00/EnlData00'].shape[0] == 4

    def test_the_failure_is_forgotten_by_the_next_experiment(self, h5saver, qapp, monkeypatch):
        monkeypatch.setattr(h5saver, 'enable_swmr', lambda: (_ for _ in ()).throw(OSError('refused')))
        unit = make_unit()
        experiment = Experiment(h5saver, [unit])
        experiment.write(unit, 0)
        assert experiment.saver.swmr_error

        experiment.saver.configure(experiment.recordings)

        assert experiment.saver.swmr_error == ''
        experiment.finish()

    def test_the_groups_which_hold_it_up_are_named(self, h5saver, qapp):
        unit = make_unit()
        experiment = Experiment(h5saver, [unit])
        assert experiment.saver.waiting_for == sorted(experiment.saver.expected) and experiment.saver.waiting_for

        first = build_dtes(experiment.recordings[0], make_measurement(unit, 0))[0]
        experiment.saver.add_data_bundle(DataBundle(dte=first))

        assert first.name not in experiment.saver.waiting_for and experiment.saver.waiting_for
        assert not experiment.saver.swmr_started  # (SWMR waits for the others)
        for dte in build_dtes(experiment.recordings[0], make_measurement(unit, 0)):
            experiment.saver.add_data_bundle(DataBundle(dte=dte))
        assert experiment.saver.waiting_for == [] and experiment.saver.swmr_started
        experiment.finish()

    def test_the_groups_of_a_lost_unit_which_never_had_data_do_not_hold_swmr_up(self, h5saver, qapp):
        first, second = make_unit(1), make_unit(2)
        experiment = Experiment(h5saver, [first, second])
        for dte in build_dtes(experiment.recordings[0], make_measurement(first, 0)):
            experiment.saver.add_data_bundle(DataBundle(dte=dte))
        assert not experiment.saver.swmr_started
        assert experiment.saver.waiting_for and all(title.startswith('Unit2_') for title in experiment.saver.waiting_for)

        experiment.saver.release_unit(2)  # (its spectrometer is lost: it will never send anything)

        assert experiment.saver.waiting_for == []
        experiment.saver.add_data_bundle(DataBundle(dte=build_dtes(experiment.recordings[0],
                                                                    make_measurement(first, 1))[0]))
        assert experiment.saver.swmr_started
        experiment.finish()

    def test_the_groups_of_a_unit_which_had_data_stay_in_the_file_when_it_is_lost(self, h5saver, qapp):
        first, second = make_unit(1), make_unit(2)
        experiment = Experiment(h5saver, [first, second])
        for unit, recording in zip((first, second), experiment.recordings):
            for dte in build_dtes(recording, make_measurement(unit, 0)):
                experiment.saver.add_data_bundle(DataBundle(dte=dte))
        assert experiment.saver.swmr_started

        experiment.saver.release_unit(2)
        for dte in build_dtes(experiment.recordings[0], make_measurement(first, 1)):
            experiment.saver.add_data_bundle(DataBundle(dte=dte))
        experiment.finish()

        with h5py.File(experiment.path, 'r') as f:
            groups = titles(f['RawData/Datalogger000'])
            assert groups['Unit2_ROIs']['Data0D/CH00/EnlData00'].shape[0] == 1  # (what it recorded is kept)
            assert groups['Unit1_ROIs']['Data0D/CH00/EnlData00'].shape[0] == 2

    def test_a_new_experiment_expects_the_units_again(self, h5saver, qapp):
        first, second = make_unit(1), make_unit(2)
        experiment = Experiment(h5saver, [first, second])
        experiment.saver.release_unit(2)
        assert all(title.startswith('Unit1_') for title in experiment.saver.expected)

        experiment.saver.configure(experiment.recordings)

        assert any(title.startswith('Unit2_') for title in experiment.saver.expected)
        experiment.finish()
