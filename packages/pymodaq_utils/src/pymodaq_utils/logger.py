# -*- coding: utf-8 -*-
"""
Created the 27/10/2022

@author: Sebastien Weber
"""
import sys
import logging
import threading
import time
import warnings
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from pymodaq_utils.config import get_set_config_dir, GlobalConfig as Config

config = Config()


class SafeTimedRotatingFileHandler(TimedRotatingFileHandler):
    """A TimedRotatingFileHandler which goes on writing when the log file cannot be rotated

    On Windows a file cannot be renamed while another handle holds it open (another instance of the application, or
    another handler of the same process): the standard handler then raises at each record after midnight, prints a
    traceback each time to the console and loses the records. This one keeps appending to the same file and tries again
    at the next rollover time.
    """

    def doRollover(self):
        try:
            super().doRollover()
        except OSError:  # (PermissionError, WinError 32: the file is in use)
            if self.stream is None:  # (the standard handler closed it before it tried to rename it)
                self.stream = self._open()
            self.rolloverAt = self.computeRollover(int(time.time()))


_FILE_HANDLERS: dict[str, logging.Handler] = {}  # one handler per log file: two of them could not rotate it
_FILE_HANDLERS_LOCK = threading.Lock()


def get_file_handler(log_file_path) -> logging.Handler:
    """The handler which writes to this log file (created the first time, then shared by all the loggers)"""
    key = str(Path(log_file_path).resolve()).lower() if sys.platform == 'win32' else str(Path(log_file_path).resolve())
    with _FILE_HANDLERS_LOCK:
        handler = _FILE_HANDLERS.get(key)
        if handler is None:
            handler = SafeTimedRotatingFileHandler(log_file_path, when='midnight')
            _FILE_HANDLERS[key] = handler
        return handler


def set_logger(logger_name, add_handler=False, base_logger=False, add_to_console=False, log_level=None,
               logger_base_name='pymodaq') -> logging.Logger:
    """defines a logger of a given name and eventually add an handler to it

    Parameters
    ----------
    logger_name: (str) the name of the logger (usually it is the module name as returned by get_module_name
    add_handler (bool) if True adds a (shared) TimedRotatingFileHandler to the logger instance (should be True if logger set from
                main app
    base_logger: (bool) specify if this is the parent logger (usually where one defines the handler)

    Returns
    -------
    logger: (logging.Logger) logger instance
    See Also
    --------
    get_module_name, logging.handlers.TimedRotatingFileHandler
    """
    if not base_logger:
        logger_name = f'{logger_base_name}.{logger_name}'

    logger = logging.getLogger(logger_name)

    if log_level is None:
        log_level = config('utils','general', 'debug_level')
        if not isinstance(log_level, list):
            print(
                f"{config('utils', 'general', 'debug_level')} is not a list, please delete your "
                f"actual pymodaq_utils configuration file to "
                f"reflect this new type")
            sys.exit(-1)
        log_level = log_level[0]
    logger.setLevel(log_level)
    if add_handler and not logger.handlers:
        try:
            log_path = get_set_config_dir('log', user=True)
            log_file_path = log_path.joinpath(f'{logger_base_name}.log')
            if not (log_file_path.exists() and log_file_path.is_file()):
                log_file_path.touch(mode=0o777)
            handler = get_file_handler(log_file_path)
        except Exception as e:
            print(e)
            print(f"Could not set up logger at {log_file_path}. Probably because of missing rights.")
            print("Falling back to console logging.")
            handler = logging.StreamHandler()

        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        logger.addHandler(handler)

        logging.captureWarnings(True)
        # only catch DeprecationWarning in DEBUG level
        if log_level == 'DEBUG':
            warnings.filterwarnings('default', category=DeprecationWarning)
        else:
            warnings.filterwarnings('ignore', category=DeprecationWarning)
        warnings_logger = logging.getLogger("py.warnings")
        warnings_logger.addHandler(handler)

    if add_to_console:
        console_handler = logging.StreamHandler()
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
    return logger


def get_base_logger(logger: logging.Logger) -> logging.Logger:
    while logger.name != 'pymodaq':
        logger = logger.parent
    return logger


def get_module_name(module__file__path):
    """from the full path of a module extract its name"""
    path = Path(module__file__path)
    return path.stem

