import pytest

from pymodaq_gui.parameter import Parameter

from pymodaq_plugins_thermospectro.ramp import BackMode, RampMode
from pymodaq_plugins_thermospectro.rois import Roi, default_rois
from pymodaq_plugins_thermospectro.unit_settings import NONE, UnitSettings, leaves, unit_definition


@pytest.fixture
def settings(qapp):
    param = Parameter.create(**unit_definition(1, default_rois(1)))
    return UnitSettings(param, 1)


def widgets_enabled(param) -> bool:
    """Whether the user can edit a parameter: what its widgets in the tree say (a group does not disable them)"""
    widgets = [getattr(item, 'widget', None) or getattr(item, 'button', None) for item in param.items]
    return all(widget.isEnabled() for widget in widgets if widget is not None)


class TestDefaults:
    def test_the_times(self, settings):
        assert settings.integration_time == 500.
        assert settings.readout_time == 1.0

    def test_what_is_recorded_by_default(self, settings):
        assert settings.record_spectrum
        assert settings.record_temperature
        assert all(roi.record for roi in settings.rois)

    def test_what_is_plotted_by_default(self, settings):
        assert settings.plot_temperature
        assert all(roi.plot for roi in settings.rois)

    def test_six_rois_as_the_default_ones(self, settings):
        assert len(settings.roi_keys) == 6
        assert settings.roi_keys == [f'roi{i:02d}' for i in range(6)]
        assert settings.rois == default_rois(1)

    def test_nothing_is_selected_yet(self, settings):
        assert settings.spectrometer == NONE and settings.thermostat == NONE

    def test_the_ramp_is_run_for_one_cycle(self, settings):
        assert settings.run_ramp
        assert settings.param['ramp', 'n_cycles'] == 1

    def test_the_unit_is_used(self, settings):
        assert settings.enabled

    def test_an_unused_unit(self, qapp):
        param = Parameter.create(**unit_definition(2, [], enabled=False))
        unit = UnitSettings(param, 2)

        assert not unit.enabled
        assert unit.rois == [] and unit.roi_keys == []
        assert unit.param.name() == 'unit2'


class TestModules:
    def test_the_modules_of_the_dashboard_are_offered(self, settings):
        settings.set_modules(['Spectro1', 'Spectro2'], ['Therm1'], spectrometer='Spectro2', thermostat='Therm1')

        assert settings.spectrometer == 'Spectro2'
        assert settings.thermostat == 'Therm1'
        assert settings.child('spectrometer').opts['limits'] == [NONE, 'Spectro1', 'Spectro2']

    def test_the_selection_is_kept_when_it_still_exists(self, settings):
        settings.set_modules(['A', 'B'], ['T'], spectrometer='B')

        settings.set_modules(['A', 'B', 'C'], ['T', 'U'])

        assert settings.spectrometer == 'B'
        assert settings.thermostat == NONE  # (it was never selected)

    def test_a_module_which_disappeared_is_deselected(self, settings):
        settings.set_modules(['A', 'B'], ['T'], spectrometer='B', thermostat='T')

        settings.set_modules(['A'], [])

        assert settings.spectrometer == NONE and settings.thermostat == NONE

    def test_a_wanted_module_which_does_not_exist_is_ignored(self, settings):
        settings.set_modules(['A'], ['T'], spectrometer='Z', thermostat='T')

        assert settings.spectrometer == NONE and settings.thermostat == 'T'


class TestRois:
    def test_read_as_roi_objects(self, settings):
        settings.child('rois', 'roi01', 'name').setValue('peak')
        settings.child('rois', 'roi01', 'record').setValue(False)

        roi = settings.roi('roi01')

        assert roi.name == 'peak' and not roi.record and roi.plot
        assert (roi.start, roi.stop) == (default_rois(1)[1].start, default_rois(1)[1].stop)

    def test_edges_typed_in_the_wrong_order_are_sorted(self, settings):
        settings.child('rois', 'roi00', 'start').setValue(700.)
        settings.child('rois', 'roi00', 'stop').setValue(600.)

        assert (settings.roi('roi00').start, settings.roi('roi00').stop) == (600., 700.)

    def test_add_a_roi(self, settings):
        key = settings.add_roi(Roi('new', 500., 510., record=False))

        assert key == 'roi06'
        assert settings.roi_keys[-1] == 'roi06'
        assert settings.roi('roi06') == Roi('new', 500., 510., False, True)
        assert settings.child('rois', 'roi06').title() == 'new'

    def test_a_new_roi_never_takes_the_name_of_an_existing_one(self, settings):
        settings.remove_roi('roi02')

        keys = [settings.add_roi(Roi('x', 1., 2.)), settings.add_roi(Roi('y', 3., 4.))]

        assert keys == ['roi06', 'roi07']
        assert len(set(settings.roi_keys)) == len(settings.roi_keys) == 7

    def test_remove_a_roi(self, settings):
        settings.remove_roi('roi02')

        assert settings.roi_keys == ['roi00', 'roi01', 'roi03', 'roi04', 'roi05']
        assert len(settings.rois) == 5

    def test_replace_all_the_rois(self, settings):
        settings.set_rois([Roi('a', 1., 2.), Roi('b', 3., 4.)])

        assert settings.roi_keys == ['roi00', 'roi01']
        assert [roi.name for roi in settings.rois] == ['a', 'b']

    def test_no_roi_at_all(self, settings):
        settings.set_rois([])

        assert settings.rois == []
        assert settings.add_roi(Roi('a', 1., 2.)) == 'roi00'

    def test_set_the_edges(self, settings):
        settings.set_roi_edges('roi03', 610.5, 620.25)

        assert (settings.roi('roi03').start, settings.roi('roi03').stop) == (610.5, 620.25)

    def test_a_removed_roi_is_recognized(self, settings):
        group = settings.child('rois', 'roi04')

        group.remove()

        assert settings.owned_removed(group)
        assert not settings.owned_removed(settings.child('rois', 'roi00'))

    def test_the_roi_of_another_unit_is_not_recognized(self, settings, qapp):
        other = UnitSettings(Parameter.create(**unit_definition(2, default_rois(2))), 2)
        group = other.child('rois', 'roi04')
        group.remove()

        assert not settings.owned_removed(group)
        assert other.owned_removed(group)

    def test_refreshing_forgets_what_was_removed(self, settings):
        group = settings.child('rois', 'roi04')
        group.remove()
        settings.refresh_known_rois()

        assert not settings.owned_removed(group)

    def test_a_new_roi_is_as_wide_as_the_last_one(self, settings):
        last = settings.rois[-1]

        roi = settings.new_roi(700.)

        assert roi.width == pytest.approx(last.width)
        assert roi.center == pytest.approx(700.)
        assert roi.name == 'ROI 7'

    def test_a_new_roi_when_there_is_none(self, settings):
        settings.set_rois([])

        roi = settings.new_roi(700.)

        assert roi.width == 5. and roi.name == 'ROI 1'

    def test_a_new_roi_gets_a_name_which_is_free(self, settings):
        settings.child('rois', 'roi00', 'name').setValue('ROI 7')

        assert settings.new_roi(700.).name == 'ROI 8'

    def test_the_group_is_called_as_the_roi(self, settings):
        settings.child('rois', 'roi00', 'name').setValue('peak')

        settings.update_roi_title('roi00')

        assert settings.child('rois', 'roi00').title() == 'peak'


class TestRampModes:
    def ramp(self, settings, **values):
        for name, value in values.items():
            settings.child('ramp', name).setValue(value)
        settings.refresh_ramp()
        return settings.child('ramp')

    def test_the_default_ramp_is_from_25_to_35_in_five_minutes(self, settings):
        ramp = settings.child('ramp')

        assert (ramp['initial'], ramp['final'], ramp['velocity'], ramp['duration']) == (25., 35., 2., 5.)

    def test_initial_final_velocity_computes_the_duration(self, settings):
        ramp = self.ramp(settings, initial=20., final=50., velocity=3.)

        assert ramp['duration'] == pytest.approx(10.)
        assert ramp.child('duration').opts['readonly']
        assert not ramp.child('velocity').opts['readonly']
        assert not ramp.child('final').opts['readonly']

    def test_initial_final_duration_computes_the_velocity(self, settings):
        ramp = self.ramp(settings, mode=RampMode.DURATION.value, initial=20., final=50., duration=15.)

        assert ramp['velocity'] == pytest.approx(2.)
        assert ramp.child('velocity').opts['readonly']
        assert not ramp.child('duration').opts['readonly']

    def test_initial_velocity_duration_computes_the_final_value(self, settings):
        ramp = self.ramp(settings, mode=RampMode.FINAL.value, initial=20., velocity=3., duration=10.)

        assert ramp['final'] == pytest.approx(50.)
        assert ramp.child('final').opts['readonly']
        assert not ramp.child('velocity').opts['readonly'] and not ramp.child('duration').opts['readonly']

    def test_cooling(self, settings):
        ramp = self.ramp(settings, mode=RampMode.DURATION.value, initial=50., final=20., duration=10.)

        assert ramp['velocity'] == pytest.approx(-3.)

    def test_changing_the_mode_keeps_the_same_ramp(self, settings):
        self.ramp(settings, initial=20., final=50., velocity=3.)  # 10 minutes

        ramp = self.ramp(settings, mode=RampMode.FINAL.value)

        assert (ramp['initial'], ramp['final'], ramp['velocity'], ramp['duration']) == pytest.approx((20., 50., 3., 10.))
        assert ramp.child('final').opts['readonly']

    def test_an_input_edited_after_a_mode_change_moves_the_computed_one(self, settings):
        self.ramp(settings, mode=RampMode.DURATION.value, initial=20., final=50., duration=15.)

        ramp = self.ramp(settings, duration=30.)

        assert ramp['velocity'] == pytest.approx(1.)

    def test_the_hold_times_and_the_cycles_define_the_total(self, settings):
        ramp = self.ramp(settings, initial=20., final=50., velocity=3., hold_final=2., hold_initial=1., n_cycles=4)

        # 10 min up, 2 min, 10 min back (same speed), 1 min: 23 min a cycle, four times
        assert ramp['cycle_duration'] == '23:00 (ramp 10:00, way back 10:00), 4 cycles: 1:32:00'

    def test_what_is_wrong_is_said_and_the_fields_are_left_alone(self, settings):
        ramp = self.ramp(settings, initial=20., final=20., velocity=3.)

        assert ramp['cycle_duration'].startswith('invalid: ')
        assert 'differ' in ramp['cycle_duration']
        with pytest.raises(ValueError, match='differ'):
            settings.program()

    def test_a_zero_velocity_is_invalid(self, settings):
        ramp = self.ramp(settings, velocity=0.)

        assert 'velocity' in ramp['cycle_duration']

    def test_the_program(self, settings):
        self.ramp(settings, initial=20., final=50., velocity=3., hold_final=2., hold_initial=1., n_cycles=4)

        program = settings.program()

        assert (program.initial, program.final) == (20., 50.)
        assert program.rate_out == pytest.approx(0.05) and program.rate_back == pytest.approx(0.05)
        assert (program.hold_final, program.hold_initial, program.n_cycles) == (120., 60., 4)
        assert program.duration_out == pytest.approx(600.)

    def test_the_total_duration_is_given_for_a_number_of_cycles(self, settings):
        ramp = self.ramp(settings, initial=20., final=50., velocity=3., n_cycles=3)

        assert '3 cycles' in ramp['cycle_duration']

    def test_until_stopped_has_no_total(self, settings):
        ramp = self.ramp(settings, n_cycles=0)

        assert 'cycles' not in ramp['cycle_duration']
        assert settings.program().n_cycles == 0


class TestWayBack:
    def ramp(self, settings, **values):
        for name, value in values.items():
            settings.child('ramp', name).setValue(value)
        settings.refresh_ramp()
        return settings.child('ramp')

    def test_by_default_it_is_as_fast_as_the_way_there(self, settings):
        ramp = self.ramp(settings, initial=20., final=50., velocity=3.)

        assert ramp['back_mode'] == BackMode.SAME.value
        assert ramp['back_velocity'] == pytest.approx(3.)
        assert ramp['back_duration'] == pytest.approx(10.)
        assert ramp.child('back_velocity').opts['readonly'] and ramp.child('back_duration').opts['readonly']

    def test_a_given_velocity(self, settings):
        ramp = self.ramp(settings, initial=20., final=50., velocity=3., back_mode=BackMode.VELOCITY.value,
                         back_velocity=6.)

        assert ramp['back_duration'] == pytest.approx(5.)
        assert ramp.child('back_duration').opts['readonly']
        assert not ramp.child('back_velocity').opts['readonly']
        assert settings.program().rate_back == pytest.approx(0.1)

    def test_a_given_duration(self, settings):
        ramp = self.ramp(settings, initial=20., final=50., velocity=3., back_mode=BackMode.DURATION.value,
                         back_duration=5.)

        assert ramp['back_velocity'] == pytest.approx(6.)
        assert not ramp.child('back_duration').opts['readonly']
        assert settings.program().duration_back == pytest.approx(300.)

    def test_the_way_back_follows_the_ramp_when_it_is_the_same(self, settings):
        ramp = self.ramp(settings, initial=20., final=50., velocity=3.)
        ramp = self.ramp(settings, velocity=6.)

        assert ramp['back_velocity'] == pytest.approx(6.)
        assert ramp['back_duration'] == pytest.approx(5.)

    def test_an_invalid_way_back_is_said(self, settings):
        ramp = self.ramp(settings, back_mode=BackMode.VELOCITY.value, back_velocity=0.)

        assert 'way back' in ramp['cycle_duration']
        with pytest.raises(ValueError, match='way back'):
            settings.program()


class TestAxisUnits:
    def test_the_edges_of_the_rois_are_in_nm_by_default(self, settings):
        assert settings.child('rois', 'roi00', 'start').opts['suffix'] == 'nm'

    def test_they_follow_the_units_of_the_axis_of_the_spectrum(self, settings):
        settings.set_axis_units('1/cm')

        assert all(settings.child('rois', key, edge).opts['suffix'] == '1/cm'
                   for key in settings.roi_keys for edge in ('start', 'stop'))

    def test_also_for_the_rois_added_later(self, settings):
        settings.set_axis_units('')

        key = settings.add_roi(Roi('new', 1., 2.))

        assert settings.child('rois', key, 'start').opts['suffix'] == ''
        assert settings.child('rois', key, 'stop').opts['suffix'] == ''

    def test_the_values_do_not_change(self, settings):
        before = settings.rois

        settings.set_axis_units('1/cm')

        assert settings.rois == before


class TestUnits:
    def test_the_units_of_the_thermostat_are_shown(self, settings):
        settings.set_units('°C')

        assert settings.child('ramp', 'initial').opts['suffix'] == '°C'
        assert settings.child('ramp', 'velocity').opts['suffix'] == '°C/min'
        assert settings.child('ramp', 'back_velocity').opts['suffix'] == '°C/min'
        assert settings.child('status', 'temperature').opts['suffix'] == '°C'

    def test_no_units(self, settings):
        settings.set_units('')

        assert settings.child('ramp', 'velocity').opts['suffix'] == '/min'


class TestChanges:
    @pytest.mark.parametrize('path, expected', [
        (('enabled',), ('enabled', None)),
        (('spectrometer',), ('modules', 'spectrometer')),
        (('thermostat',), ('modules', 'thermostat')),
        (('integration_time',), ('integration', None)),
        (('readout_time',), ('readout', None)),
        (('record', 'spectrum'), ('record', 'spectrum')),
        (('record', 'temperature'), ('record', 'temperature')),
        (('plot_temperature',), ('plot_temperature', None)),
        (('rois', 'roi02', 'start'), ('roi_edges', 'roi02')),
        (('rois', 'roi02', 'stop'), ('roi_edges', 'roi02')),
        (('rois', 'roi02', 'name'), ('roi_name', 'roi02')),
        (('rois', 'roi02', 'record'), ('roi_record', 'roi02')),
        (('rois', 'roi02', 'plot'), ('roi_plot', 'roi02')),
        (('ramp', 'velocity'), ('ramp', 'velocity')),
        (('ramp', 'mode'), ('ramp', 'mode')),
        (('status', 'phase'), None),
    ])
    def test_classify(self, settings, path, expected):
        assert settings.classify(settings.child(*path)) == expected

    def test_what_the_program_changes_itself_is_ignored(self, settings):
        with settings.busy():
            assert settings.classify(settings.child('ramp', 'velocity')) is None
        assert settings.classify(settings.child('ramp', 'velocity')) == ('ramp', 'velocity')

    def test_computed_fields_do_not_report_themselves(self, settings):
        seen = []
        settings.param.sigTreeStateChanged.connect(
            lambda param, changes: seen.extend(settings.classify(p) for p, kind, _ in changes if kind == 'value'))

        settings.child('ramp', 'final').setValue(45.)  # the duration changes by itself
        settings.refresh_ramp()

        assert ('ramp', 'final') in seen
        assert ('ramp', 'duration') not in seen

    def test_another_unit_is_not_classified(self, settings, qapp):
        other = UnitSettings(Parameter.create(**unit_definition(2, default_rois(2))), 2)

        assert settings.classify(other.child('integration_time')) is None
        assert settings.classify(settings.param) is None

    def test_the_removal_of_a_roi_is_not_a_value(self, settings):
        group = settings.child('rois', 'roi01')
        group.remove()

        assert settings.classify(group) is None


class TestLock:
    FREE = [('plot_temperature',), ('rois', 'roi00', 'plot'), ('rois', 'roi05', 'plot')]
    LOCKED = [('integration_time',), ('readout_time',), ('spectrometer',), ('thermostat',), ('enabled',),
              ('record', 'spectrum'), ('record', 'temperature'), ('rois', 'roi00', 'start'),
              ('rois', 'roi00', 'stop'), ('rois', 'roi00', 'name'), ('rois', 'roi00', 'record'),
              ('rois', 'add_roi'), ('rois', 'reset_rois'),
              ('ramp', 'run'), ('ramp', 'mode'), ('ramp', 'initial'), ('ramp', 'take_current'), ('ramp', 'hold_final'),
              ('ramp', 'back_mode'), ('ramp', 'hold_initial'), ('ramp', 'n_cycles')]

    def test_what_would_change_the_experiment_is_locked(self, settings, qtbot):
        from pyqtgraph.parametertree import ParameterTree
        parameter_tree = ParameterTree()
        parameter_tree.setParameters(settings.param, showTop=False)
        qtbot.addWidget(parameter_tree)
        parameter_tree.show()

        settings.lock(True)

        for path in self.LOCKED:
            assert not widgets_enabled(settings.child(*path)), path
        for path in self.FREE:
            assert widgets_enabled(settings.child(*path)), path
        assert not settings.child('rois', 'roi00').opts['removable']

        settings.lock(False)

        for path in self.LOCKED + self.FREE:
            assert widgets_enabled(settings.child(*path)), path
        assert settings.child('rois', 'roi00').opts['removable']

    def test_the_computed_fields_stay_read_only(self, settings, qtbot):
        settings.lock(True)
        settings.lock(False)

        assert settings.child('ramp', 'duration').opts['readonly']
        assert settings.child('ramp', 'back_velocity').opts['readonly']

    def test_the_status_is_not_touched(self, settings):
        settings.lock(True)

        assert all(leaf.opts.get('enabled', True) for leaf in leaves(settings.child('status')))
