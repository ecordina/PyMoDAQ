"""The five configurations of the lab: which instruments an application has, and what they are called

Each configuration is an application of its own (a command, an extension of the Dashboard): it has the settings and the
windows of its instruments and nothing else. The instruments are control modules of the Dashboard with fixed names:

=========================  =====================================  =====================================
command                    instruments                            units of the extension
=========================  =====================================  =====================================
``thermo_2spectro``        2 spectrometers, 2 thermal controllers 1 (Spectro1 + Thermo1), 2 (Spectro2 + Thermo2)
``thermo_spectro``         1 spectrometer, 1 thermal controller   1 (Spectro1 + Thermo1)
``camera``                 1 camera                               3 (Camera1)
``thermo_camera``          1 camera, 1 thermal controller         3 (Camera1 + Thermo1)
``thermo_camera_spectro``  1 camera, 1 thermal controller,        1 (Spectro1 + Thermo1) and
                           1 spectrometer                         3 (Camera1, which only reads the temperature of Thermo1)
=========================  =====================================  =====================================

Pure data, no Qt and no PyMoDAQ: the launcher, the detection of the instruments and the extensions all use it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

SPECTROMETER_NUMBERS = (1, 2)  # the units of the spectrometers
CAMERA_NUMBER = 3  # the unit of the camera


@dataclass(frozen=True)
class Configuration:
    """What an application has

    Parameters
    ----------
    command: str
        The command which starts it (``thermo_camera_spectro``)
    class_name: str
        The name of its extension class (the key of the extension in PyMoDAQ is this name in capitals)
    title: str
        What the extension is called in the Dashboard and in its window
    numbers: tuple of int
        The units of the extension: 1 and 2 for the spectrometers, 3 for the camera
    spectrometers, thermostats: int
        How many spectrometers and thermal controllers
    camera: bool
        Whether there is a camera
    """
    command: str
    class_name: str
    title: str
    numbers: tuple
    spectrometers: int
    thermostats: int
    camera: bool

    @property
    def spectrometer_names(self) -> list[str]:
        return [f'Spectro{number}' for number in range(1, self.spectrometers + 1)]

    @property
    def thermostat_names(self) -> list[str]:
        return [f'Thermo{number}' for number in range(1, self.thermostats + 1)]

    @property
    def camera_name(self) -> Optional[str]:
        return 'Camera1' if self.camera else None

    @property
    def module_names(self) -> list[str]:
        """The names of the control modules, the thermal controllers first"""
        return self.thermostat_names + self.spectrometer_names + ([self.camera_name] if self.camera else [])


CONFIGURATIONS = {
    configuration.command: configuration for configuration in (
        Configuration('thermo_2spectro', 'Thermo2Spectro', 'Two spectrometers with a thermal controller each',
                      (1, 2), spectrometers=2, thermostats=2, camera=False),
        Configuration('thermo_spectro', 'ThermoOneSpectro', 'A spectrometer with a thermal controller',
                      (1,), spectrometers=1, thermostats=1, camera=False),
        Configuration('camera', 'CameraOnly', 'A camera', (3,), spectrometers=0, thermostats=0, camera=True),
        Configuration('thermo_camera', 'ThermoCamera', 'A camera with a thermal controller',
                      (3,), spectrometers=0, thermostats=1, camera=True),
        Configuration('thermo_camera_spectro', 'ThermoCameraSpectro',
                      'A camera and a spectrometer with a thermal controller',
                      (1, 3), spectrometers=1, thermostats=1, camera=True),
    )
}


def configuration_of_class(class_name: str) -> Optional[Configuration]:
    """The configuration of an extension class, None for the classes which are not one of the five"""
    for configuration in CONFIGURATIONS.values():
        if configuration.class_name == class_name:
            return configuration
    return None
