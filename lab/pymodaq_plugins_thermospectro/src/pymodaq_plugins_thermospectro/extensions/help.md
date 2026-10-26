# ThermoSpectro

Five applications record spectrometers and a camera while a thermal controller **holds a temperature** or **runs a
ramp**. The command which started this one (`thermo_2spectro`, `thermo_spectro`, `camera`, `thermo_camera`,
`thermo_camera_spectro`) gave it the settings and the windows of its instruments and nothing else.

Each detector and its thermal controller make a *unit* (*Spectrometer 1*, *Spectrometer 2* and *Camera* in the settings
tree): their own settings, plots and temperature mode. A camera and a spectrometer can **share one thermal controller**:
one unit drives it (*Drive it*), the other only records its temperature.

## Typical workflow

1. The command looked for the instruments connected to the computer, connected the ones it recognized and told which it did
   not (a message, and the status bar). For one which was **not recognized**, select it in the settings of its module in the
   Dashboard window (for a thermal controller, its port, *COM6* for instance, in *Port* or *Other port*), then click the
   *Ini* button of the module, or **Connect the modules** (the plug button of the toolbar here). **Nothing is acquired
   until you press Start.**
2. Click **Live** (optional): each instrument is read out once per **readout time** (1 s by default), after its
   **integration time** (**exposure time**) was set on it, and the temperature is read at the same time. The readout time
   must be longer than the integration time, times the *Naverage* of the module if it averages. Use it to set the ROIs.
3. For each thermal controller choose its **Mode**: *Hold at a temperature* (and the temperature) or *Run a ramp*.
4. Under *Experiment*, **Log for**: *Until I stop it* or *For a given time* (and its **Duration** in hours: 48 for two
   days). Tick what is recorded to disk. In the *Saving* tab choose the folder and the name of the file and write what
   you want to remember of the experiment: its title, the sample, the operator and the protocol (see *The Saving tab*).
5. Click **Start**: everything is recorded until **Stop**, or the end of the duration. The status bar shows the time
   elapsed and left. While it runs, **note what you observe** (the field of the toolbar, or the *Saving* tab): each
   observation is saved in the file with its time.
6. **Stop**: a controller **held at a temperature stays at it** and the log ends at once; so does one whose ramps are over
   (it stays where they ended); a **ramp** which is going on is brought back to its initial value (a second Stop offers to
   stop right there), then the log ends (with one unit on a ramp and another holding a temperature, Stop brings the ramp
   back, then ends the log: the held controller stays). **Pause** freezes the set-point of a ramp.
   A cycle of ramps does **not** end the log (unless *When the ramps are over* says *End the log*): after the last cycle the
   log goes on at the initial value (or the final one) until you press Stop, or the duration is over. **End at the top** and
   **End at the bottom** end the ramps whenever you want and go on logging there.

## The ROIs

A ROI (region of interest) is a window of the spectrum; its value is **the mean of the pixels in the window**, or whatever
you choose for it (see *Value* below). Each spectrometer starts with **six ROIs**, picked at random (always the same ones). They are drawn on the spectrum
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

Each ROI has a **Value** too (in the ROI manager, and in the settings of the ROI), **what the ROI gives**: the **mean** of the
pixels of its window (the default), their **sum**, **median**, **min**, **max** or **std** (standard deviation), each a number at
each readout, plotted and saved with the temperature (the label of the ROI in the file says which: `ROI 1 sum (450-455 nm)`); or
**crop**: the window is a part of the spectrum to keep, the spectrum in the window (with its wavelengths) is saved at each
readout (or every *Crops every* seconds, under *Record to disk*) in a group of its own, `Unit1_Crop1`..., and in a csv file of its
own once converted. A crop has nothing to plot (no curve, no button) and its window is drawn with dashed edges; it must be inside
the spectrum (otherwise the experiment does not start). The *Value* is remembered with the ROIs and cannot be changed during an
experiment. A ROI gives one thing: for a number and a crop of the same window, make two ROIs.

**Per ms of integration** (under the readout time of the spectrometer): the value of each ROI is divided by the integration time,
in ms, of its readout: the curves and the file are then per ms (`A.U./ms`) and do not jump when the integration time is
changed during the experiment. **Spectra per ms of integration** (the next box, **off** by default) divides in the same way what
is saved of the spectrum: each full spectrum and the part of the spectrum of each crop; it is a separate option because a full
spectrum is the raw measurement (the integration time is saved next to it). **Intensity unit** (`A.U.` by default, arbitrary units) is the unit
of what the instrument measures, which its plugin does not say: it is on the axis of the curves and saved with the data. None of
them can be changed during an experiment (the integration time can: see below).

The wavelengths of a spectrometer are always in **nm** (the axis does not switch to *knm* beyond 1000 nm). The **color of
a ROI** is the color of the light at the wavelength in the middle of its window: a ROI between 440 and 460 nm is blue (450
nm), one around 532 nm is green, one between 640 and 660 nm is red. It is the color of its window on the spectrum, of its
curve, of its button and of its number in the ROI manager, and it follows the window while you drag it. Beyond the
visible range the color is the violet (ultraviolet, below 380 nm) or the deep red (infrared, above 780 nm) of its end of
the rainbow, greyer the further it is, so that two ROIs there still have their own color. The colors are made lighter on
a dark background and darker on a light one when they could not be seen well. When the spectrum is not in nm (a spectrometer without wavelengths) and for the
rectangles of a camera, each ROI has a color of its own from a palette.

## Hold at a temperature, or a ramp

**Hold at a temperature**: the temperature is sent to the thermal controller when the log starts (it switches its
temperature control on and goes there); it is the controller which holds it, nothing is sent again, and it stays at it when
the log ends. The file records it as the set-point, with the phase `hold`. A temperature (or a ramp) which is **out of the
range of the controller** (its *Bounds* in the module's settings, which QNW_TC fills with the range of its holder) is
**refused when you press Start**, so that a typo (400 for 40) does not leave a two day log with a controller doing nothing.

**Run a ramp**: it goes from the *initial value* to the *final value* (above or below). Three of these four numbers define it,
the fourth one is computed and shown (read-only); choose which in **Defined by**:

* initial value, final value, velocity
* initial value, final value, duration
* initial value, velocity, duration (a negative velocity cools down)

Then the temperature **waits at the final value**, **goes back to the initial value** (*Way back defined by*: the
same velocity as the way there, another velocity, or a duration) and **waits at the initial value**. The cycle is
repeated *Number of cycles* times (0: until you end the ramps, see below, or press Stop). If the thermostat does not
start at the initial value, it is brought there first. The ramps of the two units are independent (different values,
speeds, number of cycles). Untick *Drive it* to only record a unit, without driving its thermal controller.

**When the ramps are over**: the log is not over. The controller **stays where the ramps ended and the log goes on**, until
you press **Stop** (or the duration of the log is over); the file records those readouts with the phase `hold`, the set-point
being that temperature and the cycle the last one. *When the ramps are over* says where that is:

* **Stay at the initial value (bottom), go on logging** (the default): after the way back of the last cycle;
* **Stay at the final value (top), go on logging**: the last cycle stops when the top is reached (no wait there and no way
  back), the controller stays at the high temperature;
* **End the log**: the log ends with the ramps (the controller stays at the initial value).

**End at the top / End at the bottom** (the arrows of the toolbar) do the same at any time, while a ramp is going on, whatever
the setting says: the temperature goes to the final value (the top, at the speed of the ramp) or back to the initial value (the
bottom, at the speed of the way back) from wherever it is, stays there, and the log goes on. This is how cycles which last
until you stop them (*Number of cycles* 0) are ended, at the high or at the low temperature. **Stop**, on the other hand, ends the
log: during the ramps it brings them back to the initial value first, then ends (a second Stop offers to stop right there).

The thermostat is given a new set-point every second (*Set-point every*), so any thermostat with a PyMoDAQ plugin can
be used, whatever its own ramping capabilities. Protect it with the bounds of its actuator (*Set bounds* in the
settings of the DAQ_Move): the extension does not know its limits.

## The camera

The camera works as a spectrometer, with images: each readout takes one image (after its **exposure time**, which is
set on the camera when the live view starts, like the integration time of a spectrometer), and the value of a ROI is
**the mean of the pixels in its rectangle**. The camera starts with **four ROIs**, small squares picked at random (always
the same ones), which are fitted to the size of the image when the first image comes if you did not change them.

* each ROI has a **Value** (in the ROI manager, and in the settings of the ROI): the **mean** of its pixels (the default), their
  **sum**, **median**, **min**, **max** or **std** (standard deviation), each a number at each readout, or **crop**: the ROI is
  just a crop, its pixels are saved as images at each readout (or every *Crops every* seconds under *Record to disk*), and
  there is nothing to plot (no curve, no button; the rectangle is dashed). The axis of the curves says what they are, and the
  legend adds the value when they differ. A crop must be inside the image (otherwise the experiment does not start); the
  *Value* is remembered with the ROIs and cannot be changed during an experiment. A ROI gives one thing: for a number and a crop
  of the same rectangle, make two ROIs;
* a camera which gives colors (several channels, shown as red, green and blue) has the boxes **R G B** for each ROI in the ROI
  manager (and a *Channels* setting): the channels the ROI is measured on, all of them by default. With two or more ticked, the
  operation is done on each channel and **the results are averaged**: with green and blue ticked and *std*, the ROI gives the mean
  of std(green) and std(blue). Move the mouse over the boxes to read it. There are no boxes for a camera with one channel, a
  crop keeps all the channels, and the labels in the file say which channels when it is not all of them;
* the image is drawn small, with a numbered rectangle for each ROI: **drag a rectangle** to move it, **drag its
  corner** to resize it, **double click** (or right click) on the image to add a ROI there, right click on a
  rectangle to delete it;
* the **ROI manager** has a table for the camera too: the name and the four edges of each rectangle, in pixels
  (*x from*, *x to*: the columns, *y from*, *y to*: the rows, the first row being the top one);
* the full image is **not recorded by default** (it is megabytes): tick *The full image* under *Record to disk* to save
  it (`Unit3_Image`) and say how often with **Full image every** (60 s by default; 0: at each readout). The ROIs and the temperature are recorded as for a spectrometer
  (`Unit3_ROIs`, `Unit3_Temperature`, `Unit3_Program`).

## The Saving tab

It says where each experiment is saved, what the file is called and what you write about the experiment. What you type
there is **remembered from one session to the next**, but for the title and the sample: they describe one experiment, and
the ones of yesterday are not those of the one which starts.

* **Folder** (*Browse...* opens a dialog): it is made if it does not exist. It is checked when you press Start: if nothing
  can be written there the experiment does not start, and the message says why.
* **Name**: the name of the file, without `.h5` (what a file name cannot hold, such as `/` or `:`, becomes `_`).
  **Add the date and the time to the name** (on by default) makes `Name_20261022_153012.h5`; without it the file is
  `Name.h5`. **A file is never replaced**: if it exists, the next one is `Name_001.h5`. **Put the file in folders of the year
  and the day** makes `2026/20261022/` in the folder, the way PyMoDAQ arranges its files. *Next file* shows what the next
  file will look like (`YYYYMMDD_HHMMSS` is the date and the time when it starts), *Current file* the one which is being
  written, or the last one.
* **The experiment**: a *title*, the *sample*, the *operator* and the *protocol* (a text of any length, up to a million
  characters: what is done, how, with what). They are written in the file when the log starts, and cannot be changed until it ends (these fields are
  locked): use the observations for what happens during the log.
* **Observations**: while a log runs, type what you notice (the door was opened, the sample changed color...) in the field of
  this tab or in the one of the toolbar and press **Enter**. It is saved in the file at once, with its time, and listed in
  the tab with the time of the day and the time since the start; the list is emptied when the next log starts (the
  observations are in the file of their experiment). An observation has at most a thousand characters. They can be written
  only while a log runs and *Do logging* is on (otherwise nothing could be saved: the fields are greyed, and the tab says why).
* **The h5 file (advanced)**, closed at first: the settings of PyMoDAQ's h5 saver (backend, compression...).

The folder, the name and the experiment are locked during a log (the file exists from its start); the observations are not.

## The plots

For each unit, a small plot of the **spectrum** (the **image** of a camera) with its ROIs, and a plot of the **ROIs** over time with the
**temperature** (measured, and the set-point dashed) drawn on top of them, on **its own scale** (at the right). Every
window, plots, settings and saving, can be **closed at any time and opened again from the Plots menu**. *Plot the
last* limits how much of the time axis is drawn.

## What is recorded

Each readout saves the ROIs and the temperature; the **whole spectrum** (image) only every *Full spectrum every* (*Full image
every*) seconds, which keeps a long log small (0: at each readout).

Each readout also saves **the integration (exposure) time** the instrument was set to (`Unit1_Integration`, in ms; *The
integration time* under *Record to disk*), and **it can be changed while the experiment runs**: the field of the settings stays
usable, the instrument is given the new time before its next readout and every readout is saved with the time it was made with.
A time not shorter than the readout time (times the averages of the module) cannot be followed during an experiment: it is
refused, told, and the setting goes back to the one of the instrument.

Independently of each other, per unit: **the full spectrum** (the full image of a camera), **each ROI**, **the temperature** (with the set-point,
the cycle and the phase of the ramp: after the ramps, the phase is `hold` and the cycle the last one). Untick the *Do logging* button of the toolbar to record nothing at all.

Each experiment is written in **a new h5 file**, in the folder and with the name of the *Saving* tab (`Dataset_20261022_153012.h5` by
default), in **SWMR** mode (single writer, multiple readers): another program can open it while it is written, for instance:

```python
import h5py
with h5py.File('Dataset_20261022_153012.h5', 'r', swmr=True) as f:
    ...  # refresh() a dataset to see what was added since
```

The file has one group `Datalogger000`, and in it one group per kind of data, found by its title (the names
`Detector000`... only number them): `Unit1_Spectrum`, `Unit1_ROIs`, `Unit1_Integration`, `Unit1_Temperature`, `Unit1_Program` (and
`Unit2_...`, and `Unit3_Image`, `Unit3_ROIs`... for the camera; `Unit1_Crop1`... are the ROIs which are crops: the part of the spectrum, or the pixels of the camera). Their arrays are enlarged along a time axis (epoch seconds, **the same for the groups of a unit**, it is
the time at which the readout started); the attributes of the groups tell the spectrometer and the thermostat, the
integration and readout times and, for the ROIs, their windows. The codes of `Unit1_Program` phases are in its
`phase_codes` attribute. The file can be loaded with the H5 browser or `pymodaq_data.h5modules.data_saving.DataLoader`.

What you wrote about the experiment is in the attributes of the group `RawData` (`experiment_title`, `sample`,
`operator`, `protocol`, and `application`, the command which made the file), written the way PyMoDAQ writes its attributes (a
small json text: the H5 browser and the converter below decode it). The observations are in the group
`Datalogger000/Notes`, with two arrays enlarged by each observation: `time` (epoch seconds, **the same time as the data**) and
`text` (utf-8).

**The units are kept**: the arrays have their `units` (the temperature, the integration time in ms, the values of the ROIs in
the intensity unit, per ms if they were divided by the integration time, the full spectra and the crops of a spectrometer in
the intensity unit, or per ms if they were divided, the images in the intensity unit), and the groups say it in `values_units`,
`intensity_units` and `per_integration`.

**csv, tif and text files**: the command `lab_export FILE.h5` (see `lab_apps`) turns the file into
`FILE_spectrometer1.csv` (`time (s) | temperature (°C) | integration time (ms) | ROI 1 mean (450-455 nm) [A.U.] | ...`, one row
per readout), `FILE_spectrometer1_spectra.csv` (the full spectra, one row each, one column per wavelength),
`FILE_spectrometer1_crop_<name>.csv` (the part of the spectrum of each crop, the same), `FILE_camera.csv` (the ROIs of the
camera which are numbers, with the frame of the tif where there is an image), `FILE_camera.tif` (the images, axes T, C, Y, X),
`FILE_camera_crop_<name>.tif` (the pixels of each crop), and `FILE_experiment.txt` (the title, the sample, the operator, the
protocol, the observations with their time, the instruments, the ROIs, the units and the files; the observations are also in
`FILE_notes.csv`). The file can be converted while the experiment runs (it is read in SWMR mode, on Windows too; the first
seconds and the last moments are waited for, `--wait`; the text says that the files hold what was saved so far), but do not
leave it open elsewhere when the experiment ends. The time is in seconds from the first measurement, the same zero everywhere; `--per-integration-time` divides
the values, the spectra and the crops of the spectra by the integration time of each readout (not twice what was divided already); `--list` tells what a file holds; `--help` has the rest.

## Safety

A **ramp** is never left hot by accident: its set-point returns to the initial value on Stop, when its spectrometer or its
thermal controller does not deliver anything for the time set in *Stop if no data for* (30 s, and at least a few readouts),
and when a module is closed. A controller **held at a temperature**, or whose **ramps are over** (it was told to stay at the
top, or at the bottom), is left where it is when the log ends (that is the point of holding it) and is not touched when a
module goes silent: the log goes on, tells it, and tells it when the data come back (a glitch must not end a log of two
days). A thermal controller which is closed cannot be brought back: the
extension says so, with its last set-point. When the extension is closed during a ramp, a dialog asks whether to set the
initial temperatures at once (the proposed answer) or to bring them back gradually; when PyMoDAQ itself is quit, they are
set at once. When no ramp is going on (the controllers are held, or the ramps are over) there is nothing to bring back: the
dialog only asks whether to close the log, and the controllers stay where they are.
