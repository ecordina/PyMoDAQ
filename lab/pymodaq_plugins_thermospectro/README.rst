pymodaq_plugins_thermospectro
#############################

A PyMoDAQ *dashboard extension*, **ThermoSpectro**: one or two spectrometers, **each with its own thermostat**, and a
**camera**, are recorded while the temperature of the thermostats follows a ramp.

For each of the (one or two) spectrometers:

* the **integration time is 500 ms** and the **readout time 1 s** by default: the spectrometer is read out, and a
  measurement recorded, once per readout time;
* **six ROIs** (windows of the spectrum, whose value is the mean of their pixels) are defined by default (at random).
  They are edited on the spectrum or in the settings and **remembered**: the next experiment starts with exactly
  the same ROIs;
* the **temperature ramp** is defined by three of: initial value, final value, velocity, duration; it is followed by a
  wait at the final value, the way back to the initial value (defined in the same way) and a wait, and the cycle is
  repeated a number of times chosen by the user. The ramps are independent from one thermostat to the other;
* the **full spectrum**, **each ROI** and **the temperature** are recorded at each readout, each of them independently of
  the others (every one can be left out);
* the plots show the **spectrum**, small, and the **ROIs** over time, those which are to be plotted, with the
  **temperature** drawn on top of them on its own scale. Every plot can be closed at any time and opened again;
* the data are saved in an **h5 file in SWMR mode** (one new file per experiment), readable by another program while it
  is written.

It works with the control modules of the Dashboard: 1D detectors (the spectrometers), a 2D detector (the camera) and
actuators whose position is a temperature. Any plugin can be used, the ramp is made by sending the thermostat a new set-point every second (like
PyMoDAQ's *Ramping* extension does), so it does not depend on a feature of the thermostat.

With ``pymodaq_plugins_oceaninsight`` the spectrometers can look by default at the ones which are connected, each of
the two modules taking another one (see the *Device* setting of its viewer: *Auto*).


Installation
============

In the environment PyMoDAQ runs from (an editable install, so that changes are taken into account when PyMoDAQ is
restarted)::

    python -m pip install -e /path/to/pymodaq_plugins_thermospectro

Then type ``thermospectro``: it starts the Dashboard with a preset and opens the extension. The preset is
``ThermoSpectroSeabreeze`` (two spectrometers) by default; ``thermospectro ThermoSpectroSingleSeabreeze`` starts the
one with a single spectrometer, ``thermospectro ThermoSpectroMock`` and ``thermospectro ThermoSpectroSingleMock`` the
ones with Mock modules, and the name of any preset of the experiments folder of PyMoDAQ can be given. The presets of
the package (see "The presets" below) are all copied there, the first time, so that the Dashboard lists them; a file
which is already there is never replaced. The command is installed in the ``bin`` folder of the environment
(``Scripts`` on Windows): to use it without activating the environment, make an alias of its full path
(``alias thermospectro=/path/to/env/bin/thermospectro``).

Without the command: start PyMoDAQ with ``dashboard`` (or ``pymodaq``, then *Dashboard*), load a preset with
**Tools > Experiment > Load Experiments** and open the extension with **Tools > Extensions > ThermoSpectro**.


Using it
========

1. Start ``thermospectro``. **Nothing is connected when a preset opens**: when the spectrometers and the thermostats are
   plugged in, click *Connect the modules* (the plug button of the toolbar). They are connected, that is initialized,
   one after the other, and the ones which could not be are told at the end (the *Ini* button of each module in the
   Dashboard does it one by one). The first 1D detector is the spectrometer 1, the second one the spectrometer 2, the
   thermostat of each is guessed from its name, change them in the settings if needed. A spectrometer which is not
   there leaves its unit unticked (*Use it*).
2. Click **Live**: each spectrometer is read out once per readout time, after its integration time was set, and the
   temperature is read.
3. Set up the **ROIs** in the *ROI manager* tab (see below) or on the spectra (drag a window or its edges, double
   click to add one) and set the **temperature ramp** of each unit in the settings.
4. Tick what is **recorded** (each ROI has its *Record* box, the unit has *The full spectrum* and *The temperature*),
   choose the folder in the *Saving* tab and click **Start**.

**Pause** freezes the set-points (the recording goes on). **Stop** does not leave the thermostats where they are: each
set-point goes back to the initial value of its ramp, at the speed of the ramp, and the experiment ends when they
are all there (a second Stop offers to stop right away). The same return happens when something goes wrong: a spectrometer or
a thermostat does not deliver anything for the time of *Stop if no data for* (30 s, and at least a few readouts), or a
spectrometer is closed. If a thermostat itself is closed, nothing can bring it back: the extension says so, with the
last set-point it sent. When the extension is closed during an experiment, it asks whether to set the initial
temperatures at once (the proposed answer) or to bring them back gradually; when the whole of PyMoDAQ is quit, they are
set at once.

The ramps, the ROIs, the recording options, the settings and the file cannot be changed during an experiment (the
buttons showing or hiding a ROI or the temperature can be used). The thermostat is only given set-points: protect it with
the bounds of its actuator (*Set bounds* in the settings of the DAQ_Move), the extension does not know its limits.

See ``help.md`` (the *Help* action of the extension) for the details of the settings.


The camera
==========

The third unit of the extension is a **camera**, any 2D detector of the Dashboard (the Thorlabs cameras of
``pymodaq_plugins_thorlabs``, for instance). It is read out once per readout time, after its exposure time was set (the
*exposure_time* setting of the module, in ms), and each of its **rectangular ROIs** has a value: the mean of the pixels of
the rectangle, followed in time like the ROIs of a spectrum, with the temperature on top. The camera starts with four
ROIs (squares picked at random, the same ones every time, fitted to the size of the image when the first one comes). They
are drawn on the image, where a rectangle is dragged to move it and by its corner to resize it, and listed in the ROI
manager (the edges are in pixels, *x* along the columns and *y* along the rows). Double click on the image to add one,
right click on a rectangle to delete it. They are remembered in ``rois.json`` too.

The full image is not recorded by default (it is megabytes at each readout): tick *The full image* under *Record to
disk*. The groups of the file are ``Unit3_Image``, ``Unit3_ROIs``, ``Unit3_Temperature`` and ``Unit3_Program``.

**One thermostat for a camera and a spectrometer.** The camera and a spectrometer can share a thermostat: both are
recorded against the same temperature, and the ramp is run by one of the units only (the other one has *Run the ramp*
unticked, and only reads the temperature). When the extension starts with a preset which has a camera, a spectrometer and
a single thermostat, the camera takes the thermostat of the first spectrometer without driving it. Two units which both
run a ramp on the same thermostat are refused, with a message.


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
``Unit1_Spectrum``     the full spectra, with their wavelengths (*Unit2_...* for the second spectrometer)
``Unit1_ROIs``         one array per recorded ROI, labeled like ``ROI 1 (454.2-464.6 nm)``
``Unit1_Temperature``  the measured temperature
``Unit1_Program``      the set-point, the cycle and the phase code of the ramp (``phase_codes`` attribute: idle 0,
                       approach 1, ramp 2, hold final 3, back 4, hold initial 5, return 6, done 7)
====================== ===================================================================================

Their arrays are enlarged along a ``time`` axis in epoch seconds, **the same for the groups of a unit**: the time at which
the readout started (the spectrum is integrated after it). The attributes of the groups give the spectrometer and the
thermostat, the integration and readout times and the windows of the ROIs; the settings of the unit are saved with each
group. Open the file with PyMoDAQ's H5 browser or ``pymodaq_data.h5modules.data_saving.DataLoader``; while it is written::

    import h5py
    f = h5py.File(path, 'r', swmr=True)     # then dataset.refresh() shows what was added

The end of the experiment closes SWMR and updates the attributes of the arrays: a file which was not closed (a crash)
can be read until its last flush (once per write) but its ``shape`` attributes may be late.


The presets
===========

The package brings twelve presets (the Dashboard calls them *experiments*), in
``src/pymodaq_plugins_thermospectro/resources/presets/``: with the actual instruments, and with simulated ones to try the
extension without hardware.

*Spectrometers*, each with its thermostat (two ``Mock`` thermostats, to be replaced by the plugin of the real thermostat):

* ``ThermoSpectroSeabreeze``: two Seabreeze spectrometers (*Spectro1*, *Spectro2*) and
  two ``Mock`` thermostats (*Thermo1*, *Thermo2*, actuators whose *axis* is ``Temp``, in °C);
* ``ThermoSpectroSingleSeabreeze``: the first of them, *Spectro1* and *Thermo1*, alone;
* ``ThermoSpectroMock``: two ``Mock`` 1D detectors (*Spectro1*, *Spectro2*) and the same two ``Mock`` thermostats. The
  Mock spectra go from 0 to 512: put the ROIs there (the default ones are for a spectrometer working around
  450-850 nm);
* ``ThermoSpectroSingleMock``: the first of them, *Spectro1* and *Thermo1*, alone.

*A camera* (the third unit), with the thermostat of Quantum Northwest (``QNW_TC`` of ``pymodaq_plugins_quantumnorthwest``,
named *Thermo1*), for the two families of Thorlabs cameras of ``pymodaq_plugins_thorlabs`` (install it with
``python -m pip install pymodaq_plugins_thorlabs``; *Thorlabs_TSI* is for the scientific sCMOS cameras Zelux, Kiralux and
Quantalux, *UC480* for the uc480 cameras of Thorlabs and the IDS uEye ones; the old *Thorlabs_DCx* is not used):

* ``ThermoCameraThorlabsTSI`` and ``ThermoCameraThorlabsUC480``: *Camera1* and *Thermo1*;
* ``ThermoCameraSpectroThorlabsTSI`` and ``ThermoCameraSpectroThorlabsUC480``: *Spectro1* (Seabreeze), *Camera1* and
  *Thermo1*, one thermostat for the two detectors;
* ``ThermoCameraMock`` and ``ThermoCameraSpectroMock``: the same with ``Mock`` modules (a 2D ``Mock`` detector is the
  camera, the thermostat is a ``Mock`` one), to try without hardware. The Mock image is 100 x 200 pixels.

* ``ThermoZeluxMockThermo`` and ``ThermoZeluxMockSpectroMockThermo``: a Thorlabs *Zelux* camera (``Thorlabs_TSI``) with a
  ``Mock`` thermostat, alone or with a ``Mock`` spectrometer (the setup of the second computer of the lab, started by
  ``lab/windows/zelux.bat`` and ``zeluxspec.bat``).

The type of a module cannot be changed in the experiment manager: to use another camera plugin, remove *Camera1* from the
experiment (**Tools > Experiment**), *Add* a detector of the other plugin (a 2D one) with the same name, and save it under
another name. ``thermospectro ThermoCameraSpectroThorlabsTSI`` starts with one of them (any preset name works). The
presets with a camera were written by ``tools/make_camera_presets.py``, which can make others.

None of the modules of the presets is initialized when the preset opens: *Connect the modules* does it. With a single
spectrometer the second unit is left unticked (*Use it*) and its windows are hidden. The thermostats of
the presets are ``Mock`` ones, as the plugin of the thermostat is the one of yours: in the experiment manager of the
Dashboard (**Tools > Experiment**) remove *Thermo1* and *Add* an actuator of that plugin with the same name, then save
the experiment (the *Thermostat* setting of a unit offers every actuator of the Dashboard). ``thermospectro`` never
replaces a preset which is already in the experiments folder, so such a change is kept. For the temperature
controllers of Quantum Northwest (TC 125, TC 1, TC 1B) there is ``pymodaq_plugins_quantumnorthwest``: its actuator is
``QNW_TC`` (choose its port in its settings before connecting, and leave its *Ramp* at 0: the extension makes the ramp).

The Dashboard looks for its presets in one folder, the one where ``default.xml`` is: ``/etc/.pymodaq/experiments`` on
Linux, ``C:\ProgramData\.pymodaq\experiments`` on Windows (not ``~/.pymodaq``). ``thermospectro`` copies there the
presets of the package which are not there yet (whichever preset it starts), so all of them are in the menu
**Tools > Experiment > Load Experiments** of the Dashboard. To do it by hand, copy the files to that folder and
restart the Dashboard (its menu is made when it starts).


Tests
=====

The tests need ``pytest`` and ``pytest-qt``; they run with a Dashboard on the Mock modules, without a display (a run
is long, it can be split with ``-k``)::

    QT_QPA_PLATFORM=offscreen python -m pytest tests


Authors
=======

* E. Cordina (emmanuel.cordina@inrae.fr)

License: MIT
