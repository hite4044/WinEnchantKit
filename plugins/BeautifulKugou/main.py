import logging
from threading import Event, Thread

import wx
from win32.lib import win32con
from win32gui import SetWindowLong, GetWindowLong, GetWindowText

from base import *
from dwm import *
from kugou_finder import get_main_kugou_window, add_style

name = "酷狗美化"
logger = logging.getLogger("WinEnchantKitLogger_beautiful_kugou")


def right_corner_border_style(hwnd: int, enable_round_corner: bool, corner_type: int):
    # 边框
    style = GetWindowLong(hwnd, win32con.GWL_STYLE)
    new_style = add_style(style, win32con.WS_BORDER)
    SetWindowLong(hwnd, win32con.GWL_STYLE, new_style)

    if enable_round_corner:
        # 圆角
        DwmSetWindowAttribute(
            hwnd,
            DWMWINDOWATTRIBUTE.DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(ctypes.c_int(corner_type)),
            ctypes.sizeof(ctypes.c_int),
        )


def blur_behind(hwnd: int, color: tuple[int, int, int, int],
                cfg: dict[str, Any]) -> str | None:
    set_back_type = cfg["set_back_type"]
    enable_blur_behind = cfg["enable_blur_behind"]

    back_type = cfg["back_type"]
    accent_state = cfg["accent_state"]
    color_hex = "".join(map(lambda x: hex(x)[2:].zfill(2), color[-2::-1]))
    if len(color_hex) != 6:
        return "颜色格式错误" + color_hex

    if set_back_type:
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
        fEnable=enable_blur_behind,
        hRgnBlur=0,
        fTransitionOnMaximized=False,
    )
    DwmEnableBlurBehindWindow(hwnd, ctypes.byref(bb))

    if cfg["enable_set_composition"]:
        accent = ACCENT_POLICY(AccentState=accent_state,
                               GradientColor=(color[3] << 24) | (int(color_hex, 16) & 0xFFFFFF))
        attrib = WINDOWCOMPOSITIONATTRIBDATA(
            Attrib=WINDOWCOMPOSITIONATTRIB.WCA_ACCENT_POLICY,
            pvData=ctypes.byref(accent),
            cbData=ctypes.sizeof(accent),
        )
        SetWindowCompositionAttribute(hwnd, ctypes.byref(attrib))

    # 拓展标题栏效果至客户区
    margins = MARGINS(-1, -1, -1, -1) if enable_blur_behind else MARGINS(0, 0, 0, 0)
    DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins))
    return None


class Plugin(BasePlugin):
    config = ModuleConfig(
        {
            "tip": TipParam("有些效果开启又关闭后需重启才能恢复"),
            "inv_non_launched": FloatParam(2.0, "检查窗口的间隔时间"),
            "inv_launched": FloatParam(10.0, "酷狗启动后的检查间隔时间"),

            "enable_set_composition": BoolParam(False, "设置窗口效果 (Win 10 16299+)"),
            "accent_state": ChoiceParamPlus(ACCENT_STATE.ACCENT_ENABLE_ACRYLICBLURBEHIND,
                                            {
                                                ACCENT_STATE.ACCENT_DISABLED: "禁用",
                                                ACCENT_STATE.ACCENT_ENABLE_ACRYLICBLURBEHIND: "模糊 (带颜色)",
                                                ACCENT_STATE.ACCENT_ENABLE_BLURBEHIND: "模糊 (不带颜色)",
                                                ACCENT_STATE.ACCENT_ENABLE_TRANSPARENTGRADIENT: "透明 (带颜色)",
                                                ACCENT_STATE.ACCENT_ENABLE_HOSTBACKDROP: "透明 (不带颜色)",
                                                ACCENT_STATE.ACCENT_ENABLE_GRADIENT: "仅无透明度颜色",
                                            }, "模糊效果"),
            "accent_color": ColorParam((0x2B, 0x2B, 0x2B), "模糊背景颜色"),
            "accent_alpha": IntParam(152, "模糊背景透明度"),

            "enable_round_corner": BoolParam(False, "启用窗口圆角 (Win 11 22000+)"),
            "corner_type": ChoiceParamPlus(DWM_WINDOW_CORNER_PREFERENCE.DWMWCP_ROUND,
                                           {
                                               DWM_WINDOW_CORNER_PREFERENCE.DWMWCP_DEFAULT: "默认",
                                               DWM_WINDOW_CORNER_PREFERENCE.DWMWCP_ROUND: "圆角",
                                               DWM_WINDOW_CORNER_PREFERENCE.DWMWCP_ROUNDSMALL: "小圆角",
                                               DWM_WINDOW_CORNER_PREFERENCE.DWMWCP_DONOTROUND: "直角",
                                           }, "圆角类型"),

            "enable_blur_behind": BoolParam(False, "启用窗口背景模糊 (颜色加深)"),

            "set_back_type": BoolParam(False, "设置背景材质 (Win 11 22621+) (无效)"),
            "back_type": ChoiceParamPlus(DWM_SYSTEMBACKDROP_TYPE.DWMSBT_TRANSIENTWINDOW,
                                         {
                                             DWM_SYSTEMBACKDROP_TYPE.DWMSBT_NONE: "无",
                                             DWM_SYSTEMBACKDROP_TYPE.DWMSBT_MAINWINDOW: "Mica (桌面壁纸模糊)",
                                             DWM_SYSTEMBACKDROP_TYPE.DWMSBT_TRANSIENTWINDOW: "Acrylic (窗口模糊)",
                                             DWM_SYSTEMBACKDROP_TYPE.DWMSBT_TABBEDWINDOW: "Mica Alt (桌面壁纸模糊 (更深))"
                                         }, "背景材质"),
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
        logger.info(f"插件线程已启动")
        first_flag = True
        hwnd_cache = None
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
            if kugou_launched:
                try:
                    GetWindowText(hwnd_cache)
                    continue
                except OSError:
                    logger.info(f"窗口已关闭")
                    kugou_launched = False

            kugou_hwnd = get_main_kugou_window()
            if kugou_hwnd is None:
                kugou_launched = False
                continue
            if kugou_launched:
                continue
            hwnd_cache = kugou_hwnd
            logger.info(f"酷狗窗口已找到: {kugou_hwnd}, 修改窗口")
            self.update_window(kugou_hwnd)
            kugou_launched = True
        logger.info(f"插件线程已退出")

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
        # noinspection PyTypeChecker
        color: tuple[int, int, int, int] = tuple(self.config["accent_color"]) + (self.config["accent_alpha"],)
        right_corner_border_style(hwnd, self.config["enable_round_corner"], self.config["corner_type"])
        msg = blur_behind(hwnd, color, self.config)
        if msg is not None:
            wx.MessageBox(msg, "错误")
