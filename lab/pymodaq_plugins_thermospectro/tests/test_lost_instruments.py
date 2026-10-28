"""What the extension does when an instrument stops answering during an experiment

A spectrometer or a camera which gives nothing is closed and initialized again (*Restart after missed* readouts in a row
make an attempt, *Restart attempts* of them), then it is lost. The experiment goes on while one of them is read out, and
ends when all of them are lost, once the thermostats are back at their initial temperature. A thermostat which fails never
ends the experiment: its unit goes on recording, without the temperature.

The Dashboard has Mock modules; an instrument which is lost is a module whose readouts are never answered (a restart does
not fix it), a thermostat which crashes is one which is closed.
"""
import numpy as np
import pytest

from pymodaq_gui.utils.custom_app import WorkFlowActions

from pymodaq_plugins_thermospectro.saving import AUTO_NOTE
from pymodaq_plugins_thermospectro.unit import UnitState

from test_extension import H5Experiment, data_files, end_run, h5_path, run, spy_setpoints, wait_for, wait_for_end
from test_extension_log import hold
from test_extension_saving import notes_in

pytestmark = [pytest.mark.filterwarnings('ignore'),
              pytest.mark.errors_expected]  # (an instrument which is lost is an error of the log)

CAMERA = 3
SLOW = dict(velocity=30.)  # 0.5 per second: from 0 to 3 in 6 s, and back
VERY_SLOW = dict(velocity=3.)  # 60 s up


def quick(*units, after=1, attempts=1, pending=0.3):
    """A readout which gets nothing is given up after `pending` s, the instrument is restarted at once (`after` of them
    in a row) and lost after `attempts` restarts"""
    for unit in units:
        unit.settings.child('restart_after').setValue(after)
        unit.settings.child('restart_attempts').setValue(attempts)
        unit.pending_timeout = lambda pending=pending: pending


def mute(unit):
    """The instrument stops answering: a restart does not fix it"""
    unit.detector.snap = lambda *args, **kwargs: None


def unmute(unit):
    del unit.detector.snap


def lost(qtbot, *units, timeout=60000):
    qtbot.waitUntil(lambda: all(unit.failed for unit in units), timeout=timeout)


def said(ext):
    """What the status bar was told"""
    messages = []
    ext.status_signal.connect(messages.append)
    return messages


def measuring(qtbot, *units, n=3, timeout=20000):
    """These units get new data (the experiment goes on with them)"""
    before = {unit.number: unit.n_measurements for unit in units}
    wait_for(qtbot, lambda: all(unit.n_measurements >= before[unit.number] + n for unit in units), timeout=timeout)


def one_unit(ext):
    ext.unit(2).settings.child('enabled').setValue(False)
    return ext.unit(1)


class TestASpectrometerWithItsThermostat:
    """Thermo + spectro: the spectrometer lost ends the experiment, the thermostat crashing does not"""

    def test_the_experiment_ends_when_the_spectrometer_is_lost_the_thermostat_goes_back_first(self, ext, qtbot, tmp_path):
        unit = one_unit(ext)
        quick(unit)
        sent = spy_setpoints(ext)
        messages = said(ext)
        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: unit.ramp_state.setpoint > 0.5)

        mute(unit)
        lost(qtbot, unit)

        assert 'No spectrometer or camera is read out any more' in ext.notifications[-1][1]
        assert 'lost' in ext.notifications[-1][1] and 'restart' in ext.notifications[-1][1]
        wait_for_end(qtbot, ext, timeout=60000)
        assert sent[1][-1] == 0.  # (the thermostat is back at its initial temperature)
        assert any('every spectrometer and camera is lost' in message for message in messages)
        with H5Experiment(tmp_path) as saved:  # (what was recorded before is in the file, and the file is closed)
            assert len(saved.time('Unit1_ROIs')) >= 3

    def test_a_held_log_ends_too_the_controller_stays_where_it_is(self, ext, qtbot):
        unit = one_unit(ext)
        hold(ext)
        quick(unit)
        sent = spy_setpoints(ext)
        run(qtbot, ext)
        wait_for(qtbot, lambda: unit.n_measurements >= 3)

        mute(unit)

        wait_for_end(qtbot, ext, timeout=60000)
        assert unit.failed and sent[1] == [2.0]  # (held at its temperature: there is nothing to bring back)

    def test_a_log_without_ramp_ends_too(self, ext, qtbot):
        unit = one_unit(ext)
        unit.settings.child('ramp', 'run').setValue(False)
        quick(unit)
        run(qtbot, ext)
        wait_for(qtbot, lambda: unit.n_measurements >= 3)

        mute(unit)

        wait_for_end(qtbot, ext, timeout=60000)

    def test_the_experiment_goes_on_when_the_thermostat_is_closed(self, ext, qtbot):
        unit = one_unit(ext)
        run(qtbot, ext, **VERY_SLOW)
        wait_for(qtbot, lambda: unit.n_measurements >= 3)

        unit.thermostat.init_signal.emit(False)  # (it crashes)

        text = ext.notifications[-1][1]
        assert 'cannot be returned to its initial temperature' in text and 'The experiment goes on' in text
        assert unit.program_over and unit.thermostat_failed  # (no ramp to wait for any more...)
        measuring(qtbot, unit, n=5)
        qtbot.wait(500)
        assert ext.run_active and not unit.failed  # (... but the log goes on)
        end_run(qtbot, ext)

    def test_the_experiment_goes_on_when_the_thermostat_stops_answering(self, ext, qtbot):
        unit = one_unit(ext)
        ext.settings['experiment', 'watchdog'] = 1.0
        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: unit.ramp_state.setpoint > 0.5)

        unit._disconnect(unit.thermostat)  # (only its temperature stops coming)
        unit.temperature.time -= 5.

        qtbot.waitUntil(lambda: unit.returning, timeout=15000)
        assert 'The experiment goes on' in ext.notifications[-1][1]
        wait_for(qtbot, lambda: unit.program_over, timeout=30000)
        qtbot.wait(800)  # (it was the ramp which ended, not the log)
        assert ext.run_active and not unit.failed
        measuring(qtbot, unit, n=3)
        end_run(qtbot, ext)

    def test_a_spectrometer_which_answers_again_after_a_restart_is_not_lost(self, ext, qtbot):
        unit = one_unit(ext)
        quick(unit, attempts=5)
        run(qtbot, ext, **VERY_SLOW)
        wait_for(qtbot, lambda: unit.n_measurements >= 3)

        mute(unit)
        wait_for(qtbot, lambda: unit.n_reinits >= 1, timeout=30000)
        unmute(unit)

        measuring(qtbot, unit, n=3, timeout=60000)
        assert not unit.failed and ext.run_active and unit.state == UnitState.RUNNING
        end_run(qtbot, ext)

    def test_the_experiment_says_the_time_without_data_when_it_is_told_never_to_restart(self, ext, qtbot):
        unit = one_unit(ext)
        unit.settings.child('restart_after').setValue(0)  # (never restarted, never given up on for missed readouts)
        ext.settings['experiment', 'watchdog'] = 1.0
        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: unit.ramp_state.setpoint > 0.5)

        unit.detector.command_hardware.disconnect()  # (the spectrometer stops answering)

        qtbot.waitUntil(lambda: unit.failed, timeout=30000)  # (after the time of the experiment, not after restarts)
        assert 'No spectrum from Spectro1' in ext.notifications[-1][1] and unit.n_reinits == 0
        wait_for_end(qtbot, ext, timeout=60000)


class TestTheFileTellsWhatHappened:
    """What happens to an instrument is written in the file as an observation marked [auto], with its time"""

    def test_the_restarts_the_loss_and_the_end_are_in_the_file_with_their_time(self, ext, qtbot, tmp_path):
        unit = one_unit(ext)
        quick(unit)  # (one restart, then lost)
        run(qtbot, ext, **VERY_SLOW)
        wait_for(qtbot, lambda: unit.n_measurements >= 3)

        mute(unit)
        wait_for_end(qtbot, ext, timeout=120000)

        times, texts = notes_in(h5_path(tmp_path))
        assert len(texts) == 3 and all(text.startswith(AUTO_NOTE) for text in texts)
        assert 'closed and initialized again (restart 1 of 1' in texts[0]
        assert 'it is lost' in texts[1] and 'No spectrometer or camera is read out any more' in texts[1]
        assert 'The set-point is brought back to the initial temperature.' in texts[1]
        assert texts[2].startswith(f'{AUTO_NOTE} The experiment ends. Nothing can be read any more')
        with H5Experiment(tmp_path) as saved:
            data = saved.time('Unit1_ROIs')
        assert data[-1] < times[0] <= times[1] <= times[2]  # (the data stop, the instrument is restarted, then lost)
        assert times[2] - times[1] > 1.  # (the experiment ends when the thermostat is back, later than the loss)

    def test_the_loss_of_one_unit_says_that_the_experiment_goes_on_and_a_stop_is_not_an_incident(self, ext, qtbot,
                                                                                                   tmp_path):
        first, second = ext.unit(1), ext.unit(2)
        quick(first, attempts=0)
        run(qtbot, ext, **VERY_SLOW)
        wait_for(qtbot, lambda: first.n_measurements >= 3 and second.n_measurements >= 3)

        mute(first)
        lost(qtbot, first)
        end_run(qtbot, ext)

        texts = notes_in(h5_path(tmp_path))[1]
        assert len(texts) == 1  # (no restart: it was not restarted, no end: the user stopped it)
        assert 'The experiment goes on with Spectrometer 2 (Spectro2).' in texts[0]

    def test_the_data_coming_back_are_in_the_file(self, ext, qtbot, tmp_path):
        unit = one_unit(ext)
        quick(unit, attempts=5)
        run(qtbot, ext, **VERY_SLOW)
        wait_for(qtbot, lambda: unit.n_measurements >= 3)
        mute(unit)
        wait_for(qtbot, lambda: unit.n_reinits >= 1, timeout=30000)
        unmute(unit)
        measuring(qtbot, unit, n=3, timeout=60000)

        end_run(qtbot, ext)

        texts = notes_in(h5_path(tmp_path))[1]
        assert 'closed and initialized again' in texts[0]
        assert any('delivers spectra again after' in text for text in texts)
        assert not any('it is lost' in text for text in texts)

    def test_a_thermostat_which_crashes_is_in_the_file(self, ext, qtbot, tmp_path):
        unit = one_unit(ext)
        run(qtbot, ext, **VERY_SLOW)
        wait_for(qtbot, lambda: unit.n_measurements >= 3)

        unit.thermostat.init_signal.emit(False)
        measuring(qtbot, unit, n=3)
        end_run(qtbot, ext)

        texts = notes_in(h5_path(tmp_path))[1]
        assert len(texts) == 1 and texts[0].startswith(AUTO_NOTE)
        assert 'was closed' in texts[0] and 'cannot be returned to its initial temperature' in texts[0]
        assert 'The experiment goes on' in texts[0]

    def test_what_the_user_wrote_and_what_the_application_wrote_are_in_the_order_of_time(self, ext, qtbot, tmp_path):
        unit = one_unit(ext)
        quick(unit, attempts=0)
        run(qtbot, ext, **VERY_SLOW)
        wait_for(qtbot, lambda: unit.n_measurements >= 3)
        assert ext.add_note('I touched the fiber')

        mute(unit)
        lost(qtbot, unit)
        qtbot.wait(300)
        end_run(qtbot, ext)

        times, texts = notes_in(h5_path(tmp_path))
        assert texts[0] == 'I touched the fiber' and texts[1].startswith(AUTO_NOTE)
        assert list(times) == sorted(times)
        lines = ext.saving_panel.notes_text().splitlines()  # (and the window lists them all)
        assert len(lines) >= 2 and lines[0].endswith('I touched the fiber') and AUTO_NOTE in lines[1]

    def test_nothing_is_written_when_the_log_is_not_saved(self, ext, qtbot, tmp_path):
        unit = one_unit(ext)
        quick(unit)
        ext.get_action(WorkFlowActions.LOG).trigger()  # (Do logging: off)
        run(qtbot, ext, **VERY_SLOW)
        wait_for(qtbot, lambda: unit.n_measurements >= 3)

        mute(unit)
        wait_for_end(qtbot, ext, timeout=120000)

        assert data_files(tmp_path) == [] and ext.saving_panel.notes_text() == ''
        assert unit.failed  # (it went as before: the dialogs and the status bar tell it)


class TestInTheLiveView:
    def test_a_lost_unit_is_told_without_the_words_of_an_experiment(self, ext, qtbot):
        unit = one_unit(ext)
        quick(unit)
        ext.start_monitoring()
        wait_for(qtbot, lambda: unit.n_measurements >= 3)

        mute(unit)
        lost(qtbot, unit)

        text = ext.notifications[-1][1]
        assert 'lost' in text and 'experiment' not in text
        assert ext.monitoring and 'LOST' in unit.status_text()

    def test_an_experiment_reads_a_lost_unit_again(self, ext, qtbot):
        unit = one_unit(ext)
        quick(unit)
        ext.start_monitoring()
        wait_for(qtbot, lambda: unit.n_measurements >= 3)
        mute(unit)
        lost(qtbot, unit)
        unmute(unit)  # (the instrument is back)

        run(qtbot, ext, **VERY_SLOW)
        wait_for(qtbot, lambda: unit.n_measurements >= 3)

        assert not unit.failed and ext.run_active
        end_run(qtbot, ext)


class TestTwoSpectrometersWithTheirThermostats:
    """Two thermo + two spectro: the experiment ends when both spectrometers are lost, a thermostat never ends it"""

    def test_one_spectrometer_lost_the_other_unit_goes_on(self, ext, qtbot):
        sent = spy_setpoints(ext)
        first, second = ext.unit(1), ext.unit(2)
        quick(first)
        run(qtbot, ext, **VERY_SLOW)
        wait_for(qtbot, lambda: first.n_measurements >= 3 and second.n_measurements >= 3)

        mute(first)
        lost(qtbot, first)

        text = ext.notifications[-1][1]
        assert 'The experiment goes on with Spectrometer 2 (Spectro2)' in text and 'brought back' in text
        assert first.returning and not second.failed and not second.returning  # (its own thermostat goes back)
        assert ext._module_and_data_saver._released == ('Unit1_',)  # (SWMR does not wait for what it will not send)
        wait_for(qtbot, lambda: first.program_over, timeout=90000)
        assert sent[1][-1] == 0. and ext.run_active
        measuring(qtbot, second, n=5)
        end_run(qtbot, ext)

    def test_the_experiment_ends_when_both_spectrometers_are_lost(self, ext, qtbot, tmp_path):
        sent = spy_setpoints(ext)
        first, second = ext.unit(1), ext.unit(2)
        quick(first, second)
        run(qtbot, ext, **VERY_SLOW)
        wait_for(qtbot, lambda: first.n_measurements >= 3 and second.n_measurements >= 3)

        mute(first)
        lost(qtbot, first)
        qtbot.wait(300)
        assert ext.run_active  # (one is still read out)
        mute(second)
        lost(qtbot, second)

        assert 'No spectrometer or camera is read out any more' in ext.notifications[-1][1]
        wait_for_end(qtbot, ext, timeout=90000)
        assert sent[1][-1] == 0. and sent[2][-1] == 0.
        with H5Experiment(tmp_path) as saved:  # (the first stopped recording before the second)
            assert 3 <= len(saved.time('Unit1_ROIs')) < len(saved.time('Unit2_ROIs'))

    def test_the_second_spectrometer_lost_first_does_not_matter(self, ext, qtbot):
        first, second = ext.unit(1), ext.unit(2)
        quick(first, second)
        run(qtbot, ext, **VERY_SLOW)
        wait_for(qtbot, lambda: first.n_measurements >= 3 and second.n_measurements >= 3)

        mute(second)
        lost(qtbot, second)
        assert ext.run_active and not first.failed
        mute(first)

        wait_for_end(qtbot, ext, timeout=90000)

    def test_a_thermostat_which_crashes_does_not_stop_the_experiment(self, ext, qtbot):
        first, second = ext.unit(1), ext.unit(2)
        run(qtbot, ext, **VERY_SLOW)
        wait_for(qtbot, lambda: first.n_measurements >= 3 and second.n_measurements >= 3)

        first.thermostat.init_signal.emit(False)

        assert first.program_over and not second.program_over and ext.run_active
        measuring(qtbot, first, second, n=5)
        assert ext.run_active and not first.failed and not second.failed
        end_run(qtbot, ext)

    def test_both_thermostats_crashing_does_not_stop_the_experiment_either(self, ext, qtbot):
        first, second = ext.unit(1), ext.unit(2)
        run(qtbot, ext, **VERY_SLOW)
        wait_for(qtbot, lambda: first.n_measurements >= 3 and second.n_measurements >= 3)

        first.thermostat.init_signal.emit(False)
        second.thermostat.init_signal.emit(False)

        measuring(qtbot, first, second, n=5)
        qtbot.wait(500)
        assert ext.run_active and first.thermostat_failed and second.thermostat_failed
        assert 'The experiment goes on' in ext.notifications[-1][1]
        end_run(qtbot, ext)

    def test_a_lost_spectrometer_and_a_thermostat_which_crashes_in_the_other_unit(self, ext, qtbot):
        first, second = ext.unit(1), ext.unit(2)
        quick(first)
        run(qtbot, ext, **VERY_SLOW)
        wait_for(qtbot, lambda: first.n_measurements >= 3 and second.n_measurements >= 3)

        mute(first)
        lost(qtbot, first)
        second.thermostat.init_signal.emit(False)

        measuring(qtbot, second, n=5)  # (the second spectrometer is read out: the experiment goes on)
        assert ext.run_active
        end_run(qtbot, ext)


@pytest.mark.parametrize('dashboard', ['camera_only'], indirect=True)
class TestACameraAlone:
    """Camera only: the camera lost ends the experiment"""

    def test_the_experiment_ends_when_the_camera_is_lost(self, ext, qtbot):
        unit = ext.unit(CAMERA)
        quick(unit)
        sent = spy_setpoints(ext)
        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: unit.ramp_state.setpoint > 0.5)

        mute(unit)

        wait_for_end(qtbot, ext, timeout=90000)
        assert unit.failed and sent[CAMERA][-1] == 0.

    def test_a_camera_without_thermostat_ends_the_experiment_too(self, ext, qtbot):
        unit = ext.unit(CAMERA)
        unit.settings.child('thermostat').setValue('None')
        quick(unit)
        run(qtbot, ext)
        wait_for(qtbot, lambda: unit.n_measurements >= 3)
        assert unit.thermostat is None

        mute(unit)

        wait_for_end(qtbot, ext, timeout=90000)
        assert unit.failed

    def test_the_experiment_goes_on_when_the_thermostat_of_the_camera_crashes(self, ext, qtbot):
        unit = ext.unit(CAMERA)
        run(qtbot, ext, **VERY_SLOW)
        wait_for(qtbot, lambda: unit.n_measurements >= 3)

        unit.thermostat.init_signal.emit(False)

        measuring(qtbot, unit, n=5)
        assert ext.run_active and not unit.failed
        end_run(qtbot, ext)


@pytest.mark.parametrize('dashboard', ['camera'], indirect=True)
class TestACameraAndASpectrometerWhichShareAThermostat:
    """Camera + thermo + spectro: either one lost does not stop the experiment, both do (the camera only reads the
    temperature of the thermostat the spectrometer drives)"""

    @staticmethod
    def units(ext):
        ext.unit(2).settings.child('enabled').setValue(False)
        return ext.unit(1), ext.unit(CAMERA)

    def test_the_experiment_goes_on_when_the_camera_is_lost(self, ext, qtbot):
        spectrometer, camera = self.units(ext)
        quick(camera)
        run(qtbot, ext, **VERY_SLOW)
        wait_for(qtbot, lambda: spectrometer.n_measurements >= 3 and camera.n_measurements >= 3)

        mute(camera)
        lost(qtbot, camera)

        assert 'The experiment goes on with Spectrometer 1 (Spectro1)' in ext.notifications[-1][1]
        measuring(qtbot, spectrometer, n=5)
        assert ext.run_active and not spectrometer.failed and spectrometer.runner.mode == 'cycling'
        end_run(qtbot, ext)

    def test_the_experiment_goes_on_with_the_ramp_when_the_spectrometer_is_lost(self, ext, qtbot, tmp_path):
        spectrometer, camera = self.units(ext)
        quick(spectrometer)
        sent = spy_setpoints(ext)
        run(qtbot, ext, **VERY_SLOW)
        wait_for(qtbot, lambda: spectrometer.n_measurements >= 3 and camera.n_measurements >= 3)

        mute(spectrometer)
        lost(qtbot, spectrometer)

        text = ext.notifications[-1][1]
        assert 'The experiment goes on with Camera (Camera1)' in text and 'brought back' not in text
        assert not spectrometer.returning and spectrometer.runner.mode == 'cycling'  # (the camera reads the temperature)
        n = len(sent[1])
        measuring(qtbot, camera, n=5)
        assert ext.run_active and not camera.failed
        assert len(sent[1]) > n and sent[1][-1] > 0.05  # (the ramp is going on)
        end_run(qtbot, ext)
        with H5Experiment(tmp_path) as saved:  # (the temperature goes on being recorded by the camera)
            temperature = saved.zero_d('Unit3_Temperature')
            assert np.isfinite(temperature).all() and len(temperature) > len(saved.time('Unit1_ROIs'))

    def test_the_ramp_ends_the_experiment_when_the_camera_still_reads_it(self, ext, qtbot):
        spectrometer, camera = self.units(ext)
        quick(spectrometer)
        sent = spy_setpoints(ext)
        run(qtbot, ext, **SLOW)
        wait_for(qtbot, lambda: spectrometer.ramp_state.setpoint > 0.5)

        mute(spectrometer)

        wait_for_end(qtbot, ext, timeout=90000)  # (the ramp went to the top and back: the experiment is over)
        assert spectrometer.failed and not camera.failed
        assert max(sent[1]) > 2.5 and sent[1][-1] == 0.

    def test_the_experiment_ends_when_the_camera_and_the_spectrometer_are_lost(self, ext, qtbot, tmp_path):
        spectrometer, camera = self.units(ext)
        quick(spectrometer, camera)
        sent = spy_setpoints(ext)
        run(qtbot, ext, **VERY_SLOW)
        wait_for(qtbot, lambda: spectrometer.n_measurements >= 3 and camera.n_measurements >= 3)

        mute(spectrometer)
        lost(qtbot, spectrometer)
        assert ext.run_active and spectrometer.runner.mode == 'cycling'
        mute(camera)
        lost(qtbot, camera)

        qtbot.waitUntil(lambda: spectrometer.returning or spectrometer.program_over, timeout=10000)
        assert 'No spectrometer or camera is read out any more' in ext.notifications[-1][1]
        wait_for_end(qtbot, ext, timeout=120000)  # (nobody reads the thermostat: its ramp is brought back first)
        assert sent[1][-1] == 0.
        texts = notes_in(h5_path(tmp_path))[1]  # (and the file tells the whole story)
        assert sum('it is lost' in text for text in texts) == 2
        assert any('Its ramp goes on: another unit reads the temperature of Thermo1' in text for text in texts)
        assert any('no other unit reads the temperature any more' in text for text in texts)
        assert texts[-1].startswith(f'{AUTO_NOTE} The experiment ends.')

    def test_the_experiment_goes_on_when_the_shared_thermostat_crashes(self, ext, qtbot):
        spectrometer, camera = self.units(ext)
        run(qtbot, ext, **VERY_SLOW)
        wait_for(qtbot, lambda: spectrometer.n_measurements >= 3 and camera.n_measurements >= 3)

        spectrometer.thermostat.init_signal.emit(False)

        measuring(qtbot, spectrometer, camera, n=5)
        assert ext.run_active and spectrometer.thermostat_failed and camera.thermostat_failed
        end_run(qtbot, ext)
