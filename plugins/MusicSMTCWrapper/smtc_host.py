# -*- coding: utf-8 -*-
"""SMTC 宿主: 负责插件自建 SMTC 会话与源会话 (酷狗/萌音等) 的全部细节。

职责边界:
* 本模块只与 WinRT SMTC / MediaPlayer 打交道, 不关心歌曲从哪来、封面怎么搜;
* 由 Plugin 持有并驱动 (``Plugin.smtc_host``)。

对外接口 (Plugin 只依赖这些):
    bind(config)                     同步配置 (allways_playing / default_* / exchange_title2album)
    create() / destroy()             建/删自建 SMTC 会话
    find_source() / release_source() 发现/断开源会话
    check_source_valid()             源会话是否仍有有效歌曲
    get_source_status()              源会话播放状态
    get_source_info()                源会话 SessionMediaProperties (交给 MusicBackend 解析)
    is_kugou_lyric_source()          源会话是否为 MoeKoeMusic 萌音 (进度条/空格修正用)
    display(music, cover_image)      把歌曲信息 (含封面) 写到自建 SMTC 会话
    display_default()                写入配置里的默认信息
    update_timeline()                进度条透传 (仅萌音)
    set_playback_status()            播放状态
    register_button_handler(handler) 注册自建会话的按钮回调
    handle_button(args)              SMTC 按钮 -> 源会话操作
"""
from __future__ import annotations

from io import BytesIO
from typing import Any, Callable, Union

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

from _smtc_backend_base import (
    StandardMusicInfo,
    plugin_logger,
    wait_result,
)

from typing import cast as type_cast

logger = plugin_logger


class SMTCHost:
    smtc: SMTControls
    """插件自建 SMTC 会话 + 源会话的宿主"""

    # 自建会话的 app_media_id (Windows 用它区分不同会话, 固定值保证外部能识别本插件)
    APP_MEDIA_ID = "hd_cover_kugou"

    def __init__(self):
        self.config: dict[str, Any] = {}
        self.sessions: SessionManager | None = None
        self.smtc: SMTControls = type_cast(SMTControls, None)
        self.player: MediaPlayer | None = None
        self.source: Session | None = None
        self.source_app_id: str | None = None
        self.button_handler: Callable[[Any, SMTCButtonPressedEventArgs], None] | None = None
        self.fake_playing = False
        # 保持 InMemoryRandomAccessStream 存活, 否则 Windows 侧读到的是已释放的流
        self._frames: list[Any] = []

    # ------------------------------------------------------------ 配置同步
    def bind(self, config: Any):
        """配置更新入口: Plugin 在每次刷新前调用, 保证配置立即生效"""
        if isinstance(config, dict):
            self.config = config
        else:
            self.config = {name: getattr(config, name) for name in getattr(config, "names", [])}
            self.config.update(dict(config))

    @property
    def allways_playing(self) -> bool:
        return bool(self.config.get("allways_playing", False))

    @property
    def exchange_title2album(self) -> bool:
        return bool(self.config.get("exchange_title2album", False))

    # ------------------------------------------------------------ 自建会话
    def create(self):
        """创建自建 SMTC 会话 (原 backend\\base_backend.py::create_smtc)"""
        if self.sessions is None:
            self.sessions = wait_result(SessionManager.request_async())
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
        """销毁自建会话并断开源会话 (原 main.py::stop_raw 中对象置空的部分)"""
        self.smtc: SMTControls = None
        self.player = None
        self.release_source()
        self._frames.clear()

    def register_button_handler(self, handler: Callable[[Any, SMTCButtonPressedEventArgs], None]):
        """记录按钮回调; 若会话已存在则立即注册"""
        self.button_handler = handler
        if self.smtc is not None:
            self.smtc.add_button_pressed(handler)

    # ------------------------------------------------------------ 源会话
    def find_source(self) -> Session:
        """在系统会话列表中找源平台会话。

        匹配酷狗 + MoeKoeMusic 萌音 (萌音托管酷狗播放, 因此也纳入源会话范围)。
        """
        if self.sessions is None:
            self.sessions = wait_result(SessionManager.request_async())
        for session in self.sessions.get_sessions():
            app_user_model_id = session.source_app_user_model_id
            name = (app_user_model_id or "").lower()
            if name in ("kugou", "kugou.exe") or "moekoe" in name:
                logger.info("找到 Kugou SMTC会话")
                self.set_source(session, app_user_model_id)
                return session
        raise RuntimeError("找不到酷狗SMTC会话")

    def set_source(self, session: Session, app_id: str | None = None):
        self.source = session
        self.source_app_id = app_id

    @property
    def has_source(self) -> bool:
        return self.source is not None

    def release_source(self):
        self.source = None
        self.source_app_id = None
        self.fake_playing = False

    def is_kugou_lyric_source(self) -> bool:
        """源会话是否为 MoeKoeMusic 萌音 (原 main.py::is_moe_koe_music)"""
        if self.source is None:
            return False
        return "MoeKoe" in (self.source.source_app_user_model_id or "")

    def check_source_valid(self) -> bool:
        """源会话是否仍在播放有效歌曲。

        必须 title + artist 都非空: 源平台暂停/切歌瞬间会短暂返回空属性,
        此时不能覆盖自建会话 (旧实现只检查 title+artist+album_title 拼接非空, 这里收紧为 artist 也有效,
        因为搜索封面必须要有 artist)。
        """
        if self.source is None:
            return False
        try:
            info = wait_result(self.source.try_get_media_properties_async())
        except Exception as e:
            logger.warning(f"读取源SMTC信息失败: {e.__class__.__name__}: {e}")
            return False
        return bool((info.title or "").strip()) and bool((info.artist or "").strip())

    def get_source_info(self) -> SessionMediaProperties:
        """取源会话媒体属性 (交给 MusicBackend 解析)"""
        if self.source is None:
            raise RuntimeError("源SMTC会话不存在")
        return wait_result(self.source.try_get_media_properties_async())

    def get_source_status(self) -> MediaPlaybackStatus:
        """源会话播放状态 (用于同步自建会话)"""
        if self.source is None:
            return MediaPlaybackStatus.STOPPED
        # playback_status是GlobalSystemMediaTransportControlsSessionPlaybackStatus对象，需要转换为MediaPlaybackStatus对象
        return getattr(MediaPlaybackStatus,
                self.source.get_playback_info().playback_status.name)

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
        """把标准化歌曲信息写入自建 SMTC 会话 (原 main.py::update_info 的写入部分)。

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
        # 萌音会话不需要前置空格; 其它平台靠这个额外空格把高清会话排在后面, 避免抢占
        start_fix = " " if not self.is_kugou_lyric_source() else ""
        updater = self.smtc.display_updater
        updater.app_media_id = self.APP_MEDIA_ID
        updater.type = MediaPlaybackType.MUSIC
        updater.music_properties.title = title
        updater.music_properties.artist = artist
        updater.music_properties.album_title = album_title
        updater.music_properties.album_artist = start_fix + album_artist
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

    def update_timeline(self):
        """把源会话的进度条透传给自建会话 (仅萌音需要, 原 main.py::on_timeline_update)"""
        if self.smtc is None or self.source is None:
            return
        if not self.is_kugou_lyric_source():
            return
        global_prop = self.source.get_timeline_properties()
        prop = TimelineProperties()
        prop.start_time = global_prop.start_time
        prop.end_time = global_prop.end_time
        prop.position = global_prop.position
        prop.max_seek_time = global_prop.max_seek_time
        prop.min_seek_time = global_prop.min_seek_time
        self.smtc.update_timeline_properties(prop)

    # ------------------------------------------------------------ 按钮
    def handle_button(self, args: SMTCButtonPressedEventArgs):
        """SMTC 按钮 -> 源会话操作 (原 main.py::on_button_press 的会话操作部分)"""
        if self.source is None:
            return
        button = args.button
        logger.info(f"用户按下按钮: {button.name}")
        if button == SMTCButton.PREVIOUS:
            wait_result(self.source.try_skip_previous_async())
        elif button == SMTCButton.NEXT:
            wait_result(self.source.try_skip_next_async())
        elif button == SMTCButton.PLAY:
            if not self.allways_playing:
                self.set_playback_status(MediaPlaybackStatus.PLAYING)
            self.source.try_play_async()
        elif button == SMTCButton.PAUSE and self.fake_playing:
            # 「永不暂停」模式下用户按暂停时我们假装暂停一次, 再按即恢复播放
            self.source.try_play_async()
            self.fake_playing = False
        elif button == SMTCButton.PAUSE:
            if not self.allways_playing:
                self.set_playback_status(MediaPlaybackStatus.PAUSED)
            self.source.try_pause_async()
            if self.allways_playing:
                self.fake_playing = True
