import logging
from threading import Event, Thread

import wx
from win32.lib import win32con
from win32gui import SetWindowLong, GetWindowLong

from base import *
from dwm import *
from kugou_finder import get_main_kugou_window, add_style

name = "酷狗美化"
logger = logging.getLogger("WinEnchantKitLogger_beautiful_kugou")


def right_corner_border_style(hwnd: int):
    # 边框
    style = GetWindowLong(hwnd, win32con.GWL_STYLE)
    new_style = add_style(style, win32con.WS_BORDER)
    SetWindowLong(hwnd, win32con.GWL_STYLE, new_style)

    # 圆角
    DwmSetWindowAttribute(
        hwnd,
        DWMWINDOWATTRIBUTE.DWMWA_WINDOW_CORNER_PREFERENCE,
        ctypes.byref(ctypes.c_int(DWM_WINDOW_CORNER_PREFERENCE.DWMWCP_ROUND)),
        ctypes.sizeof(ctypes.c_int),
    )


def blur_behind(hwnd: int, color: tuple[int, int, int, int], enable: bool) -> str | None:
    back_type = DWM_SYSTEMBACKDROP_TYPE.DWMSBT_TRANSIENTWINDOW if enable else DWM_SYSTEMBACKDROP_TYPE.DWMSBT_NONE
    accent_state = ACCENT_STATE.ACCENT_ENABLE_BLURBEHIND if enable else ACCENT_STATE.ACCENT_DISABLED
    color_hex = "".join(map(lambda x: hex(x)[2:].zfill(2), color[:3]))
    if len(color_hex) != 6:
        return "颜色格式错误"

    # 亚克力背景
    DwmSetWindowAttribute(
        hwnd,
        DWMWINDOWATTRIBUTE.DWMWA_SYSTEMBACKDROP_TYPE,
        ctypes.byref(ctypes.c_int(back_type)),
        ctypes.sizeof(ctypes.c_int),
    )

    # 模糊透明效果
    bb = DWM_BLURBEHIND(
        dwFlags=DWM_BB_ENABLE,
        fEnable=enable,
        hRgnBlur=0,
        fTransitionOnMaximized=False,
    )
    DwmEnableBlurBehindWindow(hwnd, ctypes.byref(bb))
    accent = ACCENT_POLICY(AccentState=accent_state,
                           GradientColor=(color[3] << 24) | (int(color_hex, 16) & 0xFFFFFF))
    attrib = WINDOWCOMPOSITIONATTRIBDATA(
        Attrib=WINDOWCOMPOSITIONATTRIB.WCA_ACCENT_POLICY,
        pvData=ctypes.byref(accent),
        cbData=ctypes.sizeof(accent),
    )
    SetWindowCompositionAttribute(hwnd, ctypes.byref(attrib))

    # 拓展标题栏效果至客户区
    margins = MARGINS(-1, -1, -1, -1) if enable else MARGINS(0, 0, 0, 0)
    DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins))


class Plugin(BasePlugin):
    config = ModuleConfig(
        {
            "inv_non_launched": FloatParam(2.0, "检查窗口的间隔时间: "),
            "inv_launched": FloatParam(10.0, "酷狗启动后的检查间隔时间："),
            "blur_behind": BoolParam(True, "是否启用模糊背景："),
            "blur_color": ColorParam((0x2B, 0x2B, 0x2B), "模糊背景颜色 (不起作用)："),
            "blur_alpha": IntParam(152, "模糊背景透明度 (不起作用)：")
        }
    )
    enable = False
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
        logger.info(f"[{name}]: 插件线程已启动")
        first_flag = True
        while True:
            wait_time = self.config["inv_launched"] if kugou_launched else self.config["inv_non_launched"]
            try:
                if first_flag:
                    first_flag = False
                else:
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
            logger.info(f"[{name}]: 酷狗窗口已找到: {kugou_hwnd}, 修改窗口")
            self.update_window(kugou_hwnd)
            kugou_launched = True
        logger.info(f"[{name}]: 插件线程已退出")

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

    def update_window(self, hwnd: int):
        enable = self.config["blur_behind"]
        # noinspection PyTypeChecker
        color: tuple[int, int, int, int] = tuple(self.config["blur_color"]) + (self.config["blur_alpha"],)
        right_corner_border_style(hwnd)
        msg = blur_behind(hwnd, color, enable)
        if msg is not None:
            wx.MessageBox(msg, "错误")
