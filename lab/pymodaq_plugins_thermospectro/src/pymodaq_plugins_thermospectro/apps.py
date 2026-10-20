# -*- coding: utf-8 -*-
"""``lab_apps``: the names of the applications of the lab, and what each one is for

    lab_apps              # the applications, the instruments of each, and the other commands
    lab_apps --names      # only the names, one per line (for a script)

Nothing is opened, looked for or connected: this only reads the table of the applications (:mod:`.configurations`), so it
is instant (and does not need Qt). It also works without the command being installed:
``python -m pymodaq_plugins_thermospectro.apps``.
"""
from __future__ import annotations

import argparse
import shutil
import textwrap
from pathlib import Path
from typing import Optional, Sequence

from pymodaq_plugins_thermospectro.configurations import CONFIGURATIONS, Configuration

#: The commands of the lab which are not applications: (what to type, what it does)
OTHER_COMMANDS = (
    ('lab_apps', 'this list'),
    ('qnw_tc_check COM3 --trace', 'first contact with a Quantum Northwest controller (nothing is set on it)'),
)


def plural(count: int, noun: str) -> str:
    return f'{count} {noun}' + ('' if count == 1 else 's')


def instruments(configuration: Configuration) -> str:
    """What an application has, in words: ``1 camera + 1 spectrometer + 1 thermal controller``"""
    parts = []
    if configuration.camera:
        parts.append('1 camera')
    if configuration.spectrometers:
        parts.append(plural(configuration.spectrometers, 'spectrometer'))
    if configuration.thermostats:
        parts.append(plural(configuration.thermostats, 'thermal controller'))
    return ' + '.join(parts)


def export_command() -> str:
    """How to start the converter of the h5 files (a script of the folder ``tools`` of the plugin, when it is there)"""
    script = Path(__file__).resolve().parents[2] / 'tools' / 'thermospectro_export.py'
    return f'python {script if script.exists() else "tools/thermospectro_export.py"} FILE.h5'


def describe(width: Optional[int] = None) -> str:
    """The applications and the other commands, as a text (its paragraphs are wrapped to `width` columns)"""
    width = width or min(max(shutil.get_terminal_size((80, 24)).columns, 60), 100)

    def paragraph(text: str) -> str:
        return textwrap.fill(text, width=width)

    rows = [(command, instruments(configuration)) for command, configuration in CONFIGURATIONS.items()]
    first = max(len(command) for command, _ in rows)
    lines = ['The applications of the lab (type the name in a terminal):', '']
    lines += [f'  {command.ljust(first)}   {what}' for command, what in rows]
    lines += ['',
              paragraph('Each one looks at what is connected to the computer, connects what it recognizes and tells what '
                        'it does not: nothing is acquired until you press Start (or Live) in its window.'),
              paragraph('Options of each: --mock (simulated instruments), --port COM6 (the port of a thermal controller, '
                        'once for each), --camera-plugin NAME, --thermostat-plugin NAME, --no-detect; <name> --help '
                        'says more.'),
              '',
              'The other commands:']
    first = max(len(command) for command, _ in OTHER_COMMANDS)
    lines += [textwrap.fill(f'{command.ljust(first)}   {what}', width=width, initial_indent='  ',
                            subsequent_indent=' ' * (first + 5)) for command, what in OTHER_COMMANDS]
    lines += ['  csv and tif files from the h5 file of a log:', f'    {export_command()}']
    return '\n'.join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog='lab_apps', description='The names of the applications of the lab.')
    parser.add_argument('--names', action='store_true', help='only the names, one per line')
    arguments = parser.parse_args(argv)
    print('\n'.join(CONFIGURATIONS) if arguments.names else describe())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
