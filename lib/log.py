import io
import logging
import sys
import tempfile
from datetime import datetime
from os import makedirs
from os.path import abspath, dirname, expandvars, join


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


def _warn(message: str) -> None:
    """日志系统自身出问题时用标准错误提示，避免再次抛出异常。"""
    if sys.stderr is not None:
        try:
            print(f"[log] {message}", file=sys.stderr)
        except Exception:  # noqa: BLE001  日志失败不应影响主程序
            pass


def _create_log_file_handler() -> logging.Handler:
    """创建按日期命名的日志文件处理器。

    %APPDATA% 有可能不可写（例如目录被沙箱或安全软件标记为低完整性），
    这时依次回退到临时目录和程序目录，避免日志初始化失败导致整个程序无法启动。
    """
    filename = f"log_{datetime.now().strftime('%Y-%m-%d')}.log"
    candidates = [
        expandvars(f"%APPDATA%/WinEnchantKit/logs/{filename}"),
        join(tempfile.gettempdir(), "WinEnchantKit", "logs", filename),
        join(dirname(dirname(abspath(__file__))), "logs", filename),
    ]
    for path in candidates:
        try:
            makedirs(dirname(path), exist_ok=True)
            return logging.FileHandler(filename=path)
        except OSError as e:
            _warn(f"日志文件不可写，改用下一个位置：{path}（{e}）")
    _warn("所有日志位置均不可写，本次运行仅输出到控制台。")
    return logging.NullHandler()


time_rotating_file_handler = _create_log_file_handler()
time_rotating_file_handler.setLevel(logging.DEBUG)
time_rotating_file_handler.setFormatter(TimedFormatter())


logger = logging.getLogger("WinEnchantKitLogger")
logger.setLevel(GLOBAL_LEVEL)
console_handler = logging.StreamHandler(io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8'))
console_handler.setLevel(GLOBAL_LEVEL)
console_handler.setFormatter(ColoredFormatter())
logger.addHandler(console_handler)
logger.addHandler(time_rotating_file_handler)
