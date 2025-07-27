import io
import logging
import sys
from datetime import datetime
from os import makedirs
from os.path import expandvars


class AnsiColorCodes:
    ITALIC = "\033[3m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BOLD_RED = "\033[1;91m"
    RESET = "\033[0m"


NO_TIME_FMT = "%(module)s.py:%(lineno)d [%(levelname)s]"
# noinspection SpellCheckingInspection
TIME_FMT = "[%(asctime)s] %(module)s:%(lineno)d [%(levelname)s]"
GLOBAL_LEVEL = logging.DEBUG
USE_COLOR = True
#logging.basicConfig(encoding="utf-8")

COLOR_MAP = {
    logging.DEBUG: AnsiColorCodes.ITALIC,
    logging.INFO: AnsiColorCodes.GREEN,
    logging.WARNING: AnsiColorCodes.YELLOW,
    logging.ERROR: AnsiColorCodes.RED,
    logging.CRITICAL: AnsiColorCodes.BOLD_RED,
}
COLOR_MAP.setdefault(0, AnsiColorCodes.RESET)


class ColoredFormatter(logging.Formatter):

    def __init__(self):
        super().__init__()
        self.formatter = logging.Formatter(NO_TIME_FMT, datefmt="%y-%m-%d %H:%M:%S")

    def update_formatter(self, use_time: bool = True):
        if use_time:
            self.formatter = logging.Formatter(TIME_FMT, datefmt="%y-%m-%d %H:%M:%S")
        else:
            self.formatter = logging.Formatter(NO_TIME_FMT, datefmt="%y-%m-%d %H:%M:%S")

    def format(self, record):
        if USE_COLOR:
            return f"{COLOR_MAP.get(record.levelno)}{self.formatter.format(record)} : {record.message}{AnsiColorCodes.RESET}"
        else:
            return f"{self.formatter.format(record)} : {record.message}"


class TimedFormatter(logging.Formatter):
    def __init__(self):
        super().__init__()
        self.formatter = logging.Formatter(TIME_FMT)

    def format(self, record):
        return f"{self.formatter.format(record)} : {record.message}"


class PluginFormatter(ColoredFormatter):
    def __init__(self, name: str = None):
        super().__init__()
        self.name = name

    def format(self, record):
        if USE_COLOR:
            return f"{COLOR_MAP.get(record.levelno)}{self.formatter.format(record)} : [{self.name}] {record.message}{AnsiColorCodes.RESET}"
        else:
            return f"{self.formatter.format(record)} : [{self.name}] {record.message}"


def get_plugin_logger(id_: str, name: str):
    plugin_logger = logging.getLogger(f"WinEnchantKitLogger_{id_}")
    plugin_logger.setLevel(GLOBAL_LEVEL)
    plugin_console_handler = logging.StreamHandler(io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8'))
    plugin_console_handler.setLevel(GLOBAL_LEVEL)
    plugin_console_handler.setFormatter(PluginFormatter(name))
    plugin_logger.addHandler(plugin_console_handler)
    plugin_logger.addHandler(time_rotating_file_handler)
    return plugin_logger


makedirs(expandvars('%APPDATA%/WinEnchantKit/logs'), exist_ok=True)
time_rotating_file_handler = logging.FileHandler(
    filename=expandvars(f"%APPDATA%/WinEnchantKit/logs/log_{datetime.now().strftime('%Y-%m-%d')}.log"))
time_rotating_file_handler.setLevel(logging.DEBUG)
time_rotating_file_handler.setFormatter(TimedFormatter())


logger = logging.getLogger("WinEnchantKitLogger")
logger.setLevel(GLOBAL_LEVEL)
console_handler = logging.StreamHandler(io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8'))
console_handler.setLevel(GLOBAL_LEVEL)
console_handler.setFormatter(ColoredFormatter())
logger.addHandler(console_handler)
logger.addHandler(time_rotating_file_handler)
