# -*- coding: utf-8 -*-
"""MusicSMTCWrapper (音乐封面高清化) 插件入口。

职责 (只做编排, 具体实现分散在各自模块):

* ``PluginConfig``   —— 插件配置 (所有配置项与 help_string 都在这里)
* ``SourceSMTC``     —— 源平台官方 SMTC 会话的发现/读取/事件 (smtc_source.py, 单线程)
* ``SMTCHost``       —— 插件托管的 SMTC 会话 (smtc_host.py, 单线程, 可自托管进度)
* ``BaseMusicBackend`` 及各平台后端 —— 歌曲搜索 / 封面下载 / 本地缓存 (platform/)

信息流:
    SourceSMTC.snapshot()  →  平台后端 fetch() (缓存 → 联网 → 封面相似度)
                           →  字段指向映射  →  SMTCHost.update()

导入约定: 本插件作为 namespace 包 ``plugins.MusicSMTCWrapper`` 被加载, 包内一律使用相对导入。
插件目录里的 ``platform/`` 与标准库 ``platform`` 同名, 因此程序一启动就把插件目录从
``sys.path`` 移除, 避免标准库被遮蔽; 相对导入不依赖 ``sys.path``, 不受影响。
"""
import os
import sys

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

# 插件加载器把插件目录插入过 sys.path[0] (可能是相对路径, 例如 "plugins\MusicSMTCWrapper"):
# 这里立刻移除, 防止 plugin/platform/ 遮蔽标准库 platform。比较时两边都做绝对化 + 大小写归一。
_TARGET_DIR = os.path.normcase(os.path.normpath(os.path.abspath(_PLUGIN_DIR)))
for _entry in list(sys.path):
    try:
        _entry_path = os.path.normcase(os.path.normpath(os.path.abspath(_entry or os.getcwd())))
    except (TypeError, ValueError, OSError):
        continue
    if _entry_path == _TARGET_DIR:
        try:
            sys.path.remove(_entry)
        except ValueError:
            pass

import logging
from io import BytesIO
from os import makedirs
from os.path import dirname, isfile, join, expandvars
from queue import Empty, Queue
from threading import Event, Thread
from time import sleep

import pylnk3
import wx
import win32con as con
from PIL import Image
from win32comext.shell.shell import ShellExecuteEx

from base import *  # noqa: F403 - WinEnchantKit 的参数类型 (ModuleConfigPlus / ChoiceParamPlus / ...)

from . import music_base
from .music_base import (
    FIELD_ALBUM_ARTIST,
    FIELD_ALBUM_TITLE,
    FIELD_ARTIST,
    FIELD_CHOICES,
    FIELD_TITLE,
    LOGGER_NAME,
    MusicInfo,
    normalize_field,
)
from .platform import kugou, netease, qqmusic
from .platform.base_backend import DEFAULT_COVER_SIZE, FULL_COVER_SIZE, BaseMusicBackend
from .smtc_host import SMTCHost
from .smtc_source import (
    PLATFORM_AUTO,
    PLATFORM_KUGOU,
    PLATFORM_NETEASE,
    PLATFORM_QQ,
    SourceSMTC,
    SourceSnapshot,
)

name = "音乐封面高清化"
logger = logging.getLogger(LOGGER_NAME)
logger.setLevel(logging.DEBUG)

# 配置界面 / 消息框标题
WINDOW_TITLE = "WinEnchantKit - 音乐封面高清化"

# 无源会话时显示的默认信息 (与旧插件默认值一致)
DEFAULT_MUSIC = MusicInfo("Title", "Artist", "Album Title", "Album Artist", 0.0)

# 刷新请求类型
_REFRESH_FULL = "full"
_REFRESH_TIMELINE = "timeline"
# 请求合并前的防抖时间 (秒): 吸收短时间内的连续事件
REFRESH_DEBOUNCE = 0.15
# 生命周期 / 源接口的等待超时
LIFECYCLE_TIMEOUT = 30.0

# 源 SMTC 平台选项
SOURCE_PLATFORM_CHOICES = {
    PLATFORM_AUTO: "自动",
    PLATFORM_KUGOU: "酷狗/MoeKoeMusic",
    PLATFORM_NETEASE: "网易云",
    PLATFORM_QQ: "QQ音乐",
}
# 音乐搜索平台选项
SEARCH_PLATFORM_CHOICES = {
    PLATFORM_AUTO: "自动决定",
    PLATFORM_KUGOU: "优先酷狗音乐",
    PLATFORM_NETEASE: "优先网易云",
    PLATFORM_QQ: "优先QQ音乐",
}
# 封面尺寸选项 (SMTC 最终使用的封面大小)
COVER_SIZE_CHOICES = {
    150: "150x150",
    240: "240x240",
    400: "400x400",
    480: "480x480",
    640: "640x640",
    800: "800x800",
}
# 封面缓存格式
COVER_FORMAT_CHOICES = {
    "raw": "原文件",
    "jpeg": "JPEG",
    "png": "PNG",
}


class PluginConfig(ModuleConfigPlus):
    """插件配置。

    help_string 是给用户/AI 看懂参数含义的说明; 所有会修改行为、需要重启或不重启生效的
    逻辑都在 ``Plugin.update_config`` 里分发。
    """

    def __init__(self):
        super().__init__()
        self.source_platform: ChoiceParamPlus | str = ChoiceParamPlus(
            PLATFORM_AUTO, SOURCE_PLATFORM_CHOICES, "源SMTC平台"
        ).set_help("控制歌曲信息从哪个SMTC会话获取。\n"
                   "自动: 自动搜索并绑定下面可选项的SMTC会话, 除非绑定类型的SMTC会话失效, 否则不轻易切换类型")
        self.search_platform: ChoiceParamPlus | str = ChoiceParamPlus(
            PLATFORM_AUTO, SEARCH_PLATFORM_CHOICES, "音乐搜索平台"
        ).set_help("控制从哪个平台搜索对应的歌曲信息。\n"
                   "自动决定: 根据源SMTC平台的类型决定")
        self.allow_cross_platform: BoolParam | bool = BoolParam(
            True, "允许从不同平台搜索"
        ).set_help("允许从定义除外的平台搜索出歌曲信息并使用 (相同平台搜不到时再试其他平台)")
        self.use_cover_similarity: BoolParam | bool = BoolParam(
            True, "通过封面相似度检测"
        ).set_help("允许在搜索不出对应专辑时, 逐个下载搜索结果封面与源SMTC封面(150x150)做相似度比对, "
                   "选出最相似的那张 ")
        self.cover_size: ChoiceParamPlus | int = ChoiceParamPlus(
            480, COVER_SIZE_CHOICES, "封面尺寸"
        ).set_help("SMTC最终使用的封面大小")
        self.use_max_cover: BoolParam | bool = BoolParam(
            False, "使用最大封面"
        ).set_help("勾选后, SMTC将使用音乐平台提供的最大封面")
        self.self_host_progress: BoolParam | bool = BoolParam(
            True, "自行托管进度"
        ).set_help("勾选后, SMTC将在音乐平台未提供歌曲进度信息时, "
                   "通过歌曲信息、SMTC播放状态及计时器自行计算并更新歌曲进度信息")
        self.map_title: ChoiceParamPlus | str = ChoiceParamPlus(
            FIELD_TITLE, FIELD_CHOICES, "标题指向"
        ).set_help("SMTC标题显示歌曲信息中的哪个字段")
        self.map_artist: ChoiceParamPlus | str = ChoiceParamPlus(
            FIELD_ARTIST, FIELD_CHOICES, "作者指向"
        ).set_help("SMTC作者显示歌曲信息中的哪个字段")
        self.map_album_title: ChoiceParamPlus | str = ChoiceParamPlus(
            FIELD_ALBUM_TITLE, FIELD_CHOICES, "专辑标题指向"
        ).set_help("SMTC专辑标题显示歌曲信息中的哪个字段")
        self.map_album_artist: ChoiceParamPlus | str = ChoiceParamPlus(
            FIELD_ALBUM_ARTIST, FIELD_CHOICES, "专辑作者指向"
        ).set_help("SMTC专辑作者显示歌曲信息中的哪个字段")
        self.never_pause: BoolParam | bool = BoolParam(
            False, "不暂停SMTC"
        ).set_help("用于防止在某些情况下歌曲暂停时SMTC会话自动切换。\n"
                   "也可以前往Wallpaper Engine设置-常规-媒体禁用列表以防止使用官方SMTC")
        self.cover_cache_format: ChoiceParamPlus | str = ChoiceParamPlus(
            "jpeg", COVER_FORMAT_CHOICES, "封面缓存格式"
        ).set_help("封面在本地缓存的格式")
        self.cover_cache_quality: IntParam | int = IntParam(
            90, "封面缓存质量 (1-100)"
        ).set_help("控制JPEG封面缓存的质量")
        self.refresh_now: ButtonParam = ButtonParam(
            desc="立即更新信息",
            help_string="立即重新获取并更新SMTC信息 (会无视缓存联网获取)",
        )
        self.clear_cache: ButtonParam = ButtonParam(
            desc="清除缓存",
            help_string="清除歌曲信息以及封面缓存",
        )
        self.install_shortcut: ButtonParam = ButtonParam(
            desc="安装图标快捷方式 (需要管理员)",
            help_string="使得在SMTC页面出现 [🅺 SMTCWrapper] 而不是 [未知应用]\n"
                        r"文件位置在 [C:\ProgramData\Microsoft\Windows\Start Menu\Programs\SMTCWrapper.lnk]",
        )


class _Action:
    """生命周期线程的队列消息类型"""

    START = "start"
    STOP = "stop"
    APPLY_CONFIG = "apply_config"


class Plugin(BasePlugin):
    """插件入口: 配置 / 生命周期 / 刷新编排。

    线程:
      * GUI 线程: 配置按钮、wx 对话框 (只投递请求, 不做网络/SMTC 调用)
      * 生命周期线程: start_raw / stop_raw (串行执行, 防止启停交叉)
      * 刷新线程: 读源会话快照 → 选后端 → 解析歌曲信息/封面 → 写托管会话
    """

    def __init__(self):
        self.config = PluginConfig()
        self.config.refresh_now.handler = self.refresh_info
        self.config.clear_cache.handler = self.clear_cache
        self.config.install_shortcut.handler = self.install_shortcut
        self.config.load()

        # 运行状态 (由生命周期线程维护)
        self._running = False
        self._lifecycle_queue: Queue = Queue()
        self._lifecycle_thread = Thread(target=self._lifecycle_loop, name="MusicSMTCWrapper-Lifecycle",
                                        daemon=True)

        # 刷新线程: ("full", force) / ("timeline", False)
        self._refresh_queue: Queue = Queue()
        self._refresh_thread = Thread(target=self._refresh_loop, name="MusicSMTCWrapper-Refresh",
                                      daemon=True)

        # 后端注册表: 新增平台后端只需在这里加一行 (顺序不影响选路, 选路在 _backend_order)
        self._backends: dict[str, BaseMusicBackend] = {}
        self._register_backends()

        # 源 SMTC / 托管 SMTC
        self._source = SourceSMTC()
        self._source.on_source_changed = self._on_source_changed
        self._source.on_info_changed = self._on_info_changed
        self._source.on_timeline_changed = self._on_timeline_changed
        self._host = SMTCHost()
        self._host.set_button_callback(self._on_host_button)

        # 刷新线程独占的状态 (同一首歌/上一份展示信息)
        self._last_source_key = ""
        self._last_music: MusicInfo | None = None

        self._lifecycle_thread.start()
        self._refresh_thread.start()

    # ================================================================ 后端注册
    def _register_backends(self):
        for backend in (kugou.KugouMusicBackend(), netease.NeteaseMusicBackend(),
                        qqmusic.QqMusicBackend()):
            self._backends[backend.platform_id] = backend

    def _platform_backend(self, platform: str) -> BaseMusicBackend | None:
        return self._backends.get(str(platform or ""))

    def _backend_order(self, source_platform: str) -> list[BaseMusicBackend]:
        """后端尝试顺序: 相同平台 > 不同平台 (允许跨平台时); 主平台没有后端时自动降级。

        * 搜索平台为 auto → 主平台 = 源 SMTC 平台;
        * 搜索平台为具体平台 → 主平台 = 该平台 (用户明确指定);
        * 主平台没有后端 (例如未来新增但尚未实现后端的平台) → 降级到源平台, 再降级到全部后端;
        * 其余后端只在「允许从不同平台搜索」时追加。
        """
        order: list[BaseMusicBackend] = []
        search_mode = str(self.config.search_platform or PLATFORM_AUTO)
        primary = source_platform if search_mode == PLATFORM_AUTO else search_mode
        backend = self._platform_backend(primary)
        if backend is not None:
            order.append(backend)
        else:
            logger.warning(f"搜索平台 [{primary}] 没有对应后端, 自动降级")
            fallback = self._platform_backend(source_platform)
            if fallback is not None:
                order.append(fallback)
        allow_cross = bool(self.config.allow_cross_platform)
        for candidate in self._backends.values():
            if candidate in order:
                continue
            if not order:
                # 一个可用后端都没有: 无论是否允许跨平台都必须兜底
                order.append(candidate)
            elif allow_cross:
                order.append(candidate)
        return order

    # ================================================================ 生命周期
    def start(self):
        """插件启动入口 (GUI 调用): 投递给生命周期线程, 不阻塞 GUI"""
        self._lifecycle_queue.put((_Action.START, None, None))

    def stop(self):
        """插件停止入口 (GUI 调用): 等待清理完成, 避免线程/会话残留"""
        done = Event()
        self._lifecycle_queue.put((_Action.STOP, None, done))
        if not done.wait(timeout=LIFECYCLE_TIMEOUT):
            logger.warning("停止插件超时: 后台清理可能仍在进行")

    def _lifecycle_loop(self):
        while True:
            item = self._lifecycle_queue.get()
            action, payload, done = item
            try:
                if action == _Action.START:
                    self._start_raw()
                elif action == _Action.STOP:
                    self._stop_raw()
                elif action == _Action.APPLY_CONFIG:
                    old_config, new_config = payload
                    self._apply_config_runtime(old_config, new_config)
            except Exception as e:  # noqa: BLE001 - 后台线程不能因为一次异常就退出
                logger.error(f"插件生命周期操作失败: {e.__class__.__name__}: {e}", exc_info=True)
            finally:
                if done is not None:
                    done.set()

    def _start_raw(self):
        if self._running:
            logger.warning("插件已经在运行, 忽略重复启动")
            return
        logger.info("启动 MusicSMTCWrapper")
        self._apply_backend_config()
        for backend in self._backends.values():
            backend.load_cache()

        # 手动计算配置在 SMTC 会话启动前同步给托管会话
        self._host.configure(never_pause=bool(self.config.never_pause),
                             self_host_progress=bool(self.config.self_host_progress))
        if not self._host.create():
            logger.error("创建托管 SMTC 会话失败")
        self._source.start()
        self._source.set_platform_mode(self._source_platform_mode())
        self._source.find()
        self._running = True
        self._last_source_key = ""
        self._last_music = None
        if self._source.has_source():
            self._request_refresh(force=True)
        else:
            logger.info("暂时没有找到目标SMTC会话, 显示默认信息")
            self._show_default()

    def _stop_raw(self):
        if not self._running:
            return
        logger.info("停止 MusicSMTCWrapper")
        self._running = False
        for backend in self._backends.values():
            try:
                backend.save_cache()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"保存缓存失败: {e.__class__.__name__}: {e}")
        self._source.stop()
        self._host.destroy()
        self._last_source_key = ""
        self._last_music = None

    def update_config(self, old_config: dict[str, Any], new_config: dict[str, Any]):
        """配置更新 (GUI 在「应用/确定」后调用)。

        配置值 / 后端 / 托管会话配置立即生效; 需要操作源会话的部分 (平台切换、重新筛选)
        投递给生命周期线程, 避免 GUI 线程同步等待 WinRT。
        """
        self.config.load_values(new_config)
        self._apply_backend_config()
        self._host.configure(never_pause=bool(self.config.never_pause),
                             self_host_progress=bool(self.config.self_host_progress))
        if not self._running:
            return
        self._lifecycle_queue.put((_Action.APPLY_CONFIG, (old_config, new_config), None))

    def _apply_config_runtime(self, old_config: dict[str, Any], new_config: dict[str, Any]):
        """在生命周期线程上应用需要改源会话的配置 (源平台模式变化 / 重新解析信息)。"""
        if not self._running:
            return
        if old_config.get("source_platform") != new_config.get("source_platform"):
            logger.info(f"源SMTC平台变更: {old_config.get('source_platform')} -> "
                        f"{new_config.get('source_platform')}")
            self._source.set_platform_mode(self._source_platform_mode())
            self._source.find()
        # 字段指向 / 封面尺寸 / 搜索平台等配置变化要立刻重新解析一遍 (命中缓存时很快)
        self._last_source_key = ""
        self._last_music = None
        self._request_refresh(force=False)

    # ================================================================ 配置读取
    def _apply_backend_config(self):
        save_format, quality = self._cover_save_policy()
        for backend in self._backends.values():
            backend.update_config(save_format, quality)

    def _cover_save_policy(self) -> tuple[str | None, int]:
        """配置 -> 缓存层需要的 (格式, 质量); None 表示保持原图格式"""
        fmt = str(self.config.cover_cache_format or "jpeg")
        save_format = {"jpeg": "JPEG", "png": "PNG"}.get(fmt)
        try:
            quality = int(self.config.cover_cache_quality)
        except (TypeError, ValueError):
            quality = 90
        return save_format, max(1, min(100, quality))

    def _cover_target_size(self) -> int:
        """配置 -> 请求封面尺寸 (FULL_COVER_SIZE 表示最大封面)"""
        if bool(self.config.use_max_cover):
            return FULL_COVER_SIZE
        try:
            return int(self.config.cover_size)
        except (TypeError, ValueError):
            return DEFAULT_COVER_SIZE

    def _source_platform_mode(self) -> str:
        mode = str(self.config.source_platform or PLATFORM_AUTO)
        return mode if mode in SOURCE_PLATFORM_CHOICES else PLATFORM_AUTO

    def _display_mapping(self) -> dict[str, str]:
        """字段指向配置 -> MusicInfo.mapped 用的映射"""
        return {
            FIELD_TITLE: normalize_field(self.config.map_title),
            FIELD_ARTIST: normalize_field(self.config.map_artist),
            FIELD_ALBUM_TITLE: normalize_field(self.config.map_album_title),
            FIELD_ALBUM_ARTIST: normalize_field(self.config.map_album_artist),
        }

    # ================================================================ 源会话回调
    # 这些回调在 SourceSMTC 的回调分发线程上执行: 只投递刷新请求, 不做耗时操作。
    def _on_source_changed(self):
        self._request_refresh(force=False)

    def _on_info_changed(self):
        self._request_refresh(force=False)

    def _on_timeline_changed(self):
        self._request_refresh(kind=_REFRESH_TIMELINE)

    def _on_host_button(self, button: str):
        """托管会话的按钮 -> 转发给源会话 (在回调分发线程上执行, send_button 是异步投递)"""
        logger.debug(f"托管SMTC按钮: {button}")
        self._source.send_button(str(button or ""))

    # ================================================================ 刷新线程
    def _request_refresh(self, force: bool = False, kind: str = _REFRESH_FULL):
        if not self._running:
            return
        self._refresh_queue.put((kind, bool(force)))

    def refresh_info(self):
        """「立即更新信息」按钮: 无视缓存联网刷新 (GUI 线程只投递)"""
        self._request_refresh(force=True)

    def _refresh_loop(self):
        while True:
            item = self._refresh_queue.get()
            if item is None:
                return
            kind, force = item
            # 防抖 + 合并: 连续事件只处理一次, 保持「全量优先、force 优先」
            sleep(REFRESH_DEBOUNCE)
            while True:
                try:
                    nxt = self._refresh_queue.get_nowait()
                except Empty:
                    break
                if nxt is None:
                    return
                nkind, nforce = nxt
                if nkind == _REFRESH_FULL:
                    kind = _REFRESH_FULL
                force = force or nforce
            if not self._running:
                continue
            try:
                if kind == _REFRESH_TIMELINE:
                    self._refresh_timeline()
                else:
                    self._refresh(force)
            except Exception as e:  # noqa: BLE001 - 刷新失败只记录, 不能打死刷新线程
                logger.error(f"刷新SMTC信息失败: {e.__class__.__name__}: {e}", exc_info=True)

    def _refresh_timeline(self):
        """只同步进度/播放状态 (不联网, 不重新读封面)"""
        snapshot = self._source.snapshot(include_thumbnail=False)
        if snapshot is None or not snapshot.is_valid:
            return
        self._host.set_playback_status(snapshot.playback_status)
        timeline = self._timeline_of(snapshot)
        if timeline is not None:
            self._host.update_timeline(*timeline)

    def _refresh(self, force: bool = False):
        """完整刷新: 读源会话信息 -> 后端解析 -> 写托管会话"""
        if not self._running:
            return
        snapshot = self._source.snapshot(include_thumbnail=True)
        if snapshot is None:
            # 瞬时读取失败 (例如 WinRT 5s 超时): 有源且已展示过信息时保持现状, 避免默认信息闪烁
            if self._source.has_source() and self._last_music is not None:
                logger.debug("读取源SMTC快照失败, 保持当前显示")
                return
            self._last_source_key = ""
            self._last_music = None
            self._show_default()
            return
        if not snapshot.is_valid:
            self._last_source_key = ""
            self._last_music = None
            self._show_default()
            return

        source_info = snapshot.to_music_info()
        timeline = self._timeline_of(snapshot)
        source_key = snapshot.unique_key()

        # 同一首歌: 只更新播放状态/进度, 不联网
        if not force and source_key == self._last_source_key and self._last_music is not None:
            self._host.set_playback_status(snapshot.playback_status)
            if timeline is not None:
                self._host.update_timeline(*timeline)
            return

        info, cover = self._resolve_music(snapshot, source_info, force)
        if not self._running:
            # 解析期间插件被停止: 丢弃这次结果 (避免停止后仍写托管会话)
            return
        display = info.mapped(self._display_mapping()).with_length(info.length)
        duration = display.length
        if duration <= 0 and timeline is not None:
            duration = max(0.0, timeline[2] - timeline[0])

        self._host.update(display, cover, snapshot.playback_status,
                          duration=duration, source_timeline=timeline)
        self._last_source_key = source_key
        self._last_music = display
        if cover is not None:
            logger.info(f"更新SMTC信息: {display.title} - {display.artist} "
                        f"(专辑: {display.album_title or '-'}; 封面: {cover.width}x{cover.height})")
        else:
            logger.info(f"更新SMTC信息: {display.title} - {display.artist} (无封面)")

    def _resolve_music(self, snapshot: SourceSnapshot, source_info: MusicInfo,
                       force: bool) -> tuple[MusicInfo, Image.Image | None]:
        """按优先级解析歌曲信息与封面: 相同后端 > 不同后端 > 源SMTC封面"""
        source_cover = self._image_from_bytes(snapshot.thumbnail)
        size = self._cover_target_size()
        use_similarity = bool(self.config.use_cover_similarity)
        for backend in self._backend_order(snapshot.platform):
            try:
                resolved = backend.fetch(source_info, size, source_cover=source_cover,
                                         use_similarity=use_similarity, force_refresh=force)
            except Exception as e:  # noqa: BLE001 - 单个后端失败不应影响后续后端
                logger.error(f"后端 [{backend.platform_id}] 解析失败: {e.__class__.__name__}: {e}")
                continue
            if resolved is None:
                continue
            info = resolved.info.with_length(resolved.info.length or source_info.length)
            logger.debug(f"后端 [{backend.platform_id}] 命中 (缓存: {resolved.from_cache})")
            return info, resolved.cover
        # 所有后端都失败: 回退源 SMTC 缩略图 (不放大, 保持原样)
        logger.warning(f"没有找到可用的高清歌曲信息, 使用源SMTC封面: "
                       f"{source_info.title} - {source_info.artist}")
        return source_info, source_cover

    @staticmethod
    def _timeline_of(snapshot: SourceSnapshot) -> tuple[float, float, float] | None:
        """源会话进度 -> (start, position, end); 平台没提供进度时返回 None"""
        try:
            start = float(snapshot.timeline_start or 0.0)
            position = float(snapshot.timeline_position or 0.0)
            end = float(snapshot.timeline_end or 0.0)
        except (TypeError, ValueError):
            return None
        if end <= 0.0 or end <= start:
            return None
        return start, max(start, min(position, end)), end

    @staticmethod
    def _image_from_bytes(data: bytes | None) -> Image.Image | None:
        if not data:
            return None
        try:
            with Image.open(BytesIO(bytes(data))) as image:
                image.load()
                return image.copy()
        except Exception as e:  # noqa: BLE001 - 源缩略图坏了就当没有
            logger.debug(f"读取源SMTC缩略图失败: {e.__class__.__name__}: {e}")
            return None

    def _show_default(self):
        """没有可用源会话时显示默认信息"""
        self._last_source_key = ""
        self._last_music = None
        if self._host is None:
            return
        self._host.display_default()
        self._host.set_playback_status("stopped")

    # ================================================================ 缓存 / 按钮
    def clear_cache(self):
        """「清除缓存」按钮: 清空所有后端的歌曲信息与封面缓存"""
        conf = wx.MessageBox("确定要清除歌曲信息以及封面缓存吗?", WINDOW_TITLE,
                             wx.YES_NO | wx.ICON_QUESTION)
        if conf != wx.YES:
            return
        for backend in self._backends.values():
            total = max(1, backend.cache_file_count() + 1)
            dialog = wx.ProgressDialog("正在删除缓存", "请稍候...", total, None,
                                       wx.CENTRE | wx.RESIZE_BORDER)
            try:
                for done, count, filename in backend.clear_cache():
                    dialog.Update(done, f"({done}/{count})\n正在删除缓存: {filename}")
            finally:
                dialog.Destroy()
        self._request_refresh(force=True)

    # ================================================================ 快捷方式
    def install_shortcut(self):
        """「安装图标快捷方式」按钮: 让 SMTC 页面显示 [SMTCWrapper] 而不是 [未知应用]"""
        program_dir = r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs"
        temp_dir = expandvars(r"%TEMP%\WinEnchantKit")
        makedirs(temp_dir, exist_ok=True)
        temp_path = join(temp_dir, "SMTCWrapper.lnk")
        target_path = join(program_dir, "SMTCWrapper.lnk")

        target_executable, icon_file = self._shortcut_target()
        try:
            lnk = pylnk3.for_file(target_executable, icon_file=icon_file, icon_index=0)
        except Exception as e:  # noqa: BLE001
            logger.error(f"创建快捷方式对象失败: {e.__class__.__name__}: {e}")
            wx.MessageBox(f"{e.__class__.__name__}: {e}\n创建快捷方式失败", WINDOW_TITLE,
                          wx.OK | wx.ICON_ERROR)
            return
        try:
            lnk.save(target_path)
            wx.MessageBox("创建快捷方式成功！\n记得重启程序哦", "成功！ - ( •̀ ω •́ )✧",
                          wx.OK | wx.ICON_INFORMATION)
            return
        except OSError:
            # 没有管理员权限: 先存到临时目录, 再用提权 bat 复制
            pass
        try:
            lnk.save(temp_path)
        except Exception as e:  # noqa: BLE001
            logger.error(f"保存临时快捷方式失败: {e.__class__.__name__}: {e}")
            wx.MessageBox(f"{e.__class__.__name__}: {e}\n保存临时快捷方式失败", WINDOW_TITLE,
                          wx.OK | wx.ICON_ERROR)
            return
        bat_path = join(temp_dir, "MusicSMTCWrapper-Shortcut-Install.bat")
        with open(bat_path, "w+", encoding="utf-8") as f:
            f.write("\n".join([
                f'move /Y "{temp_path}" "{target_path}"',
            ]))
        try:
            ShellExecuteEx(lpVerb="runas", lpFile=bat_path, nShow=con.SW_HIDE)
            wx.MessageBox("创建快捷方式成功！\n记得重启程序哦", "成功！ - ( •̀ ω •́ )✧",
                          wx.OK | wx.ICON_INFORMATION)
            return
        except Exception as e:  # noqa: BLE001
            logger.error(f"{e.__class__.__name__}: {e} -> 运行快捷方式复制脚本失败")
            ret = wx.MessageBox(f"{e.__class__.__name__}: {e}\n"
                                "运行快捷方式复制脚本失败\n"
                                "要打开 lnk所在目录 和 目标文件夹 吗?",
                                "失败啦~ - ＞︿＜",
                                wx.YES_NO | wx.ICON_ERROR)
        if ret == wx.YES:
            os.startfile(program_dir)
            Thread(target=os.system, args=(f'explorer /select,"{temp_path}"',), daemon=True).start()
            wx.MessageBox("已打开两个文件夹！\n"
                          "请移动lnk至 [C:\\ProgramData\\Microsoft\\Windows\\Start Menu\\Programs]\n"
                          "创建快捷方式成功！(虽说是保存到别处\n"
                          "记得重启程序哦",
                          "成功！ - (*^▽^*)", wx.OK | wx.ICON_INFORMATION)

    @staticmethod
    def _shortcut_target() -> tuple[str, str]:
        """快捷方式指向的可执行文件 + 图标文件。

        * 源码运行: 指向当前 Python 解释器, 图标用仓库 assets/icon.ico (WinEnchantKit 图标)
        * 冻结成 exe: 指向程序自身并直接使用自身图标
        """
        if getattr(sys, "frozen", False):
            return sys.executable, sys.executable
        orig_argv = list(getattr(sys, "orig_argv", []) or [""])
        exec_name = "pythonw.exe" if orig_argv and "pythonw.exe" in str(orig_argv[0]) else "python.exe"
        target_executable = join(sys.base_prefix, exec_name)
        icon_file = join(dirname(dirname(_PLUGIN_DIR)), "assets", "icon.ico")
        if not isfile(icon_file):
            logger.warning(f"找不到 WinEnchantKit 图标: {icon_file}, 使用解释器自身图标")
            icon_file = sys.executable
        return target_executable, icon_file
