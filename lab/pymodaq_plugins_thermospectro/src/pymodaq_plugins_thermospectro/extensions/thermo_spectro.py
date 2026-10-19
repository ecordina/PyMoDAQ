# -*- coding: utf-8 -*-
"""ThermoSpectro: one or two spectrometers and a camera, each with a thermostat, recorded during temperature ramps

The extension drives *units*, a unit being a spectrometer (a 1D detector of the Dashboard) and the thermostat (an
actuator whose position is a temperature) controlling its temperature. One or two units can be used, with their own
settings; a third one, the camera (a 2D detector, with rectangular ROIs on its images), works the same way and can
share the thermostat of a spectrometer (only one of them runs the ramp):

* the integration time (500 ms by default) and the readout time (1 s by default): the spectrometer is read out, and a
  measurement taken, once per readout time;
* six ROIs by default, windows of the spectrum whose value is the mean of their pixels. They are edited on the spectrum
  (drag the window or its edges) or in the settings, and remembered for the next experiment;
* a temperature ramp, defined by three of: initial value, final value, velocity, duration. It is followed by a wait at the
  final value, the way back to the initial value (defined in the same way) and a wait, and run as many times as asked.
  The thermostat is given a new set-point every second or so, so any thermostat with a PyMoDAQ plugin can be used;
* what is recorded in the h5 file: the full spectrum, each ROI, the temperature, each independently of the others.

The spectra are shown small, the ROIs (those which are to be plotted) and the temperature, on its own scale, in a
plot per unit. Every plot can be closed and opened again from the Plots menu.

*Live* (camera icon) reads the spectrometers out without running anything: use it to set the ROIs. *Start* runs the
ramps and records, *Pause* freezes the set-points, *Stop* brings every thermostat back to its initial value before the
experiment ends, which is also what happens when something goes wrong (a module stops answering). The file of an
experiment is written in SWMR mode, so that it can be read by another program while it is written.
"""
from __future__ import annotations

import re
import sys
import time
from typing import Optional, Sequence

from qtpy import QtCore, QtWidgets

from pymodaq_utils.logger import set_logger, get_module_name

from pymodaq_data import DataToExport
from pymodaq_data.h5modules.data_saving import DataBundle

from pymodaq_gui import utils as gutils
from pymodaq_gui.parameter import Parameter
from pymodaq_gui.utils import Dock
from pymodaq_gui.utils.app_worker import ExtensionWorker, SaverWorker
from pymodaq_gui.managers.h5manager import FileAction
from pymodaq_gui.utils.custom_app import WorkFlowActions
from pymodaq_gui.utils.shared_ui import MenuToolbarNames

from pymodaq.control_modules.enums import DAQTypesEnum
from pymodaq.extensions.utils import CustomExt

from pymodaq_plugins_thermospectro import saving
from pymodaq_plugins_thermospectro.camera import RectRoi, default_rect_rois
from pymodaq_plugins_thermospectro.camera_widgets import HINT as CAMERA_HINT
from pymodaq_plugins_thermospectro.ramp import format_duration
from pymodaq_plugins_thermospectro.roi_manager import HINT
from pymodaq_plugins_thermospectro.rois import RoiStore, default_store_path
from pymodaq_plugins_thermospectro.saving import ThermoSpectroSaver, UnitRecording, build_dtes
from pymodaq_plugins_thermospectro.unit import CameraUnit, Measurement, Problem, Unit, UnitState, logged
from pymodaq_plugins_thermospectro.unit_settings import (CAMERA, NONE, CameraSettings, UnitSettings,
                                                         unit_definition)
from pymodaq_plugins_thermospectro.views import TEMPERATURE, CameraView, UnitView

logger = set_logger(get_module_name(__file__))

EXTENSION_NAME = 'ThermoSpectro'  # the name displayed in the extension list of the dashboard
CLASS_NAME = 'ThermoSpectro'

NUMBERS = (1, 2, 3)  # the units: spectrometer 1 and 2, and the camera
CAMERA_NUMBER = 3


def guess_thermostat(actuators: list[str], number: int) -> str:
    """The actuator which most probably is the thermostat of a spectrometer

    One with ``therm`` or ``temp`` in its name (any actuator if there is none), and the number of the spectrometer in
    it if possible, else the n-th one. The camera shares the thermostat of the first spectrometer.
    """
    candidates = [name for name in actuators if re.search('therm|temp', name, re.IGNORECASE)] or list(actuators)
    numbered = [name for name in candidates if re.search(rf'(?<!\d){number}(?!\d)', name)]
    if numbered:
        return numbered[0]
    if len(candidates) >= number:
        return candidates[number - 1]
    return NONE


class ThermoSpectro(CustomExt):
    """Record one or two spectrometers and a camera, each with a thermostat, during temperature ramps (see help.md,
    next to this file)"""

    h5_base_group_name = 'ThermoSpectro'
    show_h5file_statusbar_widgets = True
    show_workflow_actions = True
    icon_name = 'equalizer'

    def __init__(self, parent: gutils.DockArea, dashboard):
        # the base class may call do_things_after_experiment_set(), so what it uses is defined first
        self.units: list[Unit] = []
        self.views: list[UnitView] = []
        self._store = RoiStore(default_store_path())
        self.params = self._build_params()
        self._modules_offered = False
        self._offering = False  # the modules are being offered: the windows of the units are arranged all at once
        self._monitoring = False
        self._monitoring_for_run = False
        self._run_active = False
        self._finishing = False
        self._pending_start = False
        self._saving = False
        self._swmr = False
        self._recordings: dict[int, UnitRecording] = {}
        self._origin = time.time()
        self._roi_dirty: set[int] = set()
        self._boxes: list[QtWidgets.QMessageBox] = []
        self._ui_ready = False

        super().__init__(parent, dashboard, add_toolbar_break=False)

        self._worker = ThermoSpectroWorker(self)
        self._module_and_data_saver = ThermoSpectroSaver(self)

        for number in NUMBERS:
            if number == CAMERA_NUMBER:
                unit = CameraUnit(CameraSettings(self.settings.child(f'unit{number}'), number), self.modules_manager,
                                  parent=self)
            else:
                unit = Unit(UnitSettings(self.settings.child(f'unit{number}'), number), self.modules_manager,
                            parent=self)
            unit.measurement.connect(self._on_measurement)
            unit.notice.connect(self.status_signal.emit)
            unit.problem.connect(self._on_unit_problem)
            unit.axis_units_changed.connect(lambda units, u=unit: self._on_axis_units(u, units))
            self.units.append(unit)
        self.views = [(CameraView if isinstance(unit, CameraUnit) else UnitView)(unit, parent=self)
                      for unit in self.units]
        for view in self.views:
            if isinstance(view, CameraView):
                view.rect_edited.connect(self._on_rect_edited)
            else:
                view.roi_edited.connect(self._on_roi_edited)
            view.add_requested.connect(self._on_add_requested)
            view.plot_toggled.connect(self._on_plot_toggled)
            view.roi_field_edited.connect(self._on_roi_field_edited)
            view.add_clicked.connect(self._on_add_clicked)
            view.delete_requested.connect(self._on_delete_requested)
            view.reset_requested.connect(self._on_reset_requested)

        self._ramp_timer = QtCore.QTimer(self)
        self._ramp_timer.timeout.connect(self._on_ramp_tick)
        self._display_timer = QtCore.QTimer(self)
        self._display_timer.setInterval(250)
        self._display_timer.timeout.connect(self._on_display_tick)
        self._start_timer = QtCore.QTimer(self)  # the time an experiment waits for the modules to deliver
        self._start_timer.setSingleShot(True)
        self._start_timer.timeout.connect(self._check_pending_start)
        self._roi_timer = QtCore.QTimer(self)  # the ROIs are remembered a moment after the last change
        self._roi_timer.setSingleShot(True)
        self._roi_timer.setInterval(500)
        self._roi_timer.timeout.connect(self.save_rois)
        self._init_timer = QtCore.QTimer(self)  # the modules are initialized one after the other
        self._init_timer.setInterval(200)
        self._init_timer.timeout.connect(self._init_poll)
        self._init_queue: list = []
        self._init_attempted: list = []
        self._init_current = None
        self._init_attempt_over = False
        self._init_deadline = 0.0

        self.setup_ui()
        self._ui_ready = True
        self._connect_buttons()
        self.set_action_enabled(WorkFlowActions.START, True)
        self.set_action_enabled(WorkFlowActions.STOP, False)
        self.set_action_enabled(WorkFlowActions.PAUSE, False)
        self._offer_modules()
        for unit in self.units:
            unit.settings.refresh_ramp()
        self._update_all_views()
        self._display_timer.start()

    # ----------------------------------------------------------------------------------------------------------
    # settings
    # ----------------------------------------------------------------------------------------------------------
    def _build_params(self) -> list[dict]:
        return [
            unit_definition(number, self._store.load(number)) for number in NUMBERS if number != CAMERA_NUMBER
        ] + [
            unit_definition(CAMERA_NUMBER, self._store.load(CAMERA_NUMBER, RectRoi, default_rect_rois), kind=CAMERA)
        ] + [
            {'title': 'Experiment:', 'name': 'experiment', 'type': 'group', 'children': [
                {'title': 'Set-point every:', 'name': 'update_period', 'type': 'float', 'value': 1.0, 'min': 0.05,
                 'suffix': 's', 'tip': 'How often the thermostats are given a new set-point during a ramp'},
                {'title': 'Stop if no data for:', 'name': 'watchdog', 'type': 'float', 'value': 30.0, 'min': 0.,
                 'suffix': 's', 'tip': 'The ramp of a unit is brought back to its initial value if its spectrometer '
                                       'or its thermostat does not deliver anything for this long (at least a few '
                                       'readout times). 0: never'},
                {'title': 'Plot the last:', 'name': 'history', 'type': 'float', 'value': 0.0, 'min': 0.,
                 'suffix': 'min', 'tip': '0: plot everything since the start'},
                {'title': 'Elapsed:', 'name': 'elapsed', 'type': 'str', 'value': '', 'readonly': True},
            ]},
        ] + SaverWorker.params

    def unit(self, number: int) -> Unit:
        return self.units[number - 1]

    def view(self, number: int) -> UnitView:
        return self.views[number - 1]

    def _active_units(self) -> list[Unit]:
        """The units which take part: those which are ticked"""
        return [unit for unit in self.units if unit.settings.enabled]

    def _connect_buttons(self):
        """The push buttons of the settings (they are replaced when settings are loaded from a file)"""
        for unit in self.units:
            actions = {('rois', 'add_roi'): lambda *_, u=unit: self._add_roi_clicked(u),
                       ('rois', 'reset_rois'): lambda *_, u=unit: self.reset_rois(u.number),
                       ('ramp', 'take_current'): lambda *_, u=unit: self._take_current_temperature(u)}
            for path, slot in actions.items():
                try:
                    unit.settings.child(*path).sigActivated.connect(slot)
                except Exception:
                    logger.warning(f"The settings of unit {unit.number} have no {'/'.join(path)} button")

    def set_settings(self, settings):
        """Settings loaded from a file replace the whole tree: the units follow"""
        super().set_settings(settings)
        if not self.units:
            return
        for unit in self.units:
            unit.settings.param = self.settings.child(f'unit{unit.number}')
            unit.settings.refresh_known_rois()
            unit.settings.refresh_ramp()
            if unit.state == UnitState.IDLE:
                unit.attach()
        self._connect_buttons()
        if self._run_active:  # (the toolbar loading settings is disabled during a run)
            self._lock_ui(True)
        self._update_all_views()
        for unit in self.units:
            self._schedule_roi_save(unit)

    def value_changed(self, param: Parameter):
        if not self.units:
            return
        for unit in self.units:
            kind = unit.settings.classify(param)
            if kind is not None:
                self._on_unit_setting(unit, *kind)
                return
        path = self.settings.childPath(param)
        if path and path[0] == 'experiment' and path[-1] == 'history':
            for view in self.views:
                view._marker = None

    def _on_unit_setting(self, unit: Unit, kind: str, key: Optional[str]):
        settings, view = unit.settings, self.view(unit.number)
        if kind == 'enabled':
            if not self._offering:
                self._apply_unit_visibility(unit)
        elif kind == 'modules':
            if unit.state == UnitState.IDLE:
                self._modules_changed(unit)
        elif kind == 'integration':
            if unit.state == UnitState.LIVE:
                unit.apply_integration_time()
        elif kind == 'readout':
            unit.update_timing()
        elif kind == 'plot_temperature':
            view.update_definitions()
        elif kind == 'ramp':
            settings.refresh_ramp()
        elif kind == 'roi_edges':
            self._rois_changed(unit, reset=True)
        elif kind == 'roi_name':
            settings.update_roi_title(key)
            self._rois_changed(unit)
        elif kind in ('roi_record', 'roi_plot'):
            self._rois_changed(unit)

    def child_added(self, param: Parameter, data):
        for unit in self.units:
            if param is unit.settings.child('rois') and not unit.settings.is_busy:
                unit.settings.refresh_known_rois()
                self._rois_changed(unit, reset=True)

    def param_deleted(self, param: Parameter):
        """A ROI was removed (by the user: right click on it)"""
        for unit in self.units:
            if unit.settings.owned_removed(param):
                if not unit.settings.is_busy:  # (otherwise the program is replacing the ROIs and updates the plots)
                    unit.settings.refresh_known_rois()
                    self._rois_changed(unit, reset=True)
                return

    # ----------------------------------------------------------------------------------------------------------
    # user interface
    # ----------------------------------------------------------------------------------------------------------
    @property
    def _size_hint(self) -> QtCore.QSize:
        return QtCore.QSize(1500, 900)

    def setup_docks_and_widgets(self):
        self.docks['settings'] = Dock('Settings', closable=True, autoOrientation=False)
        self.docks['settings'].addWidget(self.settings_tree)
        self.docks['saving'] = Dock('Saving', closable=True, autoOrientation=False)
        self.docks['saving'].addWidget(self.h5_manager.get_h5saver(create_new_file=False).settings_tree)
        manager = QtWidgets.QWidget()  # the ROI manager: the ROIs of every spectrometer in one window
        layout = QtWidgets.QVBoxLayout(manager)
        layout.setContentsMargins(0, 0, 0, 0)
        for view in self.views:
            layout.addWidget(view.manager_box, 1)
        hint = QtWidgets.QLabel(f'{HINT}\nCamera: {CAMERA_HINT}')
        hint.setWordWrap(True)
        hint.setStyleSheet('color: #888888;')
        layout.addWidget(hint)
        self.docks['roimanager'] = Dock('ROI manager', closable=True, autoOrientation=False)
        self.docks['roimanager'].addWidget(manager)
        for view in self.views:
            self.docks.update(view.docks)

        # the settings at the left (the ROI manager is a tab next to them), the spectra in a small row above the ROIs
        area = self.dockarea
        area.addDock(self.docks['settings'])
        area.addDock(self.docks['saving'], 'below', self.docks['settings'])
        area.addDock(self.docks['roimanager'], 'below', self.docks['saving'])
        area.addDock(self.docks['spectrum1'], 'right', self.docks['settings'])
        area.addDock(self.docks['spectrum2'], 'right', self.docks['spectrum1'])
        area.addDock(self.docks[f'spectrum{CAMERA_NUMBER}'], 'right', self.docks['spectrum2'])
        area.addDock(self.docks['rois1'], 'bottom', self.docks['spectrum1'])
        area.addDock(self.docks['rois2'], 'bottom', self.docks['spectrum2'])
        area.addDock(self.docks[f'rois{CAMERA_NUMBER}'], 'bottom', self.docks[f'spectrum{CAMERA_NUMBER}'])
        self.docks['settings'].raiseDock()
        self.docks['settings'].setStretch(x=1, y=1)
        for number in NUMBERS:
            self.docks[f'spectrum{number}'].setStretch(x=3, y=1)
            self.docks[f'rois{number}'].setStretch(x=3, y=4)
        for key, dock in self.docks.items():
            dock.sigClosed.connect(lambda _=None, k=key: self._on_dock_closed(k))
        self.populate_status_bar()

    def do_things_after_ui_setup(self):
        """The spectra take a fifth of the height of the plots, the image of the camera a bit more than a half; the rest
        of the proportions wait for the window"""
        try:
            for number in NUMBERS:
                container = self.docks[f'spectrum{number}'].container()
                if container is not None and container.count() == 2:
                    # (a spectrum is a small strip above its curves; the image of the camera gets half of the height)
                    container.setSizes([450, 330] if number == CAMERA_NUMBER else [190, 600])
        except Exception:
            logger.exception('The layout of the windows could not be set')
        self._layout_ticks = 0
        self._layout_timer = QtCore.QTimer(self)
        self._layout_timer.setInterval(100)
        self._layout_timer.timeout.connect(self._set_layout)
        self._request_layout()

    def _request_layout(self):
        """Set the proportions of the windows as soon as the window is on the screen (and the windows are in place)"""
        self._layout_ticks = 0
        self._layout_timer.start()

    def _set_layout(self):
        """The settings and the ROI managers (tabs) take two fifths of the window, the units the rest

        The splitters have no size before the window is on the screen: this waits for it (for a while).
        """
        self._layout_ticks += 1
        try:
            area = self.dockarea
            if (not area.isVisible() or area.width() < 200) and self._layout_ticks < 100:
                return
            self._layout_timer.stop()
            top, width = area.topContainer, area.width()
            if top is None or width < 200 or top.type() != 'horizontal':  # (no unit: only the tabs are left)
                return
            if top.count() == 2:  # (the settings and the units)
                top.setSizes([int(width * 0.4), width - int(width * 0.4)])
            elif top.count() == 3:
                left, middle = int(width * 0.38), int(width * 0.31)
                top.setSizes([left, middle, width - left - middle])
            elif top.count() > 3:  # (the settings, and a column for each of the three units)
                left = int(width * 0.28)
                others = (width - left) // (top.count() - 1)
                top.setSizes([left] + [others] * (top.count() - 1))
        except RuntimeError:  # the window is gone (the extension was closed)
            self._layout_timer.stop()
        except Exception:
            self._layout_timer.stop()
            logger.exception('The layout of the windows could not be set')

    def setup_menus_and_toolbars(self, menubar: QtWidgets.QMenuBar = None):
        self.add_toolbar(MenuToolbarNames.FILE, MenuToolbarNames.FILE.capitalize(), self.mainwindow,
                         toolbar=self.h5_manager.toolbar, add_break=False)
        self.add_menu(MenuToolbarNames.FILE, MenuToolbarNames.FILE.capitalize(), parent_menu=menubar)
        self.add_menu(MenuToolbarNames.TOOLS, MenuToolbarNames.TOOLS.capitalize(), parent_menu=menubar)
        self.add_menu('plots', 'Plots', parent_menu=menubar)
        self.create_dashboard_toolbar(add_break=False)

    DOCK_TITLES = {'settings': 'Settings', 'saving': 'Saving', 'roimanager': 'ROI manager', 'spectrum1': 'Spectrum 1',
                   'rois1': 'ROIs 1', 'spectrum2': 'Spectrum 2', 'rois2': 'ROIs 2', 'spectrum3': 'Image',
                   'rois3': 'ROIs camera'}

    def setup_actions(self):
        self.toolbar.addSeparator()
        self.add_action('monitor', 'Live', 'videocam', 'Read the spectrometers out and show the spectra and the ROIs '
                        'live, without running the temperature ramps', checkable=True, icon_checked='videocam_off',
                        toolbar=self.toolbar, menu='actions')
        self.add_action('initialize', 'Connect the modules', 'plug_connect',
                        'Connect (initialize) the spectrometers and thermostats of the units which are not, one after '
                        'the other. Nothing is connected when a preset is opened', toolbar=self.toolbar,
                        menu='actions')
        for key in self.docks:
            self.add_action(f'show_{key}', self.DOCK_TITLES.get(key, key), '',
                            'Show or close this window. A closed plot can be opened again here', checkable=True,
                            checked=True, toolbar=None, auto_toolbar=False, menu='plots')

    def connect_things(self):
        self.status_signal[str].connect(self._on_status_signal)
        self.connect_action('monitor', self.set_monitoring)
        self.connect_action('initialize', self.initialize_modules)
        self.connect_action(WorkFlowActions.START, self.start_run)
        self.connect_action(WorkFlowActions.STOP, self.stop_run)
        self.connect_action(WorkFlowActions.PAUSE, self.pause_run)
        for key in self.docks:
            self.connect_action(f'show_{key}', lambda checked, k=key: self.set_dock_visible(k, checked))

    def _on_status_signal(self, message: str):
        self.update_status(message)

    # -- the windows -------------------------------------------------------------------------------------------

    def dock_visible(self, key: str) -> bool:
        """The window is open (it can be hidden behind another one in a tab)"""
        return self.docks[key].container() is not None

    def set_dock_visible(self, key: str, visible: bool = True):
        """Close a window, or open it again where it was"""
        dock = self.docks[key]
        is_open = dock.container() is not None
        if visible and not is_open:
            self._place_dock(key)
            dock.label.setVisible(not dock.labelHidden)  # (closing a dock closes its title bar too)
        elif not visible and is_open:
            dock.close()
        if self.is_action_checked(f'show_{key}') != visible:
            self.set_action_checked(f'show_{key}', visible)

    def _place_dock(self, key: str):
        """Open a closed window next to the ones which are there: a spectrum above the ROIs of its unit, a unit next
        to the other one"""
        dock = self.docks[key]
        choices = {'settings': [('left', None)], 'saving': [('below', 'settings'), ('left', None)],
                   'roimanager': [('below', 'saving'), ('below', 'settings'), ('left', None)]}
        if key[-1].isdigit():
            number = int(key[-1])
            spectrum = key.startswith('spectrum')
            kind, other = ('spectrum', 'rois') if spectrum else ('rois', 'spectrum')
            choices[key] = [('top' if spectrum else 'bottom', f'{other}{number}'),
                            ('right', f'{kind}{number - 1}'), ('left', f'{kind}{number + 1}'),
                            ('right', 'settings'), ('left', None)]
        for position, neighbor_key in choices.get(key, [('left', None)]):
            neighbor = self.docks.get(neighbor_key) if neighbor_key else None
            if neighbor_key and (neighbor is None or neighbor.container() is None):
                continue
            if position in ('left', 'right') and neighbor is not None and neighbor_key[-1].isdigit():
                column = neighbor.container()  # (a unit goes next to the whole column of the other one)
                if column is not None and column.type() == 'vertical':
                    neighbor = column
            self.dockarea.addDock(dock, position, neighbor)
            return
        self.dockarea.addDock(dock)

    def _on_dock_closed(self, key: str):
        """The user closed a window: the menu follows"""
        if self.has_action(f'show_{key}') and self.is_action_checked(f'show_{key}'):
            self.set_action_checked(f'show_{key}', False)

    def _apply_unit_visibility(self, unit: Unit):
        """A unit which is not used shows no plot; one which is used shows them"""
        self._apply_units_visibility([unit])

    def _apply_units_visibility(self, units: Sequence[Unit]):
        """The windows of the units which are used are open, the others are closed (and their part of the ROI manager
        is hidden)

        The spectra are opened before the plots of the ROIs, for all the units: each plot goes below its spectrum, so
        the units make two columns, as they do when the extension is created.
        """
        for kind in ('spectrum', 'rois'):
            for unit in units:
                self.set_dock_visible(f'{kind}{unit.number}', unit.settings.enabled)
        for unit in units:
            self.view(unit.number).manager_box.setVisible(unit.settings.enabled)

    # -- messages to the user (replaced in the tests, which cannot answer a dialog) ----------------------------

    def _notify(self, title: str, text: str, error: bool = False):
        """Tell the user without blocking: the timers go on, the thermostats are not left alone"""
        logger.error(text) if error else logger.info(text)
        self.status_signal.emit(text)
        box = QtWidgets.QMessageBox(QtWidgets.QMessageBox.Icon.Critical if error else
                                    QtWidgets.QMessageBox.Icon.Information, title, text,
                                    QtWidgets.QMessageBox.StandardButton.Ok, self.mainwindow)
        box.setWindowModality(QtCore.Qt.WindowModality.NonModal)
        box.finished.connect(lambda _=None, b=box: self._boxes.remove(b) if b in self._boxes else None)
        self._boxes.append(box)
        box.show()

    def _ask_abort_return(self) -> bool:
        """The thermostats are on their way back to their initial value: leave them where they are?"""
        where = ', '.join(f'{unit.title}: {unit.ramp_state.setpoint:.4g} {unit.units}'
                          for unit in self._running_units() if unit.ramp_state is not None and not unit.program_over)
        answer = QtWidgets.QMessageBox.question(
            self.mainwindow, 'Stop now?',
            'The thermostats are returning to their initial temperature.\n\nStop here and leave the set-points '
            f'where they are ({where})?',
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No)
        return answer == QtWidgets.QMessageBox.StandardButton.Yes

    def _ask_quit_choice(self) -> str:
        """An experiment is going on and the extension is asked to close: 'return', 'jump' or 'cancel'"""
        box = QtWidgets.QMessageBox(QtWidgets.QMessageBox.Icon.Question, 'An experiment is going on',
                                    'The thermostats are not at their initial temperature.\n\n'
                                    'Bring them back gradually (the extension stays open until it is done), '
                                    'or set the initial temperatures now and close?\n\n'
                                    '(If you are quitting PyMoDAQ, choose "Set them now": the extension cannot '
                                    'go on after that.)', parent=self.mainwindow)
        now = box.addButton('Set them now and close', QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
        gradually = box.addButton('Bring them back gradually', QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(now)  # Enter must not leave a thermostat hot
        box.setEscapeButton(cancel)
        box.exec()
        if box.clickedButton() is gradually:
            return 'return'
        if box.clickedButton() is now:
            return 'jump'
        return 'cancel'

    # ----------------------------------------------------------------------------------------------------------
    # modules
    # ----------------------------------------------------------------------------------------------------------
    def do_things_after_experiment_set(self, experiment_name: str, show_dashboard: bool = None):
        super().do_things_after_experiment_set(experiment_name, show_dashboard)
        if self.units and self._ui_ready:
            self._offer_modules()

    def _offer_modules(self):
        """Offer the modules of the Dashboard in the settings of the units, and choose some if nothing is chosen

        The first spectrometer is the first 1D detector (by name), the second one the second 1D detector. The
        thermostat is the actuator named like a thermostat (with the number of the spectrometer in it if possible).
        """
        if any(unit.state != UnitState.IDLE for unit in self.units):
            return
        self._offering = True
        try:
            self._offer_modules_now()
        finally:
            self._offering = False
        self._apply_units_visibility(self.units)
        self._update_all_views()
        self._request_layout()  # (the windows of the units were opened or closed)

    def _offer_modules_now(self):
        """The lists of modules and the choices of the units (the windows follow, see :meth:`_offer_modules`)"""
        manager = self.modules_manager
        detectors = sorted(module.title for module in manager.detectors_all if module.daq_type == DAQTypesEnum.DAQ1D)
        cameras = sorted(module.title for module in manager.detectors_all if module.daq_type == DAQTypesEnum.DAQ2D)
        actuators = sorted(manager.actuators_name)
        for unit in self.units:
            settings = unit.settings
            is_camera = isinstance(settings, CameraSettings)
            names = cameras if is_camera else detectors
            if is_camera:  # the first 2D detector
                detector = names[0] if names else NONE
            else:
                detector = names[unit.number - 1] if len(names) >= unit.number else NONE
            thermostat = guess_thermostat(actuators, 1 if is_camera else unit.number)
            settings.set_modules(names, actuators,
                                 detector if settings.spectrometer not in names else None,
                                 thermostat if settings.thermostat not in actuators else None)
            if not self._modules_offered:  # the modules there are in the preset are the ones used
                settings.param.child('enabled').setValue(detector != NONE)
                if is_camera:  # a thermostat which a spectrometer drives already is not driven by the camera too
                    shared = any(other.settings.enabled and other.settings.thermostat == settings.thermostat != NONE
                                 for other in self.units if other is not unit)
                    settings.child('ramp', 'run').setValue(not shared)
            unit.attach()
        # (the extension of `dashboard -x preset -e ThermoSpectro` starts while the preset is still being loaded: the
        # units are ticked when the modules are there)
        self._modules_offered = self._modules_offered or bool(detectors) or bool(cameras)

    def _on_axis_units(self, unit: Unit, units: str):
        """The spectrum is not in nm (or it is, now): so are the edges of the ROIs"""
        unit.settings.set_axis_units(units)
        if isinstance(unit.settings, CameraSettings) and unit.spectrum is not None \
                and unit.settings.fit_default_rois(unit.spectrum.shape):
            self._rois_changed(unit, reset=True)  # (the default ROIs are made for another size of image)
        self.view(unit.number).update_definitions()

    def _modules_changed(self, unit: Unit):
        """The modules selected for a unit changed"""
        unit.attach()
        view = self.view(unit.number)
        view.update_definitions()
        view.clear('waiting for data' if unit.detector is not None else view.no_detector_message())

    def _update_all_views(self):
        for view in self.views:
            view.update_definitions()
            view.clear('waiting for data' if view.unit.detector is not None else view.no_detector_message())

    def initialize_modules(self):
        """Initialize the modules of the units which are not initialized yet, one after the other

        Two spectrometers must not look for their device together: the next module is initialized when the previous
        one is done, whether it worked or not (or after a minute, if it does not say anything). The modules which could
        not be initialized are told at the end.
        """
        modules = []
        for unit in self._active_units():
            for module in (unit.detector, unit.thermostat):
                if module is not None and not module.initialized_state and module not in modules:
                    modules.append(module)
        self._stop_waiting_for_init()
        self._init_queue = modules
        self._init_attempted = []
        if modules:
            self._init_timer.start()

    def _stop_waiting_for_init(self):
        """Forget the module which was being initialized"""
        if self._init_current is not None:
            try:
                self._init_current.init_signal.disconnect(self._on_init_finished)
            except (TypeError, RuntimeError):
                pass
        self._init_current = None

    def _on_init_finished(self, initialized: bool):
        """The module said how its initialization went (init_signal is also emitted when it fails)"""
        self._init_attempt_over = True

    def _init_poll(self):
        current = self._init_current
        if current is not None and not (current.initialized_state or self._init_attempt_over) \
                and time.monotonic() < self._init_deadline:
            return  # still being initialized
        self._stop_waiting_for_init()
        if not self._init_queue:
            self._init_timer.stop()
            failed = [module.title for module in self._init_attempted if not module.initialized_state]
            self._init_attempted = []
            self._refresh_units()
            if failed:
                self._notify('Initialization', f'{", ".join(failed)} could not be initialized, see the log of PyMoDAQ '
                                               'and the status of the module', error=True)
            return
        module = self._init_queue.pop(0)
        if module.initialized_state:
            return
        self._init_current = module
        self._init_attempt_over = False
        self._init_deadline = time.monotonic() + 60.
        self._init_attempted.append(module)
        module.init_signal.connect(self._on_init_finished)
        module.init_hardware_ui(True)

    def _refresh_units(self):
        """The thermostats were connected: the units of the temperature (°C...) are known, show them"""
        for unit in self.units:
            if unit.state == UnitState.IDLE:
                unit.settings.set_units(unit.units)
        for view in self.views:
            view.update_definitions()

    def _report_problems(self, problems: list[str]):
        """Tell why the live view or the experiment cannot start (and what to do if the modules are not connected)"""
        text = '\n'.join(problems)
        if any('is not initialized' in problem for problem in problems):
            text += '\n\nClick "Connect the modules" (the plug button of the toolbar) first.'
        self._notify('Cannot start', text)

    def _check_cross_problems(self, units: list[Unit]) -> list[str]:
        """What prevents the units to be used together"""
        problems = []
        if not units:
            problems.append('No spectrometer is used: tick one of the spectrometers (or the camera)')
        for index, first in enumerate(units):
            for second in units[index + 1:]:
                names = f'{first.label} and the {second.label}'
                if first.detector is not None and first.detector is second.detector:
                    problems.append(f'The {names} are the same module')
                if (first.thermostat is not None and first.thermostat is second.thermostat
                        and first.has_program and second.has_program):
                    problems.append(f'The {names} would both drive the '
                                    f'thermostat {first.thermostat.title}: untick "Run the ramp" for one of them')
        return problems

    def _take_current_temperature(self, unit: Unit):
        if unit.temperature is None:
            self._notify('No temperature', 'The temperature of the thermostat is not known yet: start the live '
                                           'view first')
            return
        unit.settings.child('ramp', 'initial').setValue(round(unit.temperature.value, 4))

    # ----------------------------------------------------------------------------------------------------------
    # ROIs
    # ----------------------------------------------------------------------------------------------------------
    def _rois_changed(self, unit: Unit, reset: bool = False):
        """The ROIs of a unit (or one of them) changed: the plots follow, and the ROIs are remembered"""
        if not self._ui_ready:
            return
        if reset:
            unit.reset_series()
        self.view(unit.number).update_definitions()
        self._schedule_roi_save(unit)

    def _schedule_roi_save(self, unit: Unit):
        self._roi_dirty.add(unit.number)
        self._roi_timer.start()

    def save_rois(self):
        """Remember the ROIs of the units which changed, for the next experiment"""
        self._roi_timer.stop()
        for number in sorted(self._roi_dirty):
            try:
                self._store.save(number, self.unit(number).settings.rois)
            except OSError:
                logger.exception(f'The ROIs of {self.unit(number).label} could not be remembered '
                                 f'({self._store.path})')
        self._roi_dirty.clear()

    def add_roi(self, number: int, center: Optional[float] = None, y: Optional[float] = None) -> str:
        """Add a ROI around a wavelength of a spectrometer (by default the middle of its spectrum), or around a point
        (`center`, `y`) of the image of a camera (by default its middle)

        Returns
        -------
        str: the name of the new ROI (the name of its parameter in the settings)
        """
        unit = self.unit(number)
        if self._run_active:
            raise RuntimeError('The ROIs cannot be changed during an experiment')
        if isinstance(unit.settings, CameraSettings):
            shape = self.view(number).spectrum_plot.shape
            x = center if center is not None else (shape[1] / 2. if shape else 500.)
            y = y if y is not None else (shape[0] / 2. if shape else 500.)
            new = unit.settings.new_roi(x, y)
        else:
            if center is None:
                x_range = self.view(number).spectrum_plot.x_range
                center = (x_range[0] + x_range[1]) / 2. if x_range else 650.
            new = unit.settings.new_roi(center)
        key = unit.settings.add_roi(new)
        self._rois_changed(unit, reset=True)
        return key

    def remove_roi(self, number: int, key: str):
        if self._run_active:
            raise RuntimeError('The ROIs cannot be changed during an experiment')
        unit = self.unit(number)
        unit.settings.remove_roi(key)
        self._rois_changed(unit, reset=True)

    def reset_rois(self, number: int):
        """Replace the ROIs of a spectrometer by the six default ones (the four of a camera)"""
        if self._run_active:
            return
        unit = self.unit(number)
        unit.settings.set_rois(unit.settings.default_rois())
        self._rois_changed(unit, reset=True)

    def _add_roi_clicked(self, unit: Unit):
        if not self._run_active:
            self.add_roi(unit.number)

    def _on_add_requested(self, number: int, position: float, y: Optional[float] = None):
        """A ROI is wanted where the user double clicked: at a wavelength, or at a point of the image"""
        if not self._run_active:
            self.add_roi(number, position, y)

    def _on_roi_field_edited(self, number: int, key: str, field: str, value):
        """The user changed the name or the *Record* box of a ROI in the ROI manager: its settings follow (and the
        settings do the rest, as when they are edited by hand)"""
        unit = self.unit(number)
        if self._run_active or key not in unit.settings.roi_keys:
            return
        unit.settings.child('rois', key, field).setValue(value)

    def _on_add_clicked(self, number: int):
        """*Add a ROI* in the ROI manager: the new ROI is there, ready to be named"""
        if self._run_active:
            return
        key = self.add_roi(number)
        self.view(number).manager.focus_roi(key)

    def _on_delete_requested(self, number: int, key: str):
        """The user deleted a ROI (in the ROI manager, or with the menu of its window on the spectrum)"""
        if not self._run_active and key in self.unit(number).settings.roi_keys:
            self.remove_roi(number, key)

    def _on_reset_requested(self, number: int):
        self.reset_rois(number)

    def _on_roi_edited(self, number: int, key: str, start: float, stop: float):
        """The user dragged a ROI on a spectrum, or typed its edges in the ROI manager: its settings follow"""
        unit = self.unit(number)
        if self._run_active or key not in unit.settings.roi_keys:
            return
        unit.settings.set_roi_edges(key, start, stop)
        self._rois_changed(unit, reset=True)

    def _on_rect_edited(self, number: int, key: str, x0: float, x1: float, y0: float, y1: float):
        """The user dragged a rectangle on an image, or typed its edges in the ROI manager: its settings follow"""
        unit = self.unit(number)
        if self._run_active or key not in unit.settings.roi_keys:
            return
        unit.settings.set_roi_edges(key, x0, x1, y0, y1)
        self._rois_changed(unit, reset=True)

    def _on_plot_toggled(self, number: int, name: str, plotted: bool):
        """The user clicked a button above the plot of the ROIs"""
        unit = self.unit(number)
        if name == TEMPERATURE:
            unit.settings.child('plot_temperature').setValue(plotted)
        elif name in unit.settings.roi_keys:
            unit.settings.child('rois', name, 'plot').setValue(plotted)

    # ----------------------------------------------------------------------------------------------------------
    # locking
    # ----------------------------------------------------------------------------------------------------------
    def _lock_modules(self, locked: bool):
        """The modules in use are not swapped"""
        for unit in self.units:
            for leaf in ('enabled', unit.settings.DETECTOR, 'thermostat'):
                unit.settings.child(leaf).setOpts(enabled=not locked)

    def _lock_ui(self, locked: bool):
        """Forbid what would change an experiment which is running (the plot buttons stay usable)"""
        for unit in self.units:
            unit.settings.lock(locked)
        for view in self.views:
            view.set_locked(locked)
        for leaf in ('update_period', 'watchdog'):
            self.settings.child('experiment', leaf).setOpts(enabled=not locked)
        self.set_action_enabled('monitor', not locked)
        self.set_action_enabled('initialize', not locked)
        self.set_action_enabled(WorkFlowActions.LOG, not locked)  # the file is prepared when the experiment starts
        # what would replace the settings, or the file the saver thread is writing in
        for name in ('load_settings', 'update_settings', 'clear_settings'):
            if self._settings_tree.has_action(name):
                self._settings_tree.set_action_enabled(name, not locked)
        for action in (FileAction.NEW_FILE, FileAction.LOAD, FileAction.SAVE, FileAction.OPEN_FILE,
                       FileAction.CLOSE_FILE):
            if self.h5_manager.has_action(action):
                self.h5_manager.set_action_enabled(action, not locked)
        self.docks['saving'].setEnabled(not locked)
        if locked:
            self._lock_modules(True)
        elif not self._monitoring:
            self._lock_modules(False)

    # ----------------------------------------------------------------------------------------------------------
    # live view
    # ----------------------------------------------------------------------------------------------------------
    @property
    def monitoring(self) -> bool:
        return self._monitoring

    def set_monitoring(self, monitor: bool = True):
        self.start_monitoring() if monitor else self.stop_monitoring()

    def start_monitoring(self) -> bool:
        """Read the spectrometers out (and ask for the temperatures) continuously, to show them and the ROIs"""
        if self._monitoring:
            return True
        units = self._active_units()
        for unit in units:
            unit.attach()
        problems = self._check_cross_problems(units) + [p for unit in units for p in unit.problems()]
        if problems:
            self.set_action_checked('monitor', False)
            self._report_problems(problems)
            return False
        self._origin = time.time()
        self._monitoring = True
        self._lock_modules(True)
        for unit in units:
            unit.start_live(self._origin)
            self.view(unit.number).clear('waiting for data')
        self.set_action_checked('monitor', True)
        self.set_permanent_status('Live')
        return True

    def stop_monitoring(self):
        if not self._monitoring:
            return
        if self._run_active:
            raise RuntimeError('An experiment is going on, stop it first')
        self._monitoring = False
        for unit in self.units:
            unit.stop_live()
        self._lock_modules(False)
        self.set_action_checked('monitor', False)
        self.set_permanent_status('')

    # ----------------------------------------------------------------------------------------------------------
    # display
    # ----------------------------------------------------------------------------------------------------------
    def _on_display_tick(self):
        span = self.settings['experiment', 'history'] * 60.
        for view in self.views:
            if self.dock_visible(f'spectrum{view.number}') or self.dock_visible(f'rois{view.number}'):
                view.refresh(span)
        if self._pending_start:
            self._begin_if_ready()
        if self._monitoring and not self._run_active:
            for unit in self._active_units():
                for message in unit.live_notices(time.monotonic()):
                    self.status_signal.emit(message)

    # ----------------------------------------------------------------------------------------------------------
    # the experiment
    # ----------------------------------------------------------------------------------------------------------
    @property
    def run_active(self) -> bool:
        return self._run_active

    def _running_units(self) -> list[Unit]:
        return [unit for unit in self.units if unit.state == UnitState.RUNNING]

    def _update_period(self) -> float:
        """Never zero: a setting loaded from a file does not keep its minimum"""
        return max(0.05, float(self.settings['experiment', 'update_period']))

    def _watchdog_limit(self, unit: Unit) -> float:
        """The time without data after which a ramp is brought back (0: never), at least a few readout times"""
        limit = float(self.settings['experiment', 'watchdog'])
        if limit > 0:
            limit = max(limit, 3. * (unit.settings.readout_time + unit.acquisition_time()) + 5.)
        return limit

    def start_run(self, *_):
        """Run the ramps of the units and record"""
        if self._run_active or self._pending_start:
            return
        units = self._active_units()
        for unit in units:
            if unit.state == UnitState.IDLE:
                unit.attach()
        problems = self._check_cross_problems(units) + [p for unit in units for p in unit.problems()]
        if problems:
            self._report_problems(problems)
            return
        if not self._monitoring:
            self._monitoring_for_run = True
            if not self.start_monitoring():
                self._monitoring_for_run = False
                return
        self.set_action_enabled(WorkFlowActions.START, False)
        self.set_action_enabled(WorkFlowActions.STOP, True)
        missing = self._missing_data()
        if missing:
            # the first measurement must have everything, or the arrays of the file would not line up
            self._pending_start = True
            self.set_permanent_status(f'Waiting for {", ".join(missing)}')
            timeout = max(30., max(3. * (u.settings.readout_time + u.acquisition_time()) + 10. for u in units))
            self._start_timer.start(int(timeout * 1000))
            return
        self._begin_run()

    def _missing_data(self) -> list[str]:
        """The modules which have not delivered anything usable yet: a spectrum and a temperature are needed"""
        return [name for unit in self._active_units() for name in unit.missing()]

    def _begin_if_ready(self):
        if self._pending_start and not self._missing_data():
            self._pending_start = False
            self._start_timer.stop()
            self._begin_run()

    def _check_pending_start(self):
        if self._pending_start:
            missing = ', '.join(self._missing_data()) or 'the modules'
            self._pending_start = False
            self._end_start_attempt()
            self._notify('Cannot start', f'Nothing came from {missing}', error=True)

    def _end_start_attempt(self):
        self._start_timer.stop()
        self.set_action_enabled(WorkFlowActions.START, True)
        self.set_action_enabled(WorkFlowActions.STOP, False)
        if self._monitoring_for_run:
            self._monitoring_for_run = False
            self.stop_monitoring()
        else:
            self.set_permanent_status('Live' if self._monitoring else '')

    def _prepare_file(self) -> bool:
        """A new file for the experiment (SWMR if the h5py backend is there), the previous one is closed"""
        manager = self.h5_manager
        # (the h5saver property would reopen the file of the last experiment, which may be open elsewhere)
        h5saver = manager._init_h5_saver() if hasattr(manager, '_init_h5_saver') else manager.get_h5saver()
        manager.close_file()
        swmr = saving.prepare_file(h5saver)
        manager.update_file_status_led()
        return swmr

    def _begin_run(self):
        units = self._active_units()
        try:
            origin, now = time.time(), time.perf_counter()
            recordings = [UnitRecording.from_unit(unit) for unit in units]
            self._recordings = {recording.number: recording for recording in recordings}
            self._saving = self.is_action_checked(WorkFlowActions.LOG) and any(r.is_saved for r in recordings)
            self._swmr = False
            if self._saving:
                self._swmr = self._prepare_file()
                self._module_and_data_saver.configure(recordings)
                self._worker.start()  # (it may show a dialog, the timers run meanwhile: the run is not active yet)
            elif self.is_action_checked(WorkFlowActions.LOG):
                self.status_signal.emit('Nothing is recorded: no spectrum, ROI or temperature is ticked')
            self._origin = origin
            for unit in units:
                unit.begin_run(origin, now)
            self._run_active = True
            self._lock_ui(True)
            self.set_action_enabled(WorkFlowActions.PAUSE, True)
            self.set_action_checked(WorkFlowActions.PAUSE, False)
            self._ramp_timer.start(int(round(self._update_period() * 1000)))
            self._on_ramp_tick()
        except Exception as e:
            logger.exception('The experiment could not start')
            self._run_active = False
            self._ramp_timer.stop()
            for unit in units:
                unit.end_run()
            try:
                if self._worker.is_running:
                    self._worker.stop('The experiment could not start')
            except Exception:
                logger.exception('The saver could not be stopped')
            self._lock_ui(False)
            self._end_start_attempt()
            self._notify('Cannot start', f'The experiment could not start: {e}', error=True)

    @logged
    def _on_measurement(self, measurement: Measurement):
        """A unit read its spectrometer out: what is recorded goes to the thread which writes the file"""
        if not (self._run_active and self._saving and self._worker.is_running) or self._finishing:
            return
        recording = self._recordings.get(measurement.unit)
        if recording is None:
            return
        for dte in build_dtes(recording, measurement):
            self._worker.save(dte)

    @logged
    def _on_ramp_tick(self):
        if not self._run_active:
            return
        now = time.perf_counter()
        for unit in self._running_units():
            unit.program_tick(now)
        self._check_watchdogs()
        self._show_status()
        if self._experiment_over():
            self._finish_run(self._over_message())

    def _check_watchdogs(self):
        """A spectrometer or a thermostat which delivers nothing any more: its ramp is brought back"""
        now = time.monotonic()
        for unit in self._running_units():
            found = unit.silent_for(now, self._watchdog_limit(unit))
            if found is not None:
                unit.fail(*found)

    def _experiment_units(self) -> list[Unit]:
        return [unit for unit in self.units if unit.state == UnitState.RUNNING]

    def _experiment_over(self) -> bool:
        """Every ramp is over (the units without ramp only record until Stop), or nothing can be read any more"""
        units = self._experiment_units()
        if not units:
            return False
        programmed = [unit for unit in units if unit.program is not None]
        if programmed:
            return all(unit.program_over for unit in programmed)
        return all(unit.failed for unit in units)

    def _over_message(self) -> str:
        programmed = [unit for unit in self._experiment_units() if unit.program is not None]
        return 'The temperature ramps are over' if programmed else 'Nothing can be read any more'

    def _show_status(self):
        units = self._experiment_units()
        elapsed = format_duration(time.time() - self._origin)
        self.settings.child('experiment', 'elapsed').setValue(elapsed)
        self.set_permanent_status(f'{elapsed}  ' + '   |   '.join(unit.status_text() for unit in units))

    def pause_run(self, pause: bool = True):
        """Freeze the set-points (the recording goes on) or carry on"""
        if not self._run_active:
            return
        now = time.perf_counter()
        for unit in self._running_units():
            unit.pause(now, pause)
        self._on_ramp_tick()

    def stop_run(self, *_, reason: str = 'Stopped by the user'):
        """Bring the thermostats back to their initial value, then end the experiment

        A second call, while they are on their way back, offers to stop right there.
        """
        if self._pending_start:
            self._pending_start = False
            self._end_start_attempt()
            return
        if not self._run_active or self._finishing:
            return
        now = time.perf_counter()
        programmed = [unit for unit in self._running_units() if unit.program is not None and not unit.program_over]
        cycling = [unit for unit in programmed if not unit.returning]
        if cycling:
            for unit in cycling:
                unit.stop_program(now, reason)
            self.set_action_enabled(WorkFlowActions.PAUSE, False)
            self.set_action_checked(WorkFlowActions.PAUSE, False)
            self._on_ramp_tick()
            return
        if programmed:
            if self._ask_abort_return():
                self._finish_run('Stopped before the thermostats were back to their initial temperature', abort=True)
            return
        self._finish_run(reason)  # nothing to bring back

    def stop(self):
        """Programmatic stop of the extension (see stop_run)"""
        self.stop_run()

    def _on_unit_problem(self, unit: Unit, kind: Problem, message: str):
        """A module of a unit does not work: the unit has done what it can for its thermostat, the user is told"""
        kind = Problem(kind)
        if kind == Problem.THERMOSTAT_LOST:
            last = f'{unit.last_sent:.4g} {unit.units}' if unit.last_sent is not None else 'unknown'
            text = (f'{message}. It cannot be returned to its initial temperature by the extension: its last '
                    f'set-point was {last}. Check the thermostat.')
        elif unit.returning:
            text = f'{message}\n\nThe set-point is brought back to the initial temperature.'
        else:
            text = message
        self._notify('Problem', text, error=True)
        if self._run_active:
            self._on_ramp_tick()  # (the experiment may be over)

    def _finish_run(self, message: str, abort: bool = False):
        """End the experiment: the thermostats are where they should be (or the user said to stop right there)

        Each step may fail (a lost thermostat, a full disk...) without keeping the following ones from being done.
        """
        if not self._run_active or self._finishing:
            return
        self._finishing = True
        units = self._running_units()
        problems = []
        try:
            self._ramp_timer.stop()
            now = time.perf_counter()
            for unit in units:
                if unit.runner is None or unit.thermostat is None:
                    continue
                if abort:
                    unit.abort_program(now)
                else:
                    state = unit.runner.state(now)
                    if state.finished and (unit.last_sent is None or abs(unit.last_sent - state.setpoint) > 1e-9):
                        unit.send_setpoint(state.setpoint)  # the initial temperature goes first
            if self._worker.is_running and not self._worker.drain():
                # (the saver thread is stopped by stop(): what is still queued would be lost)
                problems.append('The saver stopped answering: the end of the experiment is missing from the file')
        except Exception:
            logger.exception('A problem while ending the experiment')
        finally:
            self._run_active = False
            self._finishing = False
        try:
            if self._worker.is_running:
                self._worker.stop(message)
        except Exception as e:
            logger.exception('The data file could not be closed properly')
            problems.append(f'The data file could not be closed properly: {e}')
        if self._module_and_data_saver.n_errors:
            problems.append(f'{self._module_and_data_saver.n_errors} data could not be saved, see the log')
        for unit in units:
            if not abort and unit.program is not None and unit.thermostat is not None \
                    and not unit.thermostat.initialized_state:
                problems.append(f'The thermostat {unit.thermostat.title} is not initialized any more: '
                                'the initial temperature may not have been set')
        try:
            for unit in units:
                unit.end_run()
            self._lock_ui(False)
            self.set_action_enabled(WorkFlowActions.START, True)
            self.set_action_enabled(WorkFlowActions.STOP, False)
            self.set_action_enabled(WorkFlowActions.PAUSE, False)
            self.set_action_checked(WorkFlowActions.PAUSE, False)
            self.set_permanent_status(message)
            self.status_signal.emit(message)
        except Exception:
            logger.exception('The interface could not be restored')
        if self._monitoring_for_run:
            self._monitoring_for_run = False
            try:
                self.stop_monitoring()
            except Exception:
                logger.exception('The live view could not be stopped')
        if problems:
            self._notify('Problem', '\n'.join(problems), error=True)

    # ----------------------------------------------------------------------------------------------------------
    # closing
    # ----------------------------------------------------------------------------------------------------------
    def _wait_for_saver(self, timeout: float = 10.0):
        """Let the thread writing the file finish what it was given"""
        end = time.perf_counter() + timeout
        while time.perf_counter() < end and self._worker.is_saving:
            QtWidgets.QApplication.processEvents()
            time.sleep(0.01)

    def _dashboard_is_quitting(self) -> bool:
        """True when this call comes from DashBoard.quit_fun, which ignores the answer and closes the modules after"""
        frame = sys._getframe(1)
        while frame is not None:
            if frame.f_code.co_name == 'quit_fun' and frame.f_locals.get('self') is self.dashboard:
                return True
            frame = frame.f_back
        return False

    def _quit_fun(self) -> bool:
        if self._pending_start:
            self._pending_start = False
            self._end_start_attempt()
        if self._run_active:
            if self._dashboard_is_quitting():  # the extension cannot go on: nothing else is safe
                logger.info('PyMoDAQ is quitting during an experiment: the initial temperatures are set now')
                choice = 'jump'
            else:
                choice = self._ask_quit_choice()
            if choice == 'return':
                self.stop_run(reason='The extension was asked to close')
                return False
            if choice == 'cancel':
                return False
            # 'jump': the set-points are changed at once, nothing can follow the ramps once the extension is gone
            now = time.perf_counter()
            for unit in self._running_units():
                unit.abort_program(now, jump=True)
            self._finish_run('Closed during the experiment, the initial temperatures were set', abort=True)
            self._wait_for_saver()
        if self._monitoring:
            self.stop_monitoring()
        self._display_timer.stop()
        self._layout_timer.stop()
        self._init_timer.stop()  # (the modules which were being connected are left alone)
        self._stop_waiting_for_init()
        self.save_rois()
        self.h5_manager.close_file()
        return True


class ThermoSpectroWorker(ExtensionWorker):
    """Runs the thread that writes the file (see ExtensionWorker)"""

    def __init__(self, app: ThermoSpectro, parent=None):
        super().__init__(app=app, parent=parent)

    @property
    def is_saving(self) -> bool:
        """The thread writing the file is still running: some data may be left to write"""
        return bool(self._app.settings[SaverWorker.worker_setting_name, 'worker_running'])

    def save(self, dte: DataToExport):
        """Queue some data to be written by the saver thread"""
        self.thread_manager.n_jobs[SaverWorker.name] += 1
        self.saver_worker.data_to_save_signal.emit(DataBundle(dte=dte))

    def drain(self, stall: float = 10.0) -> bool:
        """Wait until the saver thread has written everything it was given

        ExtensionWorker.stop() ends this thread as soon as the number of tasks shown in the settings is 0, but it is
        only updated when the thread reports a job done: what has just been queued would be dropped. A slow disk can
        make it take a while: this gives up only if nothing was written for `stall` seconds.

        Returns
        -------
        bool
            False if it gave up
        """
        worker = self.saver_worker
        if worker is None:
            return True
        done, changed = worker._n_jobs_done, time.perf_counter()
        while worker._n_jobs_done < self.thread_manager.n_jobs[SaverWorker.name]:
            if worker._n_jobs_done != done:
                done, changed = worker._n_jobs_done, time.perf_counter()
            elif time.perf_counter() - changed > stall:
                logger.error(f'The saver wrote nothing for {stall:g} s: '
                             f'{self.thread_manager.n_jobs[SaverWorker.name] - done} data are lost')
                return False
            time.sleep(0.001)
        return True

    def _update_status(self, msg: str):
        logger.info(msg)

    def _start(self):
        """Called by start(), once the saver thread is running: the group of the experiment is made in the file"""
        self.module_and_data_saver.get_set_node(new=True)

    def _pause(self, do_pause: bool = True):
        pass

    def _stop(self, msg: str = None):
        pass

    def _on_workers_terminated(self):
        """The file is closed: the end of SWMR makes it complete (the shapes of its arrays are updated)"""
        try:
            h5saver = self._app._module_and_data_saver.h5saver  # (not the properties: they would reopen a file)
            if h5saver is not None:
                saving.finalize_file(h5saver)
        except Exception:
            logger.exception('The end of SWMR failed: the file may need to be opened once for its attributes '
                             'to be updated')
