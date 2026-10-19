# The environments of the lab computers

`pc1-pip.txt` and `pc2-pip.txt` list the Python packages of the two Windows computers (Python 3.12 environments, taken from
their `site-packages` on 2026-10-12). They are references: to make the environment of a computer again, or to see what
differs between them.

| | PC 1 | PC 2 |
|---|---|---|
| `pymodaq` | 5.3.1 | 5.3.4 |
| `pymodaq_utils` | 5.3.3 | 5.3.4 |
| Qt | PyQt6 | PyQt6 and PyQt5 |
| libusb for pyseabreeze | `libusb-package` + a `libusb_fix.pth` file | `libusb-package` |

The lab plugins are **not** in the lists: they are installed from this repository (`python lab/install_lab.py`), which is
how a computer gets its updates (`git pull`, then the same command if a dependency changed).

What the two computers had changed in their installed plugins is now part of the plugins of this repository:

* PC 1: `pymodaq_plugins_oceaninsight/hardware/seabreeze_utils.py` opens the USB devices one at a time (two spectrometers
  connected together corrupted each other's answers), and `libusb_fix.pth` (the DLL folder of `libusb-package` and the
  pyseabreeze backend) is done by the plugin itself on Windows (`prepare_usb`), nothing is to be put in `site-packages`.
* PC 2: the image of the camera in ThermoSpectro is shown by the image viewer of PyMoDAQ (colour channels, histogram,
  levels...), and the launchers `lab/windows/zelux.bat` and `zeluxspec.bat` start the presets `ThermoZeluxMockThermo` and
  `ThermoZeluxMockSpectroMockThermo` (these presets were rebuilt from the names in the launchers: the PC's own files were not
  in the environment; compare them with the ones of the computer).
