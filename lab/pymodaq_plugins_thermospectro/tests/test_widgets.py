import numpy as np
import pytest
from qtpy import QtCore

from pymodaq_plugins_thermospectro.widgets import (CurvesPlot, PALETTE, PlotToggles, SpectrumPlot, TEMPERATURE_COLOR,
                                                   roi_color)


@pytest.fixture
def plot(qtbot):
    widget = SpectrumPlot('Spectrometer 1')
    qtbot.addWidget(widget)
    widget.resize(400, 200)
    widget.show()
    x = np.linspace(400., 700., 601)  # 0.5 nm per pixel
    widget.set_spectrum(x, np.exp(-((x - 550.) / 20.) ** 2), 'nm', 'wavelength')
    return widget


class FakeClick:
    """What the plot is told of a click (a mouse click event of pyqtgraph)"""

    def __init__(self, position, double=True, button=QtCore.Qt.MouseButton.LeftButton, accepted=False):
        self._position, self._double, self._button, self._accepted = position, double, button, accepted

    def double(self):
        return self._double

    def button(self):
        return self._button

    def isAccepted(self):
        return self._accepted

    def accept(self):
        self._accepted = True

    def scenePos(self):
        return self._position

    def screenPos(self):
        return QtCore.QPointF(10., 10.)


class TestSpectrumPlot:
    def test_the_whole_spectrum_is_visible(self, plot):
        assert plot.x_range == (400., 700.)
        assert plot.getPlotItem().viewRange()[0] == pytest.approx([400., 700.])

    def test_a_window_is_highlighted_for_a_roi(self, plot):
        plot.set_roi('roi00', 549., 551., '#1f77b4', '1')

        assert plot.roi_keys == ['roi00']
        assert plot.roi('roi00') == pytest.approx((549., 551.))

    def test_a_window_is_never_narrower_than_a_pixel(self, plot):
        plot.set_roi('roi00', 550., 550., '#1f77b4', '1')

        low, high = plot.roi('roi00')
        assert high - low == pytest.approx(0.5)
        assert (low + high) / 2. == pytest.approx(550.)

    def test_the_edges_can_be_given_in_any_order(self, plot):
        plot.set_roi('roi00', 560., 540., '#1f77b4', '1')

        assert plot.roi('roi00') == pytest.approx((540., 560.))

    def test_moving_a_window_by_the_program_does_not_emit(self, plot, qtbot):
        plot.set_roi('roi00', 549., 551., '#1f77b4', '1')
        with qtbot.assertNotEmitted(plot.roi_changed):
            plot.set_roi('roi00', 599., 601., '#1f77b4', '1')

        assert plot.roi('roi00') == pytest.approx((599., 601.))

    def test_dragging_a_window_emits_its_new_edges(self, plot, qtbot):
        plot.set_roi('roi00', 549., 551., '#1f77b4', '1')
        region = plot._regions['roi00']

        with qtbot.waitSignal(plot.roi_changed) as blocker:
            region.setRegion((599., 601.))
            region.sigRegionChangeFinished.emit(region)

        assert blocker.args[0] == 'roi00'
        assert blocker.args[1:] == pytest.approx([599., 601.])

    def test_the_window_can_be_moved_and_its_edges_too(self, plot):
        plot.set_roi('roi00', 549., 551., '#1f77b4', '1')
        region = plot._regions['roi00']

        assert region.movable
        assert all(line.movable for line in region.lines)

    def test_the_windows_can_be_locked(self, plot):
        plot.set_roi('roi00', 549., 551., '#1f77b4', '1')
        plot.set_locked(True)
        assert not plot._regions['roi00'].movable
        assert not any(line.movable for line in plot._regions['roi00'].lines)

        plot.set_roi('roi01', 599., 601., '#2ca02c', '2')  # one created while locked
        assert not plot._regions['roi01'].movable

        plot.set_locked(False)
        assert plot._regions['roi00'].movable and plot._regions['roi01'].movable
        assert all(line.movable for line in plot._regions['roi00'].lines)

    def test_a_window_cannot_be_dragged_out_of_the_spectrum(self, plot):
        plot.set_roi('roi00', 549., 551., '#1f77b4', '1')
        region = plot._regions['roi00']

        region.setRegion((100., 102.))

        assert min(region.getRegion()) >= 400.

    def test_remove_roi(self, plot):
        plot.set_roi('roi00', 549., 551., '#1f77b4', '1')
        plot.set_roi('roi01', 599., 601., '#2ca02c', '2')

        plot.remove_roi('roi00')

        assert plot.roi_keys == ['roi01']
        plot.remove_roi('roi00')  # (nothing happens)

    def test_a_double_click_asks_for_a_roi_there(self, plot, qtbot):
        view = plot.getPlotItem().vb
        position = view.mapViewToScene(QtCore.QPointF(600., 0.5))

        with qtbot.waitSignal(plot.add_requested) as blocker:
            plot._on_clicked(FakeClick(position))

        assert blocker.args[0] == pytest.approx(600., abs=1.)

    def test_a_window_being_dragged_tells_where_it_is(self, plot, qtbot):
        plot.set_roi('roi00', 549., 551., '#1f77b4', '1')
        region = plot._regions['roi00']

        with qtbot.waitSignal(plot.roi_moving) as blocker:
            region.setRegion((599., 601.))  # (what the mouse does all along a drag)

        assert blocker.args[0] == 'roi00'
        assert blocker.args[1:] == pytest.approx([599., 601.])

    def test_the_program_moving_a_window_does_not_say_it_is_being_dragged(self, plot, qtbot):
        plot.set_roi('roi00', 549., 551., '#1f77b4', '1')

        with qtbot.assertNotEmitted(plot.roi_moving):
            plot.set_roi('roi00', 599., 601., '#1f77b4', '1')

    def test_the_pitch_of_the_spectrum(self, plot):
        assert plot.pitch == pytest.approx(0.5)

    def test_a_single_click_does_nothing(self, plot, qtbot):
        position = plot.getPlotItem().vb.mapViewToScene(QtCore.QPointF(600., 0.5))

        with qtbot.assertNotEmitted(plot.add_requested):
            plot._on_clicked(FakeClick(position, double=False))

    def test_a_double_click_outside_of_the_plot_does_nothing(self, plot, qtbot):
        with qtbot.assertNotEmitted(plot.add_requested):
            plot._on_clicked(FakeClick(QtCore.QPointF(-50., -50.)))

    @staticmethod
    def popup(plot):
        from qtpy import QtWidgets
        menu = QtWidgets.QApplication.activePopupWidget() or plot._menu
        return menu if isinstance(menu, QtWidgets.QMenu) and menu.isVisible() else None

    def test_a_right_click_on_the_spectrum_offers_to_add_a_roi_there(self, plot, qtbot):
        position = plot.getPlotItem().vb.mapViewToScene(QtCore.QPointF(600., 0.5))

        plot._on_clicked(FakeClick(position, double=False, button=QtCore.Qt.MouseButton.RightButton))

        menu = self.popup(plot)
        assert menu is not None and [a.text() for a in menu.actions()][0].startswith('Add a ROI here')
        with qtbot.waitSignal(plot.add_requested) as blocker:
            menu.actions()[0].trigger()
        assert blocker.args[0] == pytest.approx(600., abs=1.)

    def test_a_right_click_which_a_window_took_does_not_offer_to_add(self, plot):
        position = plot.getPlotItem().vb.mapViewToScene(QtCore.QPointF(600., 0.5))

        plot._on_clicked(FakeClick(position, double=False, button=QtCore.Qt.MouseButton.RightButton, accepted=True))

        assert plot._menu is None

    def test_a_right_click_outside_of_the_spectrum_offers_nothing(self, plot):
        plot._on_clicked(FakeClick(QtCore.QPointF(-50., -50.), double=False, button=QtCore.Qt.MouseButton.RightButton))

        assert plot._menu is None

    def test_a_right_click_on_a_window_offers_to_delete_it(self, plot, qtbot):
        plot.set_roi('roi03', 549., 551., '#1f77b4', '4')

        plot._regions['roi03'].context_requested.emit(FakeClick(QtCore.QPointF(5., 5.), double=False,
                                                                button=QtCore.Qt.MouseButton.RightButton))

        menu = self.popup(plot)
        assert menu is not None and [a.text() for a in menu.actions()] == ['Delete the ROI 4']
        with qtbot.waitSignal(plot.delete_requested) as blocker:
            menu.actions()[0].trigger()
        assert blocker.args == ['roi03']

    def test_the_windows_cannot_be_deleted_nor_added_while_locked(self, plot):
        plot.set_roi('roi03', 549., 551., '#1f77b4', '4')
        plot.set_locked(True)
        position = plot.getPlotItem().vb.mapViewToScene(QtCore.QPointF(600., 0.5))

        plot._regions['roi03'].context_requested.emit(FakeClick(position, double=False,
                                                                button=QtCore.Qt.MouseButton.RightButton))
        plot._on_clicked(FakeClick(position, double=False, button=QtCore.Qt.MouseButton.RightButton))

        assert plot._menu is None

    def test_a_real_right_click_on_a_window_reaches_it(self, plot, qtbot):
        """With the mouse events of Qt, not the stand-ins above: the window takes the click, the plot adds nothing"""
        from qtpy import QtWidgets
        plot.set_roi('roi00', 549., 551., '#1f77b4', '1')
        view = plot.getPlotItem().vb
        on_window = plot.mapFromScene(view.mapViewToScene(QtCore.QPointF(550., 0.5)))
        on_spectrum = plot.mapFromScene(view.mapViewToScene(QtCore.QPointF(650., 0.5)))

        qtbot.mouseClick(plot.viewport(), QtCore.Qt.MouseButton.RightButton, pos=on_window)
        menu = QtWidgets.QApplication.activePopupWidget()
        assert menu is not None and [a.text() for a in menu.actions()] == ['Delete the ROI 1']
        menu.close()
        qtbot.wait(20)

        qtbot.mouseClick(plot.viewport(), QtCore.Qt.MouseButton.RightButton, pos=on_spectrum)
        menu = QtWidgets.QApplication.activePopupWidget()
        assert menu is not None and [a.text() for a in menu.actions()][0].startswith('Add a ROI here')
        menu.close()

    def test_clear(self, plot):
        plot.clear_spectrum('no spectrometer')

        assert plot.x_range is None
        assert plot._message.toPlainText() == 'no spectrometer'
        assert plot._message.isVisible()

    def test_the_message_goes_away_with_the_spectrum(self, plot):
        plot.clear_spectrum()
        plot.set_spectrum(np.arange(10.), np.arange(10.), 'nm', 'wavelength')

        assert not plot._message.isVisible()


class TestPlotToggles:
    ENTRIES = [('roi00', '1  ROI 1', '#1f77b4', True), ('roi01', '2  ROI 2', '#ff7f0e', False),
               ('temperature', 'T', TEMPERATURE_COLOR, True)]

    def test_one_button_per_curve(self, qtbot):
        toggles = PlotToggles()
        qtbot.addWidget(toggles)

        toggles.set_entries(self.ENTRIES)

        assert toggles.names == ['roi00', 'roi01', 'temperature']
        assert toggles.texts == ['1  ROI 1', '2  ROI 2', 'T']
        assert toggles.button('roi00').isChecked()
        assert not toggles.button('roi01').isChecked()

    def test_clicking_a_button_tells_which_curve(self, qtbot):
        toggles = PlotToggles()
        qtbot.addWidget(toggles)
        toggles.set_entries(self.ENTRIES)

        with qtbot.waitSignal(toggles.toggled) as blocker:
            toggles.button('roi01').click()

        assert blocker.args == ['roi01', True]

        with qtbot.waitSignal(toggles.toggled) as blocker:
            toggles.button('temperature').click()

        assert blocker.args == ['temperature', False]

    def test_following_a_change_made_elsewhere_does_not_emit(self, qtbot):
        toggles = PlotToggles()
        qtbot.addWidget(toggles)
        toggles.set_entries(self.ENTRIES)

        with qtbot.assertNotEmitted(toggles.toggled):
            toggles.set_plotted('roi01', True)

        assert toggles.button('roi01').isChecked()

    def test_the_buttons_are_replaced(self, qtbot):
        toggles = PlotToggles()
        qtbot.addWidget(toggles)
        toggles.set_entries(self.ENTRIES)

        toggles.set_entries(self.ENTRIES[:1])

        assert toggles.names == ['roi00']
        toggles.set_entries([])
        assert toggles.names == []

    def test_the_old_buttons_do_not_stay_behind(self, qtbot):
        from qtpy import QtWidgets
        toggles = PlotToggles()
        qtbot.addWidget(toggles)
        toggles.show()
        toggles.set_entries(self.ENTRIES)

        toggles.set_entries(self.ENTRIES)  # (no event loop in between: the deferred deletion has not happened)

        assert len(toggles.findChildren(QtWidgets.QPushButton)) == 3


class TestCurvesPlot:
    def test_curves_and_data(self, qtbot):
        plot = CurvesPlot('time', 'min', 'ROIs', '')
        qtbot.addWidget(plot)

        plot.set_curves([('a', '#1f77b4', False), ('b', '#ff7f0e', True)])
        plot.set_data(np.arange(5.), [np.arange(5.), 2 * np.arange(5.)])

        assert len(plot.curves) == 2
        assert plot.curves[1].getData()[1].tolist() == [0., 2., 4., 6., 8.]
        assert len(plot.legend.items) == 2

    def test_the_curves_are_replaced(self, qtbot):
        plot = CurvesPlot()
        qtbot.addWidget(plot)
        plot.set_curves([('a', '#1f77b4', False), ('b', '#ff7f0e', True)])

        plot.set_curves([('c', '#2ca02c', False)])

        assert len(plot.curves) == 1
        assert len(plot.legend.items) == 1

    def test_nothing_to_plot(self, qtbot):
        plot = CurvesPlot()
        qtbot.addWidget(plot)
        plot.set_curves([])

        plot.set_data(np.arange(3.), [])

        assert plot.curves == []

    def test_there_is_no_second_scale_until_it_is_needed(self, qtbot):
        plot = CurvesPlot()
        qtbot.addWidget(plot)
        plot.show()

        assert not plot.has_secondary_scale
        assert not plot.getPlotItem().getAxis('right').isVisible()

    def test_the_temperature_has_its_own_scale_at_the_right(self, qtbot):
        plot = CurvesPlot('time', 'min', 'ROIs', '')
        qtbot.addWidget(plot)
        plot.resize(500, 300)
        plot.show()
        plot.set_curves([('ROI 1', '#1f77b4', False)])
        plot.set_secondary_label('temperature', '°C', TEMPERATURE_COLOR)
        plot.set_secondary_curves([('temperature', TEMPERATURE_COLOR, False), ('set-point', TEMPERATURE_COLOR, True)])

        t = np.arange(10.)
        plot.set_data(t, [1000. + 100. * t])  # counts
        plot.set_secondary_data(t, [20. + t, 20. + t])  # degrees: a very different scale
        qtbot.wait(100)  # (the main scale fits its data when the events are processed)

        assert plot.has_secondary_scale
        assert len(plot.secondary_curves) == 2
        left = plot.getPlotItem().vb.viewRange()[1]
        right = plot._right.viewRange()[1]
        assert left[0] < 1000. and left[1] > 1900.
        assert 15. < right[0] < 21. and 28. < right[1] < 35.
        assert [item[1].text for item in plot.legend.items] == ['ROI 1', 'temperature', 'set-point']

    def test_the_two_scales_share_the_time(self, qtbot):
        plot = CurvesPlot()
        qtbot.addWidget(plot)
        plot.resize(500, 300)
        plot.show()
        plot.set_curves([('a', '#1f77b4', False)])
        plot.set_secondary_curves([('T', TEMPERATURE_COLOR, False)])
        plot.set_data(np.arange(100.), [np.arange(100.)])
        plot.set_secondary_data(np.arange(100.), [np.arange(100.)])

        plot.getPlotItem().vb.setXRange(10., 20., padding=0)

        assert plot._right.viewRange()[0] == pytest.approx([10., 20.])

    def test_the_second_scale_goes_away_when_there_is_no_curve_left(self, qtbot):
        plot = CurvesPlot()
        qtbot.addWidget(plot)
        plot.show()
        plot.set_secondary_curves([('T', TEMPERATURE_COLOR, False)])
        assert plot.has_secondary_scale

        plot.set_secondary_curves([])

        assert not plot.has_secondary_scale
        assert plot.secondary_curves == []
        assert len(plot.legend.items) == 0

    def test_the_main_curves_are_not_touched_when_the_second_ones_change(self, qtbot):
        plot = CurvesPlot()
        qtbot.addWidget(plot)
        plot.set_curves([('a', '#1f77b4', False)])
        plot.set_secondary_curves([('T', TEMPERATURE_COLOR, False)])

        plot.set_secondary_curves([])
        plot.set_secondary_curves([('T', TEMPERATURE_COLOR, False)])

        assert len(plot.curves) == 1
        assert [item[1].text for item in plot.legend.items] == ['a', 'T']


def test_colors_are_reused_after_the_palette():
    assert roi_color(0) == PALETTE[0]
    assert roi_color(len(PALETTE)) == PALETTE[0]


def test_the_temperature_has_a_color_of_its_own():
    assert TEMPERATURE_COLOR not in PALETTE
