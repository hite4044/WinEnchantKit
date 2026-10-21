# -*- coding: utf-8 -*-
"""MusicSMTCWrapper (音乐封面高清化) 插件入口。

Plugin 只负责流程编排, 并同时持有两个 SMTC 侧组件:
  * `SourceManager` (smtc_host.py) —— 源平台会话: 发现/筛选/事件注册注销/读取信息, 单线程
  * `SMTCHost`     (smtc_host.py) —— 插件自建的 SMTC 会话

流程: 读取配置中的歌曲平台 / 识别源会话属于哪个平台
  → SourceManager 提供源会话的 SessionMediaProperties
  → 交给对应 MusicBackend.get_music_info() 处理        (smtc_backend.*)
  → 拿到封面后交给 SMTCHost 更新到插件的 SMTC 会话
两个组件之间只通过回调联系, 回调在插件启动时一次性设置。
新增音乐平台后端: 在 smtc_backend\\ 下新增模块, 实现 MusicBackend 子类 + 声明
platform_id / cache_name / source_markers, 然后在 _build_backends() 里注册一行即可。
"""
import os
import sys

# 插件加载器只把插件根目录放进 sys.path, 而后端包在本目录的 smtc_backend/ 下, 这里补一次。
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

import winreg
from os.path import expandvars, join
from queue import Queue
from threading import Thread

import pylnk3
import wx
import win32con as con
from win32comext.shell.shell import ShellExecuteEx
from winsdk.windows.media import MediaPlaybackStatus
from winsdk.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionMediaProperties as SessionMediaProperties,
)

from base import *
from music_reporter import MusicReportManager
from smtc_backend import kugou, netease
from smtc_backend.base import (
    FULL_COVER_SIZE,
    MusicBackend,
    StandardMusicInfo,
    plugin_logger,
)
from smtc_host import SMTCHost, SourceManager

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
        self.music_platform: ChoiceParamPlus | str = ChoiceParamPlus("kugou",
                                                                     {"kugou": "Kugou", "netease": "网易云"},
                                                                     "歌曲平台")
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
    """平台 id -> 后端实例。新增后端只需新建模块并在这里加一行。"""
    return {
        kugou.KugouMusicBackend.platform_id: kugou.KugouMusicBackend(save_format, save_quality),
        netease.NeteaseMusicBackend.platform_id: netease.NeteaseMusicBackend(save_format, save_quality),
    }


def _find_backend(backends: dict[str, MusicBackend], source_id: str | None,
                  fallback_platform: str) -> MusicBackend | None:
    """按源会话的应用 id 找对应后端 (source_markers); 认不出来时用配置里选的平台"""
    source = (source_id or "").lower()
    if source:
        for backend in backends.values():
            if any(marker in source for marker in getattr(backend, "source_markers", ())):
                return backend
    return backends.get(fallback_platform)


class Plugin(BasePlugin):

    def __init__(self):
        self.config = PluginConfig()
        self.config.refresh_info.handler = lambda: self.on_source_update(force_update=True)
        self.config.install_kugou_lnk.handler = self.install_kugou_lnk
        self.config.clear_cache.handler = self.remove_cache
        self.config.create_music_report.handler = self.create_report
        self.config.load()

        self.action_queue: Queue[Action | MediaPlaybackStatus] = Queue()
        self.result_queue: Queue[int] = Queue()
        self.action_thread = Thread(target=self.action_thread_func, daemon=True)

        self.smtc_host = SMTCHost()
        self.source_manager = SourceManager()
        # 回调在启动时一次性设置: 两个管理器之间只通过回调联系
        self.source_manager.on_changed = self.on_source_changed
        self.source_manager.on_updated = self.on_source_update
        self.source_manager.on_timeline_changed = self.on_timeline_update
        self.source_manager.on_plugin_playback = self.smtc_host.set_playback_status
        self.smtc_host.set_button_callback(self.on_button_press)

        self.backends = _build_backends(*self._cover_save_policy())

        self.last_song = None

        self.music_reporter = MusicReportManager()

        self.action_thread.start()

    # ================================================================== 平台/配置
    @property
    def platform_id(self) -> str:
        return str(self.config.music_platform)

    def get_backend(self, source_id: str = None) -> MusicBackend | None:
        """按源会话应用 id 取后端; 认不出来时用配置里选的平台"""
        return _find_backend(self.backends, source_id, self.platform_id)

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
        """配置同步入口: 配置 -> SourceManager / SMTCHost / 各后端"""
        self.source_manager.bind(self.config)
        self.smtc_host.bind(self.config, self.source_manager.source_id)
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
        logger.info("启动源会话管理器 (注册会话列表事件 + 单线程 worker)")
        # SourceManager 自己负责源会话事件的注册/注销, 以及所有源会话操作的单线程化
        self.source_manager.bind(self.config)
        self.source_manager.start()
        self.update_source_session()

    def stop(self):
        self.music_reporter.flush()
        self.music_reporter.save()

        self.action_queue.put(Action.END)

    def stop_raw(self):
        self.save_cache()
        logger.info("停止源会话管理器 (注销事件 + 停 worker)")
        self.source_manager.stop()
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

            if old_config["music_platform"] != new_config["music_platform"]:
                # 歌曲平台变了: 让 SourceManager 注销旧会话事件并按新平台重新筛选
                logger.info(f"歌曲平台变更: {old_config['music_platform']} -> {new_config['music_platform']}")
                self.source_manager.switch_platform(self.platform_id)
                self.smtc_host.bind(self.config, self.source_manager.source_id)
                self.last_song = None
                self.on_source_update(force_update=True)

    def action_thread_func(self):
        while True:
            action = self.action_queue.get()
            if action == Action.START:
                self.start_raw()
            elif action == Action.END:
                self.stop_raw()
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
    # 以下三个回调由 SourceManager 调用 (在它的 worker 线程 / WinRT 事件线程上),
    # 回调本身只做编排, 所有源会话的读取都通过 SourceManager (它内部再串行到唯一 worker)。
    def on_source_changed(self):
        """会话列表发生变化: 当前源会话还有效就什么都不做, 否则断开重找"""
        if self.check_source_valid():
            return
        logger.info("SMTC会话已失效")
        self.update_source_session(update_or_destroyed=False)

    def update_source_session(self, update_or_destroyed: bool = False):
        """重新筛选源会话; update_or_destroyed=True 时先主动断开当前的"""
        logger.debug(f"更新SMTC源: {update_or_destroyed}")
        if self.source_manager.has_source:
            self.source_manager.release()
            self.last_song = None
            self.smtc_host.bind(self.config, None)
            self.smtc_host.set_playback_status(MediaPlaybackStatus.STOPPED)
            self.default()
        if self.source_manager.find():
            self.smtc_host.bind(self.config, self.source_manager.source_id)
            if self.config.allways_playing:
                self.smtc_host.set_playback_status(MediaPlaybackStatus.PLAYING)
            self.on_source_update()
        else:
            logger.info("暂时没有找到目标SMTC")

    def check_source_valid(self) -> bool:
        return self.source_manager.valid()

    def default(self):
        if self.smtc_host.smtc is None:
            wx.MessageBox("请先启用插件", "WinEnchantKit - 高清酷狗封面", wx.OK | wx.ICON_WARNING)
            return
        self.smtc_host.display_default()

    # ================================================================== 歌曲信息刷新
    def on_timeline_update(self, *_):  # 当歌曲进度条更新时
        if not self.check_source_valid():
            return
        # 只有萌音需要把进度条透传给插件会话
        if "moekoe" in (self.source_manager.source_id or "").lower():
            self.smtc_host.update_timeline(self.source_manager.timeline_properties())

    def on_source_update(self, *_, force_update: bool = False):
        if not self.check_source_valid():
            if force_update:
                self.default()
            return

        info = self.source_manager.info()
        if info is None:
            return
        song_id = f"{info.title}{info.artist}{info.album_title}{info.album_artist}"

        # 同步播放状态
        status: MediaPlaybackStatus = self.source_manager.status()
        if status != self.smtc_host.playback_status and not self.config.allways_playing:
            self.smtc_host.set_playback_status(status)

        self._handle_music_report(info, status)
        self.on_timeline_update()

        if song_id == self.last_song and not force_update:
            # logger.debug(f"歌曲信息更新, 歌名相同, 不更新, {str([status])[1:-1]}, {song_id}")
            return
        # 后端按源会话应用 id 选 (切换平台/软件时自动切换), 认不出来才用配置里选的平台
        backend = self.get_backend(self.source_manager.source_id)
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
            logger.warning("搜索不到歌曲信息, 使用原封面")
            return
        # 归一化可能为空的字段
        music.album_title = music.album_title or ""
        music.album_artist = music.album_artist or ""
        if music.cover is None:
            cover_text = "无高清封面(保留原封面)"
        elif music.cover_slot:
            cover_text = f"{music.cover.width}x{music.cover.height} (缓存 {music.cover_slot}px)"
        else:
            cover_text = f"{music.cover.width}x{music.cover.height}"
        logger.info(f"更新歌曲信息: {music.title} - {music.artist}, 封面: {cover_text}")
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
        """插件会话按钮 -> 交给 SourceManager (在它的 worker 线程上操作源会话)"""
        if not self.check_source_valid():
            return
        self.source_manager.handle_button(args)

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
