from pathlib import Path
from timeit import timeit

import numpy as np
from easydict import EasyDict as edict
from pymodaq_utils.utils import ThreadCommand, getLineInfo
from pymodaq.utils.data import DataFromPlugins, Axis, DataToExport
from pymodaq.control_modules.viewer_utility_classes import DAQ_Viewer_base, comon_parameters, main

from pymodaq_plugins_oceaninsight import set_logger
from pymodaq_plugins_oceaninsight.hardware.seabreeze_utils import (
    AUTO, BACKENDS, DEFAULT_BACKEND, open_spectrometer, prepare_usb, release_backend, active_pixel_slice)

prepare_usb()  # (Windows: the libusb DLL of libusb-package must be known before pyusb looks for it)
from seabreeze.spectrometers import Spectrometer  # noqa: E402  only to annotate: each spectrometer is bound to its backend

logger = set_logger(Path(__file__).stem)


class DAQ_1DViewer_Seabreeze(DAQ_Viewer_base):
    """PyMoDAQ plugin for the Ocean Insight (Ocean Optics) spectrometers handled by python-seabreeze

    Nothing is looked for, and no spectrometer is touched, until the user initializes (connects) the module.

    The python-seabreeze backend, pyseabreeze by default or cseabreeze, is chosen in the Backend list of the settings,
    it is used when the spectrometer is initialized (pyseabreeze needs pyusb). The electric dark pixels of the detector
    are not part of the spectra.

    By default (Auto in the Device list) the connected spectrometers are sorted by serial number and the module opens
    the one whose rank is the number ending its name: *Spectro 1* the first one, *Spectro 2* the second one... A module
    whose name has no number opens the first one which is not open yet. The Device list shows the connected
    spectrometers once a module is initialized: a given one can be chosen in it.
    """
    #Supports pseudo hardware-averaging
    hardware_averaging = True

    params = comon_parameters + [
        {'title': 'Backend:', 'name': 'backend', 'type': 'list', 'limits': list(BACKENDS), 'value': DEFAULT_BACKEND,
         'tip': 'The python-seabreeze backend used when the spectrometer is initialized'},
        {'title': 'Device:', 'name': 'device', 'type': 'list', 'limits': [AUTO], 'value': AUTO,
         'tip': 'Auto: the spectrometer whose rank, by serial number, is the number ending the name of the module. '
                'The connected spectrometers are listed here when a module is initialized'},
        {'title': 'Integration (ms):', 'name': 'integration', 'type': 'float', 'value': 1.0},
        {'title': 'Advanced:', 'name': 'advanced', 'type': 'group', 'children': [
            {'title': 'Serial Number:', 'name': 'serial_number', 'type': 'str', 'value': "", 'readonly': True},
            {'title': 'Non Linearity correction:', 'name': 'correct_non_linearity', 'type': 'bool', 'value': False},
            {'title': 'Max Intensity', 'name': 'max_intensity', 'type': "float", 'value': 65535, 'readonly': True},
            {'title': 'Pixels:', 'name': 'pixels', 'type': 'int', 'value': 2048, 'readonly': True},
            {'title': 'Dark Channels:', 'name': 'dark_channels', 'type': 'int', 'value': 10, 'readonly': True},
            {'title': 'Readout Time (ms)', 'name': 'readout_time', 'type': 'float', 'value': 666, 'readonly': True},
        ]}
    ]

    def ini_attributes(self):
        self.controller: Spectrometer = None
        self.pixel_slice: slice = slice(None)  # the pixels of the detector that are not dark, set in ini_detector
        self._backend_in_use = None  # the backend of the spectrometer this module opened (not a slave one)

    def commit_settings(self, param):
        """
        """
        if param.name() == "integration":
            self.controller.integration_time_micros(param.value() * 1000)
            # There is no way to get feedback from the spectrometer on the currently set integration time
        elif param.name() == "backend":
            self.emit_status(ThreadCommand('Update_Status', [
                f"The {param.value()} backend will be used when the spectrometer is initialized again", 'log']))
        # elif ...
        ##

    def ini_detector(self, controller=None):
        """Detector communication initialization

        Parameters
        ----------
        controller: (object) custom object of a PyMoDAQ plugin (Slave case). None if only one detector by controller (Master case)

        Returns
        -------
        self.status (edict): with initialization status: three fields:
            * info (str)
            * controller (object) initialized controller
            *initialized: (bool): False if initialization failed otherwise True
        """
        self.ini_detector_init(controller, None)

        if self.is_master:
            # the spectro currently selected in the list (its serial number), opened with the chosen backend
            backend = self.settings['backend']
            logger.info(f"Initializing the spectrometer with the '{backend}' seabreeze backend")
            seen = []
            try:
                self.controller = open_spectrometer(backend, self.settings['device'],
                                                    module_title=getattr(self, '_title', None), seen=seen)
            finally:  # (the list is made when the user connects, whether it worked or not)
                self._show_devices(seen)
            self._backend_in_use = backend
            #####################################

        # Oceanoptics spectrometers (at least the ones i Know) have fixed axis
        # get the x_axis
        wavelengths = self.controller.wavelengths()  # Way to get the x axis
        # get inactive pixels: the electric dark ones (the models do not have the same, some have none at all)
        # and the unused ones next to them
        dark_pixels = self.controller.f.spectrometer.get_electric_dark_pixel_indices()
        self.pixel_slice = active_pixel_slice(len(wavelengths), dark_pixels)
        data_x_axis = wavelengths[self.pixel_slice]  # Get rid of the dark pixels. Seabreeze gives the wavelengths in nm
        self.settings.child('advanced').child('dark_channels').setValue(len(wavelengths) - len(data_x_axis))
        self.x_axis = Axis(data=data_x_axis, label='wavelength', units='nm', index=0)

        # Get the name
        specname = f"Ocean Insight {self.controller.model}"

        # initialize viewers pannel with the future type of data

        self.data_grabed_signal_temp.emit([
            DataFromPlugins(name=specname,
                            data=[np.zeros_like(data_x_axis)],
                            dim='Data1D',
                            labels=['Intensity'],
                            axes=[self.x_axis]), ])


        # Update the parameters
        # Here we need to do a few things. Get the integration time limits and set it in the settings
        tlimits = np.array(self.controller.integration_time_micros_limits) / 1000
        self.settings.child('integration').setLimits(tlimits)
        # Here we need to update the advanced parameters
        advanced_settings = self.settings.child('advanced')
        sn = self.controller.serial_number
        advanced_settings.child('serial_number').setValue(sn)
        # non linearity coefficients
        nlc_feat = self.controller.f.nonlinearity_coefficients
        if nlc_feat is not None and not any(np.isnan(nlc_feat.get_nonlinearity_coefficients())):
            advanced_settings.child('correct_non_linearity').setValue(True)
        else:
            advanced_settings.child('correct_non_linearity').setValue(False)
            advanced_settings.child('correct_non_linearity').setOpts(enabled=False)

        # measure the readout time
        Nperf = 200
        self.settings.child('integration').setValue(tlimits[0])  # Update the parameter value to the lower limit
        self.controller.integration_time_micros(tlimits[0]*1000)  # Set the readout time to lower limit
        perf = timeit(lambda: self.controller.intensities(), number=Nperf)  # time the execution of code in [s]
        self.settings.child('advanced').child('readout_time').setValue(1000*perf/Nperf)  # set the settings

        ##############################

        info = f"Initialized {specname} spectrometer"
        if self._backend_in_use is not None:
            info += f" with the {self._backend_in_use} backend"
        initialized = True
        return info, initialized

    def _show_devices(self, devices):
        """List the connected spectrometers in the Device list

        What is selected stays selected, even if it is not connected now: it is the choice of the user.
        """
        if not devices:
            return
        try:
            device = self.settings.child('device')
            current = device.value()
            items = [AUTO] + [str(item) for item in devices]
            if current not in items:
                items.append(current)
            device.setLimits(items)
        except Exception:  # (this is only to show them: it must not hide why the spectrometer could not be opened)
            logger.exception('The Device list could not be updated')

    def close(self):
        """
        Terminate the communication protocol
        """
        try:
            if self.controller is not None:
                self.controller.close()
        finally:
            if self._backend_in_use is not None:
                release_backend(self._backend_in_use)
                self._backend_in_use = None

    def grab_data(self, Naverage = 1, **kwargs):
        """
        Parameters
        ----------
        Naverage: (int) Number of hardware averaging
        kwargs: (dict) of others optionals arguments
        """
        nlc = self.settings.child('advanced').child('correct_non_linearity').value()
        # synchrone version (blocking function)
        # Pseudo-hardware-averaging
        if Naverage > 1:
            data = [self.controller.intensities(correct_nonlinearity=nlc)[self.pixel_slice] for i in range(Naverage)]
            data = np.array(data)
            data = data.mean(0)
        # Otherwise normal single-acquisition
        else:
            data = self.controller.intensities(correct_nonlinearity=nlc)[self.pixel_slice]

        self.dte_signal.emit(DataToExport('Spectro', data=[
            DataFromPlugins(name='oceanseabreeze', data=[data], dim='Data1D',
                            labels=['spectrum'], axes=[self.x_axis.copy()])]))

    def stop(self):
        pass


if __name__ == '__main__':
    main(__file__, init=False)
