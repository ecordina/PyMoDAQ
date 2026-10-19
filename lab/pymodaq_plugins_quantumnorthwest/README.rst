pymodaq_plugins_quantumnorthwest
################################

A PyMoDAQ plugin for the temperature controllers of **Quantum Northwest** (QNW): the **TC 125**, the **TC 1** and the
**TC 1B**, which drive the Peltier-controlled cuvette holders (qpod, Luma 40, CD 250, Flash 300, qX2, qX3, t2 Sport...).

It provides one actuator, ``QNW_TC``, whose *position is a temperature* (°C): moving it sets the target temperature of
the sample holder and switches the temperature control on, its value is the temperature of the holder (or of the
external probe in the cuvette). Anything of PyMoDAQ which moves an actuator can set the temperature with it: the
Dashboard, the scans, the PID, ramps, and the ThermoSpectro extension.

.. warning::

   **It was written from the manuals of the controllers and tested against a simulated controller.** It was connected
   to one real controller, a TC 125 (firmware 9.1) of a qpod: it connects and reads the temperatures, the target and the
   status; moving it was not tried yet. The simulation follows the manuals, including their differences from one
   model to the other, but a real controller may still say something the manuals do not. The first time, try it
   with the cuvette holder empty and look at what it does (see *Trying it* below).

   What that TC 125 does differently from its manual: it does not know the highest and lowest targets (``MT``, ``LT``),
   the heat exchanger sensor (``HT``, ``HL``) or the question which error it counts (``ER ?``), and it refuses them with
   ``[F1 ER 9]``, which does not say which command it is about. The plugin finds that out when it connects (the bounds
   of the actuator are then not set, and an error which the controller counts is shown as *cannot say which*).
   ``sim://qpod`` simulates it.

Controllers
===========

====================  ==========================  =============================================================
Controller            Firmware of the manual      Manual
====================  ==========================  =============================================================
TC 125 (225, 425)     9.1                         *Appendix: serial communications* of the manuals of the qpod and
                                                  of the CD 250
TC 1                  1.0                         Appendix of the manual of the Luma 40
TC 1B                 2.2 - 2.6                   *Serial communications for the TC 1B*, in the TC 1B User's Guide
====================  ==========================  =============================================================

The three of them talk at 19200 baud (8 data bits, no parity, one stop bit, no flow control) with commands between
square brackets, like ``[F1 CT ?]`` (*what is the temperature of the holder?*) answered by ``[F1 CT 22.84]``. The
plugin only uses what they have in common, and finds out what the controller can do more when it connects (for
instance, the TC 1 and the TC 1B set the speed of the stirrer, the TC 125 turns it on and off only).

* **The TC 125 needs its serial option**: a USB or RS-232 connector at the back (the *SER* option). Without it the
  controller cannot be controlled by a computer.
* Only the sample holder (``F1`` in the manuals) is driven. For a dual controller (TC 225, TC 1B/Dual) the reference
  holder (``R1``) is not, and the cell changer of a multi-position holder (``F2``) is not either.

Installation
============

In the environment PyMoDAQ runs from::

    python -m pip install -e /path/to/pymodaq_plugins_quantumnorthwest

(it installs ``pyserial``, which PyMoDAQ does not need otherwise). Restart PyMoDAQ: ``QNW_TC`` is in the list of the
actuators.

**Connection.** A USB controller needs the driver of its USB-serial converter (an FTDI one, see the manual of the
controller: it is part of QNW's software package) *before* the USB cable is plugged in. It then is a serial port,
``COMx`` on Windows, ``/dev/ttyUSBx`` on Linux, where the user has to be allowed to use it (on Ubuntu: be in the group
``dialout``, ``sudo usermod -a -G dialout $USER`` and log in again). An RS-232 controller is connected with a straight
cable, **not** a null-modem one.

Using it
========

In the Dashboard, add an actuator of type ``QNW_TC``, choose its **port** in the settings and initialize it. A port
which is not in the list (it was plugged in after PyMoDAQ started: the list is made again after a failed connection)
can be typed in *Other port*, which also takes the URLs of pyserial (``socket://host:port``, ``rfc2217://...``).
The module says what it found: *firmware 9.1, single cuvette holder with probe capability (ID 11), probe connected*.

The settings of the module:

*Temperature*
    * **Measured by**: the value of the actuator is the temperature of the metal body of the **holder**, or of the
      external **probe** (a series 400 or 500 thermistor probe in the cuvette, which is closer to the sample).
    * **Control on**: the temperature control of the controller (the holder goes to the target and stays there). It is
      the state of the controller, which can be switched on and off here.
    * **Switch on when moving**: the controller does *not* switch the control on when a computer sets a target (it
      does with its own buttons): moving the actuator does it, if ticked.
    * **Wait for stability**: a move is over when the temperature is within *epsilon* of the target **and** the
      controller says it is stable (it stayed within 0.05 °C for a minute). Untick it to end the move at *epsilon*.
      (Not used when the program which moves the actuator does not wait for the move, like ThermoSpectro.)
    * **Ramp (°C/min)**: the controller reaches a new target at this rate (0.01 to 10), 0 for as fast as it can.
      It is set when the module is initialized (0 switches off a ramp which another program left in the controller).
      Keep 0 when the program which moves the actuator makes its own ramp.
    * **Home**: the temperature of the home button.
*Stirrer*
    On or off, and its speed (rpm) with a TC 1 or a TC 1B. The stirrer of a TC 125 turns at the speed of the knob of
    its front panel.
*Status* (read from the controller every *Refresh* seconds)
    The *state* (``off``, ``seeking`` while the temperature changes, ``holding`` when it is stable), the temperatures of
    the holder, of the probe and of the heat exchanger of the Peltier element (the controller shuts the control down
    above 60 °C, if not enough cooling water flows), the target which the controller has, and its last **fault**:
    a fault is also written in the log and shown by PyMoDAQ once.
*When closed*
    What is done to the controller when the module is closed: nothing (default: it goes on by itself), switch the
    control off, or switch the control and the stirrer off.

The **bounds** of the actuator are set to the temperatures the holder accepts (for instance -30 to 105 °C) when the
module is initialized, if the controller tells them (the TC 125 of a qpod does not: the controller then refuses a target
it does not accept, and the plugin does not know it before). They are not switched on: a target out of them is refused (and not sent) with an error which
says the range. Tick *Set Bounds* to have PyMoDAQ bring a target back to them instead.
The **stop** button keeps the holder at the temperature it has now (it becomes the target, and a ramp stops).
**Move relative** changes the *target*, not the temperature of the holder, which takes minutes to reach it.
The **timeout** of the moves is one hour (a holder is slow).

With ThermoSpectro
------------------

``pymodaq_plugins_thermospectro`` makes a temperature ramp by sending its thermostat a new set-point every second:
this actuator can be that thermostat. In the experiment manager of the Dashboard (*Tools > Experiment*), remove the
``Mock`` actuator ``Thermo1`` of the preset and add an actuator ``QNW_TC`` with the same name (the type of a module
cannot be changed, and the extension finds its thermostat by name), save the preset, and connect the modules with the
button of the extension. Leave **Ramp** at 0. A preset does not keep the settings of a plugin: the module starts with
the first serial port found (``/dev/ttyUSB0``...), choose the right one in its settings before connecting if it is
another.

Trying it without a controller
------------------------------

``qnw_tc_demo`` opens the Dashboard with a Mock spectrometer and a simulated TC 125 under the ThermoSpectro extension, already
connected (``qnw_tc_demo --port sim://tc1b?speed=60`` for another simulated controller).

With the real instruments, ``qnw_tc_start /dev/ttyUSB0`` does the same with a Seabreeze spectrometer and the controller on that port.

The port ``sim://tc125`` (a simulated TC 125), ``sim://qpod`` (the TC 125 of a qpod, as it really answers) or
``sim://tc1b`` (a TC 1B) is a simulated controller: it can be tried
without any hardware, and it is what the tests use. Options follow a ``?``: ``sim://tc125?speed=60`` makes the
simulated time run 60 times faster than the real one (a holder goes from 22 to 40 °C in about 20 s instead of 20 min),
``probe=0`` leaves out the external probe, ``latency=0.2`` makes the answers take 0.2 s, ``unknown=silent`` makes the
controller say nothing of the commands it does not know (``quote`` and ``code`` are the other two ways). The module
says *SIMULATED CONTROLLER* when it uses one.

With a real controller, the first time: when it is connected the plugin only asks what it is and what it can do,
stops the periodic reports another program may have left on, and switches its ramp off (the *Ramp* setting): it sets
no target and does not switch the temperature control or the stirrer on. The target is checked against the range
the controller gave, and the temperature control only starts with the first move. Try a target close to the
temperature of the holder, watch *Status*, and compare the temperatures with the display of the controller.

If it does not work
===================

Connect the controller without PyMoDAQ and look at what it says::

    qnw_tc_check /dev/ttyUSB0 --trace

(``qnw_tc_check`` alone lists the serial ports, ``python -m pymodaq_plugins_quantumnorthwest.hardware.diagnose`` is the
same command). It connects the controller as the plugin does, then reads the temperatures, the status and the last error
with the time each answer took, and sets nothing. ``--trace`` shows every byte sent and received, ``--watch 30`` goes on
reading the temperature for 30 s. If the controller says something else than the manual, this is what shows it.

When the controller does not answer, check, in this order: that it is switched on; that it **has a serial interface**
(the TC 125's is an option); that the USB driver was installed *before* the cable was plugged in (the controller
then shows as a serial port); that you are allowed to use the port (Linux: group ``dialout``); that it is the right port
(the list is made again after a failed connection); the cable (RS-232: straight, not null-modem); and that no other
program (QNW's T-App, a terminal) holds the port.

What the driver does
====================

* ``hardware/qnw_tc.py``, ``QnwTC``: the driver, which does not need PyMoDAQ to be used (it uses its logger). It reads
  everything the controller says, whether it was asked for it or not (the buttons of the front panel are reported, and a
  periodic report which another program left on is not a problem), and keeps the last value of each message.
* A *set* command is not waited for an answer: the TC 125 does not give one. What matters is checked by a question: the
  status of the controller (``[F1 IS ?]``) says whether the control or the stirrer is on. The same target is not sent
  twice within 5 s.
* Only the commands of the manual of the controller are sent: to a controller with firmware 9 (TC 125, 225, 425) the
  ones of its appendix, and the stirrer speed commands (``SS S``, ``LS``, ``MS``) only to the TC 1 and the TC 1B. A command
  the controller does not understand is an error it counts (``[F1 IS ?]`` gives the number) and says
  (``[F1 ER 9 <<the command>>]`` or ``[F1 ER 09]``, or nothing): what only some models know is asked once when
  connecting and not again if the controller does not understand it.
* A fault of the controller (errors 5 to 8) is reported by the controller once: it stays in the *Fault* setting until the
  temperature control works again (switched on from the plugin, or at the front panel of the controller).
* The line may send back what it receives (a loopback plug, some serial servers): a message identical to the command
  which was just sent is not an answer.
* ``hardware/simulator.py``: the simulated controller. ``hardware/protocol.py``: the messages and their values.

The manuals contradict themselves in places (the answers to the heat exchanger questions are written ``CT`` in the one
of the TC 125, the answer to the question about the probe is written ``PR`` or ``PS``, the answer to *lowest stirrer
speed* is written ``MS``): the driver takes what is reasonable. The heat exchanger temperature and the probe status are
for information only.

Tests
=====

From the folder of the package, in the environment of PyMoDAQ (``pytest`` and ``pytest-qt`` are needed)::

    python -m pytest

They cover the language of the controllers, the simulated controller, the driver (the two models, lines which are slow,
cut, noisy or silent, a loopback, threads), the driver on real ports of pyserial (a pseudo-terminal and a TCP socket
which play the controller) and the plugin, and apply the acceptance checks of PyMoDAQ for plugins (``check_plugin``).
