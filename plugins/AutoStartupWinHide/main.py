import json
import logging
import re
import pywintypes
from copy import deepcopy
from dataclasses import dataclass
from threading import Thread, Event

import psutil
import win32con as con
import wx
# noinspection PyPackageRequirements
from win32 import win32gui
from win32process import GetWindowThreadProcessId

from base import *
from window_watcher import WindowWatcher

name = "自启应用隐藏"
logger = logging.getLogger("WinEnchantKitLogger_auto_startup_app_hide")

WIN_STYLE_MAP = {name for name in dir(con) if name.startswith("WS_")}
WIN_EX_STYLE_MAP = {name for name in dir(con) if name.startswith("WS_EX_")}


class HideWay(int):
    CLOSE = 0
    MINIMIZE = 1
    HIDE = 2


@dataclass
class HideInfo:
    rule_name: str
    title: str
    cls_name: str
    proc_name: str
    window_cnt: int
    use_re: bool = False
    style: list[str] | str = ""
    hide_way: int | None = HideWay.CLOSE
    action_dealy: float = 0.0
    enable_show_check: bool = False
    do_last_action: bool = False


def extract_window_style(hwnd: int) -> list[str]:
    style = win32gui.GetWindowLong(hwnd, con.GWL_STYLE)
    ex_style = win32gui.GetWindowLong(hwnd, con.GWL_EXSTYLE)
    result = []
    for t_name in WIN_STYLE_MAP:
        if style & getattr(con, t_name):
            result.append(t_name)
    for t_name in WIN_EX_STYLE_MAP:
        if ex_style & getattr(con, t_name):
            result.append(t_name)
    return result


class PluginConfig(ModuleConfigPlus):
    def __init__(self):
        super().__init__()
        self.watch_delay: FloatParam | float = FloatParam(0.0, "开始窗口创建监测 延时")
        self.watch_last: FloatParam | float = FloatParam(60, "窗口创建检测 持续时间")
        self.check_exist_wind: BoolParam | bool = BoolParam(True, "检查存在窗口")
        self.check_delay: FloatParam | float = FloatParam(0.0, "检查存在窗口 延时")
        self.hide_way: ChoiceParamPlus | HideWay = ChoiceParamPlus(HideWay.CLOSE,
                                                                   {
                                                                       HideWay.CLOSE: "0 - 关闭窗口",
                                                                       HideWay.MINIMIZE: "1 - 最小化窗口",
                                                                       HideWay.HIDE: "2 - 隐藏窗口"
                                                                   }, "隐藏方式")

        self.windows: TableParam | list[HideInfo] = TableParam \
                (
                [],
                "窗口 (悬浮/点击 查看提示)",
                [str, str, str, str, int, bool, str, int, float, bool, bool],
                [("规则名", 110), ("标题", 150), ("类名", 170), ("进程名", 120), ("次数", 37), ("启用正则", 60),
                 ("窗口样式", 120), ("隐藏方式", 60), ("操作延迟", 60), ("显示检测", 90), ("仅执行最后操作", 60)],
                ("规则114514", "", "", "", "1", "F", "WS_MINIMIZEBOX|WS_VISIBLE", "", "0.0", "F", "F"),
                {
                    "哔哩哔哩": [
                        "哔哩哔哩",
                        "",
                        "Chrome_WidgetWin_1",
                        "哔哩哔哩.exe",
                        1,
                        "F",
                        "WS_MINIMIZEBOX|WS_VISIBLE",
                        HideWay.CLOSE
                    ],
                    "腾讯元宝": [
                        "腾讯元宝",
                        "腾讯元宝",
                        "Tauri Window",
                        "yuanbao.exe",
                        1,
                        "F",
                        "WS_BORDER|WS_EX_APPWINDOW",
                        HideWay.HIDE,
                        0.0,
                        "T"
                    ],
                    "QQ-NT": [
                        "QQ-NT",
                        "QQ",
                        "Chrome_WidgetWin_1",
                        "QQ.exe",
                        2,
                        "F",
                        "WS_CAPTION|WS_EX_LAYOUTRTL|WS_VISIBLE|WS_EX_APPWINDOW",
                        HideWay.HIDE,
                        2.0,
                        "F",
                        "T"
                    ],
                }
            )
        self.windows.help_string = "\n".join([
            "右键列表可以添加预定义规则",
            "[标题/类名/进程名] 需设置其中的任何一项, 可以只使用名称的一部分, 如果不设置则忽略对应项的信息检测",
            "[次数] 进行窗口检测的次数, 用完之后将不会再执行操作, -1为无限次",
            "[启用正则] 对[标题/类名/进程名]启用正则表达式匹配, 启用需设为T, 请注意特殊符 (例 -> .)",
            "[窗口样式] 目标窗口需包含定义的窗口样式, 支持拓展样式 (WS_EX_)",
            "[隐藏方式] 按照HideWay的顺序从0开始 (例: 隐藏窗口->2), 空则使用全局设置",
            "[操作延迟] Neko: 要等一会再进行操作喵?",
            "[启用显示检测] 使本条规则监测窗口的显示而不是创建",
            "[仅执行最后操作] 仅在剩余次数等于0时操作一次"
        ])
        self.import_rules: ButtonParam = ButtonParam(desc="导入规则")
        self.export_rules: ButtonParam = ButtonParam(desc="导出规则")
        self.debug_output: BoolParam | bool = BoolParam(False, "调试输出")
        self.debug_exist_output: BoolParam | bool = BoolParam(False, "调试输出现有窗口信息")

        self.add_hook("windows", self.wnd_data_hook)
        self.saved_windows = None

    def wnd_data_hook(self, data: list[tuple]):
        self.saved_windows = deepcopy(data)
        windows = []
        for x in data:
            info = HideInfo(*x)
            info.style = info.style.split("|")
            windows.append(info)
        return windows

    def restore_wnd_data(self):
        if self.saved_windows:
            self.wnd_data_hook(self.saved_windows)
            self.saved_windows = None


class Plugin(BasePlugin):
    def __init__(self):
        self.window_cnt = 0
        self.config = PluginConfig()
        self.config.import_rules.handler = self.import_rules
        self.config.export_rules.handler = self.export_rules
        self.config.load()
        self.stop_flag = Event()
        self.create_watcher = WindowWatcher(con.EVENT_OBJECT_CREATE, self.parse_create_window)  # 监测创建窗口
        self.show_watcher = WindowWatcher(con.EVENT_OBJECT_SHOW, self.parse_show_window)  # 监测显示窗口
        self.watcher_thread = Thread(target=self.watcher_thread_func, daemon=True)
        self.check_thread = Thread(target=self.check_thread_func, daemon=True)

    def import_rules(self):
        dialog = wx.FileDialog(wx.GetActiveWindow(), "选择规则文件", "", "", "*.json",
                               wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        if dialog.ShowModal() != wx.ID_OK:
            return
        with open(dialog.GetPath(), "r", encoding="utf-8") as f:
            data = json.load(f)
        self.config["windows"].extend(data)
        self.config.params["windows"].update_handler(self.config["windows"])

    def export_rules(self):
        rules = self.config["windows"]
        dialog = wx.FileDialog(wx.GetActiveWindow(), "导出规则", defaultFile="开机自启隐藏规则.json", wildcard="*.json",
                               style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT)
        if dialog.ShowModal() == wx.ID_OK:
            content = json.dumps(rules, ensure_ascii=False, indent=4)
            with open(dialog.GetPath(), "w", encoding="utf-8") as f:
                f.write(content)

    def start(self):
        logger.info("插件启动")
        self.window_cnt = 0
        self.config.restore_wnd_data()

        self.watcher_thread = Thread(target=self.watcher_thread_func, daemon=True)
        self.check_thread = Thread(target=self.check_thread_func, daemon=True)
        self.stop_flag.clear()
        self.watcher_thread.start()
        if self.config.check_exist_wind:
            self.check_thread.start()

    def watcher_thread_func(self):
        self.stop_flag.wait(timeout=self.config.watch_delay)
        if self.stop_flag.is_set():
            return
        logger.info("开始监测窗口创建")
        self.create_watcher.start()
        self.show_watcher.start()
        self.stop_flag.wait(timeout=self.config.watch_last)
        self.create_watcher.stop()
        self.show_watcher.stop()
        logger.info(f"结束监测窗口创建, 共监测到{self.window_cnt}次窗口创建")

    def check_thread_func(self):
        self.stop_flag.wait(timeout=self.config.check_delay)
        if self.stop_flag.is_set():
            return
        windows = []
        win32gui.EnumWindows(lambda h, _: windows.append(h), None)
        cnt = 0
        for hwnd in windows:
            if not win32gui.IsWindowVisible(hwnd):
                continue
            cnt += 1
            self.parse_create_window(hwnd, is_static_check=True)
        logger.info(f"完成现有窗口检测, 共检测{cnt}个可见窗口")

    def parse_show_window(self, hwnd: int):
        self.parse_create_window(hwnd, in_show_handler=True)

    def update_config(self, old_config: dict[str, Any], new_config: dict[str, Any]):
        super().update_config(old_config, new_config)
        if self.enable:
            self.config.saved_windows = None
            self.stop()
            self.start()

    def parse_create_window(self, hwnd: int, is_static_check: bool = False, in_show_handler: bool = False):
        try:
            if win32gui.GetParent(hwnd) != 0:
                return
            title = win32gui.GetWindowText(hwnd)
            cls_name = win32gui.GetClassName(hwnd)
            proc_pid = GetWindowThreadProcessId(hwnd)[1]
            proc_name = psutil.Process(proc_pid).name()
        except pywintypes.error:
            return

        debug_func = logger.debug if self.config.debug_output else lambda _: None

        if not is_static_check or self.config.debug_exist_output:
            self.window_cnt += 1
            key_word = "静态检测" if is_static_check else ('窗口显示' if in_show_handler else '窗口创建')
            debug_func(f"{key_word}: {hwnd} -> {title}|{cls_name}|{proc_name}")
        for info in self.config.windows:
            assert isinstance(info, HideInfo)
            if not (info.title or info.cls_name or info.proc_name):
                continue
            if info.enable_show_check and not in_show_handler:  # 如果设置了T, 而且不是在显示窗口处理过程中就跳过
                continue
            elif not info.enable_show_check and in_show_handler:  # 如果设置了F, 而且是在显示窗口处理过程中就跳过
                continue

            match_func = re.match if info.use_re else lambda source, current: source in current
            if info.title and not match_func(info.title, title):
                continue
            if info.cls_name and not match_func(info.cls_name, cls_name):
                continue
            if info.proc_name and not match_func(info.proc_name, proc_name):
                continue

            window_styles = extract_window_style(hwnd)
            flag = False
            debug_func(f"要求样式: {info.style} -> 窗口样式: {window_styles}")
            for style in info.style:
                if style not in window_styles:
                    flag = True
                    break
            if flag:
                continue

            if info.window_cnt == 0:
                debug_func(f"剩余次数不足")
                continue
            info.window_cnt -= 1
            debug_func(f"窗口规则剩余使用次数： {info.window_cnt}")
            if info.do_last_action and info.window_cnt != 0:
                continue
            hide_way = int(info.hide_way) if info.hide_way else self.config.hide_way
            if info.action_dealy == 0:
                self.do_action_window(hwnd, hide_way)
            else:
                debug_func(f"执行窗口修改 [{hwnd}], 延时{info.action_dealy}s")
                wx.CallAfter(wx.CallLater, int(info.action_dealy * 1000), self.do_action_window, hwnd, hide_way)
        return

    @staticmethod
    def do_action_window(hwnd: int, hide_way: HideWay):
        title = win32gui.GetWindowText(hwnd)
        cls_name = win32gui.GetClassName(hwnd)
        proc_pid = GetWindowThreadProcessId(hwnd)[1]
        proc_name = psutil.Process(proc_pid).name()
        logger.info(f"执行窗口修改 {hwnd} -> {title}|{cls_name}|{proc_name}")
        if hide_way == HideWay.CLOSE:
            win32gui.PostMessage(hwnd, con.WM_CLOSE)
        elif hide_way == HideWay.MINIMIZE:
            win32gui.ShowWindow(hwnd, con.SW_MINIMIZE)
        elif hide_way == HideWay.HIDE:
            win32gui.ShowWindow(hwnd, con.SW_HIDE)

    def stop(self):
        self.stop_flag.set()
        self.watcher_thread.join()
        self.check_thread.join()
        self.stop_flag.clear()
