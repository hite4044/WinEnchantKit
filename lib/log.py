import logging


class AnsiColorCodes:
    ITALIC = "\033[3m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BOLD_RED = "\033[1;91m"
    RESET = "\033[0m"


NO_TIME_FMT = "%(module)s.py:%(lineno)d [%(levelname)s]"
# noinspection SpellCheckingInspection
TIME_FMT = "[%(asctime)s.%(msecs)03d] %(module)s:%(lineno)d [%(levelname)s]"
GLOBAL_LEVEL = logging.DEBUG

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
        self.formatter = logging.Formatter(NO_TIME_FMT)

    def update_formatter(self, use_time: bool = True):
        if use_time:
            self.formatter = logging.Formatter(TIME_FMT, datefmt="%y-%m-%d %H:%M:%S")
        else:
            self.formatter = logging.Formatter(NO_TIME_FMT)

    def format(self, record):
        return f"{COLOR_MAP.get(record.levelno)}{self.formatter.format(record)} : {record.message}{AnsiColorCodes.RESET}"


class PluginFormatter(ColoredFormatter):
    def __init__(self, name: str = None):
        super().__init__()
        self.name = name

    def format(self, record):
        return f"{COLOR_MAP.get(record.levelno)}{self.formatter.format(record)} : [{self.name}] {record.message}{AnsiColorCodes.RESET}"


def get_plugin_logger(id_: str, name: str):
    plugin_logger = logging.getLogger(f"WinEnchantKitLogger_{id_}")
    plugin_logger.setLevel(GLOBAL_LEVEL)
    plugin_console_handler = logging.StreamHandler()
    plugin_console_handler.setLevel(GLOBAL_LEVEL)
    plugin_console_handler.setFormatter(PluginFormatter(name))
    plugin_logger.addHandler(plugin_console_handler)
    return plugin_logger


logger = logging.getLogger("WinEnchantKitLogger")
logger.setLevel(GLOBAL_LEVEL)
console_handler = logging.StreamHandler()
console_handler.setLevel(GLOBAL_LEVEL)
console_handler.setFormatter(ColoredFormatter())
logger.addHandler(console_handler)
