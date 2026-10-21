# -*- coding: utf-8 -*-
"""SMTC 层: 源会话管理 (SourceManager) + 插件自建会话 (SMTCHost)。

职责边界:
* :class:`SourceManager` —— 源平台会话: 发现 / 筛选 / 事件注册注销 / 读取信息 / 转发按钮命令。
  **所有源会话的 SMTC 操作都严格跑在它自己的同一个工作线程上** (一个命令队列 + 一个 worker),
  外部调用只是投递命令; WinRT 事件回调也只负责投递, 不直接碰会话。
* :class:`SMTCHost` —— 插件自建的 SMTC 会话 (MediaPlayer + DisplayUpdater)。

两者由 ``Plugin`` 同时持有, 互相之间只通过**回调**联系; 回调在插件启动时一次性设置。
"""
from __future__ import annotations

import threading
from io import BytesIO
from queue import Empty, Queue
from typing import Any, Callable

from PIL import Image
from winsdk.windows.media import (
    MediaPlaybackStatus,
    MediaPlaybackType,
    SystemMediaTransportControls as SMTControls,
    SystemMediaTransportControlsButton as SMTCButton,
    SystemMediaTransportControlsButtonPressedEventArgs as SMTCButtonPressedEventArgs,
    SystemMediaTransportControlsTimelineProperties as TimelineProperties,
)
from winsdk.windows.media.control import (
    GlobalSystemMediaTransportControlsSession as Session,
    GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    GlobalSystemMediaTransportControlsSessionMediaProperties as SessionMediaProperties,
)
from winsdk.windows.media.playback import MediaPlayer
from winsdk.windows.storage.streams import (
    DataWriter,
    InMemoryRandomAccessStream,
    RandomAccessStreamReference,
)

from smtc_backend.base import (
    StandardMusicInfo,
    plugin_logger,
    wait_result,
)

logger = plugin_logger

# 各平台源会话的匹配标记 (source_app_user_model_id 小写子串)
PLATFORM_SOURCES = {
    "kugou": ("kugou", "moekoe"),
    "netease": ("netease", "cloudmusic"),
}


class SourceManager:
    """源平台 SMTC 会话管理器 (单线程)。

    对外回调 (由 Plugin 在启动时设置, 之后不再变):
        on_changed()            会话列表发生变化 (用于重新筛选 / 判断失效)
        on_updated()            源会话歌曲信息有更新
        on_timeline_changed()   源会话进度条有更新

    对外接口:
        start() / stop()        启动/停止工作线程与事件注册
        switch_platform(id)     切换要筛选的歌曲平台
        release()               主动断开当前源会话
        find()                  在当前会话列表里筛选并接管源会话 (返回是否找到)
        has_source / source_id / source_platform
        valid()                 当前源会话是否仍有有效歌曲
        info() / status() / timeline_properties()
        handle_button(args)     插件会话按钮 -> 源会话操作
        set_plugin_playback(status)   同步插件会话播放状态
        set_fake_playing(flag)
    """

    def __init__(self):
        self._config: dict[str, Any] = {}
        self._manager: SessionManager | None = None
        self._session: Session | None = None
        self._source_id: str | None = None
        self._platform: str = "kugou"
        self.fake_playing = False

        # 回调 (Plugin 启动时设置)
        self.on_changed: Callable[[], None] = lambda: None
        self.on_updated: Callable[[], None] = lambda: None
        self.on_timeline_changed: Callable[[], None] = lambda: None
        # 由 Plugin 注入: 把播放状态写到插件会话
        self.on_plugin_playback: Callable[[MediaPlaybackStatus], None] = lambda status: None

        # 单线程: 所有源会话操作都在这个 worker 上串行执行
        self._queue: Queue = Queue()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True,
                                        name="MusicSMTCWrapper-SourceManager")
        self._pending: dict[int, dict[str, Any]] = {}
        self._seq = 0
        self._pending_lock = threading.Lock()
        self._running = False

        # 事件 token (只用于注销)
        self._manager_token = None
        self._playback_token = None
        self._timeline_token = None

    # ================================================================ 生命周期
    def start(self, register_events: bool = True):
        """启动工作线程; 需要时同时注册会话列表变更事件"""
        if not self._running:
            self._running = True
            self._worker.start()
        if register_events:
            self._register_manager_events()

    def stop(self):
        """注销全部事件并停止工作线程 (退出前清空队列)"""
        self._unregister_session_events()
        self._unregister_manager_events()
        self._session = None
        self._source_id = None
        self.fake_playing = False
        if not self._running:
            return
        # 用哨兵结束 worker 循环, 并等它真正退出 (避免线程残留)
        self._running = False
        self._queue.put((None, None, None))
        self._worker.join(timeout=3.0)

    def _register_manager_events(self):
        if self._manager is None:
            self._manager = wait_result(SessionManager.request_async())
        if self._manager_token is None:
            self._manager_token = self._manager.add_sessions_changed(
                lambda *_: self.on_changed())

    def _unregister_manager_events(self):
        if self._manager is not None and self._manager_token is not None:
            try:
                self._manager.remove_sessions_changed(self._manager_token)
            except Exception as e:  # noqa: BLE001 - 注销失败不该阻断停止流程
                logger.warning(f"注销会话列表事件失败: {e.__class__.__name__}: {e}")
        self._manager_token = None

    def _register_session_events(self):
        """接管源会话后注册它的信息/进度条事件 (幂等)"""
        if self._session is None:
            return
        self._unregister_session_events()
        self._playback_token = self._session.add_playback_info_changed(
            lambda *_: self.on_updated())
        self._timeline_token = self._session.add_timeline_properties_changed(
            lambda *_: self.on_timeline_changed())

    def _unregister_session_events(self):
        if self._session is not None:
            for token, remover in ((self._playback_token, self._session.remove_playback_info_changed),
                                   (self._timeline_token, self._session.remove_timeline_properties_changed)):
                if token is None:
                    continue
                try:
                    remover(token)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"注销源会话事件失败: {e.__class__.__name__}: {e}")
        self._playback_token = None
        self._timeline_token = None

    # ================================================================ 单线程调度
    def _submit(self, func: Callable, *args) -> int:
        with self._pending_lock:
            self._seq += 1
            job_id = self._seq
            self._pending[job_id] = {"done": threading.Event(), "result": None, "error": None}
        self._queue.put((job_id, func, args))
        return job_id

    def _call_sync(self, func: Callable, *args):
        """投递命令并等待其在本线程执行完成 (调用方可以是任意线程)"""
        if not self._running:
            return None
        job_id = self._submit(func, *args)
        with self._pending_lock:
            job = self._pending[job_id]
        # 带超时: worker 出意外时也不能把调用方 (可能是 UI 线程) 永久卡住
        if not job["done"].wait(timeout=5.0):
            with self._pending_lock:
                self._pending.pop(job_id, None)
            logger.error(f"源会话操作超时: {getattr(func, '__name__', func)}")
            return None
        with self._pending_lock:
            self._pending.pop(job_id, None)
        if job["error"] is not None:
            raise job["error"]
        return job["result"]

    def _call_async(self, func: Callable, *args):
        """只投递命令, 不等待 (事件回调/按钮这类不需要结果的地方)"""
        if self._running:
            self._submit(func, *args)

    def _worker_loop(self):
        """唯一的源会话工作线程: 串行执行命令, 直到收到停止哨兵"""
        while True:
            try:
                job_id, func, args = self._queue.get(timeout=0.2)
            except Empty:
                if not self._running:
                    return
                continue
            if job_id is None:
                return
            with self._pending_lock:
                job = self._pending.get(job_id)
            try:
                result = func(*args)
                if job is not None:
                    job["result"] = result
            except Exception as e:  # noqa: BLE001 - 命令失败要回传给调用方
                logger.error(f"源会话操作失败 [{getattr(func, '__name__', func)}]: {e.__class__.__name__}: {e}")
                if job is not None:
                    job["error"] = e
            finally:
                if job is not None:
                    job["done"].set()

    # ================================================================ 配置
    def bind(self, config: Any, platform: str = None):
        """同步配置 (Plugin 在启动 / 配置变更前调用); platform 为要筛选的歌曲平台"""
        if isinstance(config, dict):
            self._config = config
        else:
            self._config = {name: getattr(config, name) for name in getattr(config, "names", [])}
            self._config.update(dict(config))
        if platform:
            self._platform = str(platform)
        else:
            self._platform = str(self._config.get("music_platform", self._platform))

    def switch_platform(self, platform: str):
        """切换歌曲平台: 先注销旧会话事件并断开, 再按新平台重新筛选"""
        self._call_sync(self._cmd_switch_platform, str(platform))

    def _cmd_switch_platform(self, platform: str) -> bool:
        # 注意: 这里已经在 worker 线程上, 必须直接调内部命令, 不能再走 find() (那会投递给自己 -> 死锁)
        if platform != self._platform and self._session is not None:
            self._unregister_session_events()
            self._session = None
            self._source_id = None
        self._platform = platform
        return self._cmd_find()

    # ================================================================ 筛选 / 断开
    def find(self) -> bool:
        """筛选并接管源会话 (在 worker 线程上执行)"""
        return bool(self._call_sync(self._cmd_find))

    def release(self):
        """主动断开当前源会话"""
        self._call_sync(self._cmd_release)

    def _cmd_find(self) -> bool:
        if self._manager is None:
            self._manager = wait_result(SessionManager.request_async())
        markers = PLATFORM_SOURCES.get(self._platform, PLATFORM_SOURCES["kugou"])
        for session in self._manager.get_sessions():
            source_id = session.source_app_user_model_id
            name = (source_id or "").lower()
            logger.debug(f"SMTC会话: {source_id}")
            if any(marker in name for marker in markers):
                logger.info(f"找到源 SMTC会话: {source_id}")
                self._session = session
                self._source_id = source_id
                self.fake_playing = False
                self._register_session_events()
                return True
        logger.debug(f"没有找到 [{self._platform}] 的源会话")
        return False

    def _cmd_release(self):
        self._unregister_session_events()
        self._session = None
        self._source_id = None
        self.fake_playing = False

    # ================================================================ 状态读取
    @property
    def has_source(self) -> bool:
        return self._session is not None and self._session.source_app_user_model_id != ""

    @property
    def source_id(self) -> str | None:
        return self._source_id

    @property
    def source_platform(self) -> str:
        return self._platform

    def valid(self) -> bool:
        """当前源会话是否仍有有效歌曲 (title + artist 都非空)"""
        return bool(self._call_sync(self._cmd_valid))

    def _cmd_valid(self) -> bool:
        if self._session is None:
            return False
        try:
            info = wait_result(self._session.try_get_media_properties_async())
        except Exception as e:  # noqa: BLE001
            logger.warning(f"读取源SMTC信息失败: {e.__class__.__name__}: {e}")
            return False
        return bool((info.title or "").strip()) and bool((info.artist or "").strip())

    def info(self) -> SessionMediaProperties | None:
        """源会话媒体属性 (交给 MusicBackend 解析); 没有会话返回 None"""
        return self._call_sync(self._cmd_info)

    def _cmd_info(self) -> SessionMediaProperties | None:
        if self._session is None:
            return None
        return wait_result(self._session.try_get_media_properties_async())

    def status(self) -> MediaPlaybackStatus:
        """源会话播放状态 (已转成 MediaPlaybackStatus)"""
        return self._call_sync(self._cmd_status) or MediaPlaybackStatus.STOPPED

    def _cmd_status(self) -> MediaPlaybackStatus:
        if self._session is None:
            return MediaPlaybackStatus.STOPPED
        # playback_status 是 GlobalSystemMediaTransportControlsSessionPlaybackStatus, 按名字转成 MediaPlaybackStatus
        return getattr(MediaPlaybackStatus, self._session.get_playback_info().playback_status.name)

    def timeline_properties(self):
        """源会话进度条属性"""
        return self._call_sync(self._cmd_timeline)

    def _cmd_timeline(self):
        if self._session is None:
            return None
        return self._session.get_timeline_properties()

    # ================================================================ 命令转发
    def handle_button(self, args: SMTCButtonPressedEventArgs):
        """插件会话按钮 -> 源会话操作 (异步投递, 不阻塞调用线程)"""
        self._call_async(self._cmd_button, args)

    def _cmd_button(self, args: SMTCButtonPressedEventArgs):
        if self._session is None:
            return
        button = args.button
        logger.info(f"用户按下按钮: {button.name}")
        if button == SMTCButton.PREVIOUS:
            wait_result(self._session.try_skip_previous_async())
        elif button == SMTCButton.NEXT:
            wait_result(self._session.try_skip_next_async())
        elif button == SMTCButton.PLAY:
            if not self.allways_playing:
                self.set_plugin_playback(MediaPlaybackStatus.PLAYING)
            self._session.try_play_async()
        elif button == SMTCButton.PAUSE and self.fake_playing:
            # 「永不暂停」模式下用户按暂停时我们假装暂停一次, 再按即恢复播放
            self._session.try_play_async()
            self.fake_playing = False
        elif button == SMTCButton.PAUSE:
            if not self.allways_playing:
                self.set_plugin_playback(MediaPlaybackStatus.PAUSED)
            self._session.try_pause_async()
            if self.allways_playing:
                self.fake_playing = True

    # ================================================================ 与插件会话的协作
    @property
    def allways_playing(self) -> bool:
        return bool(self._config.get("allways_playing", False))

    @property
    def source_start_fix(self) -> str:
        """插件会话专辑艺术家前缀: 萌音 (MoeKoe) 不加空格, 其它平台加 (避免抢占)"""
        return "" if "moekoe" in (self._source_id or "").lower() else " "

    def set_plugin_playback(self, status: MediaPlaybackStatus):
        """同步插件会话播放状态 (回调由 Plugin 注入)"""
        self.on_plugin_playback(status)


class SMTCHost:
    """插件自建 SMTC 会话 (MediaPlayer + DisplayUpdater)。"""

    # 自建会话的 app_media_id (Windows 用它区分不同会话, 固定值保证外部能识别本插件)
    APP_MEDIA_ID = "hd_cover_kugou"

    def __init__(self):
        self.config: dict[str, Any] = {}
        self.smtc: SMTControls | None = None
        self.player: MediaPlayer | None = None
        # 当前源会话信息 (由 Plugin 同步; 只用于专辑艺术家空格修正)
        self.source_id: str | None = None
        self.button_handler: Callable[[Any, SMTCButtonPressedEventArgs], None] | None = None
        # 保持 InMemoryRandomAccessStream 存活, 否则 Windows 侧读到的是已释放的流
        self._frames: list[Any] = []

    # ------------------------------------------------------------ 配置同步
    def bind(self, config: Any, source_id: str | None = None):
        """配置更新入口 (Plugin 在启动 / 配置变更 / 源会话变化时调用)"""
        if isinstance(config, dict):
            self.config = config
        else:
            self.config = {name: getattr(config, name) for name in getattr(config, "names", [])}
            self.config.update(dict(config))
        self.source_id = source_id

    @property
    def allways_playing(self) -> bool:
        return bool(self.config.get("allways_playing", False))

    @property
    def exchange_title2album(self) -> bool:
        return bool(self.config.get("exchange_title2album", False))

    @property
    def source_start_fix(self) -> str:
        """专辑艺术家前缀: 萌音 (MoeKoe) 不加空格, 其它平台加 (避免抢占)"""
        return "" if "moekoe" in (self.source_id or "").lower() else " "

    # ------------------------------------------------------------ 自建会话
    def create(self):
        """创建自建 SMTC 会话 (原 backend\\base_backend.py::create_smtc)"""
        logger.info("创建SMTC对象")
        self.player = MediaPlayer()
        self.player.command_manager.is_enabled = False
        self.smtc = self.player.system_media_transport_controls
        self.smtc.is_enabled = True
        self.smtc.is_play_enabled = True
        self.smtc.is_pause_enabled = True
        self.smtc.is_next_enabled = True
        self.smtc.is_previous_enabled = True
        self.smtc.playback_status = MediaPlaybackStatus.STOPPED
        if self.button_handler is not None:
            self.smtc.add_button_pressed(self.button_handler)
        return self.smtc

    def destroy(self):
        """销毁自建会话"""
        self.smtc = None
        self.player = None
        self._frames.clear()

    def set_button_callback(self, handler: Callable[[Any, SMTCButtonPressedEventArgs], None]):
        """设置按钮回调 (Plugin 启动时一次性注入); 若会话已存在则立即注册"""
        self.button_handler = handler
        if self.smtc is not None:
            self.smtc.add_button_pressed(handler)

    # ------------------------------------------------------------ 自建会话内容
    @property
    def playback_status(self) -> MediaPlaybackStatus:
        if self.smtc is None:
            return MediaPlaybackStatus.STOPPED
        return self.smtc.playback_status

    def set_playback_status(self, status: MediaPlaybackStatus):
        if self.smtc is not None and self.smtc.playback_status != status:
            self.smtc.playback_status = status

    def _stream_reference(self, image: Image.Image) -> RandomAccessStreamReference:
        """把 PIL 图片转成 SMTC 需要的 RandomAccessStreamReference"""
        buffer = BytesIO()
        image.save(buffer, "PNG")
        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream)
        writer.write_bytes(buffer.getvalue())
        wait_result(writer.store_async())
        writer.detach_stream()
        # 流对象必须存活到 Windows 读完, 否则封面会变成空白
        self._frames.append(stream)
        if len(self._frames) > 8:
            del self._frames[:-8]
        return RandomAccessStreamReference.create_from_stream(stream)

    def display(self, music: StandardMusicInfo, cover_image: Image.Image | None = None):
        """把标准化歌曲信息写入自建 SMTC 会话。

        封面直接取 ``music.cover`` (PIL 图, 由 backend 在 get_music_info 阶段备好);
        ``music.has_cover`` 为 False 时不覆盖缩略图 —— 即回退保留源平台的低清封面。
        """
        if self.smtc is None:
            logger.warning("SMTC会话未创建, 忽略信息更新")
            return
        if cover_image is None and getattr(music, "has_cover", True):
            cover_image = music.cover
        title, artist = music.title, music.artist
        album_title, album_artist = music.album_title, music.album_artist
        if self.exchange_title2album:
            artist, album_artist = album_artist, artist
        updater = self.smtc.display_updater
        updater.app_media_id = self.APP_MEDIA_ID
        updater.type = MediaPlaybackType.MUSIC
        updater.music_properties.title = title
        updater.music_properties.artist = artist
        updater.music_properties.album_title = album_title
        updater.music_properties.album_artist = self.source_start_fix + album_artist
        if cover_image is not None:
            updater.thumbnail = self._stream_reference(cover_image)
        updater.update()

    def display_default(self):
        """写入配置中的默认信息 (原 main.py::default)"""
        music = StandardMusicInfo(
            title=str(self.config.get("default_title", "Title")),
            artist=str(self.config.get("default_artist", "Artist")),
            album_title=str(self.config.get("default_album_title", "Album Title")),
            album_artist=str(self.config.get("default_album_artist", "Album Artist")),
            length=0.0,
            cover=None,
            has_cover=False,
        )
        self.display(music)
        logger.debug(f"SMTC 已恢复默认信息: {music.title} - {music.artist}")

    def update_timeline(self, global_prop):
        """把源会话进度条透传给自建会话 (仅萌音需要)"""
        if self.smtc is None or global_prop is None:
            return
        prop = TimelineProperties()
        prop.start_time = global_prop.start_time
        prop.end_time = global_prop.end_time
        prop.position = global_prop.position
        prop.max_seek_time = global_prop.max_seek_time
        prop.min_seek_time = global_prop.min_seek_time
        self.smtc.update_timeline_properties(prop)
