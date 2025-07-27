import faulthandler
import io
import os
import sys
import typing
from datetime import datetime
from os.path import expandvars
from time import sleep

faulthandler.is_enabled()

os.chdir(os.path.dirname(__file__))  # 进入当前目录
sys.path.append(os.path.dirname(__file__))  # 添加模块导入路径
if sys.orig_argv[0].endswith("pythonw.exe"):  # 当使用pythonw.exe启动时
    os.makedirs(expandvars("%APPDATA%/WinEnchantKit/national_logs"), exist_ok=True)
    output_file = open(
        expandvars(f"%APPDATA%/WinEnchantKit/national_logs/log_{datetime.now().strftime('%Y-%m-%d')}.log"),
        "a+", encoding="utf-8"
    )
    output_file.write(f"\n\nWinEnchantKit Starting... ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})\n")
    sys.stdout = output_file
    sys.stderr = output_file

    import lib.log as log

    log.USE_COLOR = False
    log.NO_TIME_FMT = log.TIME_FMT
    t = typing.cast(log.ColoredFormatter, log.console_handler.formatter)
    t.update_formatter(use_time=True)
    log.logger.info("")
    log.logger.info("")
    log.logger.info(f"WinEnchantKit 启动... ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")

import wx

from gui.control_panel import ControlPanel

if __name__ == "__main__":
    app = wx.App()
    show_window = not (len(sys.argv) > 1 and "-startup" in sys.argv)
    frame = ControlPanel(None, show_window)
    app.MainLoop()
