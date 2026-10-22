"""The spectra are in nm (never knm), and each ROI of a spectrum has the color of the wavelength in the middle of its window"""
import numpy as np
import pyqtgraph as pg
import pytest
from qtpy import QtGui

from pymodaq_plugins_thermospectro.camera import RectRoi
from pymodaq_plugins_thermospectro.rois import Roi
from pymodaq_plugins_thermospectro.views import CameraView, UnitView
from pymodaq_plugins_thermospectro.widgets import (PALETTE, PlotToggles, SpectrumPlot, contrast_text, lightness,
                                                   spectrum_roi_colors, wavelength_color)

from test_hold_and_log import Bench

pytestmark = pytest.mark.filterwarnings('ignore')


def rgb(color: str) -> tuple[int, int, int]:
    c = QtGui.QColor(color)
    return c.red(), c.green(), c.blue()


def level(color: str) -> float:
    red, green, blue = (value / 255. for value in rgb(color))
    return lightness(red, green, blue)


class TestTheAxisIsInNm:
    @staticmethod
    def label(plot) -> str:
        """The label of the axis, as pyqtgraph draws it (it works its prefix out when the range or the label change)"""
        return plot.getPlotItem().getAxis('bottom').labelString()

    def test_a_spectrum_beyond_1000_nm_is_not_in_knm(self, qtbot):
        """A spectrometer of 190 to 1100 nm: the axis said (knm) and its ticks were 0.2, 0.4... 1.0"""
        plot = SpectrumPlot('Spectrometer 1')
        qtbot.addWidget(plot)
        plot.resize(400, 200)
        plot.show()
        x = np.linspace(190., 1100., 2048)

        plot.set_spectrum(x, np.exp(-((x - 650.) / 50.) ** 2), 'nm', 'photon wavelength')
        qtbot.wait(50)

        axis = plot.getPlotItem().getAxis('bottom')
        text = self.label(plot)
        assert 'knm' not in text and '(nm)' in text
        assert axis.autoSIPrefixScale == 1.0

    def test_what_a_plain_plot_does(self, qtbot):
        """(The control: this is what pyqtgraph does when it is not told not to)"""
        plot = pg.PlotWidget()
        qtbot.addWidget(plot)
        plot.resize(400, 200)
        plot.show()
        plot.getPlotItem().setLabel('bottom', 'photon wavelength', units='nm')
        plot.getPlotItem().setXRange(190., 1100., padding=0.)
        qtbot.wait(50)

        assert 'knm' in self.label(plot)

    def test_a_spectrum_below_1000_nm_is_in_nm_as_well(self, qtbot):
        plot = SpectrumPlot('Spectrometer 1')
        qtbot.addWidget(plot)
        plot.show()
        x = np.linspace(400., 700., 601)

        plot.set_spectrum(x, np.zeros_like(x), 'nm', 'photon wavelength')
        qtbot.wait(50)

        assert '(nm)' in self.label(plot)


class TestWavelengthColors:
    def test_450_nm_is_blue(self):
        red, green, blue = rgb(wavelength_color(450.))

        assert blue > 200 and red < 40 and green < 120

    @pytest.mark.parametrize('nm, dominant', [(450., 'blue'), (500., 'green'), (532., 'green'), (589., 'red'),
                                              (650., 'red')])
    def test_the_colors_of_the_rainbow(self, nm, dominant):
        red, green, blue = rgb(wavelength_color(nm))

        assert max(red, green, blue) == {'red': red, 'green': green, 'blue': blue}[dominant]

    def test_yellow_green_and_orange(self):
        assert rgb(wavelength_color(580.))[0] > 200 and rgb(wavelength_color(580.))[1] > 200 > rgb(wavelength_color(580.))[2]
        green = rgb(wavelength_color(532.))
        assert green[1] > 200 and green[2] < 40 and green[0] < 150
        orange = rgb(wavelength_color(610.))
        assert orange[0] > 200 and 60 < orange[1] < 200 and orange[2] < 40

    def test_violet_and_red_at_the_ends(self):
        red, green, blue = rgb(wavelength_color(400.))
        assert blue > 200 and red > 90 and green < 60  # violet
        red, green, blue = rgb(wavelength_color(700.))
        assert red > 200 and green < 60 and blue < 60

    def test_the_wavelengths_of_the_spectrum_have_their_own_colors(self):
        colors = [wavelength_color(nm) for nm in range(400, 641, 20)]  # (from 645 nm on, the rainbow is all red)

        assert len(set(colors)) == len(colors)

    def test_neighbours_have_neighbouring_colors(self):
        previous = rgb(wavelength_color(300.))
        for nm in range(301, 1101):
            current = rgb(wavelength_color(float(nm)))
            assert max(abs(a - b) for a, b in zip(previous, current)) <= 30, nm  # (no jump)
            previous = current

    def test_the_ultraviolet_is_violet_and_greyer_the_further_it_is(self):
        near, far = rgb(wavelength_color(370.)), rgb(wavelength_color(220.))

        assert near[2] > near[1] and far[2] > far[1]  # (still violet-ish)
        assert max(far) - min(far) < max(near) - min(near)  # (but greyer)
        assert wavelength_color(370.) != wavelength_color(220.) != wavelength_color(400.)

    def test_the_infrared_is_a_dull_red_and_greyer_the_further_it_is(self):
        near, far = rgb(wavelength_color(800.)), rgb(wavelength_color(1000.))

        assert near[0] > near[1] and far[0] > far[1] and near[0] > near[2]
        assert max(far) - min(far) < max(near) - min(near)
        assert wavelength_color(800.) != wavelength_color(1000.)
        assert wavelength_color(1000.) != wavelength_color(700.)  # (not the red of the rainbow)

    def test_what_is_not_a_wavelength_is_grey(self):
        assert wavelength_color(float('nan')) == wavelength_color(float('inf')) == PALETTE[7]

    def test_on_a_dark_background_every_color_is_light_enough(self):
        for nm in range(200, 1101, 5):
            assert level(wavelength_color(float(nm), light=False)) >= 0.2, nm

    def test_on_a_light_background_no_color_is_too_light(self):
        for nm in range(200, 1101, 5):
            assert level(wavelength_color(float(nm), light=True)) <= 0.57, nm

    def test_the_background_of_pyqtgraph_decides_by_default(self, monkeypatch):
        yellow_dark = wavelength_color(580., light=False)
        monkeypatch.setattr(pg, 'getConfigOption', lambda name: 'w' if name == 'background' else 'k')

        assert wavelength_color(580.) == wavelength_color(580., light=True) != yellow_dark
        monkeypatch.setattr(pg, 'getConfigOption', lambda name: 'k')
        assert wavelength_color(580.) == yellow_dark

    def test_a_text_is_read_on_the_color(self):
        assert contrast_text(wavelength_color(580.)) == '#000000'  # (yellow)
        assert contrast_text(wavelength_color(532.)) == '#000000'  # (green)
        assert contrast_text('#0046ff') == '#ffffff' and contrast_text('#1f77b4') == '#ffffff'


class TestTheColorOfARoi:
    def test_it_is_the_one_of_the_middle_of_its_window(self):
        """440 to 460 nm: the color of 450 nm, blue"""
        colors = spectrum_roi_colors([Roi('A', 440., 460.), Roi('B', 445., 455.), Roi('C', 520., 560.)], 'nm')

        assert colors[0] == colors[1] == wavelength_color(450.) and rgb(colors[0])[2] > 200
        assert colors[2] == wavelength_color(540.)

    def test_a_spectrum_which_is_not_in_nm_has_the_palette(self):
        rois = [Roi('A', 440., 460.), Roi('B', 520., 560.)]

        assert spectrum_roi_colors(rois, 'pixel') == PALETTE[:2]
        assert spectrum_roi_colors(rois, '') == PALETTE[:2]


class TestTheViewOfASpectrometer:
    @pytest.fixture
    def bench(self, qtbot):
        bench = Bench(qtbot)
        bench.settings.set_rois([Roi('Blue', 440., 460.), Roi('Green', 520., 540.), Roi('Red', 640., 660.),
                                 Roi('IR', 880., 920.)])
        yield bench
        bench.unit._release()

    @pytest.fixture
    def view(self, qtbot, bench):
        view = UnitView(bench.unit)
        qtbot.addWidget(view.spectrum_plot)
        x = np.linspace(190., 1100., 2048)
        view.spectrum_plot.set_spectrum(x, np.ones_like(x), 'nm', 'photon wavelength')
        view.update_definitions()
        return view

    @staticmethod
    def expected(*centers):
        return [wavelength_color(center) for center in centers]

    def test_the_windows_the_curves_the_buttons_and_the_numbers_have_the_color_of_their_wavelength(self, view):
        colors = self.expected(450., 530., 650., 900.)

        assert [view.spectrum_plot.region_color(key) for key in view.spectrum_plot.roi_keys] == colors
        assert view.toggles.colors[:4] == colors
        assert [curve.opts['pen'].color().name() for curve in view.curves_plot.curves] == colors
        for key, color in zip(view.manager.keys, colors):
            assert color in view.manager.row(key).badge.styleSheet()

    def test_the_first_one_is_blue_not_the_first_color_of_the_palette(self, view):
        assert rgb(view.spectrum_plot.region_color('roi00'))[2] > 200
        assert view.spectrum_plot.region_color('roi00') != PALETTE[0]

    def test_the_text_of_a_number_is_read_on_its_color(self, view):
        assert 'color: #ffffff' in view.manager.row('roi00').badge.styleSheet()  # (on blue)
        assert 'color: #000000' in view.manager.row('roi01').badge.styleSheet()  # (on green)

    def test_the_colors_follow_the_edges_when_the_window_is_dropped(self, view, bench):
        bench.settings.set_roi_edges('roi00', 590., 610.)  # (as the extension does once it is dropped)

        view.update_definitions()

        assert view.spectrum_plot.region_color('roi00') == wavelength_color(600.)
        assert view.toggles.colors[0] == wavelength_color(600.)
        assert view.curves_plot.curves[0].opts['pen'].color().name() == wavelength_color(600.)

    def test_a_window_takes_the_color_of_the_wavelength_it_is_dragged_to(self, view):
        region = view.spectrum_plot._regions['roi00']

        region.setRegion((590., 610.))  # (a drag: the plot says the window is moving)

        assert view.spectrum_plot.region_color('roi00') == wavelength_color(600.)
        assert wavelength_color(600.) in view.manager.row('roi00').badge.styleSheet()
        assert view.manager.row('roi00').start.value() == pytest.approx(590.)  # (its numbers still follow)

    def test_a_spectrum_in_pixels_has_the_palette(self, view, bench):
        bench.settings.set_axis_units('pixel')

        view.update_definitions()

        assert [view.spectrum_plot.region_color(key) for key in view.spectrum_plot.roi_keys] == PALETTE[:4]

    def test_dragging_in_pixels_does_not_recolor(self, view, bench):
        bench.settings.set_axis_units('pixel')
        view.update_definitions()

        view.spectrum_plot._regions['roi00']._on_move = None  # (nothing special: only the region is moved)
        view.spectrum_plot._regions['roi00'].setRegion((590., 610.))

        assert view.spectrum_plot.region_color('roi00') == PALETTE[0]

    def test_the_buttons_change_color_when_the_colors_do(self, view, bench):
        before = list(view.toggles.colors)
        bench.settings.set_roi_edges('roi01', 480., 500.)

        view.update_definitions()

        assert view.toggles.colors[1] != before[1] and view.toggles.colors[1] == wavelength_color(490.)
        assert view.toggles.colors[0] == before[0]


class TestTheViewOfACamera:
    def test_the_rectangles_keep_the_palette(self, qtbot):
        rois = [RectRoi('A', 1, 5, 1, 5), RectRoi('B', 10, 20, 10, 20)]
        view = CameraView.__new__(CameraView)  # (only its colors are looked at: no unit needed)

        assert view.roi_colors(rois) == PALETTE[:2]


def test_the_buttons_know_their_colors(qtbot):
    toggles = PlotToggles()
    qtbot.addWidget(toggles)

    toggles.set_entries([('a', 'A', '#ff0000', True), ('b', 'B', '#ffe000', False)])

    assert toggles.colors == ['#ff0000', '#ffe000']
    assert 'color: #000000' in toggles.button('b').styleSheet() and 'color: #ffffff' in toggles.button('a').styleSheet()
