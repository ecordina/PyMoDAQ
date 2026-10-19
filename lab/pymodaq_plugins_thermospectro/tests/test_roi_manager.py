import pytest

from pymodaq_plugins_thermospectro.roi_manager import EdgeSpinBox, RoiManager
from pymodaq_plugins_thermospectro.rois import Roi

KEYS = ['roi00', 'roi01', 'roi02']
ROIS = [Roi('first', 450., 460.), Roi('second', 500., 512.5, record=False), Roi('third', 600., 604., plot=False)]
COLORS = ['#111111', '#222222', '#333333']


class Events(list):
    """What the manager said, in order: ('edges', key, start, stop), ('name', key, text), ('delete', key)..."""

    def __init__(self, manager):
        super().__init__()
        manager.edges_edited.connect(lambda key, start, stop: self.append(('edges', key, start, stop)))
        manager.name_edited.connect(lambda key, name: self.append(('name', key, name)))
        manager.record_toggled.connect(lambda key, on: self.append(('record', key, on)))
        manager.plot_toggled.connect(lambda key, on: self.append(('plot', key, on)))
        manager.delete_clicked.connect(lambda key: self.append(('delete', key)))
        manager.add_clicked.connect(lambda: self.append(('add',)))
        manager.reset_clicked.connect(lambda: self.append(('reset',)))


@pytest.fixture
def manager(qtbot):
    widget = RoiManager()
    qtbot.addWidget(widget)
    widget.resize(420, 300)
    widget.show()
    widget.set_rois(KEYS, ROIS, COLORS, 'nm', (400., 900.), 0.35)
    return widget


@pytest.fixture
def events(manager):
    return Events(manager)


class TestWhatIsShown:
    def test_a_row_per_roi_in_order(self, manager):
        assert manager.keys == KEYS
        assert all(manager.row(key).isVisible() for key in KEYS)  # (they are in the list, not just in the memory)
        for index, (key, roi) in enumerate(zip(KEYS, ROIS)):
            row = manager.row(key)
            assert row.badge.text() == str(index + 1)
            assert COLORS[index] in row.badge.styleSheet()
            assert row.name.text() == roi.name
            assert (row.start.value(), row.stop.value()) == (roi.start, roi.stop)
            assert (row.record.isChecked(), row.plot.isChecked()) == (roi.record, roi.plot)

    def test_the_header_tells_the_units(self, manager):
        assert manager.header_text('start') == 'From (nm)'
        assert manager.header_text('stop') == 'To (nm)'

        manager.set_rois(KEYS, ROIS, COLORS, 'pixel')

        assert manager.header_text('start') == 'From (pixel)'
        manager.set_rois(KEYS, ROIS, COLORS, '')
        assert manager.header_text('start') == 'From'

    def test_the_arrows_step_by_a_pixel(self, manager):
        assert manager.row('roi00').start.singleStep() == pytest.approx(0.35)

        manager.set_rois(KEYS, ROIS, COLORS, 'nm')  # (the spectrum is not known)

        assert manager.row('roi00').start.singleStep() == 1.0

    def test_nothing_is_said_when_the_program_shows_something(self, manager, events):
        manager.set_rois(KEYS, [Roi('x', 10., 20., False, False)] * 3, COLORS, 'nm')
        manager.show_edges('roi01', 30., 40.)

        assert events == []
        row = manager.row('roi01')
        assert (row.name.text(), row.start.value(), row.stop.value()) == ('x', 30., 40.)
        assert not row.record.isChecked() and not row.plot.isChecked()

    def test_the_rows_are_updated_in_place(self, manager):
        rows = {key: manager.row(key) for key in KEYS}

        manager.set_rois(KEYS, [Roi('a', 1., 2.), Roi('b', 3., 4.), Roi('c', 5., 6.)], COLORS, 'nm')

        assert all(manager.row(key) is rows[key] for key in KEYS)
        assert [manager.row(key).name.text() for key in KEYS] == ['a', 'b', 'c']

    def test_a_row_goes_away_with_its_roi_and_the_others_are_renumbered(self, manager, qtbot):
        rows = {key: manager.row(key) for key in KEYS}

        manager.set_rois(['roi00', 'roi02'], [ROIS[0], ROIS[2]], COLORS[:2], 'nm')

        assert manager.keys == ['roi00', 'roi02']
        assert manager.row('roi00') is rows['roi00'] and manager.row('roi02') is rows['roi02']
        assert manager.row('roi02').badge.text() == '2'
        assert not rows['roi01'].isVisible()
        assert manager._rows_layout.count() == 3  # (two rows and the stretch)

    def test_a_row_is_added_at_the_end(self, manager):
        manager.set_rois(KEYS + ['roi03'], ROIS + [Roi('fourth', 700., 710.)], COLORS + ['#444444'], 'nm')

        assert manager.keys == KEYS + ['roi03']
        assert manager.row('roi03').name.text() == 'fourth' and manager.row('roi03').badge.text() == '4'
        assert manager._rows_layout.itemAt(3).widget() is manager.row('roi03')

    def test_the_order_of_the_rows_follows_the_list(self, manager):
        manager.set_rois(['roi02', 'roi00', 'roi01'], [ROIS[2], ROIS[0], ROIS[1]], COLORS, 'nm')

        assert manager.keys == ['roi02', 'roi00', 'roi01']
        order = [manager._rows_layout.itemAt(i).widget() for i in range(3)]
        assert order == [manager.row('roi02'), manager.row('roi00'), manager.row('roi01')]
        assert manager.row('roi02').badge.text() == '1'

    def test_a_window_out_of_the_spectrum_is_shown_in_red(self, manager):
        manager.set_rois(KEYS, [Roi('in', 450., 460.), Roi('out', 1000., 1010.), Roi('edge', 890., 910.)], COLORS,
                         'nm', (400., 900.))

        assert manager.row('roi00').start.styleSheet() == ''
        assert 'color' in manager.row('roi01').start.styleSheet() and 'outside' in manager.row('roi01').stop.toolTip()
        assert manager.row('roi02').start.styleSheet() == ''  # (partly in: it has a value)

        manager.set_rois(KEYS, [Roi('in', 450., 460.), Roi('out', 1000., 1010.), Roi('edge', 890., 910.)], COLORS,
                         'nm')  # (the spectrum is not known: nothing can be said)
        assert manager.row('roi01').start.styleSheet() == ''

    def test_an_empty_list(self, manager):
        manager.set_rois([], [], [], 'nm')

        assert manager.keys == []


class TestWhatTheUserDoes:
    def test_typing_the_edges(self, manager, events):
        manager.row('roi00').start.setValue(455.)
        manager.row('roi00').stop.setValue(470.)

        assert events == [('edges', 'roi00', 455., 460.), ('edges', 'roi00', 455., 470.)]

    def test_typing_in_a_box_with_the_keyboard(self, manager, events, qtbot):
        from qtpy import QtCore
        box = manager.row('roi00').stop  # (450-460)
        box.setFocus()
        box.lineEdit().selectAll()

        qtbot.keyClicks(box, '472.5')
        assert events == []  # (nothing is said while the number is being typed)
        qtbot.keyClick(box, QtCore.Qt.Key.Key_Return)

        assert events == [('edges', 'roi00', 450., 472.5)]

    def test_an_edge_taken_past_the_other_one_pushes_it(self, manager, events):
        manager.row('roi00').start.setValue(470.)  # (the window is 450-460)

        assert events == [('edges', 'roi00', 470., 480.)]
        manager.show_edges('roi00', 470., 480.)  # (what the settings say back)
        manager.row('roi00').stop.setValue(440.)

        assert events[-1] == ('edges', 'roi00', 430., 440.)

    def test_a_window_is_moved_by_typing_its_two_edges_whichever_way(self, manager, events):
        row = manager.row('roi00')  # 450-460
        for edge, value in ((row.start, 500.), (row.stop, 515.)):  # to the right: the beginning first
            edge.setValue(value)
            manager.show_edges('roi00', *events[-1][2:])
        assert events[-1] == ('edges', 'roi00', 500., 515.)

        for edge, value in ((row.stop, 420.), (row.start, 405.)):  # back to the left: the end first
            edge.setValue(value)
            manager.show_edges('roi00', *events[-1][2:])
        assert events[-1] == ('edges', 'roi00', 405., 420.)

    def test_the_edge_which_is_not_edited_is_not_rounded(self, manager, events):
        """The boxes show two decimals: the other edge must stay what it is in the settings"""
        manager.set_rois(KEYS, [Roi('first', 450.123456, 460.), ROIS[1], ROIS[2]], COLORS, 'nm')
        assert manager.row('roi00').start.value() == pytest.approx(450.12)  # (what is displayed)

        manager.row('roi00').stop.setValue(461.)

        assert events == [('edges', 'roi00', 450.123456, 461.)]

    def test_the_arrows_move_an_edge_by_a_step(self, manager, events, qtbot):
        manager.row('roi01').stop.stepBy(2)

        assert events[-1][:2] == ('edges', 'roi01')
        assert events[-1][3] == pytest.approx(512.5 + 2 * 0.35)

    def test_renaming(self, manager, events):
        row = manager.row('roi01')
        row.name.setText('  peak  ')
        row.name.editingFinished.emit()
        row.name.editingFinished.emit()  # (the focus is lost: it was said already)

        assert events == [('name', 'roi01', 'peak')]

    def test_a_name_which_did_not_change_is_not_said(self, manager, events):
        manager.row('roi01').name.editingFinished.emit()

        assert events == []

    def test_a_roi_always_has_a_name(self, manager, events):
        row = manager.row('roi01')
        row.name.setText('   ')
        row.name.editingFinished.emit()

        assert events == [] and row.name.text() == 'second'

    def test_the_boxes_record_and_plot(self, manager, events):
        manager.row('roi01').record.setChecked(True)
        manager.row('roi02').plot.setChecked(True)
        manager.row('roi00').plot.setChecked(False)

        assert events == [('record', 'roi01', True), ('plot', 'roi02', True), ('plot', 'roi00', False)]

    def test_deleting(self, manager, events):
        manager.row('roi01').delete.click()

        assert events == [('delete', 'roi01')]

    def test_adding_and_resetting(self, manager, events):
        manager.add_button.click()
        manager.reset_button.click()

        assert events == [('add',), ('reset',)]

    def test_a_new_roi_is_ready_to_be_named(self, manager, qtbot):
        manager.set_rois(KEYS + ['roi03'], ROIS + [Roi('ROI 4', 700., 710.)], COLORS + ['#444444'], 'nm')

        manager.focus_roi('roi03')

        assert manager.row('roi03').name.selectedText() == 'ROI 4'
        qtbot.wait(20)  # (the scroll waits for the layout)
        manager.focus_roi('nothing')  # (nothing happens)

    def test_the_wheel_over_a_box_scrolls_the_list_instead_of_moving_the_edge(self, manager):
        box = manager.row('roi00').start

        class Wheel:
            ignored = False

            def ignore(self):
                self.ignored = True

        event = Wheel()
        box.clearFocus()
        box.wheelEvent(event)

        assert event.ignored and box.value() == 450.

    def test_the_boxes_take_their_value_when_the_user_is_done_typing(self, manager, events):
        box = manager.row('roi00').stop

        assert isinstance(box, EdgeSpinBox) and not box.keyboardTracking()


class TestLocked:
    def test_only_what_is_plotted_can_be_changed_during_an_experiment(self, manager):
        manager.set_locked(True)

        assert not manager.add_button.isEnabled() and not manager.reset_button.isEnabled()
        for key in KEYS:
            row = manager.row(key)
            assert not any(w.isEnabled() for w in (row.name, row.start, row.stop, row.record, row.delete))
            assert row.plot.isEnabled()

        manager.set_locked(False)

        assert manager.add_button.isEnabled() and manager.row('roi00').start.isEnabled()

    def test_a_row_created_while_locked_is_locked(self, manager):
        manager.set_locked(True)

        manager.set_rois(KEYS + ['roi03'], ROIS + [Roi('x', 1., 2.)], COLORS + ['#444444'], 'nm')

        assert not manager.row('roi03').delete.isEnabled() and manager.row('roi03').plot.isEnabled()
