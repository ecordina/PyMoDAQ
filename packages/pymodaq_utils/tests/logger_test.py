# -*- coding: utf-8 -*-
"""
Created the 28/10/2022

@author: Sebastien Weber
"""
import logging
import logging.handlers
import time

import pytest

import pymodaq_utils
from pymodaq_utils import config as config_mod
from pymodaq_utils import logger as logger_mod


def test_get_module_name():
    config_path = config_mod.get_set_config_dir()
    assert logger_mod.get_module_name(config_path) == 'config'


def test_get_base_logger():
    logger = logger_mod.set_logger('random_name')
    assert logger_mod.get_base_logger(logger).name == 'pymodaq'


def test_the_loggers_of_a_log_file_share_one_handler(tmp_path):
    """Two handlers on one file cannot rotate it on Windows (the file is open twice)"""
    path = tmp_path / 'shared.log'
    first = logger_mod.get_file_handler(path)
    second = logger_mod.get_file_handler(str(path))
    other = logger_mod.get_file_handler(tmp_path / 'other.log')

    assert first is second
    assert other is not first
    for handler in (first, other):
        handler.close()


def test_a_log_file_which_cannot_be_rotated_is_written_all_the_same(tmp_path, monkeypatch):
    path = tmp_path / 'locked.log'
    handler = logger_mod.SafeTimedRotatingFileHandler(path, when='midnight')
    logger = logging.getLogger('test_locked_log')
    logger.setLevel('INFO')
    logger.propagate = False
    logger.addHandler(handler)

    def locked(self, source, dest):
        raise PermissionError(32, 'the file is used by another process')

    monkeypatch.setattr(logging.handlers.TimedRotatingFileHandler, 'rotate', locked)
    handler.rolloverAt = time.time() - 1  # (the day is over)
    logger.info('after midnight')
    logger.info('and again')
    handler.flush()

    assert handler.rolloverAt > time.time()  # (it does not try again at each record)
    assert path.read_text(encoding='utf-8').count('\n') == 2  # (both records are in the file, nothing was raised)
    logger.removeHandler(handler)
    handler.close()
