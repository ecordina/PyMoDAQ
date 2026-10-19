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

Each one has the settings and the windows of its instruments and nothing else (``camera`` has nothing about a
temperature, ``thermo_spectro`` nothing about a camera...). They are *dashboard extensions* (one class each, the base
class is ``ThermoSpectro``, see ``configurations.py``) started by a command which looks for the instruments.

For each spectrometer and for the camera, a *unit*:

* the **integration (exposure) time** and the **readout time** (1 s by default): the instrument is read out, and a
  measurement recorded, once per readout time;
* **ROIs**: windows of the spectrum (six by default), rectangles of the image (four), whose value is the mean of their
  pixels. They are edited on the plot or in the settings and **remembered**: the next log starts with the same ROIs;
* the thermal controller **holds a temperature** (*Mode: Hold at a temperature*: set it and log) or **runs a ramp**
  (*Mode: Run a ramp*): the ramp is defined by three of initial value, final value, velocity and duration, followed by a
  wait at the final value, the way back, a wait, repeated as many times as asked;
* what is **recorded**: the **ROIs**, the **temperature**, and now and then the **full spectrum** / the **full image**
  (each can be left out), in an **h5 file in SWMR mode**, readable by another program while it is written;
* the plots: the spectrum (the image of the camera) with its ROIs, and the ROIs over time with the temperature on top of
  them on its own scale. Every plot can be closed at any time and opened again.

The ramp is made by sending the thermal controller a new set-point every second (like PyMoDAQ's *Ramping* extension),
so any actuator whose position is a temperature can be used; a held temperature is sent once, the controller holds it.


Installation
============

From the lab repository: ``python lab/install_lab.py`` (see ``lab/README.md``), or only this package, in the environment
PyMoDAQ runs from (an editable install, so that changes are taken into account when PyMoDAQ is restarted)::

    python -m pip install -e /path/to/pymodaq_plugins_thermospectro

The five commands are installed in the ``bin`` folder of the environment (``Scripts`` on Windows): to use them without
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
   (and its numbers). *Drive it* unticked: the unit only records the temperature of a controller which another unit drives
   (a camera and a spectrometer sharing one controller: only one of them drives it).
4. Under *Experiment*, **Log for**: **Until I stop it** (any number of days) or **For a given time** (and its *Duration* in
   hours, 48 for two days). Tick what is **recorded** (each ROI has its *Record* box, the unit has *The full spectrum* /
   *The full image* and *The temperature*), choose the folder in the *Saving* tab.
5. Click **Start**: everything is recorded in a new h5 file until **Stop** (or the end of the duration). The status bar
   shows the time elapsed and left.

A temperature to hold (or a ramp) which is outside the range of the controller (the *Bounds* of its module, filled by
``QNW_TC`` with the range of its holder) is refused when you press Start.

**Stop** or the end of the duration: a thermal controller **held at a temperature stays at it** (nothing is sent) and the
log ends at once; a **ramp** is brought back to its initial temperature at the speed of the ramp, then the log ends (a
second Stop offers to stop right away). A ramp alone ends by itself when it is over, a log which holds a temperature
only ends at Stop or when its duration is over. When one unit runs a ramp and another holds a temperature, Stop (or the end
of the duration) brings the ramp back, then ends the log, and the held controller stays where it is. **Pause** freezes the
set-point of a ramp (the recording goes on).

If a spectrometer, a camera or a thermal controller does not deliver anything for *Stop if no data for* seconds (30 s, and
at least a few readouts), a ramp is brought back to its initial temperature; a log which **holds** a temperature (or only
records) **goes on**, tells it once and tells it again when the data come back, so that a transient glitch does not end a
log of two days. A module which is *closed* cannot be read any more and ends the log of its unit. If a thermal controller
itself is closed, nothing can bring it back: the extension says so. When the extension is closed during a ramp, it asks
whether to set the initial temperatures at once (the proposed answer) or to bring them back gradually.

The modes, the ROIs, the recording options, the settings and the file cannot be changed during a log (the buttons showing
or hiding a ROI or the temperature can be used). The controller is only given set-points: protect it with the bounds of its
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

The full image is not recorded by default (it is megabytes): tick *The full image* under *Record to disk*, and say
how often with *Full image every*. The groups of the file are ``Unit3_Image``, ``Unit3_ROIs``, ``Unit3_Temperature`` and
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
the spectrum), the *Record* and *Plot* boxes, and a button to delete it.

* **Set up by hand**: type the edges (Enter, or the arrows, which step by one pixel; the wheel works once the box has
  the focus) and the name, tick or untick *Record* and *Plot*. An edge typed past the other one pushes it, so a
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


Csv and tif files instead of the h5 file
========================================

``tools/thermospectro_export.py`` (also in the export folder of the Windows computer) turns the h5 file of an experiment
into files which are easier to use. It is a single script, which does not need PyMoDAQ, only ``numpy``, ``h5py`` and
``tifffile`` (``python -m pip install tifffile``)::

    python thermospectro_export.py Dataset_20260101_000.h5

* ``Dataset_20260101_000_spectrometer1.csv`` for each spectrometer: one row per readout,
  ``time (s) | temperature (°C) | ROI 1 | ROI 2 | ... | full spectrum`` (the full spectrum is one column of intensities
  separated by spaces, its wavelengths are in ``..._wavelengths.csv``; ``--spectrum columns`` makes one column per
  wavelength instead, ``--spectrum none`` leaves it out);
* ``Dataset_20260101_000_camera.tif`` for the camera: the images stitched in an ImageJ hyperstack (axes Time, Channel, Y,
  X: open it with Fiji / ImageJ or ``tifffile.imread``), and ``..._camera.csv``: one row per image (the row is the number
  of the image) with ``time (s) | temperature (°C) | spectrometer 1 ROI 1 | ... | camera ROI 1 | ...``. The spectrometer
  is read out at other instants than the camera: its value is the one of the closest readout (``--interpolate`` for a
  linear interpolation). The images are there only if *The full image* was ticked.

In a long log the whole spectrum (image) is saved only every so often: the csv of a spectrometer then has one row per
readout of the ROIs with an empty ``full spectrum`` cell where none was saved, the csv of the camera has one row per image
(the pages of the tif) and ``..._camera_log.csv`` has one row per readout, with the page of the tif where there is an image.

The time is in seconds from the first measurement of the experiment, the same zero in all the files
(``--absolute-time`` for epoch seconds). Other options: ``--short-names`` (``ROI 1`` instead of ``ROI 1 (454.2-464.6 nm)``),
``--program`` (the set-point, cycle and phase of the ramp), ``--no-tif``, ``--delimiter``, ``-o FOLDER``,
``--experiment N`` and ``--list`` (what the file holds).


The data file
=============

Each experiment is a new file, in the folder of the *Saving* tab, in SWMR mode (it needs the *h5py* backend, which the
extension switches on). It has one group ``Datalogger000`` with, found by their title (``Detector000``,
``Actuator000``... only number them):

====================== ===================================================================================
``Unit1_Spectrum``     the full spectra, with their wavelengths (*Unit2_...* for the second spectrometer), every
                       *Full spectrum every* seconds (``Unit3_Image``: the images of the camera)
``Unit1_ROIs``         one array per recorded ROI, labeled like ``ROI 1 (454.2-464.6 nm)``
``Unit1_Temperature``  the measured temperature
``Unit1_Program``      the set-point, the cycle and the phase code of the ramp (``phase_codes`` attribute: idle 0,
                       approach 1, ramp 2, hold final 3, back 4, hold initial 5, return 6, done 7, **hold 8**: the
                       controller is held at one temperature, the set-point is that temperature)
====================== ===================================================================================

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
