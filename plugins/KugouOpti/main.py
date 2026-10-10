import ctypes
import logging
from threading import Thread, Event
from time import sleep

import uiautomation as uiauto
import win32con as con
from uiautomation import WindowControl
from win32.lib import pywintypes
from win32gui import GetClassName

from base import *
from lib.kugou_finder import is_kugou_main_window, get_main_kugou_window, ProcType
from lib.window_watcher import WindowWatcher

name = "酷狗无广告"
logger = logging.getLogger("WinEnchantKitLogger_no_kugou_ad")

COINIT_MULTITHREADED = 0x0
combase = ctypes.WinDLL("combase")
CoInitializeEx = combase.CoInitializeEx
CoInitializeEx.argtypes = (ctypes.c_ulong,)
CoInitializeEx.restype = ctypes.c_long


class PluginConfig(ModuleConfigPlus):
    def __init__(self):
        super().__init__()
        self.kugou_launch_delay: float | FloatParam = FloatParam(5.0, "酷狗启动等待时间") \
            .set_help("在酷狗音乐打开后等待酷狗音乐完成启动的时间")
        self.shutdown_check_inv: float | FloatParam = FloatParam(10.0, "酷狗关闭检测间隔") \
            .set_help("检测酷狗音乐是否关闭的检测间隔")
        self.search_timeout: float | FloatParam = FloatParam(5.0, "元素搜索超时")
        self.worker_inv: float | FloatParam = FloatParam(5.0, "元素检测间隔")

        self.feed_fish_mode: bool | BoolParam = BoolParam(True, "隐藏养鱼模式入口按钮")


def find_by_index(element: uiauto.Control, index_list: list[int]):
    """根据编号寻找元素"""
    for index in index_list:
        element = element.GetChildren()[index]
    return element


TYPE_BUTTON = 0xC350


class Plugin(BasePlugin):
    wnd_watcher: WindowWatcher

    def __init__(self):
        self.config = PluginConfig()
        self.config.load()
        self.wnd_watcher = WindowWatcher(con.EVENT_OBJECT_CREATE, self.check_kugou)
        self.ready_windows = []
        self.check_thread = Thread(target=self.check_thread_func, daemon=True)
        self.kugou_daemon_thread = None
        self.kugou_hwnd = None
        self.stop_flag = Event()

    def start(self):
        kugou = get_main_kugou_window(ProcType.KUGOU)
        if kugou:
            self.kugou_hwnd = kugou
            self.check_thread = Thread(target=self.check_ad_func, daemon=True)
            self.check_thread.start()
            return

        self.ready_windows = []
        self.wnd_watcher.start()

    def check_kugou(self, hwnd: int):
        if not self.enable or self.ready_windows is None or GetClassName(hwnd) != "kugou_ui":
            return
        self.ready_windows.append(hwnd)
        if not self.check_thread.is_alive():
            self.check_thread = Thread(target=self.check_thread_func, daemon=True)
            self.check_thread.start()

    def check_thread_func(self):
        for hwnd in self.ready_windows:
            if is_kugou_main_window(hwnd):
                self.kugou_hwnd = hwnd
                break
        else:
            return
        self.ready_windows = None
        self.check_ad_func()

    def check_ad_func(self):
        import uiautomation
        CoInitializeEx(COINIT_MULTITHREADED)

        try:
            uiautomation.SetGlobalSearchTimeout(self.config.kugou_launch_delay)
            kugou_wnd = uiautomation.WindowControl(Name="酷狗音乐", searchDepth=1)
            sleep(self.config.kugou_launch_delay / 2)
            logger.info("已找到酷狗音乐主窗口")
        except LookupError:
            return
        if self.stop_flag.is_set():
            return
        uiautomation.SetGlobalSearchTimeout(self.config.search_timeout)
        self.worker(kugou_wnd)
        uiautomation.Logger.DeleteLog()

        self.kugou_daemon_thread = Thread(target=self.kugou_daemon_func, daemon=True)
        self.kugou_daemon_thread.start()

    def worker(self, kugou_wnd: WindowControl):
        logger.info("开始检测元素")
        while True:
            # 测试窗口与对象有效性
            try:
                GetClassName(self.kugou_hwnd)
            except pywintypes.error:
                break

            # 检测
            self.update_state(kugou_wnd)

            # 停止检测
            self.stop_flag.wait(timeout=self.config.worker_inv)
            if self.stop_flag.is_set():
                return

        logger.info("工作线程结束")

    def update_state(self, kugou_wnd: WindowControl):
        self.feed_fish_mode(kugou_wnd, self.config.feed_fish_mode)

    @staticmethod
    def filter_main_content(kugou_wnd: WindowControl):
        for child in kugou_wnd.GetChildren():
            if child.ControlType == 0xC369 and len(child.GetChildren()) > 2:  # 自定义
                return child
        return None

    def feed_fish_mode(self, kugou_wnd: WindowControl, hide: bool = True):
        if not self.check_is_in_player(kugou_wnd):
            logger.debug("不在播放器模式下")
            return
        fish_mode_btn = find_by_index(self.filter_main_content(kugou_wnd), [2, 0, 1])
        print(1)
        if True: # fish_mode_btn.Name == "养鱼模式" and fish_mode_btn.ControlType == TYPE_BUTTON
            print(3)
            result = fish_mode_btn.Hide() if hide else fish_mode_btn.Show()
            print(result)
            logger.debug("成功隐藏养鱼模式按钮")
        print(2)

    @staticmethod
    def check_is_in_player(kugou_wnd: WindowControl):
        for child in kugou_wnd.GetChildren():
            if child.ControlType == 0xC369:  # 自定义
                for child2 in child.GetChildren():
                    if child2.Name == "收起歌曲详情页" and child2.ControlType == TYPE_BUTTON:  # 按钮
                        return True
        return False

    def kugou_daemon_func(self):
        while True:
            self.stop_flag.wait(timeout=self.config["shutdown_check_inv"])
            if self.stop_flag.is_set():
                return
            try:
                GetClassName(self.kugou_hwnd)
                continue
            except pywintypes.error:
                break
        logger.info("酷狗窗口已关闭, 继续开始监测")
        self.start()

    def stop(self):
        if self.wnd_watcher.thread:
            if self.wnd_watcher.thread.is_alive():
                self.wnd_watcher.stop()
        self.stop_flag.set()
        if self.kugou_daemon_thread and self.kugou_daemon_thread.is_alive():
            self.kugou_daemon_thread.join()
        if self.check_thread.is_alive():
            self.check_thread.join()
        self.stop_flag.clear()


if __name__ == "__main__":
    p = Plugin()
    p.start()
    p.check_thread.join()
    print("FINISH")
