"""A thermal controller held at one temperature, a log which lasts until Stop or for a given time, and a whole spectrum
(image) saved only now and then in a long log"""
import time

import pytest

from pymodaq_gui.parameter import Parameter

from fakes import FakeCamera, FakeDetector, FakeManager, FakeThermostat
from pymodaq_plugins_thermospectro.camera import RectRoi, default_rect_rois
from pymodaq_plugins_thermospectro.ramp import PHASE_CODES, Phase
from pymodaq_plugins_thermospectro.rois import Roi, default_rois
from pymodaq_plugins_thermospectro.saving import UnitRecording, build_dtes
from pymodaq_plugins_thermospectro.unit import CameraUnit, Problem, Unit, UnitState
from pymodaq_plugins_thermospectro.unit_settings import (CAMERA, CONTROL_HOLD, CONTROL_RAMP, RAMP_FIELDS, CameraSettings,
                                                         UnitSettings, unit_definition)

from test_saving import make_measurement

pytestmark = pytest.mark.filterwarnings('ignore')


def visible(settings, name):
    return settings.param.child('ramp', name).opts.get('visible', True)


class TestSettings:
    @pytest.fixture
    def settings(self):
        return UnitSettings(Parameter.create(**unit_definition(1, default_rois(1))), 1)

    def test_a_ramp_is_the_default_of_a_unit(self, settings):
        assert settings.control == CONTROL_RAMP and not settings.holds and settings.run_ramp

    def test_hold_can_be_the_default(self):
        settings = UnitSettings(Parameter.create(**unit_definition(1, default_rois(1), control=CONTROL_HOLD)), 1)

        assert settings.holds and settings.hold_temperature == 25.0

    def test_a_held_controller_shows_its_temperature_and_not_the_ramp(self, settings):
        settings.param['ramp', 'control'] = CONTROL_HOLD
        settings.refresh_ramp()

        assert visible(settings, 'hold_temperature') and visible(settings, 'control')
        assert not any(visible(settings, name) for name in RAMP_FIELDS)

    def test_a_ramp_shows_the_ramp_and_not_the_temperature(self, settings):
        settings.param['ramp', 'control'] = CONTROL_RAMP
        settings.refresh_ramp()

        assert all(visible(settings, name) for name in RAMP_FIELDS) and not visible(settings, 'hold_temperature')

    def test_a_unit_which_does_not_drive_its_controller_shows_nothing_of_it(self, settings):
        settings.param['ramp', 'run'] = False
        settings.refresh_ramp()

        assert visible(settings, 'run')
        assert not any(visible(settings, name) for name in RAMP_FIELDS + ('control', 'hold_temperature'))

    def test_the_units_of_the_controller_are_the_ones_of_the_temperature_to_hold(self, settings):
        settings.set_units('°C')

        assert settings.param.child('ramp', 'hold_temperature').opts['suffix'] == '°C'

    def test_without_thermal_controller_its_settings_are_hidden(self):
        param = Parameter.create(**unit_definition(1, default_rois(1), thermostat=False))

        for name in ('thermostat', 'plot_temperature', 'ramp'):
            assert param.child(name).opts['visible'] is False
        assert param.child('record', 'temperature').opts['visible'] is False
        assert param.child('record', 'temperature').value() is False

    def test_the_whole_data_intervals(self):
        spectrum = UnitSettings(Parameter.create(**unit_definition(1, default_rois(1))), 1)
        camera = CameraSettings(Parameter.create(**unit_definition(3, default_rect_rois(3), kind=CAMERA)), 3)

        assert spectrum.full_interval == 0.0  # (each measurement)
        assert camera.full_interval == 60.0
        spectrum.param['record', 'spectrum_interval'] = 5.
        assert spectrum.full_interval == 5.


class Bench:
    """A unit with fake modules (a spectrometer, or a camera)"""

    def __init__(self, qtbot, camera=False, thermostat=True, control=CONTROL_HOLD, hold=40.):
        self.qtbot = qtbot
        if camera:
            self.detector = FakeCamera('Camera1')
            self.settings = CameraSettings(Parameter.create(**unit_definition(
                3, [RectRoi('Spot', 10, 20, 20, 30)], kind=CAMERA, control=control)), 3)
            self.settings.set_modules(['Camera1'], ['Thermo1'] if thermostat else [], 'Camera1',
                                      'Thermo1' if thermostat else None)
            unit_class = CameraUnit
        else:
            self.detector = FakeDetector('Spectro1')
            self.settings = UnitSettings(Parameter.create(**unit_definition(
                1, [Roi('A', 500., 510.)], control=control)), 1)
            self.settings.set_modules(['Spectro1'], ['Thermo1'] if thermostat else [], 'Spectro1',
                                      'Thermo1' if thermostat else None)
            unit_class = Unit
        self.thermostat = FakeThermostat('Thermo1', value=20.) if thermostat else None
        self.settings.child('integration_time').setValue(20.)
        self.settings.child('readout_time').setValue(0.1)
        self.settings.param['ramp', 'hold_temperature'] = hold
        self.unit = unit_class(self.settings, FakeManager(*[m for m in (self.detector, self.thermostat) if m]))
        self.unit.attach()
        self.measurements, self.notices, self.problems = [], [], []
        self.unit.measurement.connect(self.measurements.append)
        self.unit.notice.connect(self.notices.append)
        self.unit.problem.connect(lambda unit, kind, message: self.problems.append((kind, message)))

    def run(self):
        self.unit.start_live(time.time())
        self.qtbot.waitUntil(lambda: self.unit.ready, timeout=10000)
        self.unit.begin_run(time.time(), 0.0)
        self.measurements.clear()  # (what was measured by the live view is not part of the log)

    def wait_for(self, n, timeout=10000):
        self.qtbot.waitUntil(lambda: len(self.measurements) >= n, timeout=timeout)


@pytest.fixture
def bench(qtbot):
    bench = Bench(qtbot)
    yield bench
    if bench.unit.state != UnitState.IDLE:
        bench.unit._release()


class TestHold:
    def test_the_unit_holds_instead_of_running_a_ramp(self, bench):
        assert bench.unit.has_hold and not bench.unit.has_program

    def test_the_temperature_is_set_once_when_the_log_starts(self, bench):
        bench.run()
        bench.wait_for(5)

        assert bench.thermostat.targets == [40.]  # (once: a controller holds its temperature by itself)
        assert bench.unit.hold_target == 40. and bench.unit.runner is None and bench.unit.program is None

    def test_what_is_measured_says_it_is_held(self, bench):
        bench.run()
        bench.wait_for(3)

        measurement = bench.measurements[-1]
        assert measurement.setpoint == 40. and measurement.phase == Phase.HOLD and measurement.cycle == 0

    def test_the_status_tells_it(self, bench):
        bench.run()

        assert 'held at 40' in bench.unit.status_text()
        assert bench.settings.child('status', 'phase').value() == Phase.HOLD.value
        assert any('held at 40' in notice for notice in bench.notices)

    def test_the_hold_goes_on_with_the_ticks_of_the_experiment(self, bench):
        bench.run()

        assert bench.unit.program_tick(1.0) is None  # (nothing to follow)
        assert bench.thermostat.targets == [40.]

    def test_a_log_which_ends_leaves_the_controller_where_it_is(self, bench):
        bench.run()
        bench.wait_for(2)

        bench.unit.end_run()

        assert bench.thermostat.targets == [40.] and bench.unit.hold_target is None

    def test_a_temperature_which_is_not_a_number_is_refused(self, bench):
        bench.settings.param['ramp', 'hold_temperature'] = float('nan')

        assert 'not a number' in bench.unit.problems()[0]

    def test_a_ramp_is_still_run_when_it_is_asked_for(self, qtbot):
        bench = Bench(qtbot, control=CONTROL_RAMP)

        assert bench.unit.has_program and not bench.unit.has_hold

    def test_a_unit_which_only_records_holds_nothing(self, qtbot):
        bench = Bench(qtbot)
        bench.settings.param['ramp', 'run'] = False

        assert not bench.unit.has_hold and not bench.unit.has_program

    def test_without_thermostat_there_is_nothing_to_hold(self, qtbot):
        bench = Bench(qtbot, thermostat=False)

        assert not bench.unit.has_hold

    def test_the_camera_holds_a_temperature_too(self, qtbot):
        bench = Bench(qtbot, camera=True, hold=33.)
        bench.run()
        bench.wait_for(2)

        assert bench.thermostat.targets == [33.] and bench.measurements[-1].phase == Phase.HOLD
        bench.unit._release()


class TestRecordedHold:
    def test_the_hold_is_recorded_with_the_temperature(self, qapp):
        settings = UnitSettings(Parameter.create(**unit_definition(1, default_rois(1), control=CONTROL_HOLD)), 1)
        detector, thermo = FakeDetector('Spectro1'), FakeThermostat('Thermo1')
        settings.set_modules(['Spectro1'], ['Thermo1'], 'Spectro1', 'Thermo1')
        settings.child('record', 'integration_time').setValue(False)  # (it has its own tests)
        unit = Unit(settings, FakeManager(detector, thermo))
        unit.attach()
        recording = UnitRecording.from_unit(unit)
        measurement = make_measurement(unit, 3, phase=Phase.HOLD)
        measurement.setpoint, measurement.cycle = 40., 0

        dtes = {dte.name: dte for dte in build_dtes(recording, measurement)}

        assert recording.program and 'Unit1_Program' in dtes
        program = dtes['Unit1_Program'][0]
        assert [float(array[0]) for array in program.data] == [40., 0., PHASE_CODES[Phase.HOLD]]

    def test_the_new_phase_does_not_change_the_old_codes(self):
        assert [PHASE_CODES[phase] for phase in (Phase.IDLE, Phase.APPROACH, Phase.RAMP, Phase.HOLD_FINAL, Phase.BACK,
                                                 Phase.HOLD_INITIAL, Phase.RETURN, Phase.DONE)] == list(range(8))
        assert PHASE_CODES[Phase.HOLD] == 8


class TestWholeDataInterval:
    def test_the_whole_spectrum_is_saved_at_each_measurement_by_default(self, bench):
        bench.run()
        bench.wait_for(6)

        assert all(m.full_due for m in bench.measurements)

    def test_a_long_log_saves_it_now_and_then(self, bench):
        bench.settings.param['record', 'spectrum_interval'] = 0.35
        bench.run()
        bench.wait_for(10)

        due = [m.full_due for m in bench.measurements]
        assert due[0] is True  # (the first one has everything)
        assert 1 <= sum(due) < len(due) and sum(due) <= len(due) // 3 + 1  # (about one in four: 0.35 s every 0.1 s)

    def test_the_interval_starts_again_with_each_log(self, bench):
        bench.settings.param['record', 'spectrum_interval'] = 1000.
        bench.run()
        bench.wait_for(3)
        assert [m.full_due for m in bench.measurements[:3]] == [True, False, False]
        bench.unit.end_run()
        bench.unit.begin_run(time.time(), 0.0)
        bench.measurements.clear()
        bench.wait_for(1)

        assert bench.measurements[0].full_due is True

    def test_the_image_of_a_camera_follows_its_interval(self, qtbot):
        bench = Bench(qtbot, camera=True)
        bench.settings.param['record', 'image_interval'] = 1000.
        bench.run()
        bench.wait_for(4)

        assert [m.full_due for m in bench.measurements[:4]] == [True, False, False, False]
        bench.unit._release()

    def test_the_whole_spectrum_is_left_out_of_a_measurement_which_is_not_due(self, qapp):
        settings = UnitSettings(Parameter.create(**unit_definition(1, default_rois(1))), 1)
        detector, thermo = FakeDetector('Spectro1'), FakeThermostat('Thermo1')
        settings.set_modules(['Spectro1'], ['Thermo1'], 'Spectro1', 'Thermo1')
        settings.child('record', 'integration_time').setValue(False)  # (it has its own tests)
        unit = Unit(settings, FakeManager(detector, thermo))
        unit.attach()
        recording = UnitRecording.from_unit(unit)

        due = make_measurement(unit, 0)
        not_due = make_measurement(unit, 1)
        not_due.full_due = False

        assert [dte.name for dte in build_dtes(recording, due)] == ['Unit1_Spectrum', 'Unit1_ROIs', 'Unit1_Temperature',
                                                                    'Unit1_Program']
        assert [dte.name for dte in build_dtes(recording, not_due)] == ['Unit1_ROIs', 'Unit1_Temperature',
                                                                       'Unit1_Program']


@pytest.mark.errors_expected  # (a module which does not answer is an error of the log)
class TestSilence:
    """A log which holds a temperature goes on when a module is silent, and says when it answers again"""

    def test_a_silent_thermostat_is_told_without_the_words_of_a_ramp(self, bench):
        bench.run()
        bench.wait_for(2)
        bench.unit.temperature.time -= 100.

        found = bench.unit.silent_for(time.monotonic(), 30.)

        assert found[0] == Problem.THERMOSTAT_SILENT and 'the log goes on' in found[1] and 'brought back' not in found[1]

    def test_a_silent_spectrometer_does_not_stop_a_log_which_holds(self, bench):
        bench.run()
        bench.wait_for(2)

        bench.unit.fail(Problem.SPECTROMETER, 'No spectrum', halt=False)

        assert not bench.unit.failed and bench.unit._timer.isActive()

    def test_a_closed_spectrometer_does_stop_the_readouts(self, bench):
        bench.run()
        bench.wait_for(2)

        bench.unit.fail(Problem.SPECTROMETER, 'closed')

        assert bench.unit.failed and not bench.unit._timer.isActive()

    def test_the_data_coming_back_are_told_and_a_new_silence_is_told_again(self, bench):
        bench.run()
        bench.wait_for(2)
        bench.unit.fail(Problem.SPECTROMETER, 'No spectrum', halt=False)
        bench.notices.clear()

        bench.qtbot.waitUntil(lambda: any('delivers data again' in notice for notice in bench.notices), timeout=5000)

        assert Problem.SPECTROMETER not in bench.unit._reported
        bench.unit.fail(Problem.SPECTROMETER, 'No spectrum again', halt=False)
        assert [kind for kind, _ in bench.problems].count(Problem.SPECTROMETER) == 2

    def test_a_thermostat_which_answers_again_is_told(self, bench):
        bench.run()
        bench.wait_for(2)
        bench.unit.fail(Problem.THERMOSTAT_SILENT, 'No temperature', halt=False)
        bench.notices.clear()

        bench.thermostat.get_actuator_value()
        bench.qtbot.waitUntil(lambda: any('gives its temperature again' in notice for notice in bench.notices),
                              timeout=5000)

        assert Problem.THERMOSTAT_SILENT not in bench.unit._reported

    def test_a_ramp_is_still_brought_back_when_its_spectrometer_is_silent(self, qtbot):
        bench = Bench(qtbot, control=CONTROL_RAMP)
        bench.settings.param['ramp', 'initial'] = 20.
        bench.settings.param['ramp', 'final'] = 23.
        bench.settings.refresh_ramp()
        bench.run()
        bench.wait_for(2)

        bench.unit.fail(Problem.SPECTROMETER, 'No spectrum')

        assert bench.unit.failed and bench.unit.last_sent == 20.  # (the set-point goes back to the initial temperature)
        assert any('returning to the initial temperature' in notice for notice in bench.notices)
        bench.unit._release()
