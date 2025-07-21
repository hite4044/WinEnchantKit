import os
import sys
from os.path import expandvars

import wx

from gui.control_panel import ControlPanel

os.chdir(os.path.dirname(__file__))  # 进入当前目录
if "pythonw.exe" in sys.orig_argv[0] or "python.exe" not in sys.orig_argv[0]:
    output = open(expandvars("%TEMP%\wek_log_53453174.txt"), "a+", encoding="utf-8")
    sys.stdout = output
    sys.stderr = output

if __name__ == "__main__":
    app = wx.App()
    show_window = not (len(sys.argv) > 1 and sys.argv[-1] == "-startup")
    frame = ControlPanel(None, show_window)
    app.MainLoop()
