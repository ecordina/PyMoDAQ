# -*- coding: utf-8 -*-
from pathlib import Path

from pymodaq_utils.config import BaseConfig


class Config(BaseConfig):
    """Main class to deal with configuration values for this plugin"""
    config_template_path = Path(__file__).parent.joinpath('resources/config_template.toml')
    config_name = f"config_{__package__.split('pymodaq_plugins_')[1]}"
