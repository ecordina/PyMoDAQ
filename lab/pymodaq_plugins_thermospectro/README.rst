pymodaq_plugins_thermospectro
#############################

The thermal-controlled spectroscopy and imaging applications of the lab: five **applications** of PyMoDAQ, one command
each, which record spectrometers and a camera while a thermal controller **holds a temperature or runs a ramp**.

====================================  ===========================================================================
command                               instruments
====================================  ===========================================================================
``thermo_2spectro``                   two spectrometers, each with a thermal controller
``thermo_spectro``                    one spectrometer, one thermal controller
``camera``                            one camera
``thermo_camera``                     one camera, one thermal controller
``thermo_camera_spectro``             one camera and one spectrometer, one thermal controller for both
====================================  ===========================================================================

(``lab_apps`` prints this list, with the other commands of the lab; ``lab_apps --names`` only the names.)

Each one has the settings and the windows of its instruments and nothing else (``camera`` has nothing about a
temperature, ``thermo_spectro`` nothing about a camera...). They are *dashboard extensions* (one class each, the base
class is ``ThermoSpectro``, see ``configurations.py``) started by a command which looks for the instruments.

For each spectrometer and for the camera, a *unit*:

* the **integration (exposure) time** and the **readout time** (1 s by default): the instrument is read out, and a
  measurement recorded, once per readout time;
* **ROIs**: windows of the spectrum (six by default), rectangles of the image (four), whose value is the mean of their
  pixels, or their sum, median, min, max or standard deviation, or the pixels themselves, as a **crop** (you choose for
  each ROI). They are edited on the plot or in the settings and **remembered**: the next log starts with the same ROIs;
* the thermal controller **holds a temperature** (*Mode: Hold at a temperature*: set it and log) or **runs a ramp**
  (*Mode: Run a ramp*): the ramp is defined by three of initial value, final value, velocity and duration, followed by a
  wait at the final value, the way back, a wait, repeated as many times as asked (a **cycle of ramps**). **When the ramps
  are over the log goes on**, with the controller held where they ended (the initial value, the bottom, or the final value,
  the top: your choice), until you press Stop;
* what is **recorded**: the **ROIs**, the **temperature**, the **integration time** of each readout (it can be changed
  while the experiment runs), and now and then the **full spectrum** / the **full image** (each can be left out), in an
  **h5 file in SWMR mode**, readable by another program while it is written, whose folder and name you choose and in
  which you write the **protocol** of the experiment and the **observations** you make while it runs (the *Saving*
  tab). The **units** are kept (the intensity unit you give, per ms of integration if you ask for the values to be divided
  by the integration time). **``lab_export FILE.h5``** turns the file into csv files, tif files (T, C, Y, X) and a text;
* the plots: the spectrum (the image of the camera) with its ROIs, and the ROIs over time with the temperature on top of
  them on its own scale. Every plot can be closed at any time and opened again.

The ramp is made by sending the thermal controller a new set-point every second (like PyMoDAQ's *Ramping* extension),
so any actuator whose position is a temperature can be used; a held temperature is sent once, the controller holds it.


Installation
============

From the lab repository: ``python lab/install_lab.py`` (see ``lab/README.md``), or only this package, in the environment
PyMoDAQ runs from (an editable install, so that changes are taken into account when PyMoDAQ is restarted)::

    python -m pip install -e /path/to/pymodaq_plugins_thermospectro

The five commands (and ``lab_apps``, which lists them, and ``lab_export``, which converts the h5 files) are installed in the ``bin`` folder of the environment (``Scripts`` on Windows): to use them without
activating the environment, give their full path or make aliases (``alias thermo_camera=/path/to/env/bin/thermo_camera``).


Starting an application: the instruments are looked for
=======================================================

Type the command of the configuration, for instance ``thermo_camera_spectro``. The Dashboard window opens with the
control modules of the instruments (``Thermo1``, ``Spectro1``, ``Camera1``...), then the command **looks at what is
connected to the computer**:

* the **spectrometers** (Ocean Insight / Seabreeze, listed by serial number: the first one is ``Spectro1``);
* the **camera** (Thorlabs: the scientific cameras Zelux, Kiralux, Quantalux, plugin ``Thorlabs_TSI``, or the uc480 / IDS
  cameras, plugin ``UC480``);
* the **thermal controllers** (Quantum Northwest, plugin ``QNW_TC``): the serial ports of **FTDI** USB-serial converters
  (vendor 0x0403, the one of the controllers) are asked, once, for the version of the controller (``[F1 VN ?]``, which
  changes nothing in it). The other serial ports (a built-in port, another kind of adapter) are **never** opened, as
  other instruments may be on them: a controller on such a port is selected by hand (or given with ``--port``).

Looking has two side effects to know: the pyseabreeze backend opens each Ocean Insight USB device to read its serial
number, which can reset a spectrometer which another program (OceanView) is using, so close it first; and on Linux and
macOS a controller's port is opened exclusively, which only keeps out the programs which do the same: do not run two
programs on one controller. A search which takes more than a minute (a driver which hangs) is given up, and the
instruments are selected by hand.

What is recognized is assigned to its module and **connected**. The extension window opens and says, for each instrument,
what was recognized (its serial number or port) and **what was not**, and what to do: select it (for a thermal controller,
its port, ``COM6`` for instance, in the settings of the module ``Thermo1`` in the Dashboard, *Port* or *Other port*) and
click the *Ini* button of the module (or *Connect the modules* in the extension). **Nothing is acquired until you press
Start.** Options: ``--mock`` (simulated instruments, nothing is looked for), ``--port COM6`` (the port of the thermal
controller, repeated for two: when ports are given, the controllers are not looked for, so give one for each), ``--camera-plugin
UC480`` (only this family of cameras is looked for), ``--thermostat-plugin NAME``, ``--no-detect`` (the modules are made,
nothing is connected).


Using it
========

1. Start the command, check what it recognized, select and connect what it did not (see above).
2. Click **Live** (optional): the instruments are read out once per readout time and the plots are drawn, without
   recording anything: use it to set the ROIs (drag a window or its edges, double click to add one, or the *ROI manager*
   tab).
3. For each thermal controller choose its **Mode**: **Hold at a temperature** (and the temperature) or **Run a ramp**
   (and its numbers: *Number of cycles*, and **When the ramps are over**: stay at the initial value, at the final value, or
   end the log). *Drive it* unticked: the unit only records the temperature of a controller which another unit drives
   (a camera and a spectrometer sharing one controller: only one of them drives it).
4. Under *Experiment*, **Log for**: **Until I stop it** (any number of days) or **For a given time** (and its *Duration* in
   hours, 48 for two days). Tick what is **recorded** (each ROI has its *Record* box, the unit has *The full spectrum* /
   *The full image* and *The temperature*). In the *Saving* tab choose the **folder** and the **name** of the file and write
   the title, the sample, the operator and the **protocol** of the experiment (see below).
5. Click **Start**: everything is recorded in a new h5 file until **Stop** (or the end of the duration). The status bar
   shows the time elapsed and left. While it runs, write what you observe in the field of the toolbar (or in the *Saving*
   tab) and press Enter: it is saved in the file, with its time.

**The integration time.** It is saved with each readout (the integration time the instrument was set to for that
readout, in ms: ``Unit1_Integration``; *The integration time* under *Record to disk*, on by default) and **it can be changed
while an experiment runs**: the field of the settings stays usable, the instrument is given the new time before its next
readout, and each readout is saved with the time it was made with. A time which is not shorter than the readout time (as many
times as the module averages) cannot be followed during an experiment: it is refused, told, and the setting goes back to what
the instrument is set to. The instrument may accept only the closest time it can (its limits): the saved time is the one it
accepted.

**Per ms of integration** (a box under the readout time): the value of each ROI, of the spectrometers and the aggregated ROIs
of the camera, is divided by the integration (exposure) time, in ms, of its readout, so that the values (the curves and the
file) are per ms and do not jump when the time is changed. It cannot be changed during an experiment. **Spectra per ms of
integration** (the box under it, for a spectrometer; **off** by default) does the same for what is saved of the spectrum: each
full spectrum, and the part of the spectrum of each crop, is divided by the integration time of its readout. It is another
option, because a full spectrum is the raw measurement: leave it off to keep the spectra as the instrument gave them (the
integration time is saved next to them). The images and the crops of the camera are always saved as they come. ``lab_export`` can
divide what was not divided too (``--per-integration-time``).

**Units.** *Intensity unit* (under the readout time, ``A.U.`` by default: arbitrary units) is the unit of what the instrument
measures (the plugins of the instruments do not say: write ``counts``, ``V``... if you know it): it is on the axis of the
curves and saved with the data; what is divided by the integration time (the values of the ROIs, the spectra) is in that unit
per ms (``A.U./ms``). The temperature is in the unit of its thermal controller, the
integration time in ms, the wavelengths in nm, the time in s: they are all saved with their data and written in the files of
``lab_export``.

A temperature to hold (or a ramp) which is outside the range of the controller (the *Bounds* of its module, filled by
``QNW_TC`` with the range of its holder) is refused when you press Start.

**A cycle of ramps, then the log goes on.** After the last cycle the thermal controller **stays where the ramps ended and the
log goes on** (the file records those readouts with the phase *hold*, the cycle being the last one) until you press
**Stop** or the duration of the log is over. *When the ramps are over* (under the ramp of each controller) says where that is:

* **Stay at the initial value (bottom), go on logging** (the default): the way back of the last cycle, then the log goes on there;
* **Stay at the final value (top), go on logging**: the last cycle stops when the top is reached (no wait there, no way back),
  the controller stays at the high temperature;
* **End the log**: the log ends with the ramps (the controller stays at the initial value).

**End at the top** and **End at the bottom** (the arrows of the toolbar, usable while a ramp is going on) do the same whenever
you click: the temperature goes to the final value (at the speed of the ramp) or back to the initial value (at the speed of the
way back) from wherever it is, stays there, and the log goes on until Stop. It is how a cycle of ramps which lasts until it is
stopped (*Number of cycles* 0) is ended at the high or at the low temperature.

**Stop** or the end of the duration ends the log: a thermal controller **held at a temperature stays at it** (nothing is
sent) and so does one whose ramps are over: the log ends at once; a **ramp** which is going on is brought back to its initial
temperature at the speed of the way back, then the log ends (a second Stop offers to stop right away). A log which holds a
temperature, or whose ramps are over, only ends at Stop or when its duration is over. When one unit runs a ramp and another
holds a temperature, Stop (or the end of the duration) brings the ramp back, then ends the log, and the held controller
stays where it is. **Pause** freezes the set-point of a ramp (the recording goes on).

If a spectrometer, a camera or a thermal controller does not deliver anything for *Stop if no data for* seconds (30 s, and
at least a few readouts), a ramp is brought back to its initial temperature; a log which **holds** a temperature (or only
records) **goes on**, tells it once and tells it again when the data come back, so that a transient glitch does not end a
log of two days. A module which is *closed* cannot be read any more and ends the log of its unit. If a thermal controller
itself is closed, nothing can bring it back: the extension says so. When the extension is closed during a ramp, it asks
whether to set the initial temperatures at once (the proposed answer) or to bring them back gradually; when no ramp is going
on (the controllers are held, or the ramps are over) there is nothing to bring back: it only asks whether to close the log.

The modes, the ROIs, the recording options, the settings and the file (its folder and its name, the title, the sample, the
operator and the protocol) cannot be changed during a log; the buttons showing or hiding a ROI or the temperature can be
used, and so can the observations and the integration time. The controller is only given set-points: protect it with the bounds of its
actuator (*Set bounds* in the settings of the DAQ_Move), the extension does not know its limits (a Quantum Northwest TC 125
refuses a temperature it does not accept).

See ``help.md`` (the *Help* action of the extension) for the details of the settings.

**Long logs.** The ROIs and the temperature are saved at each readout, which costs nothing; the whole spectrum and above
all the whole image are large. *Full spectrum every* (default 0: at each readout) and *Full image every* (default 60 s; 0:
at each readout) say how often they are saved: a 48 h log of a camera at one image per minute is a few GB, at one per
second hundreds. The plots are thinned beyond 20 000 points per curve (the file has them all). The data are flushed to
the file at each write: a crash loses the last second.


The camera
==========

The camera of ``camera``, ``thermo_camera`` and ``thermo_camera_spectro`` is a **camera**, any 2D detector of the Dashboard (the Thorlabs cameras of
``pymodaq_plugins_thorlabs``, for instance). It is read out once per readout time, after its exposure time was set (the
*exposure_time* setting of the module, in ms), and each of its **rectangular ROIs** has a value: the mean of the pixels of
the rectangle, followed in time like the ROIs of a spectrum, with the temperature on top. The camera starts with four
ROIs (squares picked at random, the same ones every time, fitted to the size of the image when the first one comes). They
are drawn on the image, where a rectangle is dragged to move it and by its corner to resize it, and listed in the ROI
manager (the edges are in pixels, *x* along the columns and *y* along the rows). Double click on the image to add one,
right click on a rectangle to delete it. They are remembered in ``rois.json`` too.

**What a ROI of the camera gives** (*Value* in the ROI manager and in the settings of the ROI, for each ROI): the
**mean** of its pixels (the default), their **sum**, **median**, **min**, **max** or **std** (the standard deviation), each a
number at each readout, plotted and saved in the csv file with the others; or **crop**: the ROI is just a crop, its pixels
themselves are saved at each readout (or every *Crops every* seconds, under *Record to disk*), as images, in a tif file of
its own once converted. A crop has nothing to plot (no curve, no button), and its rectangle is drawn dashed on the image. The
axis of the curves says what they are (the legend adds it when they are not all the same) and their units. A crop must be inside
the image: the experiment does not start if one is outside, and says which. A ROI gives one thing: to have a ROI both as a
number and as a crop, make two ROIs of the same rectangle. The *Value* of the ROIs is remembered with them.

**The channels of a camera which gives colors.** Its images have several channels (shown as red, green and blue, in this
order). Each ROI which gives a number has a box for each channel in the ROI manager (the column *Channels*: R, G, B) and a
*Channels* setting (``all``, or the numbers counted from 1: ``2, 3``): tick the channels it is measured on, all of them by
default. If two channels or more are ticked, the operation (mean, sum, median, min, max or std) is done on each of them and
**the results are averaged**: with green and blue ticked and *std*, the value of the ROI is the mean of std(green) and
std(blue), not the standard deviation of their pixels together. At least one channel stays ticked. A camera with one channel has
no such boxes, and a crop keeps all the channels. The choice is remembered with the ROIs, cannot be changed during an experiment,
and the label of the value in the file says which channels when it is not all of them (``ROI 1 std green+blue (x 10-18, y 5-13
pixel)``); ``lab_export`` says it in its text too. Move the mouse over the boxes to read it.

The full image is not recorded by default (it is megabytes): tick *The full image* under *Record to disk*, and say
how often with *Full image every*. The groups of the file are ``Unit3_Image``, ``Unit3_ROIs`` (the ROIs which are numbers),
``Unit3_Crop1``, ``Unit3_Crop2``... (the ROIs which are crops), ``Unit3_Integration``, ``Unit3_Temperature`` and
``Unit3_Program``.

**One thermostat for a camera and a spectrometer.** The camera and a spectrometer can share a thermostat: both are
recorded against the same temperature, and the thermal controller is driven by one of the units only (the other one has
*Drive it* unticked, and only reads the temperature): in ``thermo_camera_spectro`` it is the spectrometer, the camera
takes the thermal controller of the spectrometer without driving it. Two units which both drive the same controller are
refused, with a message.


The ROI manager
===============

The **ROI manager** is the tab next to *Settings* and *Saving*. It has a table for each spectrometer (and one for the camera), one row per ROI:
its number and color (those of its window on the spectrum), its name, its two edges (*From* and *To*, in the units of
the spectrum; the four edges of a rectangle for the camera), its *Value* (what the ROI gives: its mean, its sum... or a crop,
see below), the *Record* and *Plot* boxes, and a button to delete it.

The wavelengths are always in **nm** (the axis does not switch to *knm* beyond 1000 nm). **The color of a ROI of a
spectrometer is the color of the light of the wavelength in the middle of its window**: a ROI between 440 and 460 nm is blue
(450 nm), one around 532 nm is green, one between 640 and 660 nm is red. It is the color of its window, of its curve, of its
button and of its number in the manager, and it follows the window while it is dragged. Below 380 nm (ultraviolet) and above
780 nm (infrared) it is the violet, or the deep red, of its end of the rainbow, greyer the further it is, so that two ROIs
there still have a color of their own; the colors are lightened on a dark background and darkened on a light one when they
could not be seen well. When the spectrum has no wavelengths (pixels), and for the rectangles of a camera, the ROIs have
the colors of a palette.

**What a ROI of a spectrometer gives** (*Value*, in the manager and in the settings of the ROI, for each ROI) is the same
choice as for the camera: the **mean** of the pixels of its window (the default), their **sum**, **median**, **min**, **max**
or **std** (the standard deviation), each a number at each readout, plotted and saved with the temperature and the other ROIs
(its label in the file says which: ``ROI 1 sum (450-455 nm)``); or **crop**: the window is a part of the spectrum to keep: the
spectrum in the window, with its wavelengths, is saved at each readout (or every *Crops every* seconds, under *Record to
disk*), in a group of its own (``Unit1_Crop1``...) and in a csv file of its own once converted, one row per crop and one column
per wavelength. A crop has nothing to plot (no curve, no button) and its window is drawn with dashed edges. It must be
inside the spectrum: the experiment does not start otherwise, and says which one is not. A ROI gives one thing: to have a
window both as a number and as a crop, make two ROIs of the same window. The *Value* cannot be changed during an experiment
and is remembered with the ROIs (the ROIs saved before it existed give their mean).

* **Set up by hand**: type the edges (Enter, or the arrows, which step by one pixel; the wheel works once the box has
  the focus) and the name, choose the *Value*, tick or untick *Record* and *Plot*. An edge typed past the other one pushes it, so a
  window is moved by typing its two edges, whichever way it goes. A window which is entirely outside of the spectrum is
  written in red: its value is empty.
* **Move**: drag a window, or one of its edges, on the spectrum: its numbers in the manager follow as you go, and the
  window follows what is typed.
* **Add**: *Add a ROI* puts a window in the middle of the spectrum, ready to be named; or double click on the spectrum,
  or right click on it, where the window is wanted.
* **Delete**: the button at the end of the row, or a right click on the window on the spectrum. *Reset to the default
  ROIs* brings the six default ones back.

Nothing can be changed during an experiment, except what is plotted. The manager shows the ROIs of the settings (*ROIs*
in the settings of each spectrometer, closed by default), which are the ones remembered, saved and recorded.


The ROIs are remembered
=======================

As soon as a ROI changes, the ROIs of both spectrometers are written to ``~/.pymodaq/thermospectro/rois.json``. The
extension reads them when it starts: the same windows come back, with their names and their *Record* and *Plot*
boxes. *Reset to the default ROIs* brings back the six default ones (random windows between 450 and 850 nm: the same ones
every time for a given spectrometer). The file is plain json.


The Saving tab: the folder, the name, the protocol, the observations
=====================================================================

The *Saving* tab (next to *Settings*) says where each experiment is saved, what the file is called, and lets you write
about the experiment. What you type there is **remembered from one session to the next** (in
``~/.pymodaq/thermospectro/saving.json``), except for the title and the sample: they describe one experiment, and the ones
of yesterday are not those of the one which starts.

**The file.** *Folder* (*Browse...*) is made if it does not exist, and is checked when you press **Start**: if nothing can be
written there the experiment does not start, and the message says why. *Name* is the name of the file, without ``.h5`` (what
a file name cannot hold, like ``/`` or ``:``, becomes ``_``). With **Add the date and the time to the name** (the default) the
file is ``Name_20261022_153012.h5``, without it ``Name.h5``; **a file is never replaced**, an existing ``Name.h5`` makes
the next one ``Name_001.h5``. **Put the file in folders of the year and the day** adds ``2026/20261022/`` to the folder, the
way PyMoDAQ arranges its files. *Next file* shows what the next file will look like, *Current file* the one which is written
(or the last one). *The h5 file (advanced)*, closed at first, has the settings of PyMoDAQ's h5 saver (backend, compression).

**The experiment.** A *title*, the *sample*, the *operator* and the *protocol* (a text of any length, up to a million characters:
what is done, how, with what). They are written in the file when the log starts, in the attributes ``experiment_title``, ``sample``, ``operator``
and ``protocol`` of its group ``RawData`` (and ``application``, the command which made the file); they are locked until the
log ends.

**The observations.** While a log runs, write what you notice (the door was opened, the sample changed color...) in the field
of the toolbar, or in the one of the *Saving* tab, and press **Enter**: the observation is saved in the file at once, with
its time, in the group ``Notes`` of the experiment (the arrays ``time``, in epoch seconds like the data, and ``text``), and
listed in the tab with the time of the day and the time since the start. It is how to complete the protocol while the
experiment runs: the protocol itself is fixed when the log starts. An observation has at most a thousand characters. They can be
written only while a log runs and *Do logging* is on (otherwise they could not be saved: the fields are greyed, and the tab
says why). The file stays readable by another program while observations are added (SWMR).


``lab_export``: csv, tif and text files from the h5 file
========================================================

``lab_export`` is a command (it is installed with the package, next to ``lab_apps``) which turns the h5 file of an experiment
into the files the usual tools read::

    lab_export Pomme_20261022_153012.h5            # the files go next to the h5 file
    lab_export Pomme_20261022_153012.h5 --list     # only tells what the file holds
    lab_export *.h5 -o export                      # several files, in the folder "export" (``*`` works on Windows too)

(After a ``git pull`` which brings it, install the package again to get the command: ``python lab/install_lab.py``, or ``python -m
pip install -e lab/pymodaq_plugins_thermospectro``. Without installing it, ``python
lab/pymodaq_plugins_thermospectro/tools/thermospectro_export.py FILE.h5`` does the same, and ``export.py`` of the package is a
single file which needs only ``numpy``, ``h5py`` and ``tifffile``: it can be copied to any computer, PyMoDAQ is not needed.)
The names of the new files start with the one of the h5 file:

* **the values of a spectrometer** and of its thermal controller, ``..._spectrometer1.csv``: one row per readout,
  ``time (s) | temperature (°C) | integration time (ms) | ROI 1 mean (450-455 nm) [A.U.] | ROI 2 sum ... [A.U.] | ...`` (the
  temperature if there is a thermal controller, the integration time if it was saved, each ROI which was recorded and gives
  a number, its name saying which one; the program of the ramp with ``--program``);
* **the full spectra**, if they were saved, in a file of their own, ``..._spectrometer1_spectra.csv``: one row per spectrum,
  ``time (s) | temperature (°C) | integration time (ms) | 400.123 nm | 400.456 nm | ...`` (one column per wavelength); and the
  ROIs which are **crops**, each in a file of its own too, ``..._spectrometer1_crop_<name of the ROI>.csv``: the same, with the
  wavelengths of the window only. They are kept apart from the values: the csv files of the values are the same whatever
  was saved of the spectra. The units of the spectra are in the text (``A.U.``, ``A.U./ms`` if they were divided);
* **the camera**: ``..._camera.csv`` has one row per readout, ``time (s) | temperature (°C) | integration time (ms) | ROI 1 sum (x 10-18,
  y 5-13 pixel) [A.U.] | ... | tif frame``, with the ROIs which give a number (mean, sum, median, min, max or std, as their name
  says) and, where an image was saved, its frame in the tif; ``..._camera.tif`` has the images (*The full image*) as a time
  series of the axes **T, C, Y, X** (an ImageJ hyperstack: Fiji / ImageJ, ``tifffile.imread``; the time and the temperature are
  the labels of the frames), and ``..._camera_crop_<name of the ROI>.tif`` the pixels of each ROI which is a crop, the same kind
  of tif. ImageJ cannot open files of more than 4 GB (BigTIFF): a longer series is split in ``..._part01.tif``,
  ``..._part02.tif``... and the csv file has the columns ``tif file`` and ``tif frame`` (from 1, as ImageJ counts);
* **the observations and the protocol**: ``..._experiment.txt`` is a text with the title, the sample, the operator, the protocol,
  the observations noted while the experiment ran (with the time since the start and the clock time), the instruments, the ROIs
  (and what each one gives), the integration times, the units and the list of the files which were written; the observations
  are also in ``..._notes.csv`` (``time (s) | clock time | observation``).

What is not in the file (a spectrometer or a camera which was not used, a temperature, a full spectrum, an image, observations
which were not recorded) is left out; the text is always written. **The units are in the headers**, in square brackets for what
is measured: ``[A.U.]``, or ``[A.U./ms]`` for the values which were divided by the integration time in the extension, or the
unit you gave; the time is in seconds from the first measurement of the experiment, **the same zero in all the files**
(``--absolute-time`` for epoch seconds). The csv files are in UTF-8 with a signature, so that Excel shows ``°C``.

Options: ``--per-integration-time`` divides the values of the ROIs, the spectra and the crops of the spectra which were saved as
they were measured by the integration time of each readout (``A.U./ms``; what was divided in the extension is not divided
twice, and it says so),
``--short-names`` (``ROI 1 [A.U.]`` instead of ``ROI 1 mean (454.2-464.6 nm) [A.U.]``), ``--no-spectra``, ``--no-tif``, ``--program`` (the
set-point, cycle and phase of the ramp), ``--delimiter``, ``-o FOLDER``, ``--experiment N`` (the experiment of the file, the last one by
default), ``--with-spectrometers`` (in the csv of the camera, add the temperature and the ROIs of the spectrometers at the readouts of
the camera: the closest readout of the spectrometer, or ``--interpolate``). The spectra are read from the file a few hundred
rows at a time: a log of days does not have to fit in memory.


The data file
=============

Each experiment is a new file, in the folder and with the name of the *Saving* tab (``Dataset_20261022_153012.h5`` by default),
in SWMR mode (it needs the *h5py* backend, which the extension switches on). It has one group ``Datalogger000`` with, found by
their title (``Detector000``, ``Actuator000``... only number them):

====================== ===================================================================================
``Unit1_Spectrum``     the full spectra, with their wavelengths (*Unit2_...* for the second spectrometer), every
                       *Full spectrum every* seconds (``Unit3_Image``: the images of the camera)
``Unit1_ROIs``         one array per recorded ROI which gives a number, labeled like ``ROI 1 mean (454.2-464.6 nm)`` (``ROI 1 sum
                       (x 10-18, y 5-13 pixel)`` for a camera, ``ROI 1 std green+blue (x 10-18, y 5-13 pixel)`` when it is
                       measured on some of the channels only, the attribute ``n_channels`` telling how many there are), in the
                       unit of the values (``units`` of the arrays, ``values_units`` of the group)
``Unit1_Crop1``...     for each ROI which is a crop: the part of the spectrum of a spectrometer, with its wavelengths, or the
                       pixels of a camera (one image, or several channels, per readout), with the name and the window of the
                       ROI in the attribute ``roi``
``Unit1_Integration``  the integration (exposure) time of each readout, in ms
``Unit1_Temperature``  the measured temperature
``Unit1_Program``      the set-point, the cycle and the phase code of the ramp (``phase_codes`` attribute: idle 0,
                       approach 1, ramp 2, hold final 3, back 4, hold initial 5, return 6, done 7, **hold 8**: the
                       controller is held at one temperature, the set-point is that temperature: a controller which
                       was only held, or whose ramps are over, the cycle being then the last one)
``Notes``              the observations: the arrays ``time`` (epoch seconds, the same time as the data) and ``text``
                       (utf-8), enlarged by each observation
====================== ===================================================================================

The units are kept: the arrays have their ``units`` (the temperature in the unit of the thermal controller, the integration time in
ms; the values of the ROIs, the full spectra, the crops and the images in the intensity unit, per ms if they were divided by the
integration time; arbitrary units, the default, are no unit for PyMoDAQ's data model: the arrays then have none, or ``1/ms``
when they were divided) and the groups say what they hold, written the way the intensity unit was, in ``values_units``,
``intensity_units`` and ``per_integration``.

What was written about the experiment is in the attributes ``experiment_title``, ``sample``, ``operator``, ``protocol`` and
``application`` of the group ``RawData`` (written the way PyMoDAQ writes its attributes, as a small json text: the H5 browser
and the converter decode them).

Their arrays are enlarged along a ``time`` axis in epoch seconds, **the same for the groups of a unit**: the time at which
the readout started (the spectrum is integrated after it). The attributes of the groups give the spectrometer and the
thermostat, the integration and readout times and the windows of the ROIs; the settings of the unit are saved with each
group. Open the file with PyMoDAQ's H5 browser or ``pymodaq_data.h5modules.data_saving.DataLoader``; while it is written::

    import h5py
    f = h5py.File(path, 'r', swmr=True)     # then dataset.refresh() shows what was added

The end of the experiment closes SWMR and updates the attributes of the arrays: a file which was not closed (a crash)
can be read until its last flush (once per write) but its ``shape`` attributes may be late.


Tests
=====

The tests need ``pytest`` and ``pytest-qt``; they run with a Dashboard on the Mock modules, without a display (a run
is long, it can be split with ``-k``)::

    QT_QPA_PLATFORM=offscreen python -m pytest tests


Authors
=======

* E. Cordina (emmanuel.cordina@inrae.fr)

License: MIT
