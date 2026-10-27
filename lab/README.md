# The lab plugins of PyMoDAQ

This is a fork of [PyMoDAQ](https://github.com/PyMoDAQ/PyMoDAQ) (release branch 5.3.x) which also holds, in this `lab/`
folder, the code that runs on the computers of the lab. **The branch to use is `lab`** (the default branch of the fork):
it is the release 5.3.8 of PyMoDAQ plus this folder and two fixes of PyMoDAQ itself (see "PyMoDAQ itself" below); the only other
PyMoDAQ files it changes are the two root README files (a note at the top).

| folder | what it is |
|---|---|
| `pymodaq_plugins_thermospectro` | the five **applications** (`thermo_2spectro`, `thermo_spectro`, `camera`, `thermo_camera`, `thermo_camera_spectro`): spectrometers and a camera recorded while a thermal controller holds a temperature or runs a ramp, for any length of time; they look for the instruments by themselves; **`lab_export FILE.h5`** makes csv files (the values of the ROIs, the full spectra, the crops), tif files (T, C, Y, X) and a text (protocol, observations, units) from the h5 files |
| `pymodaq_plugins_quantumnorthwest` | the actuator **QNW_TC** for the Quantum Northwest TC 125 / TC 1 / TC 1B temperature controllers, with a simulator and `qnw_tc_check` (first contact) |
| `pymodaq_plugins_oceaninsight` | the **Seabreeze** spectrometers (Ocean Insight / Ocean Optics): pyseabreeze by default, several spectrometers at once, dark pixels removed, nm axis |
| `environments/` | the Python packages of the two computers, and how they differ |
| `docs/HARDWARE.md` | the drivers to install on Windows (spectrometer, Thorlabs camera, temperature controller) |
| `install_lab.py` | installs the three plugins in the current Python environment; `--all` installs everything the lab uses (PyMoDAQ with its fixes, the camera and USB packages, the plugins) |

## Put it on a computer

You need a Python 3.10+ environment (3.12 is what the lab uses) and Git.

```powershell
git clone -b lab --single-branch https://github.com/ecordina/PyMoDAQ.git
cd PyMoDAQ
python lab/install_lab.py --all
```

`--all` installs **everything**: PyMoDAQ from this repository (with PyQt6, `--qt` chooses another Qt binding; see "PyMoDAQ itself"
below), the Thorlabs camera plugin and `tifffile`, `libusb-package` and `pyusb` for the pyseabreeze spectrometers, and the three
plugins **editable** (the environment uses the files of this folder). It is the same as `--core --camera --usb`; each of
them can be given alone, and without any of them only the three plugins are installed (and PyMoDAQ from PyPI if the
environment has none; with the camera, on Linux and macOS, `opencv-python` is replaced by `opencv-python-headless`, see
`docs/HARDWARE.md`). `--dry-run` shows what would be done. Then install the drivers of the instruments
(`docs/HARDWARE.md`) and try, without any hardware first:

```powershell
thermo_camera_spectro --mock
```

(The commands are in the `Scripts` folder of the environment; `lab_apps` lists the five applications, `thermo_2spectro`, `thermo_spectro`,
`camera`, `thermo_camera` and `thermo_camera_spectro`, with what each one is for; `lab_export FILE.h5` converts the h5 file of an experiment.) Without `--mock` the same command looks at what is connected:
it connects what it recognizes and tells what it does not (select the port of a thermal controller, `COM6` for instance, in the
settings of its module in the Dashboard, click *Ini*), then you press *Start* in the window of the application: it logs
everything until *Stop*, or for the duration you set (48 h...). Each thermal controller holds a temperature, or runs a cycle
of ramps and then **stays at its initial or final temperature while the log goes on** until you press *Stop* (the arrows of
the toolbar end the ramps at the top or at the bottom whenever you want). See the README of `pymodaq_plugins_thermospectro`.

## PyMoDAQ itself, and its fixes

`install_lab.py` installs PyMoDAQ from PyPI when the environment has none, and otherwise leaves the installed one alone
(the computers of the lab have 5.3.1 and 5.3.4, see `environments/`). This repository holds the release **5.3.8** of PyMoDAQ
with the fixes the lab needs, in `packages/`:

* **a hardware initialization which failed is closed before the next one** (branch `fix/failed_hardware_init_5.3.x` of the
  PyMoDAQ repository, `control_modules/utils.py`): after a failed *Ini* (wrong or busy port, spectrometer not found...) the
  next attempts initialized the hardware 2, 3... times and quitting the application aborted the process (`QThread:
  Destroyed while thread is still running`). The PID actuator, which cannot be initialized without its extension, says so.
* **the log file is rotated without errors on Windows** (`pymodaq_utils/logger.py`): `pymodaq_utils` and `pymodaq_data` each added a
  handler on the same `pymodaq.log`, and Windows cannot rename a file which is open twice, so after midnight every message printed
  a `--- Logging error ---` traceback to the console and was lost. The loggers now share one handler per file, and a rotation
  which fails (another instance of the application holds the file) no longer raises: the file is written to, and rotated the next day.

These fixes are only on a computer which runs PyMoDAQ **from this repository**: `python lab/install_lab.py --core` installs
the five packages of `packages/` (editable, as `5.3.8+lab`, no git tag needed) in place of the installed PyMoDAQ; then
`git pull` is enough to update them too. Without `--core` the computer keeps the PyMoDAQ it has, and the fixes are not there.

## Update a computer

```powershell
cd PyMoDAQ
git pull
python lab/install_lab.py --all               # only needed when a dependency changed, or a new command came (--no-pymodaq if
                                              # the computer keeps the PyMoDAQ it has, plus --camera --usb if it uses them)
```

(A **new command**, like `lab_export`, is created by the installation: after a `git pull` which brings one, run `install_lab.py`
once, or `python -m pip install -e lab/pymodaq_plugins_thermospectro`.)

then start PyMoDAQ again. Do not use the *Update* button of the Plugin Manager of PyMoDAQ for the lab plugins (it would
replace the one of this repository by the release of PyPI). Nothing else is to be done on the computers: **do not edit the installed files in
`site-packages`**, change the code in this repository (on any computer: edit, `git commit`, `git push`) and pull it on
the others. A computer which was set up from the zips of the first versions (`pmdq`, `pmdq_pc2`) can keep its environment: run
`install_lab.py` in it, which replaces the installed copies of the plugins by the ones of this repository.

## Work on the code

```bash
# from the lab folder of the plugin, with its test tools (pytest, pytest-qt) in the environment
cd lab/pymodaq_plugins_thermospectro && python -m pytest -q          # the applications: ~1,250 tests, 40-60 minutes in one piece (they open real windows)
cd lab/pymodaq_plugins_quantumnorthwest && python -m pytest -q      # the controller: ~570 tests, a couple of minutes
cd lab/pymodaq_plugins_oceaninsight && python -m pytest -q          # the spectrometers: ~130 tests
python -m pytest -q lab/tests                                        # install_lab.py (from the root)
```

The tests use simulated instruments (Mock modules of PyMoDAQ, the simulated controller `sim://tc125`), no hardware is
needed. The plugins have their own README (`README.rst`), which is the reference for their settings.

A new application or instrument is a `Configuration` in `lab/pymodaq_plugins_thermospectro/src/pymodaq_plugins_thermospectro/configurations.py`
(its units and modules), an extension class in `extensions/`, and a command in `launcher.py` / `pyproject.toml`; what the
commands recognize is in `detect.py`.

## Follow PyMoDAQ

```bash
git remote add upstream https://github.com/PyMoDAQ/PyMoDAQ.git       # once
git fetch upstream
git merge upstream/5.3.x                                              # on the branch lab
```

`5.3.x` of the fork can be kept equal to the one of PyMoDAQ; the lab code only lives in `lab/`, so merges only conflict if PyMoDAQ
changes the first lines of its root `README.rst` / `README.rst.tpl`, where the note of this fork is. GitHub Actions is left
disabled on the fork (its workflows would rewrite the READMEs and push to `lab`).
