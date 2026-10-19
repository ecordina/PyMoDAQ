pymodaq_plugins_oceaninsight (Ocean Insight (Optics) Spectrometers)
###################################################################

.. image:: https://readthedocs.org/projects/pymodaq/badge/?version=latest
   :target: https://pymodaq.readthedocs.io/en/stable/?badge=latest
   :alt: Documentation Status

.. image:: https://github.com/PyMoDAQ/pymodaq_plugins_oceaninsight/workflows/Upload%20Python%20Package/badge.svg
    :target: https://github.com/PyMoDAQ/pymodaq_plugins_oceaninsight

PyMoDAQ plugin for OceanInsight (OceanOptics) spectrometers

Modified version
================

This is **a modified copy** of the plugin of the PyMoDAQ organisation, https://github.com/PyMoDAQ/pymodaq_plugins_oceaninsight
(version 1.1.1), kept in the lab repository https://github.com/ecordina/PyMoDAQ (folder ``lab/``). Its version is
``5.3.0+lab.1`` so that it is never taken for the release of PyPI: **do not use the Update button of the Plugin Manager of
PyMoDAQ for it**, it would replace this version by the original one. The original MIT license and authors are kept (the
changes of E. Cordina, 2026, are under the same license). What was changed:

* the python-seabreeze backend (pyseabreeze by default, or cseabreeze) is chosen for each spectrometer in the settings of its
  module, and the spectrometers are put in a stable order, sorted by serial number (*Auto* device, see below);
* the electric dark pixels of the detector are not part of the spectra, and the wavelengths are in nm;
* several spectrometers can be initialized at the same time: the USB devices are opened one at a time (they corrupted each
  other's answers otherwise);
* on Windows the libusb DLL of the ``libusb-package`` package is made known to pyusb by the plugin (``prepare_usb``);
* PyMoDAQ 5.x: the logger and the configuration come from ``pymodaq_utils``, and the tests of the plugin run without
  spectrometer.


Authors
=======

* Sebastien J. Weber
* Nicolas Tappy

Instruments
===========
Below is the list of instruments included in this plugin.
Url: https://www.oceaninsight.com/products/spectrometers/

Viewer1D
++++++++

* **Omnidriver**: Control of Spectrometer using the Omnidriver library (should be installed)
* **Seabreeze** : If the Omnidriver library is not available, a plugin implementation based on seabreeze is provided: https://python-seabreeze.readthedocs.io/en/latest/index.html

Which spectrometer: *Auto*
==========================

The *Device* list of a Seabreeze spectrometer starts with **Auto**, which is the default. With *Auto* a spectrometer
looks for the ones which are connected by itself, when it is initialized. The connected spectrometers are put in a
stable order, **sorted by serial number**, so that the first one is always the same one as long as the same
spectrometers are connected. Then:

* a module whose name ends with a number takes the spectrometer of this rank: **Spectro1** opens the first spectrometer
  of the sorted list, **Spectro2** the second one (``list_devices()[1 - 1]``, ``list_devices()[2 - 1]``), and so on;
* if there is no spectrometer of this rank (Spectro2 with only one connected), if this one is already open (by another
  module), or if the name has no number, the module takes the first spectrometer which is not open yet, so two modules
  never open the same one;
* if all the connected spectrometers are open, or if none is connected, the initialization says so.

To always use a given spectrometer whatever the other ones, choose it by its serial number in the *Device* list.
Nothing is looked for when PyMoDAQ starts or when a module is created: the connected spectrometers are listed, and
shown in the *Device* list, when a module is initialized (when the user clicks its initialization button), so a
spectrometer may be plugged in at any time before that.

Seabreeze backend
=================

python-seabreeze talks to the spectrometers through one of its two backends:

* ``pyseabreeze`` (default): a pure python implementation. It needs the ``pyusb`` package (``pip install pyusb``) and
  the libusb library
* ``cseabreeze``: the compiled libseabreeze, shipped with the seabreeze package

The backend of a spectrometer is chosen in the **Backend** list of its settings (next to the *Device* list). It is used
when the spectrometer is initialized: to change it, un-initialize the spectrometer, choose the other backend and
initialize it again. The PyMoDAQ log tells which backend is used ("Initializing the spectrometer with the
'pyseabreeze' seabreeze backend"), and why it cannot be used if it is the case (for instance pyseabreeze without
``pyusb``).

The two backends cannot be mixed in a PyMoDAQ session while spectrometers are open (pyseabreeze resets the USB
devices): all the spectrometers have to use the same backend, otherwise the initialization is refused with an explicit
message. Choosing the backend this way needs seabreeze 2.9.0 or later.

There is no setting of the PyMoDAQ preferences for it (an older version of the plugin had a ``seabreeze_backend``
entry: it is not used any more, and can be ignored): every module starts with ``pyseabreeze``.

The pixels of the detector that are not optically active (the electric dark pixels reported by the spectrometer, and the
unused ones next to them) are not part of the spectra. Their number is displayed in the *Advanced* settings
(*Dark Channels*), it is 0 for the models that do not have any.
