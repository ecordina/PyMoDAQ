"""The acceptance checks of PyMoDAQ for plugin packages (``check_plugin``): naming, entry points, plugin classes"""
from pymodaq_utils.plugin_testing import PluginPackageChecks


class TestPluginPackage(PluginPackageChecks):
    package_name = 'pymodaq_plugins_quantumnorthwest'
    fail_on = 'todo'  # nothing is left to do: not even a TODO comment
    strict_imports = True
