# The instruments on Windows

The drivers of the instruments are not part of the plugins: install them once per computer.

## The spectrometer (Ocean Insight / Seabreeze)

The plugin uses the **pyseabreeze** backend (pure Python, through libusb) by default.

1. `python lab/install_lab.py --usb` (or `python -m pip install libusb-package pyusb`): gives pyusb the libusb DLL. The plugin
   makes it known to Windows by itself (`prepare_usb`): there is nothing to put in `site-packages`.
2. The **WinUSB driver** on the spectrometer: plug it in, download **Zadig** (zadig.akeo.ie), choose the spectrometer
   (*Options > List all devices*), select **WinUSB**, *Replace Driver*. This replaces the driver of Ocean Optics:
   OceanView does not see that spectrometer any more (switch it back with Zadig or the Device Manager).

Two spectrometers at once: each module picks the one whose rank (by serial number) is the number ending its name
(*Spectro1*, *Spectro2*); the USB devices are opened one at a time.

Check: `python -c "from seabreeze.pyseabreeze import SeaBreezeAPI; print(SeaBreezeAPI().list_devices())"`

## The Thorlabs camera (the commands with a camera)

`python lab/install_lab.py --camera` installs `pymodaq_plugins_thorlabs` (it brings `pylablib`) and `tifffile`. The camera
needs the driver of its family:

* **Thorlabs scientific cameras** (Zelux, Kiralux, Quantalux: plugin `Thorlabs_TSI`): Thorlabs' **ThorCam** software (it
  brings the DLLs). If it complains about `ftd2xx.dll`, copy that FTDI DLL next to the Python of the environment.
* **uc480 cameras** of Thorlabs and **IDS uEye** cameras (plugin `UC480`): the uc480 / DCx driver of Thorlabs or IDS's uEye
  driver; the `uc480` DLL must be on the PATH.

Check that it is seen: `python -c "from pylablib.devices import Thorlabs; print(Thorlabs.list_cameras_tlcam())"` (TSI) or
`python -c "from pylablib.devices import uc480; print(uc480.list_cameras())"` (uc480).

**On Linux (and macOS), OpenCV must be the headless one.** `pymodaq_plugins_thorlabs` requires `opencv-python`, whose wheel
ships its own Qt 5 and points Qt to it when it is imported (PyMoDAQ imports it with the plugins, before its window is made):
PyMoDAQ and every command of the lab then stop at *Could not load the Qt platform plugin "xcb" in ".../cv2/qt/plugins" even
though it was found*. `install_lab.py --camera` (and `--all`) puts `opencv-python-headless`, the same `cv2` without the
GUI, in place of it. By hand: `python -m pip uninstall -y opencv-python` then `python -m pip install opencv-python-headless`
(`pip check` then says that the plugin wants `opencv-python`: it can be ignored). Updating `pymodaq_plugins_thorlabs` with pip
brings `opencv-python` back: run the two commands again, or `install_lab.py --camera`. (The lab commands also take the
folders of `cv2` out of `QT_QPA_PLATFORM_PLUGIN_PATH` before they make their window, so they start even then; the
`dashboard` command of PyMoDAQ does not.) Windows is not concerned.

## The temperature controller (Quantum Northwest)

* Plug it in: Windows installs the FTDI driver by itself (otherwise the FTDI VCP driver, or the one in the software of
  QNW). It is a **COM port** in *Device Manager > Ports (COM & LPT)*. On Linux the user must be in the group `dialout`.
* Close QNW's T-App (a port has one user) and look at what the controller says; nothing is set on it:

```powershell
qnw_tc_check COM3 --trace
```

The TC 125 needs its serial option (a USB or RS-232 connector at the back). The command (`thermo_spectro`...) looks for the
controller by itself; when it does not find it, choose the port in the settings of the `QNW_TC` module before connecting. The plugin's README says what the controller does and what was
verified on a real TC 125.
