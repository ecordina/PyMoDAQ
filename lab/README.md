# The lab plugins of PyMoDAQ

This is a fork of [PyMoDAQ](https://github.com/PyMoDAQ/PyMoDAQ) (release branch 5.3.x) which also holds, in this `lab/`
folder, the code that runs on the computers of the lab. **The branch to use is `lab`** (the default branch of the fork):
it is `5.3.x` of PyMoDAQ plus this folder, nothing of PyMoDAQ itself is changed.

| folder | what it is |
|---|---|
| `pymodaq_plugins_thermospectro` | the **ThermoSpectro** extension: one or two spectrometers and a camera, each with a thermostat, recorded during temperature ramps; 12 presets; `tools/thermospectro_export.py` makes csv and tif files from the h5 files |
| `pymodaq_plugins_quantumnorthwest` | the actuator **QNW_TC** for the Quantum Northwest TC 125 / TC 1 / TC 1B temperature controllers, with a simulator, `qnw_tc_check` (first contact), `qnw_tc_start` and `qnw_tc_demo` |
| `pymodaq_plugins_oceaninsight` | the **Seabreeze** spectrometers (Ocean Insight / Ocean Optics): pyseabreeze by default, several spectrometers at once, dark pixels removed, nm axis |
| `windows/` | launchers of the second computer (`zelux.bat`, `zeluxspec.bat`) |
| `environments/` | the Python packages of the two computers, and how they differ |
| `docs/HARDWARE.md` | the drivers to install on Windows (spectrometer, Thorlabs camera, temperature controller) |
| `install_lab.py` | installs the three plugins in the current Python environment |

## Put it on a computer

You need a Python 3.10+ environment (3.12 is what the lab uses) and Git.

```powershell
git clone https://github.com/ecordina/PyMoDAQ.git
cd PyMoDAQ
python lab/install_lab.py --camera --usb
```

`install_lab.py` installs PyMoDAQ 5.3 if it is missing, then the three plugins **editable** (the environment uses the
files of this folder). `--camera` adds the Thorlabs camera plugin and `tifffile`, `--usb` adds `libusb-package` and `pyusb`
for the pyseabreeze spectrometers; `--dry-run` shows what would be done. Then install the drivers of the instruments
(`docs/HARDWARE.md`) and try, without any hardware first:

```powershell
thermospectro ThermoCameraSpectroMock
```

(The commands are in the `Scripts` folder of the environment.) When everything is plugged in, click the plug button of the
extension (*Connect the modules*).

## Update a computer

```powershell
cd PyMoDAQ
git pull
python lab/install_lab.py --no-pymodaq        # only needed when a dependency changed
```

then start PyMoDAQ again. Nothing else is to be done on the computers: **do not edit the installed files in
`site-packages`**, change the code in this repository (on any computer: edit, `git commit`, `git push`) and pull it on
the others. A computer which was set up from the zips of the first versions (`pmdq`, `pmdq_pc2`) can keep its environment: run
`install_lab.py` in it, which replaces the installed copies of the plugins by the ones of this repository.

## Work on the code

```bash
# from the lab folder of the plugin, with its test tools (pytest, pytest-qt) in the environment
cd lab/pymodaq_plugins_thermospectro && python -m pytest -q          # the extension: ~900 tests, 15-25 minutes
cd lab/pymodaq_plugins_quantumnorthwest && python -m pytest -q      # the controller: ~530 tests, under a minute
cd lab/pymodaq_plugins_oceaninsight && python -m pytest -q          # the spectrometers
python -m pytest -q lab/tests                                        # install_lab.py (from the root)
```

The tests use simulated instruments (Mock modules of PyMoDAQ, the simulated controller `sim://tc125`), no hardware is
needed. The plugins have their own README (`README.rst`), which is the reference for their settings.

New presets (the experiments of the Dashboard) are written by `lab/pymodaq_plugins_thermospectro/tools/make_camera_presets.py`
(edit its table, run it) and are copied to the experiments folder of PyMoDAQ by the `thermospectro` command.

## Follow PyMoDAQ

```bash
git remote add upstream https://github.com/PyMoDAQ/PyMoDAQ.git       # once
git fetch upstream
git merge upstream/5.3.x                                              # on the branch lab
```

`5.3.x` of the fork can be kept equal to the one of PyMoDAQ; the lab code only lives in `lab/`, so merges do not conflict.
