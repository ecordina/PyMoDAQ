"""The extension with a camera: a camera and a thermostat, alone or with a spectrometer which shares the thermostat

The Dashboard has Mock modules: *Camera1* (a Mock 2D detector, 100 x 200 pixels) and, for the first class, the two Mock
spectrometers and the two Mock thermostats of the other tests.
"""
import json

import numpy as np
import pytest

from pymodaq_plugins_thermospectro.camera import RectRoi, default_rect_rois, fit_to_image
from pymodaq_plugins_thermospectro.rois import RoiStore
from pymodaq_plugins_thermospectro.unit import CameraUnit, UnitState
from pymodaq_plugins_thermospectro.unit_settings import CameraSettings
from pymodaq_plugins_thermospectro.views import CameraView

from test_extension import (H5Experiment, end_run, run, spy_setpoints, ui_enabled, wait_for, wait_for_end)

pytestmark = pytest.mark.filterwarnings('ignore')

CAMERA = 3


def camera_ready(qtbot, ext):
    ext.start_monitoring()
    wait_for(qtbot, lambda: ext.unit(CAMERA).spectrum is not None and ext.unit(CAMERA).temperature is not None)


@pytest.mark.parametrize('dashboard', ['camera'], indirect=True)
class TestCameraWithSpectrometers:
    def test_the_camera_is_the_third_unit(self, ext):
        unit = ext.unit(CAMERA)

        assert isinstance(unit, CameraUnit) and isinstance(unit.settings, CameraSettings)
        assert isinstance(ext.view(CAMERA), CameraView)
        assert [type(other) for other in ext.units[:2]] == [type(ext.unit(1)), type(ext.unit(2))] and \
            not isinstance(ext.unit(1), CameraUnit)

    def test_the_modules_of_the_dashboard_are_selected(self, ext):
        assert [(u.settings.spectrometer, u.settings.thermostat) for u in ext.units] == [
            ('Spectro1', 'Thermo1'), ('Spectro2', 'Thermo2'), ('Camera1', 'Thermo1')]
        assert all(unit.settings.enabled for unit in ext.units)

    def test_only_2d_detectors_are_offered_as_cameras(self, ext):
        assert ext.unit(CAMERA).settings.child('camera').opts['limits'] == ['None', 'Camera1']
        assert 'Camera1' not in ext.unit(1).settings.child('spectrometer').opts['limits']

    def test_the_camera_shares_the_thermostat_of_the_first_spectrometer_without_driving_it(self, ext):
        ext._modules_offered = False  # as when the extension starts
        ext._offer_modules()

        assert ext.unit(CAMERA).settings.thermostat == ext.unit(1).settings.thermostat == 'Thermo1'
        assert ext.unit(1).settings.run_ramp and not ext.unit(CAMERA).settings.run_ramp
        assert not ext.unit(CAMERA).has_program

    def test_the_windows_of_the_camera_are_there(self, ext):
        assert ext.dock_visible('spectrum3') and ext.dock_visible('rois3')
        assert not ext.view(CAMERA).manager_box.isHidden()

    def test_the_windows_of_the_camera_close_and_open_again(self, ext):
        ext.set_dock_visible('rois3', False)
        assert not ext.dock_visible('rois3')

        ext.set_dock_visible('rois3', True)

        assert ext.dock_visible('rois3')

    def test_the_camera_is_read_out_in_the_live_view(self, ext, qtbot):
        camera_ready(qtbot, ext)

        frame = ext.unit(CAMERA).spectrum
        assert frame.shape == (200, 100) and ext.unit(CAMERA).axis_units == 'pixel'
        wait_for(qtbot, lambda: len(ext.unit(CAMERA).series_rois) >= 3)
        ext.view(CAMERA).refresh()
        assert ext.view(CAMERA).spectrum_plot.shape == (200, 100)
        assert len(ext.view(CAMERA).curves_plot.curves) == 3 and len(ext.view(CAMERA).curves_plot.secondary_curves) == 2
        ext.stop_monitoring()

    def test_modules_cannot_be_changed_while_looking(self, ext):
        ext.start_monitoring()

        assert not ext.unit(CAMERA).settings.child('camera').opts['enabled']
        assert ext.unit(CAMERA).settings.child('integration_time').opts['enabled']
        ext.stop_monitoring()
        assert ext.unit(CAMERA).settings.child('camera').opts['enabled']

    def test_the_default_rois_are_fitted_to_the_size_of_the_image(self, ext, qtbot):
        ext.unit(CAMERA).settings.set_rois(default_rect_rois(CAMERA))
        ext._rois_changed(ext.unit(CAMERA), reset=True)

        camera_ready(qtbot, ext)

        wait_for(qtbot, lambda: ext.unit(CAMERA).settings.rois == fit_to_image(default_rect_rois(CAMERA), (200, 100)))
        ext.stop_monitoring()

    def test_rois_which_were_set_are_not_fitted(self, ext, qtbot):
        before = ext.unit(CAMERA).settings.rois

        camera_ready(qtbot, ext)

        assert ext.unit(CAMERA).settings.rois == before
        ext.stop_monitoring()

    def test_two_units_cannot_drive_one_thermostat(self, ext):
        ext.unit(CAMERA).settings.child('ramp', 'run').setValue(True)

        ext.start_monitoring()

        assert 'would both drive the thermostat Thermo1' in ext.notifications[-1][1]
        assert 'Spectrometer 1 and the Camera' in ext.notifications[-1][1]
        assert not ext.monitoring

    def test_the_camera_and_a_spectrometer_are_not_the_same_module(self, ext):
        ext.unit(1).settings.child('spectrometer').setLimits(['None', 'Spectro1', 'Camera1'])
        ext.unit(1).settings.child('spectrometer').setValue('Camera1')
        ext.unit(1).attach()

        ext.start_monitoring()

        assert 'are the same module' in ext.notifications[-1][1]

    def test_a_camera_and_a_spectrometer_record_the_same_thermostat(self, ext, qtbot, tmp_path):
        ext.unit(2).settings.child('enabled').setValue(False)
        sent = spy_setpoints(ext)

        run(qtbot, ext)
        wait_for(qtbot, lambda: len(ext.unit(CAMERA).series_rois) >= 3)
        wait_for_end(qtbot, ext)

        assert sent[CAMERA] == [] and len(sent[1]) > 3  # (only the spectrometer drives the thermostat)
        with H5Experiment(tmp_path) as experiment:
            assert experiment.titles == ['Unit1_Program', 'Unit1_ROIs', 'Unit1_Spectrum', 'Unit1_Temperature',
                                         'Unit3_ROIs', 'Unit3_Temperature']
            assert experiment.n_zero_d('Unit3_ROIs') == 3
            assert len(experiment.zero_d('Unit3_ROIs', 0)) == len(experiment.time('Unit3_ROIs')) >= 3
            assert experiment.attribute('Unit3_ROIs', 'camera') == 'Camera1'
            assert experiment.attribute('Unit3_ROIs', 'thermostat') == 'Thermo1'
            rois = json.loads(experiment.attribute('Unit3_ROIs', 'rois'))
            assert rois[0] == {'name': 'ROI 1', 'x0': 10.0, 'x1': 18.0, 'y0': 5.0, 'y1': 13.0, 'aggregation': 'mean'}
            assert experiment.label('Unit3_ROIs', 0).startswith('ROI 1 mean (x 10-18, y 5-13')
            # the thermostat followed the ramp of the spectrometer: the camera saw it
            temperature = experiment.zero_d('Unit3_Temperature')
            assert temperature.max() > 1.0

    def test_the_image_can_be_recorded(self, ext, qtbot, tmp_path):
        ext.unit(2).settings.child('enabled').setValue(False)
        ext.unit(1).settings.child('enabled').setValue(False)
        ext.unit(CAMERA).settings.child('record', 'image').setValue(True)
        ext.unit(CAMERA).settings.child('record', 'image_interval').setValue(0.)  # (an image at each readout)
        ext.unit(CAMERA).settings.child('ramp', 'run').setValue(True)

        run(qtbot, ext)
        wait_for(qtbot, lambda: len(ext.unit(CAMERA).series_rois) >= 3)
        end_run(qtbot, ext)

        with H5Experiment(tmp_path) as experiment:
            assert 'Unit3_Image' in experiment.titles
            images = experiment.modules['Unit3_Image']['Data2D']['CH00']['EnlData00'][:]
            assert images.ndim == 3 and images.shape[1:] == (200, 100) and images.shape[0] >= 2
            assert experiment.attribute('Unit3_Image', 'camera') == 'Camera1'

    def test_the_rois_cannot_be_changed_during_an_experiment(self, ext, qtbot):
        ext.unit(2).settings.child('enabled').setValue(False)
        run(qtbot, ext)

        assert not ui_enabled(ext.unit(CAMERA).settings.child('rois', 'roi00', 'x0'))
        assert ui_enabled(ext.unit(CAMERA).settings.child('rois', 'roi00', 'plot'))
        with pytest.raises(RuntimeError, match='cannot be changed during an experiment'):
            ext.add_roi(CAMERA)
        ext._on_rect_edited(CAMERA, 'roi00', 1, 2, 3, 4)  # (ignored)
        assert ext.unit(CAMERA).settings.roi('roi00').x0 == 10.
        end_run(qtbot, ext)
        assert ui_enabled(ext.unit(CAMERA).settings.child('rois', 'roi00', 'x0'))


@pytest.mark.parametrize('dashboard', ['camera'], indirect=True)
class TestCameraRois:
    def test_a_roi_is_added_in_the_middle_of_the_image(self, ext, qtbot):
        camera_ready(qtbot, ext)
        wait_for(qtbot, lambda: ext.view(CAMERA).refresh() or ext.view(CAMERA).spectrum_plot.shape is not None)

        key = ext.add_roi(CAMERA)

        roi = ext.unit(CAMERA).settings.roi(key)
        assert key == 'roi03' and roi.name == 'ROI 4'
        assert (roi.x0 + roi.x1) / 2 == pytest.approx(50., abs=1) and (roi.y0 + roi.y1) / 2 == pytest.approx(100., abs=1)
        assert ext.view(CAMERA).spectrum_plot.roi_keys[-1] == key and ext.view(CAMERA).manager.keys[-1] == key
        assert ext.unit(CAMERA).series_rois.n_columns == 4
        ext.stop_monitoring()

    def test_a_roi_is_added_where_the_user_double_clicked(self, ext):
        ext._on_add_requested(CAMERA, 70., 30.)

        roi = ext.unit(CAMERA).settings.rois[-1]
        assert len(ext.unit(CAMERA).settings.roi_keys) == 4
        assert (roi.x0 + roi.x1) / 2 == pytest.approx(70., abs=1) and (roi.y0 + roi.y1) / 2 == pytest.approx(30., abs=1)

    def test_a_spectrometer_gets_a_roi_at_a_wavelength_as_before(self, ext):
        before = len(ext.unit(1).settings.roi_keys)

        ext._on_add_requested(1, 600.)

        assert len(ext.unit(1).settings.roi_keys) == before + 1
        assert ext.unit(1).settings.rois[-1].center == pytest.approx(600., abs=1)

    def test_a_rectangle_dragged_on_the_image_changes_the_settings(self, ext):
        view = ext.view(CAMERA)
        view.rect_edited.emit(CAMERA, 'roi01', 5., 25., 6., 26.)

        assert ext.unit(CAMERA).settings.roi('roi01') == RectRoi('ROI 2', 5., 25., 6., 26.)
        assert view.spectrum_plot.roi('roi01') == pytest.approx((5., 25., 6., 26.))
        assert view.manager.row('roi01').boxes['y1'].value() == 26

    def test_the_settings_move_the_rectangle(self, ext):
        ext.unit(CAMERA).settings.child('rois', 'roi00', 'x0').setValue(2.)

        assert ext.view(CAMERA).spectrum_plot.roi('roi00')[0] == pytest.approx(2.)

    def test_a_roi_is_deleted_from_the_manager(self, ext):
        ext.view(CAMERA).delete_requested.emit(CAMERA, 'roi01')

        assert ext.unit(CAMERA).settings.roi_keys == ['roi00', 'roi02']
        assert ext.view(CAMERA).spectrum_plot.roi_keys == ['roi00', 'roi02']

    def test_the_names_and_the_boxes_of_the_manager_are_the_settings(self, ext):
        view = ext.view(CAMERA)
        view.roi_field_edited.emit(CAMERA, 'roi00', 'name', 'Spot')
        view.roi_field_edited.emit(CAMERA, 'roi00', 'record', False)

        roi = ext.unit(CAMERA).settings.roi('roi00')
        assert roi.name == 'Spot' and roi.record is False
        assert view.curves_plot.curves[0].name() == 'Spot'

    def test_the_default_rois_come_back(self, ext):
        ext.reset_rois(CAMERA)

        assert ext.unit(CAMERA).settings.rois == default_rect_rois(CAMERA)

    def test_the_rois_are_remembered_for_the_next_experiment(self, ext, tmp_path):
        ext.unit(CAMERA).settings.set_roi_edges('roi00', 1, 2, 3, 4)
        ext._rois_changed(ext.unit(CAMERA))

        ext.save_rois()

        saved = RoiStore(tmp_path / 'remembered' / 'rois.json').get(CAMERA, RectRoi)
        assert saved[0] == RectRoi('ROI 1', 1, 2, 3, 4) and len(saved) == 3
        assert RoiStore(tmp_path / 'remembered' / 'rois.json').get(1) is not None  # (the spectrometers' too)

    def test_the_settings_of_the_camera_are_exported_with_the_others(self, ext, tmp_path):
        """The settings of the extension (the ROIs, the exposure, the record boxes, the ramp) go to a file and come back"""
        from pymodaq_gui.parameter import ioxml
        settings = ext.unit(CAMERA).settings
        settings.child('rois', 'roi00', 'name').setValue('Spot')
        settings.set_roi_edges('roi01', 11, 31, 12, 32)
        settings.child('rois', 'roi02').remove()
        settings.child('record', 'image').setValue(True)
        settings.child('integration_time').setValue(77.)
        settings.child('ramp', 'final').setValue(9.)
        file = tmp_path / 'settings.xml'
        ioxml.parameter_to_xml_file(ext.settings, str(file))
        settings.child('rois', 'roi00', 'name').setValue('changed')
        settings.child('record', 'image').setValue(False)

        ext.set_settings(file)

        settings = ext.unit(CAMERA).settings
        assert [roi.name for roi in settings.rois] == ['Spot', 'ROI 2']
        assert settings.roi('roi01') == RectRoi('ROI 2', 11, 31, 12, 32)
        assert settings.record_spectrum is True and settings.integration_time == 77.
        assert settings.child('ramp', 'final').value() == 9.
        assert ext.view(CAMERA).spectrum_plot.roi_keys == ['roi00', 'roi01']
        assert ext.view(CAMERA).manager.keys == ['roi00', 'roi01']
        ext.unit(CAMERA).settings.child('rois', 'add_roi').activate()  # (the buttons still work)
        assert len(ext.unit(CAMERA).settings.roi_keys) == 3
        ext.unit(CAMERA).settings.child('rois', 'reset_rois').activate()
        assert ext.unit(CAMERA).settings.rois == default_rect_rois(CAMERA)

    def test_the_plot_buttons_of_the_camera(self, ext):
        view = ext.view(CAMERA)

        ext._on_plot_toggled(CAMERA, 'roi01', False)

        assert not ext.unit(CAMERA).settings.roi('roi01').plot
        assert view.toggles.names == ['roi00', 'roi01', 'roi02', 'temperature']


class TestWithoutCamera:
    def test_the_camera_is_not_used_when_there_is_none(self, ext):
        unit = ext.unit(CAMERA)

        assert not unit.settings.enabled and unit.settings.spectrometer == 'None'
        assert not ext.dock_visible('spectrum3') and not ext.dock_visible('rois3')
        assert not ext.view(CAMERA).manager_box.isVisibleTo(ext.mainwindow)

    def test_an_experiment_does_not_wait_for_it(self, ext, qtbot):
        run(qtbot, ext)

        assert [unit.number for unit in ext._running_units()] == [1, 2]
        end_run(qtbot, ext)


@pytest.mark.parametrize('dashboard', ['camera_only'], indirect=True)
class TestCameraAlone:
    def test_the_camera_and_its_thermostat_are_the_only_unit(self, ext):
        assert [unit.settings.enabled for unit in ext.units] == [False, False, True]
        unit = ext.unit(CAMERA)
        assert (unit.settings.spectrometer, unit.settings.thermostat) == ('Camera1', 'Thermo1')
        assert unit.settings.run_ramp and unit.has_program  # (nothing else drives the thermostat)
        assert ext.dock_visible('spectrum3') and not ext.dock_visible('spectrum1') and not ext.dock_visible('spectrum2')

    def test_a_camera_and_a_thermostat_make_an_experiment(self, ext, qtbot, tmp_path):
        sent = spy_setpoints(ext)

        run(qtbot, ext)
        assert ext.unit(CAMERA).state == UnitState.RUNNING
        wait_for_end(qtbot, ext)

        assert sent[CAMERA][0] == pytest.approx(0.0, abs=0.05) and max(sent[CAMERA]) == pytest.approx(3.0, abs=0.2)  # (a set-point every 0.1 s: 0.15 apart)
        assert sent[CAMERA][-1] == pytest.approx(0.0, abs=0.05)  # (and back)
        with H5Experiment(tmp_path) as experiment:
            assert experiment.titles == ['Unit3_Program', 'Unit3_ROIs', 'Unit3_Temperature']
            n = len(experiment.time('Unit3_ROIs'))
            assert n >= 10 and len(experiment.zero_d('Unit3_Temperature')) == n
            values = experiment.zero_d('Unit3_ROIs', 0)
            assert np.isfinite(values).all()
            assert experiment.attribute('Unit3_Program', 'unit') in (3, '3')

    def test_the_status_line_names_the_camera(self, ext, qtbot):
        run(qtbot, ext)

        wait_for(qtbot, lambda: 'Camera: cycle' in ext.unit(CAMERA).status_text())
        end_run(qtbot, ext)

    @pytest.mark.errors_expected
    def test_the_experiment_goes_back_when_the_camera_is_closed(self, ext, qtbot):
        run(qtbot, ext, velocity=30.)
        wait_for(qtbot, lambda: ext.unit(CAMERA).ramp_state.setpoint > 0.5)

        ext.unit(CAMERA).detector.init_signal.emit(False)

        assert ext.unit(CAMERA).returning and 'The camera Camera1 was closed' in ext.notifications[-1][1]
        end_run(qtbot, ext)
