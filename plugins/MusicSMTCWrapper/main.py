# -*- coding: utf-8 -*-
"""MusicSMTCWrapper (音乐封面高清化) 插件入口。

Plugin 只负责流程编排:
    读取配置中的歌曲平台
      → 查找 SMTC 对应会话, 获取 SessionMediaProperties   (SMTCHost)
      → 交给对应 MusicBackend.get_music_info() 处理        (backend)
      → 拿到封面后交给 SMTCHost 更新到插件的 SMTC 会话      (SMTCHost)

SMTC 会话语义在 smtc_host.SMTCHost；封面搜索/缓存/平台差异在 backend\\ 下。
新增音乐平台后端: 实现 backend\\base.py 的 MusicBackend 子类 + 建缓存目录,
然后在下面 _build_backends() 里注册一行即可, 不需要改动 Plugin 的编排逻辑。
"""
import os
import sys
import winreg
from enum import Enum
from os.path import expandvars, join
from queue import Queue
from threading import Thread
from typing import Any

import pylnk3
import wx
import win32con as con
from PIL import Image
from win32comext.shell.shell import ShellExecuteEx
from winsdk.windows.media import MediaPlaybackStatus
from winsdk.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionMediaProperties as SessionMediaProperties,
)

from _smtc_backend_base import (
    FULL_COVER_SIZE,
    MusicBackend,
    StandardMusicInfo,
    plugin_logger,
)
from _smtc_backend_kugou import KugouMusicBackend
from base import *
from music_reporter import MusicReportManager
from smtc_host import SMTCHost

name = "高清酷狗封面"
logger = plugin_logger


class CoverCacheFmt(int):
    JPG = 0
    PNG = 1
    RAW_FORMAT = 2


class PluginConfig(ModuleConfigPlus):
    def __init__(self):
        super().__init__()
        self.tip: TipParam = TipParam("插件启用后如果仍然不显示高清封面, 请在快速设置面板(开WIFI的地方)")
        self.tip2: TipParam = TipParam("箭头按钮切换SMTC会话, 直到找到那个专辑名前多了一个空格的SMTC会话")
        self.music_platform: ChoiceParamPlus | str = ChoiceParamPlus("kugou", {"kugou": "Kugou"}, "歌曲平台")
        self.cover_size: ChoiceParam | str = ChoiceParam("480",
                                                         ["480", "400", "240", "150", "120", "100", "64"],
                                                         "封面尺寸")
        self.use_max_size: BoolParam | bool = BoolParam(False, "使用最大封面尺寸")
        self.allways_playing: BoolParam | bool = BoolParam(False, "永不暂停SMTC (暂停时总是切换会话时用)")
        self.exchange_title2album: BoolParam | bool = BoolParam(False, "专辑名与作者名互换")
        self.default_title: StringParam | str = StringParam("Title", "默认标题")
        self.default_artist: StringParam | str = StringParam("Artist", "默认艺术家")
        self.default_album_title: StringParam | str = StringParam("Album Title", "默认专辑名")
        self.default_album_artist: StringParam | str = StringParam("Album Artist", "默认专辑艺术家")
        self.cover_cache_quality: IntParam | int = IntParam(90, "封面缓存质量 (仅jpg) (1-100)")
        self.cover_cache_format: ChoiceParamPlus | CoverCacheFmt = ChoiceParamPlus(CoverCacheFmt.JPG,
                                                                                   {CoverCacheFmt.JPG: "JPG",
                                                                                    CoverCacheFmt.PNG: "PNG",
                                                                                    CoverCacheFmt.RAW_FORMAT: "原格式"
                                                                                    }, "封面缓存格式")
        self.refresh_info: ButtonParam = ButtonParam(desc="立即更新信息")
        self.clear_cache: ButtonParam = ButtonParam(desc="清除封面url缓存")
        self.enable_music_report: BoolParam | bool = BoolParam(False, "启用音乐报告")
        self.create_music_report: ButtonParam = ButtonParam(desc="生成音乐报告")
        self.install_kugou_lnk: ButtonParam = ButtonParam(
            desc="安装图标快捷方式 (需要管理员)",
            help_string="使得在SMTC页面出现 [🅺 Kugou] 而不是 [未知应用]\n"
                        r"文件位置在 [C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Kugou.lnk]"
        )


class Action(Enum):
    START = 0
    END = 1
    REFRESH_INFO = 2


def _build_backends(save_format: str | None, save_quality: int) -> dict[str, MusicBackend]:
    """平台 id -> 后端实例。新增后端只需在这里加一行 + 建好缓存目录。"""
    return {
        KugouMusicBackend.platform_id: KugouMusicBackend(save_format, save_quality),
    }


class Plugin(BasePlugin):

    def __init__(self):
        self.config = PluginConfig()
        self.config.refresh_info.handler = lambda: self.on_source_update(force_update=True)
        self.config.install_kugou_lnk.handler = self.install_kugou_lnk
        self.config.clear_cache.handler = self.remove_cache
        self.config.create_music_report.handler = self.create_report
        self.config.load()

        self.action_queue: Queue[Action | MediaPlaybackStatus] = Queue()
        self.action_thread = Thread(target=self.action_thread_func, daemon=True)

        self.smtc_host = SMTCHost()
        self.backends = _build_backends(*self._cover_save_policy())

        self.last_song = None

        self.sessions_changed_token = None
        self.source_changed_token = None
        self.timeline_changed_token = None
        self.has_reg_event = False

        self.music_reporter = MusicReportManager()

        self.action_thread.start()

    # ================================================================== 平台/配置
    @property
    def platform_id(self) -> str:
        return str(self.config.music_platform)

    @property
    def current_backend(self) -> MusicBackend | None:
        """按配置里的歌曲平台动态取后端 (配置切换平台后立即生效)"""
        return self.get_backend()

    def get_backend(self, platform_id: str = None) -> MusicBackend | None:
        return self.backends.get(platform_id or self.platform_id)

    def _cover_save_policy(self) -> tuple[str | None, int]:
        """把旧的封面缓存格式/质量配置翻译成缓存层需要的 (格式, 质量)。

        ``None`` 表示「原格式」: 由封面来源决定存 PNG 还是 JPEG。
        """
        fmt = self.config.cover_cache_format
        if fmt == CoverCacheFmt.PNG:
            save_format: str | None = "PNG"
        elif fmt == CoverCacheFmt.RAW_FORMAT:
            save_format = None
        else:
            save_format = "JPEG"
        return save_format, int(self.config.cover_cache_quality)

    def _cover_target_size(self) -> int:
        """配置 -> 目标封面尺寸 (use_max_size 等价于请求最大封面)"""
        if self.config.use_max_size:
            return FULL_COVER_SIZE
        try:
            return int(self.config.cover_size)
        except (TypeError, ValueError):
            return 480

    def _sync_config(self):
        """配置同步入口: 配置 -> SMTCHost / 各后端"""
        self.smtc_host.bind(self.config)
        save_format, save_quality = self._cover_save_policy()
        for backend in self.backends.values():
            backend.update_config(save_format, save_quality)

    # ================================================================== 插件生命周期
    def start(self):
        self.action_queue.put(Action.START)

    def start_raw(self):
        self._sync_config()
        logger.info("加载封面缓存")
        self.load_cache()
        self.smtc_host.create()
        self._sync_config()
        self.default()
        logger.info("注册事件")
        self.smtc_host.register_button_handler(self.on_button_press)
        self.on_session_changed()
        if not self.has_reg_event:
            self.sessions_changed_token = self.smtc_host.sessions.add_sessions_changed(self.on_session_changed)
            self.has_reg_event = True

    def stop(self):
        self.music_reporter.flush()
        self.music_reporter.save()

        self.action_queue.put(Action.END)

    def stop_raw(self):
        self.save_cache()
        logger.info("移除事件")
        if self.smtc_host.source is not None:
            self.smtc_host.source.remove_playback_info_changed(self.source_changed_token)
            self.smtc_host.source.remove_timeline_properties_changed(self.timeline_changed_token)
            self.smtc_host.sessions.remove_sessions_changed(self.sessions_changed_token)
            self.has_reg_event = False
        self.sessions_changed_token = None
        self.source_changed_token = None
        self.timeline_changed_token = None
        logger.info("删除SMTC对象")
        self.smtc_host.destroy()
        self.last_song = None

    def update_config(self, old_config: dict[str, Any], new_config: dict[str, Any]):
        self.config.load_values(new_config)
        self._sync_config()
        if self.enable:
            if self.config.allways_playing:
                self.action_queue.put(MediaPlaybackStatus.PLAYING)
            self.action_queue.put(Action.REFRESH_INFO)

            if old_config["enable_music_report"] != new_config[
                "enable_music_report"] and not self.config.enable_music_report:
                self.music_reporter.flush()

    def action_thread_func(self):
        while self.action_queue.empty():
            action = self.action_queue.get()
            if action == Action.START:
                self.start_raw()
            elif action == Action.END:
                self.stop_raw()
            elif action == Action.REFRESH_INFO:
                self.on_source_update(force_update=True)
            elif isinstance(action, MediaPlaybackStatus):
                self.smtc_host.set_playback_status(action)

    # ================================================================== 缓存
    def load_cache(self):
        for backend in self.backends.values():
            backend.load_cache()

    def save_cache(self):
        for backend in self.backends.values():
            backend.save_cache()

    def remove_cache(self):
        conf = wx.MessageBox("确定要清除封面缓存吗?", "WinEnchantKit - 高清酷狗封面",
                             wx.YES_NO | wx.ICON_QUESTION)
        if conf != wx.YES:
            return
        for backend in self.backends.values():
            total = backend.music_cache.count() + 1
            dialog = wx.ProgressDialog("正在删除缓存", "请稍候...", max(1, total), None,
                                       wx.CENTRE | wx.RESIZE_BORDER)
            # clear_cache 是生成器: 每删一项 yield 一次进度
            for done, count, filename in backend.clear_cache():
                dialog.Update(done, f"({done}/{count})\n正在删除缓存: {filename}")
            dialog.Destroy()

    # ================================================================== 源会话事件
    def on_session_changed(self, *_):
        if self.check_source_valid():
            return
        if self.smtc_host.has_source:
            logger.info("Kugou SMTC会话已失效")
            self.smtc_host.release_source()
            self.source_changed_token = self.timeline_changed_token = self.last_song = None
            self.smtc_host.set_playback_status(MediaPlaybackStatus.STOPPED)
            self.default()
        try:
            session = self.smtc_host.find_source()
            if self.config.allways_playing:
                self.smtc_host.set_playback_status(MediaPlaybackStatus.PLAYING)
            self.source_changed_token = session.add_playback_info_changed(self.on_source_update)
            self.timeline_changed_token = session.add_timeline_properties_changed(self.on_timeline_update)
            self.on_source_update()
        except RuntimeError:
            pass

    def check_source_valid(self) -> bool:
        return self.smtc_host.check_source_valid()

    def default(self):
        if self.smtc_host.smtc is None:
            wx.MessageBox("请先启用插件", "WinEnchantKit - 高清酷狗封面", wx.OK | wx.ICON_WARNING)
            return
        self.smtc_host.display_default()

    # ================================================================== 歌曲信息刷新
    def on_timeline_update(self, *_):  # 当歌曲进度条更新时
        if not self.check_source_valid():
            return
        self.smtc_host.update_timeline()

    def on_source_update(self, *_, force_update: bool = False):
        if not self.check_source_valid():
            if force_update:
                self.default()
            return

        info = self.smtc_host.get_source_info()
        song_id = f"{info.title}{info.artist}{info.album_title}{info.album_artist}"

        # 同步播放状态
        status: MediaPlaybackStatus = self.smtc_host.get_source_status()
        if status != self.smtc_host.playback_status and not self.config.allways_playing:
            self.smtc_host.set_playback_status(status)

        self._handle_music_report(info, status)
        self.on_timeline_update()

        if song_id == self.last_song and not force_update:
            # logger.debug(f"歌曲信息更新, 歌名相同, 不更新, {str([status])[1:-1]}, {song_id}")
            return
        backend = self.current_backend or self.get_backend()
        if backend is None:
            logger.error(f"找不到平台 [{self.platform_id}] 对应的后端")
            return
        self.last_song = song_id
        # 请求尺寸在这里定下来, 后端 get_music_info 直接把封面图备好
        music = backend.get_music_info(info, self._cover_target_size())
        self.update_info(music)

    def update_info(self, music: StandardMusicInfo | None):
        """把后端解析好的歌曲信息 (含封面图) 交给 SMTCHost 写入自建 SMTC 会话。

        封面获取/缓存细节在 backend, SMTC 写入细节在 SMTCHost; 这里只做编排。
        """
        if music is None:
            logger.warning("搜索不到歌曲信息, 使用酷狗原封面")
            return
        # 归一化可能为空的字段
        music.album_title = music.album_title or ""
        music.album_artist = music.album_artist or ""
        size_text = f"{music.cover.width}x{music.cover.height}" if music.cover is not None else "无封面"
        logger.info(f"更新歌曲信息: {music.title} - {music.artist}, 封面分辨率: {size_text}")
        self.smtc_host.display(music)

    # ================================================================== 音乐报告
    def _handle_music_report(self, info: SessionMediaProperties, status: MediaPlaybackStatus):
        self.music_reporter.enabled = bool(self.config.enable_music_report)
        self.music_reporter.handle(info, status)

    def create_report(self):
        path = self.music_reporter.generate_report()
        Thread(target=os.system, args=(path,), daemon=True).start()

    # ================================================================== SMTC 按钮
    def on_button_press(self, _, args):
        if not self.check_source_valid():
            return
        self.smtc_host.handle_button(args)

    # ================================================================== 快捷方式
    @staticmethod
    def install_kugou_lnk():
        program = r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs"
        temp_dir = expandvars("%TEMP%\WinEnchantKit")
        os.makedirs(temp_dir, exist_ok=True)
        temp_path = join(temp_dir, r"Kugou.lnk")
        target_path = join(program, "Kugou.lnk")
        if "pythonw.exe" in sys.orig_argv[0]:
            exec_name = "pythonw.exe"
        else:
            exec_name = "python.exe"
        base_executable_path = join(sys.base_prefix, exec_name)
        kugou_key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\kugou")
        kugou_path = join(winreg.QueryValueEx(kugou_key, "KuGou8")[0], "KuGou.exe")

        lnk = pylnk3.for_file(base_executable_path, icon_file=kugou_path, icon_index=0)
        try:
            lnk.save(target_path)
            wx.MessageBox("创建快捷方式成功！\n记得重启程序哦", "成功！ - ( •̀ ω •́ )✧", wx.OK | wx.ICON_INFORMATION)
            return
        except OSError:
            pass
        lnk.save(temp_path)
        lnk_install_bat = expandvars(r"%TEMP%\WinEnchantKit-Plugin-HDKugouCover-Kugou-Lnk-Install.bat")
        with open(lnk_install_bat, "w+") as f:
            f.write("\n".join([
                f'move "{temp_path}" "{target_path}"'
            ]))
        try:
            ShellExecuteEx(lpVerb="runas", lpFile=lnk_install_bat, nShow=con.SW_HIDE)
            wx.MessageBox("创建快捷方式成功！\n记得重启程序哦", "成功！ - ( •̀ ω •́ )✧", wx.OK | wx.ICON_INFORMATION)
            return
        except Exception as e:
            logger.error(f"{e.__class__.__name__}: {e} -> 运行快捷方式复制脚本失败")
            ret = wx.MessageBox(f"{e.__class__.__name__}: {e}\n"
                                "运行快捷方式复制脚本失败\n"
                                "要打开 lnk所在目录 和 目标文件夹 吗?",
                                f"失败啦~ - ＞︿＜",
                                wx.YES_NO | wx.ICON_ERROR)
        if ret == wx.YES:
            os.startfile(r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs")
            Thread(target=os.system, args=[f"explorer /select,{temp_path}"], daemon=True).start()
            wx.MessageBox("已打开两个文件夹！\n"
                          "请移动lnk至 [C:\\ProgramData\\Microsoft\\Windows\\Start Menu\\Programs]\n"
                          "创建快捷方式成功！(虽说是保存到别处\n"
                          "记得重启程序哦",
                          "成功！ - (*^▽^*)", wx.OK | wx.ICON_INFORMATION)


if __name__ == "__main__":
    import logging

    logger.setLevel(logging.DEBUG)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    logger.addHandler(console_handler)
    plugin = Plugin()
    plugin.start_raw()
    input()
    plugin.stop_raw()
    input()
    plugin.start_raw()
    input()
