from types import SimpleNamespace

import numpy as np
import pytest

from pymodaq_gui.parameter import Parameter

from pymodaq_plugins_thermospectro.spectrometers import (find_integration_params, set_integration_time,
                                                         wavelength_axis)


def module(children):
    """What the extension sees of a DAQ_Viewer: its settings"""
    return SimpleNamespace(title='Spectro',
                           settings=Parameter.create(name='settings', type='group', children=children))


def seabreeze_like(**opts):
    # as in the Seabreeze plugin: a float in ms, limits set when the spectrometer is initialized
    return module([
        {'name': 'main_settings', 'type': 'group', 'children': [{'name': 'wait_time', 'type': 'int', 'value': 0}]},
        {'name': 'detector_settings', 'type': 'group', 'children': [
            {'name': 'device', 'type': 'str', 'value': 'FAKE'},
            dict(name='integration', type='float', value=1.0, **opts)]}])


class TestIntegrationTime:
    def test_the_setting_is_found_whatever_the_plugin(self):
        assert [p.name() for p in find_integration_params(seabreeze_like())] == ['integration']
        omnidriver = module([{'name': 'spectrometers', 'type': 'group', 'children': [
            {'name': 'spectro0', 'type': 'group', 'children': [
                {'name': 'exposure_time', 'type': 'int', 'value': 100}]},
            {'name': 'spectro1', 'type': 'group', 'children': [
                {'name': 'exposure_time', 'type': 'int', 'value': 100}]}]}])
        assert len(find_integration_params(omnidriver)) == 2

    def test_a_module_without_integration_time_is_left_alone(self):
        mock = module([{'name': 'rolling', 'type': 'int', 'value': 0}])

        assert find_integration_params(mock) == []
        assert set_integration_time(mock, 500.) is None

    def test_settings_of_another_type_are_ignored(self):
        mock = module([{'name': 'integration', 'type': 'list', 'limits': ['a', 'b'], 'value': 'a'}])

        assert set_integration_time(mock, 500.) is None

    def test_500_ms(self):
        spectro = seabreeze_like()

        assert set_integration_time(spectro, 500.) == 500.
        assert spectro.settings['detector_settings', 'integration'] == 500.

    def test_what_the_instrument_does_not_accept_is_clamped(self):
        spectro = seabreeze_like()
        spectro.settings.child('detector_settings', 'integration').setLimits([3.8, 100.])

        assert set_integration_time(spectro, 500.) == 100.
        assert set_integration_time(spectro, 1.) == 3.8

    def test_limits_given_as_min_and_max(self):
        spectro = seabreeze_like(min=2., max=60.)

        assert set_integration_time(spectro, 500.) == 60.

    def test_integers_are_rounded(self):
        omnidriver = module([{'name': 'exposure_time', 'type': 'int', 'value': 100}])

        assert set_integration_time(omnidriver, 499.6) == 500
        assert isinstance(omnidriver.settings['exposure_time'], int)

    def test_every_spectrometer_of_a_module_is_set(self):
        omnidriver = module([
            {'name': 'a', 'type': 'group', 'children': [{'name': 'exposure_time', 'type': 'int', 'value': 1}]},
            {'name': 'b', 'type': 'group', 'children': [{'name': 'exposure_time', 'type': 'int', 'value': 1}]}])

        set_integration_time(omnidriver, 500.)

        assert [p.value() for p in find_integration_params(omnidriver)] == [500, 500]


class TestWavelengthAxis:
    def test_nm_is_kept(self):
        values, units = wavelength_axis(np.array([500., 600.]), 'nm')

        assert units == 'nm'
        assert values.tolist() == [500., 600.]

    @pytest.mark.parametrize('units, values, expected', [
        ('m', [5e-7, 6e-7], [500., 600.]),
        ('µm', [0.5, 0.6], [500., 600.]),
        ('Å', [5000., 6000.], [500., 600.]),
    ])
    def test_lengths_are_given_in_nm(self, units, values, expected):
        converted, new_units = wavelength_axis(np.array(values), units)

        assert new_units == 'nm'
        assert converted == pytest.approx(expected)

    @pytest.mark.parametrize('units', ['', 'pixel', 'cm-1', 'eV', 'not a unit'])
    def test_other_axes_are_kept(self, units):
        values, new_units = wavelength_axis(np.array([1., 2.]), units)

        assert new_units == units
        assert values.tolist() == [1., 2.]
