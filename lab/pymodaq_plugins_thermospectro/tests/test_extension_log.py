"""The extension with Mock modules: a thermal controller held at one temperature, logs of unlimited or given duration,
the whole spectrum (image) saved now and then"""
import pytest

from pymodaq_plugins_thermospectro.ramp import PHASE_CODES, Phase
from pymodaq_plugins_thermospectro.unit import UnitState
from pymodaq_plugins_thermospectro.unit_settings import CONTROL_HOLD, CONTROL_RAMP

from test_extension import H5Experiment, end_run, run, spy_setpoints, ui_enabled, wait_for, wait_for_end

pytestmark = pytest.mark.filterwarnings('ignore')

LOG_TIMED = 'For a given time'
UNTIL_STOPPED = 'Until I stop it'


def hold(ext, temperatures=(2.0, 3.0), numbers=(1, 2)):
    for number, temperature in zip(numbers, temperatures):
        ramp = ext.unit(number).settings.child('ramp')
        ramp['control'] = CONTROL_HOLD
        ramp['hold_temperature'] = temperature
        ext.unit(number).settings.refresh_ramp()


def log_for(ext, seconds):
    ext.settings['experiment', 'log_for'] = LOG_TIMED
    ext.settings['experiment', 'hours'] = seconds / 3600.


class TestHoldingATemperature:
    def test_the_temperature_is_set_once_and_the_log_goes_on_until_stop(self, ext, qtbot, tmp_path):
        hold(ext)
        sent = spy_setpoints(ext)

        run(qtbot, ext)
        qtbot.wait(2500)

        assert ext.run_active and all(unit.state == UnitState.RUNNING for unit in ext.units[:2])
        assert sent[1] == [2.0] and sent[2] == [3.0]  # (once: the controller holds it by itself)
        assert ext.unit(1).hold_target == 2.0 and 'held at 2' in ext.unit(1).status_text()
        end_run(qtbot, ext)

        assert sent[1] == [2.0] and sent[2] == [3.0]  # (and it is left there: nothing brings it back)
        with H5Experiment(tmp_path) as saved:
            assert saved.zero_d('Unit1_ROIs', 0).size >= 15
            setpoint, cycle, phase = (saved.zero_d('Unit1_Program', index) for index in range(3))
            assert set(setpoint.tolist()) == {2.0} and set(cycle.tolist()) == {0.0}
            assert set(phase.tolist()) == {float(PHASE_CODES[Phase.HOLD])}
            assert set(saved.zero_d('Unit2_Program', 0).tolist()) == {3.0}
            assert saved.zero_d('Unit1_Temperature').size == saved.zero_d('Unit1_ROIs', 0).size

    def test_stop_ends_a_held_log_at_once(self, ext, qtbot):
        hold(ext)
        run(qtbot, ext)
        qtbot.wait(800)

        ext.stop_run()

        assert not ext.run_active  # (no way back to wait for)
        assert ext.get_action('start').isEnabled()

    def test_the_settings_of_a_held_controller_cannot_be_changed_during_the_log(self, ext, qtbot):
        hold(ext)
        run(qtbot, ext)

        assert not ui_enabled(ext.unit(1).settings.child('ramp', 'hold_temperature'))
        assert not ui_enabled(ext.settings.child('experiment', 'log_for'))
        assert not ui_enabled(ext.settings.child('experiment', 'hours'))
        end_run(qtbot, ext)
        assert ui_enabled(ext.unit(1).settings.child('ramp', 'hold_temperature'))

    def test_a_hold_and_a_ramp_in_two_units(self, ext, qtbot, tmp_path):
        """The experiment is not over when the ramp is: the other unit holds its temperature until Stop"""
        hold(ext, (2.0,), numbers=(2,))
        ext.unit(1).settings.child('ramp', 'control').setValue(CONTROL_RAMP)
        sent = spy_setpoints(ext)
        run(qtbot, ext)

        wait_for(qtbot, lambda: ext.unit(1).program_over, timeout=20000)
        qtbot.wait(1500)

        assert ext.run_active and sent[2] == [2.0]
        assert ext.unit(1).state == UnitState.RUNNING
        end_run(qtbot, ext)

    def test_a_ramp_alone_still_ends_when_it_is_over(self, ext, qtbot):
        ext.unit(1).settings.child('ramp', 'control').setValue(CONTROL_RAMP)
        ext.unit(2).settings.child('enabled').setValue(False)

        run(qtbot, ext)

        wait_for_end(qtbot, ext)
        assert not ext.run_active

    @pytest.mark.errors_expected
    def test_a_held_unit_whose_thermostat_goes_silent_is_told_and_the_log_goes_on(self, ext, qtbot):
        hold(ext)
        ext.settings['experiment', 'watchdog'] = 1.0
        run(qtbot, ext)
        qtbot.wait(500)
        unit = ext.unit(1)
        unit._disconnect(unit.thermostat)  # only its temperature stops coming
        unit.temperature.time -= 1000.

        wait_for(qtbot, lambda: any('No temperature from Thermo1' in text for _, text, _ in ext.notifications),
                 timeout=15000)
        qtbot.wait(500)

        text = [text for _, text, _ in ext.notifications if 'No temperature from Thermo1' in text][0]
        assert 'the log goes on' in text and 'brought back' not in text
        assert ext.run_active and unit.state == UnitState.RUNNING and not unit.failed
        end_run(qtbot, ext)

    @pytest.mark.errors_expected
    def test_a_held_unit_whose_spectrometer_goes_silent_is_told_and_the_log_goes_on(self, ext, qtbot):
        hold(ext)
        ext.settings['experiment', 'watchdog'] = 1.0
        run(qtbot, ext)
        qtbot.wait(500)
        unit = ext.unit(1)
        unit.detector.command_hardware.disconnect()  # the spectrometer stops answering

        wait_for(qtbot, lambda: any('No spectrum from Spectro1' in text for _, text, _ in ext.notifications),
                 timeout=15000)
        qtbot.wait(500)

        assert ext.run_active and not unit.failed and unit._timer.isActive()  # (it keeps asking: the data may come back)
        assert not ext.unit(2).failed
        end_run(qtbot, ext)


class TestHowLongTheLogLasts:
    def test_it_lasts_until_stop_by_default(self, ext, qtbot):
        assert ext.settings['experiment', 'log_for'] == UNTIL_STOPPED and ext.log_duration() is None
        hold(ext)
        run(qtbot, ext)
        qtbot.wait(2500)

        assert ext.run_active
        assert ext.settings['experiment', 'remaining'] == 'until Stop'
        end_run(qtbot, ext)

    def test_a_log_for_a_given_time_stops_by_itself(self, ext, qtbot, tmp_path):
        hold(ext)
        log_for(ext, 3.0)
        sent = spy_setpoints(ext)

        run(qtbot, ext)
        assert ext.log_duration() == pytest.approx(3.0)
        wait_for_end(qtbot, ext)

        assert ext.notifications == [] or all(not error for _, _, error in ext.notifications)
        assert sent[1] == [2.0]  # (the controller stays at its temperature)
        with H5Experiment(tmp_path) as saved:
            times = saved.time('Unit1_ROIs')
            assert 2.5 <= times[-1] - times[0] <= 6.0  # (3 s, to the readout)

    def test_the_time_left_is_shown(self, ext, qtbot):
        hold(ext)
        log_for(ext, 3600.)
        run(qtbot, ext)
        qtbot.wait(700)

        assert ext.settings['experiment', 'remaining'].endswith('left') and '59:5' in ext.settings['experiment', 'remaining']
        end_run(qtbot, ext)

    def test_the_duration_can_be_days(self, ext):
        log_for(ext, 48 * 3600.)

        assert ext.log_duration() == pytest.approx(48 * 3600.)

    def test_a_ramp_which_is_cut_by_the_end_of_the_log_is_brought_back(self, ext, qtbot):
        ext.unit(2).settings.child('enabled').setValue(False)
        ext.unit(1).settings.child('ramp', 'control').setValue(CONTROL_RAMP)
        log_for(ext, 3.0)
        sent = spy_setpoints(ext)

        run(qtbot, ext, velocity=30.)  # (0.5 per second: the ramp to 3 takes 6 s)
        wait_for_end(qtbot, ext, timeout=60000)

        assert max(sent[1]) < 3.0 and sent[1][-1] == 0.0  # (back to the initial value, from where it was)

    def test_the_duration_is_asked_once(self, ext, qtbot):
        hold(ext)
        log_for(ext, 1.0)
        run(qtbot, ext)
        wait_for_end(qtbot, ext)

        assert ext._duration_stop_asked is True
        hold(ext)  # (a new log: the flag is reset)
        log_for(ext, 1.0)
        run(qtbot, ext)
        assert ext._duration_stop_asked is False
        end_run(qtbot, ext)


class TestTheWholeSpectrumNowAndThen:
    def test_the_spectrum_is_saved_every_so_often_and_the_rois_at_each_readout(self, ext, qtbot, tmp_path):
        hold(ext)
        ext.unit(1).settings.param['record', 'spectrum_interval'] = 0.6
        run(qtbot, ext)
        qtbot.wait(3500)
        end_run(qtbot, ext)

        with H5Experiment(tmp_path) as saved:
            rois, spectra = saved.time('Unit1_ROIs'), saved.time('Unit1_Spectrum')
            assert spectra.size >= 3 and spectra.size < rois.size / 3  # (about one readout in six)
            assert spectra[0] == rois[0]  # (the first one has everything)
            assert set(spectra.tolist()) <= set(rois.tolist())  # (a spectrum is saved with its readout)
            assert saved.spectra('Unit1_Spectrum').shape[0] == spectra.size
            assert saved.time('Unit2_Spectrum').size == saved.time('Unit2_ROIs').size  # (the other unit: each readout)


class TestATemperatureTheControllerRefuses:
    def limits(self, ext, low=-30., high=105., is_bounds=False):
        thermostat = ext.unit(1).thermostat
        bounds = thermostat.settings.child(thermostat._hw_settings_name, 'bounds')
        bounds.child('is_bounds').setValue(is_bounds)
        bounds.child('min_bound').setValue(low)
        bounds.child('max_bound').setValue(high)

    def test_a_held_temperature_out_of_the_range_is_refused_at_start(self, ext, qtbot):
        self.limits(ext)  # (what QNW_TC writes at its initialization: the range of the holder)
        hold(ext, (200., 3.))

        ext.start_run()

        assert not ext.run_active
        title, text, _ = ext.notifications[-1]
        assert title == 'Cannot start' and 'the temperature to hold (200 °C) is out of the range of Thermo1: -30 to 105' in text

    def test_the_live_view_is_not_refused_for_it(self, ext, qtbot):
        self.limits(ext)
        hold(ext, (200., 3.))

        assert ext.start_monitoring()
        ext.stop_monitoring()

    def test_a_ramp_out_of_the_range_is_refused_too(self, ext):
        self.limits(ext, 0., 2.5, is_bounds=True)  # (the ramp goes to 3)
        ext.unit(1).settings.child('ramp', 'control').setValue(CONTROL_RAMP)

        ext.start_run()

        assert not ext.run_active and 'the final temperature of the ramp (3 °C)' in ext.notifications[-1][1]

    def test_a_temperature_in_the_range_runs(self, ext, qtbot):
        self.limits(ext, 0., 105.)
        hold(ext)

        run(qtbot, ext)

        assert ext.run_active
        end_run(qtbot, ext)

    def test_an_actuator_which_was_never_given_bounds_is_not_checked(self, ext, qtbot):
        self.limits(ext, 0., 1.)  # (the defaults of every actuator: bounds off)
        hold(ext, (200., 3.))

        run(qtbot, ext)

        assert ext.run_active
        end_run(qtbot, ext)
