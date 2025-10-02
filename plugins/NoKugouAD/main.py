import logging
from threading import Thread
from time import sleep

import win32con as con
from comtypes import CoInitializeEx, COINIT_MULTITHREADED
from win32.lib import pywintypes
from win32gui import GetClassName

from base import *
from lib.kugou_finder import is_kugou_main_window, get_main_kugou_window, ProcType
from lib.window_watcher import WindowWatcher

name = "酷狗无广告"
logger = logging.getLogger("WinEnchantKitLogger_no_kugou_ad")


class Plugin(BasePlugin):
    wnd_watcher: WindowWatcher
    config = ModuleConfig(
        {
            "waiting_for_launch": FloatParam(5.0, "等待酷狗启动时间"),
            "find_timeout": FloatParam(10.0, "广告加载超时"),
            "shutdown_check_inv": FloatParam(10.0, "酷狗窗口检查间隔"),
        })

    def __init__(self):
        self.wnd_watcher = WindowWatcher(con.EVENT_OBJECT_CREATE, self.check_kugou)
        self.ready_windows = []
        self.check_thread = Thread(target=self.check_thread_func, daemon=True)
        self.kugou_hwnd = None

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

        def find_by_index(element: uiautomation.Control, index_list: list[int]):
            for index in index_list:
                element = element.GetChildren()[index]
            return element

        try:
            uiautomation.SetGlobalSearchTimeout(self.config["waiting_for_launch"])
            kugou_wnd = uiautomation.WindowControl(Name="酷狗音乐", searchDepth=1)
            sleep(self.config["waiting_for_launch"] / 2)
            logger.info("已找到酷狗音乐主窗口")
        except LookupError:
            return
        uiautomation.SetGlobalSearchTimeout(self.config["find_timeout"])

        upgrade_vip_ad = next(iter(kugou_wnd.GetChildren()))
        if upgrade_vip_ad.ControlType == 0xC370 and len(upgrade_vip_ad.GetChildren()) == 1 and len(
                upgrade_vip_ad.GetChildren()[0].GetChildren()) == 1:
            upgrade_vip_ad.Hide()
            logger.info("已关闭升级会员广告")

        try:
            personal_music = kugou_wnd.GroupControl(Name="私人专属好歌")
            personal_music = personal_music.GetParentControl().GetParentControl().GetParentControl()
            music_groups = find_by_index(personal_music, [1, 0, 0])
            recommend_ad = music_groups.GetChildren()[-1]
            if len(recommend_ad.GetChildren()[0].GetChildren()) == 1:
                recommend_ad.Hide()
            logger.info("已关闭私人专属好歌广告")
        except LookupError:
            pass

        try:
            live_window = kugou_wnd.TextControl(Name="正在直播", searchDepth=4)
            live_window = live_window.GetParentControl().GetParentControl().GetParentControl()
            if live_window.ControlType == 0xC370 and live_window.FrameworkId == "Win32":
                live_window.Hide()
            logger.info("已关闭直播推荐弹窗")
        except LookupError:
            pass
        uiautomation.Logger.DeleteLog()

        Thread(target=self.kugou_daemon_func, daemon=True).start()

    def kugou_daemon_func(self):
        while True:
            sleep(self.config["shutdown_check_inv"])
            try:
                GetClassName(self.kugou_hwnd)
                continue
            except pywintypes.error:
                break
        logger.info("酷狗窗口已关闭, 继续开始监测")
        self.start()

    def update_config(self, old_config: dict[str, Any], new_config: dict[str, Any]):
        pass

    def stop(self):
        if self.wnd_watcher.thread.is_alive():
            self.wnd_watcher.stop()
        if self.check_thread.is_alive():
            self.check_thread.join()


if __name__ == "__main__":
    p = Plugin()
    p.start()
    p.check_thread.join()
    print("FINISH")
