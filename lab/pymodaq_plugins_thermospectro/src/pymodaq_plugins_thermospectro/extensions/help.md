# ThermoSpectro

ThermoSpectro records one or two spectrometers and a camera, each with a **thermostat**, while the temperature of
the thermostat follows a ramp: up to a final value, a wait, back to the initial value, a wait, as many times as asked.

Each detector and its thermostat make a *unit*: they have their own settings (*Spectrometer 1*, *Spectrometer 2* and
*Camera* in the settings tree), their own plots, their own ramp. The camera is the third unit: a 2D detector of the
Dashboard (a Thorlabs camera, for instance). It can have a thermostat of its own, or **share the one of a
spectrometer**: one unit then drives it (*Run the ramp*) and the other only records its temperature. The presets
`ThermoCamera...` start with a camera and a thermostat, `ThermoCameraSpectro...` with a camera, a spectrometer and one
thermostat (see the README).

## Typical workflow

1. Start the extension with the command `thermospectro`: the Dashboard loads a preset with the spectrometers (1D
   detectors) and the thermostats (actuators whose position is the temperature), and this extension opens. (Or load a
   preset by hand in *Tools > Experiment > Load Experiments* and open the extension in *Tools > Extensions*.)
   **Nothing is connected when a preset opens**: when the spectrometers and the thermostats are plugged in, click
   **Connect the modules** (the plug button of the toolbar): they are connected (initialized) one after the other, and
   the ones which could not be are told at the end (the *Ini* button of each module in the Dashboard does it one by
   one). The first 1D detector is spectrometer 1, the second one spectrometer 2;
   the thermostat is guessed from its name (*Thermostat1*, *Temperature*...). A spectrometer which is not in the preset
   leaves its unit unticked (*Use it*): `thermospectro ThermoSpectroSingleSeabreeze` (or `ThermoSpectroSingleMock`,
   without hardware) starts with a single unit.
2. Click **Live** (camera): each spectrometer is read out once per **readout time** (1 s by default), after the
   **integration time** (500 ms by default) was set on it, and the temperature is read at the same time. The
   readout time must be longer than the integration time, times the *Naverage* of the spectrometer module if it
   averages spectra.
3. Choose the **ROIs** (see below), then set the **temperature ramp** of each unit.
4. Tick what is recorded to disk, choose the folder in the *Saving* tab and click **Start**.
5. **Pause** freezes the set-points (the recording goes on), **Stop** brings every thermostat back to its initial
   value before the experiment ends (a second Stop offers to stop right there). The experiment also ends by itself
   when all the ramps are over.

## The ROIs

A ROI (region of interest) is a window of the spectrum; its value is **the mean of the pixels in the window**.
Each spectrometer starts with **six ROIs**, picked at random (always the same ones). They are drawn on the spectrum
and listed in the **ROI manager**, the tab next to the settings, with a table for each spectrometer:

* **type** the name and the two edges of a window (*From*, *To*): Enter, or the arrows, which step by one pixel. An
  edge typed past the other one pushes it, so a window is moved by typing its two edges, whichever way it goes. A
  window which is entirely outside of the spectrum is written in red (its value is empty);
* **drag a window** on the spectrum to move it, **drag one of its edges** to resize it: the manager follows as you go;
* **add** a ROI with *Add a ROI* (it is put in the middle of the spectrum, ready to be named), by **double clicking**
  on the spectrum, or with a **right click** on it, where you want the window;
* **delete** a ROI with the button at the end of its row, or with a **right click** on its window on the spectrum;
* *Reset to the default ROIs* brings the six first ones back.

The ROIs cannot be changed during an experiment (what is plotted can). The same ROIs are in the settings, under *ROIs*.

The ROIs are **remembered**: as soon as you change them they are saved in `~/.pymodaq/thermospectro/rois.json`
(one list per spectrometer), and the next experiment, in any session, starts with exactly the same ROIs. That file
is plain json: it can be copied to another computer.

Each ROI has two boxes: **Record** (its value is saved in the file) and **Plot** (it is drawn). They are
independent. The buttons above the plot of the ROIs do the same as *Plot*.

## The temperature ramp

A ramp goes from the *initial value* to the *final value* (above or below). Three of these four numbers define it,
the fourth one is computed and shown (read-only); choose which in **Defined by**:

* initial value, final value, velocity
* initial value, final value, duration
* initial value, velocity, duration (a negative velocity cools down)

Then the temperature **waits at the final value**, **goes back to the initial value** (*Way back defined by*: the
same velocity as the way there, another velocity, or a duration) and **waits at the initial value**. The cycle is
repeated *Number of cycles* times (0: until Stop). If the thermostat does not start at the initial value, it is
brought there first. The ramps of the two units are independent (different values, speeds, number of cycles). Untick
*Run the ramp* to only record a unit, without driving its thermostat.

The thermostat is given a new set-point every second (*Set-point every*), so any thermostat with a PyMoDAQ plugin can
be used, whatever its own ramping capabilities. Protect it with the bounds of its actuator (*Set bounds* in the
settings of the DAQ_Move): the extension does not know its limits.

## The camera

The camera works as a spectrometer, with images: each readout takes one image (after its **exposure time**, which is
set on the camera when the live view starts, like the integration time of a spectrometer), and the value of a ROI is
**the mean of the pixels in its rectangle**. The camera starts with **four ROIs**, small squares picked at random (always
the same ones), which are fitted to the size of the image when the first image comes if you did not change them.

* the image is drawn small, with a numbered rectangle for each ROI: **drag a rectangle** to move it, **drag its
  corner** to resize it, **double click** (or right click) on the image to add a ROI there, right click on a
  rectangle to delete it;
* the **ROI manager** has a table for the camera too: the name and the four edges of each rectangle, in pixels
  (*x from*, *x to*: the columns, *y from*, *y to*: the rows, the first row being the top one);
* the full image is **not recorded by default** (it is megabytes at each readout): tick *The full image* under
  *Record to disk* to save it (`Unit3_Image`). The ROIs and the temperature are recorded as for a spectrometer
  (`Unit3_ROIs`, `Unit3_Temperature`, `Unit3_Program`).

## The plots

For each unit, a small plot of the **spectrum** (the **image** of a camera) with its ROIs, and a plot of the **ROIs** over time with the
**temperature** (measured, and the set-point dashed) drawn on top of them, on **its own scale** (at the right). Every
window, plots, settings and saving, can be **closed at any time and opened again from the Plots menu**. *Plot the
last* limits how much of the time axis is drawn.

## What is recorded

Independently of each other, per unit: **the full spectrum** (the full image of a camera), **each ROI**, **the temperature** (with the set-point,
the cycle and the phase of the ramp). Untick the *Do logging* button of the toolbar to record nothing at all.

Each experiment is written in **a new h5 file** in the folder of the *Saving* tab, in **SWMR** mode (single writer,
multiple readers): another program can open it while it is written, for instance:

```python
import h5py
with h5py.File('Dataset_20260101_000.h5', 'r', swmr=True) as f:
    ...  # refresh() a dataset to see what was added since
```

The file has one group `Datalogger000`, and in it one group per kind of data, found by its title (the names
`Detector000`... only number them): `Unit1_Spectrum`, `Unit1_ROIs`, `Unit1_Temperature`, `Unit1_Program` (and
`Unit2_...`, and `Unit3_Image`, `Unit3_ROIs`... for the camera). Their arrays are enlarged along a time axis (epoch seconds, **the same for the groups of a unit**, it is
the time at which the readout started); the attributes of the groups tell the spectrometer and the thermostat, the
integration and readout times and, for the ROIs, their windows. The codes of `Unit1_Program` phases are in its
`phase_codes` attribute. The file can be loaded with the H5 browser or `pymodaq_data.h5modules.data_saving.DataLoader`.

## Safety

The thermostats are never left hot on purpose: the set-point of a unit returns to its initial value on Stop, when
its spectrometer or its thermostat does not deliver anything for the time set in *Stop if no data for* (30 s, and at
least a few readouts), and when a module is closed. A thermostat which is closed cannot be brought back: the extension
says so, with its last set-point. When the extension is closed during an experiment, a dialog asks whether to set the
initial temperatures at once (the proposed answer) or to bring them back gradually; when PyMoDAQ itself is quit, they
are set at once.
