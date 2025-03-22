import asyncio
import atexit
import json
import logging
from dataclasses import dataclass
from importlib import import_module
from os import listdir
from os.path import join, isfile, basename, exists
from subprocess import Popen, PIPE
from sys import executable
from sys import path
from sys import argv
from threading import Thread
from time import sleep
from typing import Callable

import pystray
import wx
from PIL import Image

from base import *
from gui.config import ConfigEditor
from lib.log import logger, get_plugin_logger


async def get_packages():
    packages = []
    proc = Popen([executable, "-m", "pip", "list"], shell=True, stdout=PIPE)
    proc.wait()
    for i, line in enumerate(proc.stdout.readlines()):
        line = line.decode("utf-8").rstrip("\r\n")
        if i > 2:
            packages.append(line.split(" ")[0])
    logger.info(f"找到{len(packages)}个包: " + ", ".join(packages))
    return packages


class PluginState(Enum):
    STOPPED = 0
    STOPPING = 1
    STARTING = 2
    RUNNING = 3


@dataclass
class PluginInfo:
    id: str
    info: dict[str, Any]
    main_class: BasePlugin
    state: PluginState
    line: int
    logger: logging.Logger


class ControlPanel(wx.Frame):
    def __init__(self, parent: wx.Window | None):
        super().__init__(parent, size=(850, 450), title="WinEnchantKit管理面板")
        self.packages = get_packages()
        self.plugins: dict[str, PluginInfo] = {}
        self.auto_launch_plugins: list[str] = []
        self.sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.plugins_lc = wx.ListCtrl(self, style=wx.LC_REPORT)
        self.plugins_lc.InsertColumn(0, "插件ID")
        self.plugins_lc.InsertColumn(1, "插件名", width=160)
        self.plugins_lc.InsertColumn(2, "状态", width=60)
        self.plugins_lc.InsertColumn(3, "版本", width=60)
        self.plugins_lc.InsertColumn(4, "描述", width=450)
        self.plugins_lc.SetColumnWidth(0, 0)
        self.button_panel = wx.Panel(self)
        self.button_panel.sizer = wx.BoxSizer(wx.VERTICAL)
        self.start_btn = wx.Button(self.button_panel, label="启动")
        self.stop_btn = wx.Button(self.button_panel, label="停止")
        self.config_btn = wx.Button(self.button_panel, label="配置")
        self.auto_launch_cb = wx.CheckBox(self.button_panel, label="自动启动")
        self.exit_btn = wx.Button(self.button_panel, label="退出程序")
        self.button_panel.sizer.Add(self.start_btn)
        self.button_panel.sizer.Add(self.stop_btn)
        self.button_panel.sizer.Add(self.config_btn)
        self.button_panel.sizer.Add(self.auto_launch_cb)
        self.button_panel.sizer.AddStretchSpacer()
        self.button_panel.sizer.Add(self.exit_btn)
        self.button_panel.SetSizer(self.button_panel.sizer)
        self.plugins_lc.SetMinSize(wx.Size(1920, 1080))
        self.sizer.Add(self.plugins_lc, flag=wx.EXPAND, proportion=1)
        self.sizer.Add(self.button_panel, flag=wx.EXPAND, proportion=0)
        self.SetSizer(self.sizer)
        self.start_btn.Bind(wx.EVT_BUTTON, self.start_plugin_gui)
        self.stop_btn.Bind(wx.EVT_BUTTON, self.stop_plugin_gui)
        self.config_btn.Bind(wx.EVT_BUTTON, self.config_plugin_gui)
        self.auto_launch_cb.Bind(wx.EVT_CHECKBOX, self.auto_launch_gui)
        self.exit_btn.Bind(wx.EVT_BUTTON, self.on_exit_gui)
        self.plugins_lc.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_item_selected)
        self.Bind(wx.EVT_CLOSE, self.on_close_window)
        self.start_btn.Disable()
        self.stop_btn.Disable()
        self.has_exited = False
        self.stray_icon: pystray.Icon = None
        self.stray_icon_image = Image.open("assets/icon2.png")
        self.window_icon = wx.Icon()
        self.window_icon.LoadFile("assets/icon2.png", wx.BITMAP_TYPE_PNG)
        self.SetIcon(self.window_icon)
        self.create_stray_icon()
        self.Show()
        self.read_config()
        self.load_all_plugins_gui()

    def load_all_plugins_gui(self):
        Thread(target=self.load_all_plugins, daemon=True).start()

    def load_all_plugins(self):
        logger.info("加载插件中...")
        self.packages = asyncio.run(self.packages)
        for dir_name in listdir("plugins"):
            logger.info(f"加载插件: [{dir_name}]")
            plugin_dir = join("plugins", dir_name)
            if not self.load_plugin(plugin_dir):
                logger.error(f"加载插件失败: [{dir_name}]")
        logger.info("加载插件完成")
        Thread(target=self.auto_start_plugins, daemon=True).start()

    def auto_start_plugins(self):
        sleep(5)
        for plugin_id in self.auto_launch_plugins:
            info = self.plugins[plugin_id]
            if info.state == PluginState.STOPPED:
                self.start_plugin(plugin_id)
                info.state = PluginState.RUNNING
                self.plugins_lc.SetItem(info.line, 2, "运行中")
        for index in range(self.plugins_lc.GetItemCount()):
            item: wx.ListItem = self.plugins_lc.GetItem(index)
            self.refresh_button_state(item.GetId())

    def load_plugin(self, plugin_dir: str):
        if isfile(join(plugin_dir, "plugin.json")):
            with open(join(plugin_dir, "plugin.json"), "r", encoding="utf-8") as f:
                plugin_info = json.load(f)
            if not self.inst_plugin_req_gui(plugin_info):
                return False
            path.append(plugin_dir)
            plugin_logger = get_plugin_logger(plugin_info["id"], plugin_info["name"])
            module = import_module(f"plugins.{basename(plugin_dir)}.{plugin_info['main_file'].split('.')[0]}")
            main_class: BasePlugin = getattr(module, plugin_info["main_class"])()
            line = self.add_plugin_to_gui(plugin_info)
            self.plugins[plugin_info["id"]] = PluginInfo(plugin_info["id"], plugin_info, main_class,
                                                         PluginState.STOPPED, line, plugin_logger)
            return True
        else:
            return False

    def inst_plugin_req_gui(self, plugin_info: dict[str, Any]):
        if plugin_info["requirements"]:
            msg = "正在安装插件{} ({}/{})，请稍候..."
            dialog = wx.GenericProgressDialog("安装插件中", msg, 100,
                                              style=wx.PD_APP_MODAL | wx.PD_CAN_ABORT | wx.PD_AUTO_HIDE)
            wx.CallAfter(dialog.Pulse)
            wx.CallAfter(dialog.Show)
            for i, (req, version) in enumerate(plugin_info["requirements"].items()):
                dialog.Update(0, msg.format(req, i, len(plugin_info["requirements"])))
                if req in self.packages:
                    continue
                logger.debug(f"安装依赖 {req}")
                result = self.inst_package_thread(req, version, [])
                if result is False:
                    wx.CallAfter(dialog.Destroy)
                    wx.MessageBox(f"安装依赖{req}失败，请手动安装依赖后再次尝试", "错误", wx.ICON_ERROR)
                    return False
            wx.CallAfter(dialog.Destroy)
        return True

    @staticmethod
    def inst_package_thread(package_name: str, version: str, result_list: list):
        proc = Popen([executable, "-m", "pip", "install", package_name + (version if version else "")], shell=True)
        proc.wait()
        if proc.returncode != 0:
            result_list.append(False)
            return False
        result_list.append(True)
        return True

    def add_plugin_to_gui(self, plugin_info: dict[str, Any]) -> int:
        line = self.plugins_lc.InsertItem(self.plugins_lc.GetItemCount(), plugin_info["id"])
        self.plugins_lc.SetItem(line, 1, plugin_info["name"])
        self.plugins_lc.SetItem(line, 2, "已加载")
        self.plugins_lc.SetItem(line, 3, plugin_info["version"])
        self.plugins_lc.SetItem(line, 4, plugin_info["desc"])
        return line

    def config_plugin_gui(self, _: str):
        item = self.plugins_lc.GetFocusedItem()
        if item == -1:
            wx.MessageBox("请选择一个插件", "错误", wx.ICON_ERROR)
            return
        plugin_info = self.plugins[self.plugins_lc.GetItemText(item, 0)]
        if plugin_info.main_class.config:
            dialog = ConfigEditor(self, plugin_info.main_class.config,
                                  lambda cfg: self.plugin_config_cbk(plugin_info.id, cfg))
            dialog.ShowModal()

    def plugin_config_cbk(self, id_: str, config_dict: dict[str, Any]):
        plugin_info = self.plugins[id_]
        if plugin_info.main_class.config:
            plugin_info.main_class.update_config(plugin_info.main_class.config.copy(), config_dict)

    def on_item_selected(self, event: wx.ListEvent):
        if event.GetIndex() != -1:
            self.refresh_button_state(event.GetIndex())

    def start_plugin(self, id_: str, start_before_cbk: Callable[[], None] = lambda: None) -> str | None:
        plugin_info = self.plugins[id_]
        logger.info(f"启动插件: [{plugin_info.info['name']}]")
        try:
            if plugin_info.state == PluginState.STOPPED:
                plugin_info.state = PluginState.STARTING
                start_before_cbk()
                plugin_info.main_class.start()
                plugin_info.state = PluginState.RUNNING
                plugin_info.main_class.enable = True
                logger.info(f"插件启动成功: [{plugin_info.info['name']}]")
            else:
                logger.warning(f"插件 [{plugin_info.info['name']}] 已处于启动状态")
        except Exception as e:
            plugin_info.state = PluginState.STOPPED
            logger.error(f"启动插件 [{plugin_info.info['name']}] 失败: {str(e)}")
            return str(e)

    def start_plugin_gui(self, _):
        item = self.plugins_lc.GetFocusedItem()
        if item == -1:
            wx.MessageBox("请选择一个插件", "错误", wx.ICON_ERROR)
            return
        msg = self.start_plugin(self.plugins_lc.GetItemText(item, 0), lambda: self.refresh_button_state(item))
        if msg:
            wx.MessageBox(msg, "启动插件时遇到错误", wx.ICON_ERROR)
        else:
            self.plugins_lc.SetItem(self.plugins_lc.GetFocusedItem(), 2, "运行中")
        self.refresh_button_state(item)

    def stop_plugin(self, id_: str, stop_before_cbk: Callable[[], None]) -> str | None:
        plugin_info = self.plugins[id_]
        logger.info(f"停止插件: [{plugin_info.info['name']}]")
        try:
            if plugin_info.state == PluginState.RUNNING:
                plugin_info.state = PluginState.STOPPING
                stop_before_cbk()
                plugin_info.main_class.stop()
                plugin_info.state = PluginState.STOPPED
                plugin_info.main_class.enable = False
                logger.info(f"插件 [{plugin_info.info['name']}] 已停止")
            else:
                logger.warning(f"插件 [{plugin_info.info['name']}] 已处于停止状态")
        except Exception as e:
            plugin_info.state = PluginState.RUNNING
            logger.error(f"停止插件 [{plugin_info.info['name']}] 失败: {str(e)}")
            return str(e)

    def stop_plugin_gui(self, _):
        item = self.plugins_lc.GetFocusedItem()
        if item == -1:
            wx.MessageBox("请选择一个插件", "错误", wx.ICON_ERROR)
            return
        msg = self.stop_plugin(self.plugins_lc.GetItemText(item, 0), lambda: self.refresh_button_state(item))
        if msg:
            wx.MessageBox(msg, "停止插件时遇到错误", wx.ICON_ERROR)
        else:
            self.plugins_lc.SetItem(self.plugins_lc.GetFocusedItem(), 2, "已停止")
        self.refresh_button_state(item)

    def auto_launch_gui(self, _):
        item = self.plugins_lc.GetFocusedItem()
        if item == -1:
            wx.MessageBox("请选择一个插件", "错误", wx.ICON_ERROR)
            return
        plugin_id = self.plugins_lc.GetItemText(item, 0)
        if plugin_id in self.auto_launch_plugins:
            self.auto_launch_plugins.remove(plugin_id)
        else:
            self.auto_launch_plugins.append(plugin_id)
        self.refresh_button_state(item)

    def refresh_button_state(self, item: int):
        info: PluginInfo = self.plugins[self.plugins_lc.GetItemText(item, 0)]
        if info.state == PluginState.RUNNING:
            self.start_btn.Disable()
            self.stop_btn.Enable()
        elif info.state == PluginState.STOPPED:
            self.start_btn.Enable()
            self.stop_btn.Disable()
        elif info.state == PluginState.STOPPING:
            self.start_btn.Disable()
            self.stop_btn.Disable()
        elif info.state == PluginState.STARTING:
            self.start_btn.Disable()
            self.stop_btn.Disable()
        self.auto_launch_cb.SetValue(info.id in self.auto_launch_plugins)

    def on_close_window(self, event: wx.CloseEvent):
        self.show_or_hide()
        event.Veto()

    def on_exit(self):
        if self.has_exited:
            return
        logger.info("正在退出")
        self.save_config()
        self.stray_icon.stop()
        for plugin_info in self.plugins.values():
            if plugin_info.state == PluginState.RUNNING:
                self.stop_plugin(plugin_info.id, lambda: None)
        logger.info("再见！")
        self.has_exited = True

    def show_or_hide(self):
        if self.IsShown():
            self.create_stray_icon()
            self.stray_icon.run_detached()
            self.Hide()
        else:
            self.stray_icon.stop()
            self.Show()

    def on_exit_gui(self, *_):
        self.Destroy()
        Thread(target=self.on_exit).start()

    def create_stray_icon(self):
        menu = pystray.Menu(pystray.MenuItem('显示窗口', self.show_or_hide, default=True),
                            pystray.MenuItem('退出', self.on_exit_gui))

        self.stray_icon = pystray.Icon(name='Win Enchant Kit', title="Win Enchant Kit", icon=self.stray_icon_image,
                                       menu=menu)

    def save_config(self):
        config_fp = r".\config.json"
        try:
            with open(config_fp, "w", encoding="utf-8") as f:
                f.write(json.dumps(self.auto_launch_plugins, indent=4, ensure_ascii=False))
        except OSError:
            logger.error("无法保存配置文件")

    def read_config(self):
        config_fp = r".\config.json"
        if not exists(config_fp):
            self.save_config()
            return
        try:
            with open(config_fp, "r", encoding="utf-8") as f:
                self.auto_launch_plugins = json.loads(f.read())
        except OSError:
            logger.error("无法读取配置文件")


if __name__ == "__main__":
    app = wx.App()
    frame = ControlPanel(None)
    atexit.register(frame.on_exit)
    frame.Show()
    if len(argv) > 1 and argv[1] == "-startup":
        frame.show_or_hide()
    app.MainLoop()
