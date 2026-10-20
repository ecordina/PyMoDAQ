"""Edge cases of the logs: a hold and a ramp together, a controller shared by two units, a search in the settings, settings
of another application, a unit which did not answer during the live view, the thinning of long plots"""
import numpy as np
import pytest

from pymodaq_plugins_thermospectro.unit import Problem, UnitState
from pymodaq_plugins_thermospectro.unit_settings import CONTROL_HOLD, CONTROL_RAMP, END_FINAL
from pymodaq_plugins_thermospectro.views import UnitView

from test_extension import end_run, run, spy_setpoints, wait_for, wait_for_end
from test_extension_log import hold, log_for

pytestmark = pytest.mark.filterwarnings('ignore')


def visible(param) -> bool:
    return bool(param.opts.get('visible', True))


class TestALogWhichHasARampAndAHold:
    def ramp_and_hold(self, ext):
        hold(ext, (2.0,), numbers=(2,))
        ext.unit(1).settings.child('ramp', 'control').setValue(CONTROL_RAMP)

    def test_stop_ends_the_log_when_the_ramp_is_back(self, ext, qtbot):
        self.ramp_and_hold(ext)
        sent = spy_setpoints(ext)
        run(qtbot, ext, velocity=30.)  # (0.5 per second: the ramp to 3 takes 6 s)
        qtbot.wait(1500)
        assert ext.run_active and not ext.unit(1).program_over

        ext.stop_run()

        wait_for_end(qtbot, ext, timeout=30000)  # (it did not end before: the held unit kept the log going)
        assert sent[1][-1] == 0.0 and sent[2] == [2.0]  # (the ramp is back, the held controller stays where it is)

    def test_the_end_of_the_duration_ends_it_too_and_asks_nothing(self, ext, qtbot, monkeypatch):
        self.ramp_and_hold(ext)
        log_for(ext, 2.0)
        asked = []
        monkeypatch.setattr(ext, '_ask_abort_return', lambda: asked.append(True) or True)
        sent = spy_setpoints(ext)

        run(qtbot, ext, velocity=30.)
        wait_for_end(qtbot, ext, timeout=60000)

        assert asked == []  # (the ramp is brought back, nobody is asked to give up on it)
        assert sent[1][-1] == 0.0 and sent[2] == [2.0]

    def test_a_new_log_forgets_that_the_last_one_was_ended(self, ext, qtbot):
        self.ramp_and_hold(ext)
        run(qtbot, ext, velocity=30.)
        ext.stop_run()
        assert ext._ending
        wait_for_end(qtbot, ext, timeout=30000)

        run(qtbot, ext)
        assert not ext._ending
        end_run(qtbot, ext)


class TestAControllerSharedByTwoUnits:
    def test_a_ramp_and_a_hold_on_the_same_controller_are_refused(self, ext):
        ext.unit(2).settings.child('thermostat').setValue('Thermo1')
        hold(ext, (2.0,), numbers=(2,))

        assert not ext.start_monitoring()

        text = ext.notifications[-1][1]
        assert 'would both drive the thermostat Thermo1' in text and 'untick "Drive it"' in text

    def test_two_holds_on_the_same_controller_are_refused(self, ext):
        ext.unit(2).settings.child('thermostat').setValue('Thermo1')
        hold(ext)

        assert not ext.start_monitoring()
        assert 'would both drive' in ext.notifications[-1][1]


class TestTheSearchBoxOfTheSettings:
    def test_what_is_hidden_stays_hidden_when_the_search_is_cleared(self, ext):
        settings = ext.unit(1).settings
        hold(ext, (2.0,), numbers=(1,))
        ramp = settings.child('ramp')
        assert visible(ramp.child('hold_temperature')) and not visible(ramp.child('final'))

        ext._apply_filter('temperature')
        ext._apply_filter('')

        assert visible(ramp.child('hold_temperature'))
        assert not visible(ramp.child('final')) and not visible(ramp.child('velocity'))

    def test_a_unit_which_does_not_drive_its_controller_shows_none_of_the_ramp(self, ext):
        settings = ext.unit(1).settings
        settings.child('ramp', 'run').setValue(False)

        ext._apply_filter('')

        ramp = settings.child('ramp')
        assert not visible(ramp.child('control')) and not visible(ramp.child('final'))
        assert not visible(ramp.child('hold_temperature'))

    def test_an_application_without_controller_stays_without_it(self, ext):
        settings = ext.unit(1).settings
        settings.hide_unused(thermostat=False)
        assert not visible(settings.param.child('ramp')) and not visible(settings.param.child('thermostat'))

        settings.param.child('ramp').show()  # (what the search box does to everything)
        settings.param.child('status', 'setpoint').show()
        settings.hide_unused(thermostat=False)

        assert not visible(settings.param.child('ramp')) and not visible(settings.param.child('status', 'setpoint'))


class TestLoadingSettings:
    def test_settings_without_a_unit_are_not_loaded(self, ext):
        incomplete = ext.create_parameter(ext.settings)
        incomplete.child('unit2').remove()
        before = ext.settings

        ext.set_settings(incomplete)

        assert ext.settings is before and ext.settings.child('unit2') is not None
        title, text, error = ext.notifications[-1]
        assert title == 'Settings not loaded' and error and 'lacks unit2.' in text  # (the group, not each of its settings)
        assert 'could not be read' not in text
        assert ext.unit(2).settings.param is ext.settings.child('unit2')

    def test_settings_saved_before_a_setting_existed_load_and_keep_its_current_value(self, ext):
        old = ext.create_parameter(ext.settings)
        old.child('unit1', 'ramp', 'end_at').remove()  # (what a file saved before this setting has)
        old.child('unit1', 'ramp', 'hold_temperature').setValue(7.5)
        ext.unit(1).settings.child('ramp', 'end_at').setValue(END_FINAL)

        ext.set_settings(old)

        settings = ext.unit(1).settings
        assert settings.end_at == END_FINAL  # (the current value: the file had none)
        assert settings.hold_temperature == 7.5  # (the others come from the file)
        names = [child.name() for child in settings.child('ramp').children()]
        assert names.index('end_at') == names.index('n_cycles') + 1  # (in its place, not at the end)
        assert settings.param is ext.settings.child('unit1')
        assert ext.notifications == []

    def test_a_whole_missing_group_of_a_unit_is_filled_too(self, ext):
        old = ext.create_parameter(ext.settings)
        old.child('unit2', 'ramp').remove()

        ext.set_settings(old)

        assert ext.unit(2).settings.param.child('ramp').child('n_cycles') is not None
        assert ext.unit(2).settings.program().n_cycles == 1

    def test_the_settings_of_this_application_load(self, ext):
        saved = ext.create_parameter(ext.settings)
        saved.child('unit1', 'ramp', 'hold_temperature').setValue(7.5)
        saved.child('unit1', 'ramp', 'control').setValue(CONTROL_HOLD)

        ext.set_settings(saved)

        assert ext.notifications == []
        assert ext.unit(1).settings.holds and ext.unit(1).settings.hold_temperature == 7.5
        assert ext.unit(1).settings.param is ext.settings.child('unit1')

    @pytest.mark.errors_expected
    def test_a_file_which_is_not_settings_is_told(self, ext, tmp_path):
        broken = tmp_path / 'broken.xml'
        broken.write_text('<this is not xml')

        ext.set_settings(broken)

        assert ext.notifications[-1][0] == 'Settings not loaded'


class TestAUnitWhichDidNotAnswerDuringTheLiveView:
    @pytest.mark.errors_expected
    def test_an_experiment_reads_it_again(self, ext, qtbot):
        assert ext.start_monitoring()
        unit = ext.unit(1)
        wait_for(qtbot, lambda: unit.n_measurements > 0)
        unit.fail(Problem.SPECTROMETER, 'No spectrum from Spectro1')
        assert unit.failed
        qtbot.wait(400)
        count = unit.n_measurements
        qtbot.wait(400)
        assert unit.n_measurements == count  # (it is not read out any more)

        hold(ext)
        run(qtbot, ext)
        wait_for(qtbot, lambda: unit.n_measurements >= 5)

        assert not unit.failed and unit.state == UnitState.RUNNING
        end_run(qtbot, ext)


class TestLongPlots:
    def test_a_short_plot_is_not_thinned(self):
        rows = slice(10, 500)

        assert UnitView._thinned(rows) == rows

    def test_a_long_plot_has_at_most_max_points_and_its_last_row(self):
        rows = UnitView._thinned(slice(0, 5 * UnitView.MAX_POINTS + 3))

        assert len(rows) <= UnitView.MAX_POINTS + 1
        assert rows[-1] == 5 * UnitView.MAX_POINTS + 2
        assert np.all(np.diff(rows[:-1]) == 6)  # (rounded up: 5.0001 is 6)

    def test_the_points_do_not_move_when_the_plot_grows(self):
        first = UnitView._thinned(slice(0, 3 * UnitView.MAX_POINTS - 2))
        second = UnitView._thinned(slice(0, 3 * UnitView.MAX_POINTS - 1))

        assert set(first[:-1].tolist()) <= set(second.tolist())  # (the rows already drawn stay, the last one apart)

    def test_the_rows_of_a_window_are_multiples_of_the_step(self):
        rows = UnitView._thinned(slice(1001, 1001 + 4 * UnitView.MAX_POINTS))

        step = int(rows[1] - rows[0])
        assert step == 4 and all(row % step == 0 for row in rows[:-1])
