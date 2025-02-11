import ctypes
import logging
from threading import Event, Thread

from win32.lib import win32con
from win32gui import SetWindowLong, GetWindowLong

from base import *
from kugou_finder import get_main_kugou_window, add_style

name = "酷狗圆角"
logger = logging.getLogger("WinEnchantKitLogger")

## Function Define ##
DwmSetWindowAttribute = ctypes.windll.dwmapi.DwmSetWindowAttribute
DwmSetWindowAttribute.argtypes = [
    ctypes.c_int,
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_long),
    ctypes.c_int,
]
DwmSetWindowAttribute.restype = ctypes.c_int


# noinspection PyPep8Naming,SpellCheckingInspection
class DWM_WINDOW_CORNER_PREFERENCE:
    DWMWCP_DEFAULT = 0
    DWMWCP_DONOTROUND = 1
    DWMWCP_ROUND = 2
    DWMWCP_ROUNDSMALL = 3


# noinspection PyPep8Naming,SpellCheckingInspection
class DWMWINDOWATTRIBUTE:
    DWMWA_WINDOW_CORNER_PREFERENCE = 33




class Plugin(BasePlugin):
    config = ModuleConfig(
        {
            "inv_non_launched": FloatParam(2.0, "检查窗口的间隔时间: "),
            "inv_launched": FloatParam(10.0, "酷狗启动后的检查间隔时间："),
        }
    )
    enable = True
    run_flag = False
    thread: Thread | None = None
    run_event = Event()

    def start(self):
        self.run_flag = True
        self.run_event.clear()
        self.thread = Thread(target=self.thread_func, daemon=True)
        self.thread.start()
        self.enable = True

    def thread_func(self):
        kugou_launched = False
        logger.info(f"[{name}]: " + "插件线程已启动")
        while True:
            wait_time = self.config["inv_launched"] if kugou_launched else self.config["inv_non_launched"]
            try:
                self.run_event.wait(timeout=wait_time)
            except TimeoutError:
                pass
            if not self.run_flag:
                break

            kugou_hwnd = get_main_kugou_window()
            if kugou_hwnd is None:
                kugou_launched = False
                continue
            if kugou_launched:
                continue
            logger.info(f"[{name}]: " + f"酷狗窗口已找到: {kugou_hwnd}, 修改窗口样式")
            style = GetWindowLong(kugou_hwnd, win32con.GWL_STYLE)
            new_style = add_style(style, win32con.WS_BORDER)
            SetWindowLong(kugou_hwnd, win32con.GWL_STYLE, new_style)
            DwmSetWindowAttribute(
                kugou_hwnd,
                DWMWINDOWATTRIBUTE.DWMWA_WINDOW_CORNER_PREFERENCE,
                ctypes.byref(ctypes.c_int(DWM_WINDOW_CORNER_PREFERENCE.DWMWCP_ROUND)),
                ctypes.sizeof(ctypes.c_int),
            )
            kugou_launched = True
        logger.info(f"[{name}]: " + "插件线程已退出")

    def update_config(self, _, new_config: dict[str, Any]):
        self.config.load_values(new_config)
        if self.enable:
            self.stop()
            self.start()

    def stop(self):
        assert isinstance(self.thread, Thread)
        self.run_flag = False
        self.run_event.set()
        self.thread.join()
        self.enable = False
