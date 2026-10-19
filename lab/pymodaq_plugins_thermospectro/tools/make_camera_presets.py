"""Write the presets (experiments of the Dashboard) with a camera: python tools/make_camera_presets.py

The presets are plain xml files in ``src/pymodaq_plugins_thermospectro/resources/presets``; this script made the ones
with a camera from the structure of the others (the Dashboard reads the name, the type, the dimension, whether the module
is initialized, and its axis). Edit the table at the end to make another one.
"""
from pathlib import Path

HEAD = ('type="{type}" title="{title}" visible="1" removable="{removable}" readonly="{readonly}" enabled="1" '
        'renamable="0" expanded="{expanded}" syncExpanded="0" showTop="0" strictNaming="0"')


def attrs(type_, title, removable=0, readonly=0, expanded=1, extra=''):
    return HEAD.format(type=type_, title=title, removable=removable, readonly=readonly, expanded=expanded) + extra


def param(type_, title, value, readonly=0, extra='', expanded=1):
    return f'<{title} {attrs(type_, title, 0, readonly, expanded, extra)}>{value}</{title}>'


def group(title, children, expanded=1, removable=0):
    return f'<{title} {attrs("group", title, removable, 0, expanded)}>{"".join(children)}</{title}>'


def controller(identifier, axis=None):
    children = [param('list', 'controller_status', "str('Master')", extra=" limits=\"['Master', 'Slave']\""),
                param('int', 'controller_ID', identifier)]
    if axis:
        names, chosen = axis
        children.append(param('list', 'axis', f"str('{chosen}')",
                              extra=f" valid_for_configuration=\"0\" limits=\"{names}\""))
    return group('controller', children, expanded=0)


def actuator(index, name, class_name, identifier, names, chosen):
    info = group('info', [param('str', 'type', class_name, readonly=1),
                          param('list', 'ui', "str('Simple')", extra=" limits=\"['Binary', 'Relative', 'Simple']\""),
                          param('bool', 'init', 0)], expanded=0)
    return group(f'actuator{index:02d}', [param('str', 'name', name), info, controller(identifier, (names, chosen))],
                 removable=1)


def detector(index, name, class_name, dim, identifier):
    info = group('info', [param('str', 'type', class_name, readonly=1), param('str', 'dim', dim, readonly=1),
                          param('bool', 'init', 0)], expanded=0)
    return group(f'detector{index:02d}', [param('str', 'name', name), info, controller(identifier)], removable=1)


def preset(actuators, detectors):
    root = (f'<settings {attrs("group", "Settings", extra="")}>'
            f'<actuator {attrs("groupmove", "actuator", extra=" show_pb=\"0\" addText=\"Add\"")}>'
            + '\n\t\t'.join(actuators) + '</actuator>'
            f'<detector {attrs("groupdet", "detector", extra=" show_pb=\"0\" addText=\"Add\"")}>'
            + '\n\t\t'.join(detectors) + '</detector></settings>')
    return root


MOCK_AXES = "['X', 'Y', 'Theta', 'Power', 'Temp', 'Frequency', 'Speed']"
QNW_AXES = "['Temperature']"
THERMO_MOCK = actuator(0, 'Thermo1', 'Mock', 2186, MOCK_AXES, 'Temp')
THERMO_QNW = actuator(0, 'Thermo1', 'QNW_TC', 2187, QNW_AXES, 'Temperature')

#: name: (thermostat, [(module name, plugin, dimension)])
PRESETS = {
    'ThermoCameraMock': (THERMO_MOCK, [('Camera1', 'Mock', 'DAQ2D')]),
    'ThermoCameraSpectroMock': (THERMO_MOCK, [('Spectro1', 'Mock', 'DAQ1D'), ('Camera1', 'Mock', 'DAQ2D')]),
    'ThermoCameraThorlabsTSI': (THERMO_QNW, [('Camera1', 'Thorlabs_TSI', 'DAQ2D')]),
    'ThermoCameraSpectroThorlabsTSI': (THERMO_QNW, [('Spectro1', 'Seabreeze', 'DAQ1D'),
                                                    ('Camera1', 'Thorlabs_TSI', 'DAQ2D')]),
    'ThermoCameraThorlabsUC480': (THERMO_QNW, [('Camera1', 'UC480', 'DAQ2D')]),
    'ThermoCameraSpectroThorlabsUC480': (THERMO_QNW, [('Spectro1', 'Seabreeze', 'DAQ1D'),
                                                      ('Camera1', 'UC480', 'DAQ2D')]),
}


def main():
    folder = Path(__file__).resolve().parents[1] / 'src' / 'pymodaq_plugins_thermospectro' / 'resources' / 'presets'
    for number, (name, (thermostat, detectors)) in enumerate(PRESETS.items()):
        modules = [detector(index, module, plugin, dim, 2540 + 10 * number + index)
                   for index, (module, plugin, dim) in enumerate(detectors)]
        (folder / f'{name}.xml').write_text(preset([thermostat], modules), encoding='utf-8')
        print('written', folder / f'{name}.xml')


if __name__ == '__main__':
    main()
