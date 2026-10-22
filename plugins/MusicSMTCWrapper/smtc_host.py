# -*- coding: utf-8 -*-
"""插件托管的 SMTC 会话 (MediaPlayer + SystemMediaTransportControls)。

职责边界
--------
本模块只负责**插件自己创建的那个 SMTC 会话**: 创建 / 销毁、写入歌曲信息与封面、
写播放状态、写进度条、把系统按钮事件转发给外部回调。

线程模型 (硬性要求, 见 ARCHITECTURE 第 3 节与第 10 节)
-----------------------------------------------------
* **唯一 WinRT worker 线程**: :class:`MediaPlayer` 的创建、SMTC 的全部属性写入、
  按钮事件注册/注销、异步 WinRT 操作等待, 都只在这个线程上执行;
  外部公开方法只是把命令投递到命令队列 (需要结果的方法同步等待, 超时 5s)。
* **独立回调分发线程**: SMTC 的 ``button_pressed`` 回调里**只**把按钮名投递到按钮队列,
  由该线程调用外部设置的回调; 外部回调因此可以安全地调用本模块的公开方法, 也不会
  把 WinRT 事件线程卡住。
* worker 的循环本身兼做「自托管进度」的计时器: 每 0.1s 醒一次, 播放中按
  ``当前时刻 + 基准位置`` 推进内部 position, 每 ~0.5s 写回托管会话一次 ——
  这样所有 WinRT 调用仍然只发生在 worker 线程上。

进度语义 (ARCHITECTURE 10.2)
----------------------------
1. ``update(..., source_timeline=(start, position, end))`` 且 ``end > start``:
   原样镜像源会话进度, 并关闭自托管计时;
2. 否则 ``configure(self_host_progress=True)`` 且 ``duration > 0``:
   自托管 —— 换歌重置基准, 播放中推进, 暂停/停止时冻结;
3. 都不满足: 写 ``(0, 0, duration)`` 的静态进度 (``duration`` 为 0 时也写一次 0/0/0)。

``never_pause=True`` 时托管会话的播放状态恒为 ``PLAYING``, 但内部仍记录源会话状态
(``_source_status``), 自托管进度会按源状态冻结/推进。
"""
from __future__ import annotations

import logging
import math
import threading
import time
from datetime import timedelta
from io import BytesIO
from queue import Empty, Queue
from typing import Any, Callable

from PIL import Image
from winsdk.windows.foundation import AsyncStatus
from winsdk.windows.media import (
    MediaPlaybackStatus,
    MediaPlaybackType,
    SystemMediaTransportControls as SMTControls,
    SystemMediaTransportControlsTimelineProperties as TimelineProperties,
)
from winsdk.windows.media.playback import MediaPlayer
from winsdk.windows.storage.streams import (
    DataWriter,
    InMemoryRandomAccessStream,
    RandomAccessStreamReference,
)

from .music_base import LOGGER_NAME, MusicInfo

logger = logging.getLogger(LOGGER_NAME)

# 托管会话的 app_media_id: Windows 用它区分不同应用创建的会话, 固定值保证外部可识别
APP_MEDIA_ID = "music_smtc_wrapper"
# 无源会话时写入的默认信息
DEFAULT_MUSIC = MusicInfo("Title", "Artist", "Album Title", "Album Artist", 0.0)

# 同步命令等待上限 (秒): worker 出意外时不能把调用方 (可能是 UI 线程) 永久卡住
COMMAND_TIMEOUT = 5.0
# worker 循环的轮询间隔 (秒), 同时也是自托管进度的推进精度
WORKER_POLL_INTERVAL = 0.1
# 自托管进度写回托管会话的最小间隔 (秒)
SELF_HOST_WRITE_INTERVAL = 0.5
# InMemoryRandomAccessStream 最多保留的个数: 流对象必须存活到 Windows 读完, 否则封面空白
MAX_FRAME_STREAMS = 8


def wait_result(operation: Any, timeout: float = COMMAND_TIMEOUT) -> Any:
    """轮询等待一个 winsdk 的 IAsyncOperation 完成并取回结果。

    与旧实现一致地轮询 ``status``, 但增加了超时保护: 异步操作卡死时抛
    :class:`TimeoutError`, 避免 worker 线程被永久占用。
    """
    if operation is None:
        return None
    deadline = time.monotonic() + float(timeout)
    while operation.status != AsyncStatus.COMPLETED:
        if operation.status in (AsyncStatus.ERROR, AsyncStatus.CANCELED):
            raise RuntimeError(f"异步操作未成功完成: {operation.status}")
        if time.monotonic() > deadline:
            raise TimeoutError(f"异步操作等待超时 ({timeout:.1f}s)")
        time.sleep(0.02)
    return operation.get_results()


class SMTCHost:
    """插件自建的 SMTC 会话 (MediaPlayer + DisplayUpdater)。

    公开接口见 ARCHITECTURE 10.1; 所有公开方法都可以从任意线程调用:

    * ``set_button_callback`` / ``configure`` 只改配置, 不碰 WinRT;
    * ``create`` / ``destroy`` / ``update`` / ``update_timeline`` /
      ``set_playback_status`` / ``display_default`` 投递到 worker 串行执行
      (create/update 等同步等待结果, 超时 5s 后记 error 并返回);
    * :attr:`playback_status` 读取 worker 缓存下来的托管会话状态 (小写枚举名),
      属性访问不做任何等待, 因此可以在任意线程使用。

    ``destroy()`` 之后所有公开方法都安全空转 (不抛异常、不等待); 再次 ``create()``
    会重新拉起 worker 与回调分发线程, 可以反复「创建 → 销毁」。
    """

    # ================================================================ 生命周期
    def __init__(self) -> None:
        # ---- 配置 (由 main.py 在 create 前/后设置) ----
        self._never_pause: bool = False
        self._self_host_progress: bool = False
        self._button_callback: Callable[[str], None] | None = None
        self._callback_lock = threading.Lock()

        # ---- WinRT 对象 (只在 worker 线程上读写; 其他线程不要碰) ----
        self._player: MediaPlayer | None = None
        self._smtc: SMTControls | None = None
        self._button_token: Any = None
        # 保持 InMemoryRandomAccessStream 存活, 否则 Windows 侧读到的是已释放的流
        self._frame_streams: list[Any] = []

        # ---- 命令队列 + 唯一 worker ----
        self._queue: Queue[Any] = Queue()
        self._worker: threading.Thread | None = None
        self._running: bool = False
        self._pending: dict[int, dict[str, Any]] = {}
        self._pending_lock = threading.Lock()
        self._seq: int = 0

        # ---- 按钮回调分发线程 ----
        self._button_queue: Queue[Any] = Queue()
        self._callback_thread: threading.Thread | None = None

        # ---- 托管会话状态缓存 (worker 写, 其他线程只读; 供 playback_status / 自测) ----
        self._playback_status: str = "stopped"   # 托管会话实际写入的状态
        self._source_status: str = "stopped"     # 源会话状态 (never_pause 时仅内部使用)
        self._timeline_start: float = 0.0
        self._timeline_position: float = 0.0
        self._timeline_end: float = 0.0
        self._self_host_active: bool = False
        self._self_host_playing: bool = False
        self._self_host_base: float = 0.0        # 基准位置 (秒)
        self._self_host_ref: float = 0.0         # 基准时刻 (time.monotonic)
        self._self_host_last_write: float = 0.0
        self._last_song_key: str = ""

    # ================================================================ 配置接口
    def set_button_callback(self, callback: Callable[[str], None] | None) -> None:
        """设置按钮回调 (main.py 在 create 前赋值; 由回调分发线程调用)。

        ``callback`` 收到的参数是 WinRT 按钮名的小写形式 (``"play"`` / ``"pause"`` /
        ``"next"`` / ``"previous"`` / ...)。未设置回调时按钮事件被忽略。
        """
        with self._callback_lock:
            self._button_callback = callback

    def configure(self, *, never_pause: bool = False, self_host_progress: bool = False) -> None:
        """配置托管会话行为 (可随时调用, 立即生效)。

        * ``never_pause=True``: 托管会话状态恒为 PLAYING (内部仍记录源状态);
        * ``self_host_progress=True``: 源会话没提供进度时, 用「时长 + 状态 + 计时器」自托管。
        """
        self._never_pause = bool(never_pause)
        self._self_host_progress = bool(self_host_progress)
        self._call_sync(self._cmd_apply_config)

    # ================================================================ 创建 / 销毁
    def create(self) -> bool:
        """在 worker 上创建 MediaPlayer + SMTC 会话 (同步等待)。

        重复调用是幂等的; 返回 True 表示会话可用。
        """
        self._start_threads()
        return bool(self._call_sync(self._cmd_create))

    def destroy(self) -> None:
        """在 worker 上销毁托管会话并停止内部线程 (同步等待; 可重复调用)。"""
        if threading.current_thread() is self._worker:
            # 理论上不会从 worker 调用; 防御性处理, 避免自己等自己
            self._cmd_destroy()
            self._stop_threads()
            return
        if self._running:
            self._call_sync(self._cmd_destroy)
        self._stop_threads()

    # ================================================================ 内容更新
    def update(self, info: MusicInfo, cover: Image.Image | None, playback_status: str, *,
               duration: float = 0.0,
               source_timeline: tuple[float, float, float] | None = None) -> None:
        """完整更新托管会话: 歌曲信息 / 封面 / 播放状态 / 时长 / 进度。

        * ``info``: 静态歌曲信息 (四个文本字段写入会话; ``length`` 是时长兜底);
        * ``cover``: 高清封面 (PIL 图); ``None`` 时保留会话里已有的缩略图;
        * ``playback_status``: 源会话播放状态的小写枚举名 (``"playing"`` / ``"paused"`` ...);
        * ``duration``: 歌曲时长 (秒), 0 表示未知 (会退回 ``info.length``);
        * ``source_timeline``: ``(start, position, end)``, 有效时镜像源进度并关闭自托管。
        """
        self._call_sync(self._cmd_update, info, cover, playback_status,
                        duration, source_timeline)

    def update_timeline(self, start: float, position: float, end: float) -> None:
        """轻量更新进度 (用于源会话进度变化事件)。

        ``end > start`` 时原样镜像并关闭自托管计时; 无效值 (源平台没提供进度) 被忽略,
        自托管计时继续工作。
        """
        self._call_sync(self._cmd_update_timeline, start, position, end)

    def set_playback_status(self, status: str) -> None:
        """轻量更新播放状态 (``never_pause`` 时托管会话仍保持 PLAYING)。"""
        normalized = self._normalize_status(status)
        # 即使会话未创建也先记录源状态, 便于 create 后自托管进度使用
        self._source_status = normalized
        self._call_sync(self._cmd_set_playback_status, normalized)

    def restart_progress(self, position: float = 0.0) -> None:
        """把托管会话进度重置到开头 (单曲循环 / 重新播放时用)。

        * 自托管模式: 重置计时基准, 从 ``position`` 重新推进;
        * 镜像模式: 立即写入 ``(0, position, end)`` (之后源进度事件仍会覆盖)。

        源平台不提供进度 (例如酷狗) 时, 单曲循环不会产生"进度回退"事件,
        需要 main 在检测到同一首歌重新开始时调用本方法。
        """
        self._call_sync(self._cmd_restart_progress, float(position))

    def display_default(self) -> None:
        """无源会话时写入默认信息 (:data:`DEFAULT_MUSIC`), 并复位状态/进度。"""
        self._call_sync(self._cmd_display_default)

    # ================================================================ 状态读取
    @property
    def playback_status(self) -> str:
        """当前托管会话的播放状态 (小写枚举名, 如 ``"playing"``)。"""
        return self._playback_status

    @property
    def timeline_position(self) -> float:
        """当前托管会话的进度位置 (秒; 只读 worker 缓存值, 不做 WinRT 调用)。"""
        return self._timeline_position

    @property
    def timeline_end(self) -> float:
        """当前托管会话的总时长/终点 (秒; 0 表示未知, 只读缓存值)。"""
        return self._timeline_end

    def _timeline_state(self) -> dict[str, Any]:
        """内部进度状态快照 (自测 / 调试用; 不做 WinRT 调用)。"""
        return {
            "start": self._timeline_start,
            "position": self._timeline_position,
            "end": self._timeline_end,
            "self_host": self._self_host_active,
            "self_host_playing": self._self_host_playing,
            "source_status": self._source_status,
            "playback_status": self._playback_status,
        }

    # ================================================================ 线程启动 / 停止
    def _start_threads(self) -> None:
        """确保 worker 与回调分发线程都在运行 (create / 重新 create 时调用)"""
        with self._pending_lock:
            has_pending = bool(self._pending)
        if has_pending:
            self._drain_pending()
        worker = self._worker
        if worker is None or not worker.is_alive():
            self._queue = Queue()
            self._running = True
            self._worker = threading.Thread(
                target=self._worker_loop, daemon=True,
                name="MusicSMTCWrapper-SMTCHost")
            self._worker.start()
        else:
            self._running = True
        callback_thread = self._callback_thread
        if callback_thread is None or not callback_thread.is_alive():
            self._button_queue = Queue()
            self._callback_thread = threading.Thread(
                target=self._callback_loop, daemon=True,
                name="MusicSMTCWrapper-SMTCHost-Buttons")
            self._callback_thread.start()

    def _stop_threads(self) -> None:
        """投递停止哨兵并等待内部线程退出 (可重复调用)"""
        self._running = False
        worker = self._worker
        self._worker = None
        if worker is not None:
            self._queue.put(None)
            if threading.current_thread() is not worker:
                worker.join(timeout=COMMAND_TIMEOUT)
                if worker.is_alive():
                    logger.warning("托管 SMTC worker 未在超时内退出")
        callback_thread = self._callback_thread
        self._callback_thread = None
        if callback_thread is not None:
            self._button_queue.put(None)
            if threading.current_thread() is not callback_thread:
                callback_thread.join(timeout=COMMAND_TIMEOUT)
                if callback_thread.is_alive():
                    logger.warning("托管 SMTC 按钮分发线程未在超时内退出")

    # ================================================================ 命令调度
    def _submit(self, func: Callable[..., Any], *args: Any) -> int:
        with self._pending_lock:
            self._seq += 1
            job_id = self._seq
            self._pending[job_id] = {"done": threading.Event(), "result": None, "error": None}
        self._queue.put((job_id, func, args))
        return job_id

    def _drain_pending(self) -> None:
        """停止时唤醒所有还在等待结果的调用方 (结果记为 None), 避免它们等满 5s"""
        with self._pending_lock:
            jobs = list(self._pending.values())
            self._pending.clear()
        for job in jobs:
            job["done"].set()

    def _call_sync(self, func: Callable[..., Any], *args: Any) -> Any:
        """投递命令并在 worker 上执行, 同步等待结果 (带超时保护)。"""
        if threading.current_thread() is self._worker:
            # 已经在 worker 上 (例如以后从 worker 内部调用): 直接执行, 避免自投递死锁
            try:
                return func(*args)
            except Exception as e:  # noqa: BLE001
                logger.error("托管 SMTC 操作失败 [%s]: %s: %s",
                             getattr(func, "__name__", func), e.__class__.__name__, e)
                return None
        if not self._running:
            logger.debug("托管 SMTC worker 未运行, 忽略命令: %s",
                         getattr(func, "__name__", func))
            return None
        worker = self._worker
        if worker is None or not worker.is_alive():
            logger.debug("托管 SMTC worker 已退出, 忽略命令: %s",
                         getattr(func, "__name__", func))
            return None
        job_id = self._submit(func, *args)
        with self._pending_lock:
            job = self._pending.get(job_id)
        if job is None:
            return None
        if not job["done"].wait(COMMAND_TIMEOUT):
            with self._pending_lock:
                self._pending.pop(job_id, None)
            logger.error("托管 SMTC 操作超时 (%.1fs): %s",
                         COMMAND_TIMEOUT, getattr(func, "__name__", func))
            return None
        with self._pending_lock:
            self._pending.pop(job_id, None)
        if job["error"] is not None:
            logger.error("托管 SMTC 操作异常 [%s]: %s: %s",
                         getattr(func, "__name__", func),
                         job["error"].__class__.__name__, job["error"])
            return None
        return job["result"]

    def _worker_loop(self) -> None:
        """唯一的 WinRT worker: 串行执行命令; 空闲时推进自托管进度"""
        logger.debug("托管 SMTC worker 启动: %s", threading.current_thread().name)
        try:
            while True:
                try:
                    self._maybe_advance_self_host()
                except Exception as e:  # noqa: BLE001 - 推进失败不应杀死 worker
                    logger.warning("自托管进度推进失败: %s: %s", e.__class__.__name__, e)
                try:
                    job = self._queue.get(timeout=WORKER_POLL_INTERVAL)
                except Empty:
                    if not self._running:
                        return
                    continue
                if job is None:  # 停止哨兵
                    self._drain_pending()
                    return
                job_id, func, args = job
                with self._pending_lock:
                    pending = self._pending.get(job_id)
                try:
                    result = func(*args)
                    if pending is not None:
                        pending["result"] = result
                except Exception as e:  # noqa: BLE001 - 命令失败要回传给调用方
                    logger.error("托管 SMTC 操作失败 [%s]: %s: %s",
                                 getattr(func, "__name__", func),
                                 e.__class__.__name__, e)
                    if pending is not None:
                        pending["error"] = e
                finally:
                    if pending is not None:
                        pending["done"].set()
        finally:
            logger.debug("托管 SMTC worker 退出: %s", threading.current_thread().name)

    # ================================================================ 按钮回调分发
    def _on_button_pressed(self, sender: Any, args: Any) -> None:
        """WinRT 按钮事件 (运行在 WinRT 事件线程): 只投递按钮名, 不做任何 WinRT 调用"""
        try:
            button = getattr(args, "button", None)
            name = getattr(button, "name", None)
            text = str(name if name is not None else button or "").strip().lower()
        except Exception:  # noqa: BLE001 - 事件参数异常时直接忽略
            return
        if not text:
            return
        logger.debug("托管会话收到系统按钮: %s", text)
        self._button_queue.put(text)

    def _callback_loop(self) -> None:
        """独立回调分发线程: 把按钮名交给外部回调 (回调可以安全调用本模块公开方法)"""
        logger.debug("托管 SMTC 按钮分发线程启动: %s", threading.current_thread().name)
        try:
            while True:
                name = self._button_queue.get()
                if name is None:  # 停止哨兵
                    return
                with self._callback_lock:
                    callback = self._button_callback
                if callback is None:
                    continue
                try:
                    callback(name)
                except Exception as e:  # noqa: BLE001 - 回调异常不能杀死分发线程
                    logger.error("按钮回调执行失败 (%s): %s: %s",
                                 name, e.__class__.__name__, e)
        finally:
            logger.debug("托管 SMTC 按钮分发线程退出: %s", threading.current_thread().name)

    # ================================================================ 命令实现: 生命周期
    def _cmd_create(self) -> bool:
        """在 worker 上创建 MediaPlayer + SMTC 会话 (只在 worker 调用)"""
        if self._smtc is not None:
            logger.debug("托管 SMTC 会话已存在, 忽略重复创建")
            return True
        logger.info("创建托管 SMTC 会话 (worker: %s)", threading.current_thread().name)
        try:
            player = MediaPlayer()
            player.command_manager.is_enabled = False
            smtc = player.system_media_transport_controls
            smtc.is_enabled = True
            smtc.is_play_enabled = True
            smtc.is_pause_enabled = True
            smtc.is_next_enabled = True
            smtc.is_previous_enabled = True
            # 会话的身份与类型也在创建时写好 (Windows 用 app_media_id 区分不同会话)
            updater = smtc.display_updater
            updater.app_media_id = APP_MEDIA_ID
            updater.type = MediaPlaybackType.MUSIC
            self._player = player
            self._smtc = smtc
            self._button_token = smtc.add_button_pressed(self._on_button_pressed)
        except Exception:
            # 创建到一半失败: 清理干净再抛给 _call_sync 记日志
            self._cmd_destroy()
            raise
        self._frame_streams.clear()
        self._source_status = "stopped"
        self._last_song_key = ""
        self._reset_self_host_state()
        self._apply_playback_status()
        logger.info("托管 SMTC 会话创建成功 (app_media_id=%s)", APP_MEDIA_ID)
        return True

    def _cmd_destroy(self) -> None:
        """销毁托管会话 (只在 worker 调用); 可重复调用"""
        smtc = self._smtc
        if smtc is not None:
            logger.info("销毁托管 SMTC 会话")
            token = self._button_token
            if token is not None:
                try:
                    smtc.remove_button_pressed(token)
                except Exception as e:  # noqa: BLE001 - 注销失败不阻断销毁流程
                    logger.warning("注销托管会话按钮事件失败: %s: %s",
                                   e.__class__.__name__, e)
            try:
                smtc.is_enabled = False
            except Exception as e:  # noqa: BLE001
                logger.warning("关闭托管会话失败: %s: %s", e.__class__.__name__, e)
        player = self._player
        if player is not None:
            # MediaPlayer 在部分 winsdk 版本上实现了 IClosable; 有 close 就顺手关掉
            close = getattr(player, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - 关闭失败不影响后续重建
                    pass
        self._smtc = None
        self._player = None
        self._button_token = None
        self._frame_streams.clear()
        self._reset_self_host_state()
        self._timeline_start = 0.0
        self._timeline_position = 0.0
        self._timeline_end = 0.0
        self._playback_status = "stopped"
        self._source_status = "stopped"
        self._last_song_key = ""

    # ================================================================ 命令实现: 配置 / 内容
    def _cmd_apply_config(self) -> None:
        """把 configure() 的配置立即应用到已创建的会话上 (只在 worker 调用)"""
        if self._smtc is None:
            return
        self._apply_playback_status()
        if not self._self_host_progress and self._self_host_active:
            # 关闭自托管: 冻结在当前位置, 不再推进
            position = self._compute_self_host_position()
            self._self_host_active = False
            self._self_host_playing = False
            self._timeline_position = position
            self._write_timeline(0.0, position, self._timeline_end)

    def _cmd_update(self, info: MusicInfo | None, cover: Image.Image | None,
                    playback_status: str, duration: float,
                    source_timeline: tuple[float, float, float] | None) -> bool:
        """完整更新 (只在 worker 调用)"""
        if self._smtc is None:
            logger.debug("托管 SMTC 会话未创建, 忽略 update")
            return False
        music = info.copy() if isinstance(info, MusicInfo) else DEFAULT_MUSIC.copy()
        timeline = self._normalize_timeline(source_timeline)
        total = self._coerce_duration(duration)
        if total <= 0.0 and music.length > 0.0:
            total = music.length
        if total <= 0.0 and timeline is not None:
            total = timeline[2] - timeline[0]
        self._source_status = self._normalize_status(playback_status)

        # 换歌判断: 四个文本字段 + 时长变化都算换歌 (换歌要重置自托管基准)
        song_key = f"{music.unique_key()}\x1e{total:.3f}"
        song_changed = song_key != self._last_song_key
        self._last_song_key = song_key

        updater = self._smtc.display_updater
        updater.app_media_id = APP_MEDIA_ID
        updater.type = MediaPlaybackType.MUSIC
        properties = updater.music_properties
        properties.title = music.title
        properties.artist = music.artist
        properties.album_title = music.album_title
        properties.album_artist = music.album_artist
        if cover is not None:
            try:
                updater.thumbnail = self._make_stream_reference(cover)
            except Exception as e:  # noqa: BLE001 - 封面失败不应影响文字信息
                logger.warning("写入托管会话封面失败: %s: %s", e.__class__.__name__, e)
        # 缩略图/文字属性都是暂存值, 必须 update() 才真正提交给系统
        updater.update()
        self._apply_playback_status()

        if timeline is not None:
            self._apply_source_timeline(*timeline)
        elif self._self_host_progress and total > 0.0:
            self._apply_self_host_timeline(total, song_changed)
        else:
            self._apply_static_timeline(total)
        return True

    def _cmd_update_timeline(self, start: float, position: float, end: float) -> None:
        """轻量进度更新 (只在 worker 调用)"""
        if self._smtc is None:
            return
        timeline = self._normalize_timeline((start, position, end))
        if timeline is None:
            logger.debug("忽略无效的源进度: start=%s position=%s end=%s", start, position, end)
            return
        self._apply_source_timeline(*timeline)

    def _cmd_restart_progress(self, position: float) -> None:
        """把托管进度重置到指定位置 (只在 worker 调用)。

        用于源平台不提供进度、单曲循环时没有"进度回退"事件的场景。
        """
        if self._smtc is None:
            logger.debug("托管 SMTC 会话未创建, 忽略进度重置")
            return
        end = self._timeline_end
        try:
            pos = float(position)
        except (TypeError, ValueError):
            pos = 0.0
        pos = max(0.0, pos)
        if end > 0.0:
            pos = min(pos, end)
        self._reset_self_host_state()
        if self._self_host_progress and end > 0.0:
            # 自托管模式: 从新位置重新计时
            self._self_host_active = True
            self._self_host_playing = self._source_status == "playing"
            self._self_host_base = pos
            self._self_host_ref = time.monotonic()
            self._self_host_last_write = time.monotonic()
        self._timeline_position = pos
        self._write_timeline(0.0, pos, end)
        logger.info(f"托管SMTC进度已重置: position={pos:.2f}s end={end:.2f}s")

    def _cmd_set_playback_status(self, status: str) -> None:
        """轻量播放状态更新 (只在 worker 调用)"""
        if self._smtc is None:
            return
        self._source_status = self._normalize_status(status)
        self._apply_playback_status()
        if self._self_host_active:
            # 冻结/恢复自托管推进: 以当前推进值为新基准
            position = self._compute_self_host_position()
            if self._timeline_end > 0.0:
                position = min(position, self._timeline_end)
            self._self_host_base = max(0.0, position)
            self._self_host_ref = time.monotonic()
            self._self_host_playing = self._source_status == "playing"
            self._timeline_position = self._self_host_base
            self._write_timeline(0.0, self._self_host_base, self._timeline_end)
            self._self_host_last_write = time.monotonic()

    def _cmd_display_default(self) -> None:
        """写入默认信息并复位状态/进度 (只在 worker 调用)"""
        if self._smtc is None:
            logger.debug("托管 SMTC 会话未创建, 忽略 display_default")
            return
        updater = self._smtc.display_updater
        updater.app_media_id = APP_MEDIA_ID
        updater.type = MediaPlaybackType.MUSIC
        properties = updater.music_properties
        properties.title = DEFAULT_MUSIC.title
        properties.artist = DEFAULT_MUSIC.artist
        properties.album_title = DEFAULT_MUSIC.album_title
        properties.album_artist = DEFAULT_MUSIC.album_artist
        # 与旧实现一致: 不主动清空缩略图, 没有新封面时保留上一次的图
        updater.update()
        self._last_song_key = ""
        self._source_status = "stopped"
        self._apply_playback_status()
        self._apply_static_timeline(0.0)
        logger.debug("托管 SMTC 已恢复默认信息: %s - %s",
                     DEFAULT_MUSIC.title, DEFAULT_MUSIC.artist)

    # ================================================================ 状态 / 进度内部实现
    @staticmethod
    def _normalize_status(status: Any) -> str:
        """把任意播放状态转成小写枚举名 (``playing`` / ``paused`` / ``stopped`` ...)"""
        name = getattr(status, "name", None)
        text = str(name if name is not None else status or "").strip().lower()
        if "." in text:
            text = text.rsplit(".", 1)[-1]
        return text or "stopped"

    @staticmethod
    def _to_seconds(value: Any) -> float | None:
        """把时长/进度值转成秒; 兼容 winsdk 的 ``datetime.timedelta`` 投影。

        非法值 (非数字/NaN) 返回 None, 由调用方决定兜底。
        """
        if value is None:
            return None
        if isinstance(value, timedelta):
            seconds = value.total_seconds()
            return seconds if math.isfinite(seconds) else None
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            return None
        return seconds if math.isfinite(seconds) else None

    @classmethod
    def _coerce_duration(cls, duration: Any) -> float:
        """时长归一: 非数字/负数/NaN 一律当 0"""
        value = cls._to_seconds(duration)
        return value if value is not None and value > 0.0 else 0.0

    @classmethod
    def _normalize_timeline(cls, source_timeline: Any) -> tuple[float, float, float] | None:
        """校验源进度 ``(start, position, end)``; 无效 (end <= start) 返回 None"""
        if source_timeline is None:
            return None
        try:
            raw_values = tuple(source_timeline)
        except TypeError:
            return None
        if len(raw_values) != 3:
            return None
        values = [cls._to_seconds(value) for value in raw_values]
        if any(value is None for value in values):
            return None
        start, position, end = values  # type: ignore[misc]
        if end <= start:
            return None
        start = max(0.0, start)
        if end <= start:
            return None
        position = min(max(position, start), end)
        return start, position, end

    def _target_playback_status(self) -> MediaPlaybackStatus:
        """托管会话应当写入的播放状态 (never_pause 时恒 PLAYING)"""
        if self._never_pause:
            return MediaPlaybackStatus.PLAYING
        return getattr(MediaPlaybackStatus, self._source_status.upper(),
                       MediaPlaybackStatus.STOPPED)

    def _apply_playback_status(self) -> None:
        """把当前源状态 (或 never_pause 的 PLAYING) 写入托管会话 (只在 worker 调用)"""
        if self._smtc is None:
            return
        target = self._target_playback_status()
        try:
            self._smtc.playback_status = target
        except Exception as e:  # noqa: BLE001 - 写入失败只记日志, 不阻断信息更新
            logger.warning("写入托管会话播放状态失败: %s: %s", e.__class__.__name__, e)
        self._playback_status = target.name.lower()

    def _write_timeline(self, start: float, position: float, end: float) -> None:
        """把进度写入托管会话 (只在 worker 调用); 数值做防御性归并"""
        if self._smtc is None:
            return
        try:
            start = max(0.0, float(start))
            end = max(0.0, float(end))
            if end < start:
                end = start
            position = min(max(float(position), start), end)
            if not all(math.isfinite(value) for value in (start, position, end)):
                return
            timeline = TimelineProperties()
            # winsdk 把 WinRT 的 TimeSpan 投影成 datetime.timedelta, 不能直接写浮点秒
            timeline.start_time = timedelta(seconds=start)
            timeline.end_time = timedelta(seconds=end)
            timeline.position = timedelta(seconds=position)
            timeline.min_seek_time = timedelta(seconds=start)
            timeline.max_seek_time = timedelta(seconds=end)
            self._smtc.update_timeline_properties(timeline)
            self._timeline_start = start
            self._timeline_position = position
            self._timeline_end = end
        except Exception as e:  # noqa: BLE001 - 进度失败不应影响歌曲信息显示
            logger.warning("更新托管会话进度失败: %s: %s", e.__class__.__name__, e)

    def _apply_source_timeline(self, start: float, position: float, end: float) -> None:
        """镜像源会话进度并关闭自托管 (只在 worker 调用)"""
        self._reset_self_host_state()
        self._write_timeline(start, position, end)

    def _apply_static_timeline(self, duration: float) -> None:
        """写静态进度 ``(0, 0, duration)`` (只在 worker 调用)"""
        self._reset_self_host_state()
        self._write_timeline(0.0, 0.0, duration if duration > 0.0 else 0.0)

    def _reset_self_host_state(self) -> None:
        """清空自托管计时状态 (只在 worker 调用)"""
        self._self_host_active = False
        self._self_host_playing = False
        self._self_host_base = 0.0
        self._self_host_ref = 0.0
        self._self_host_last_write = 0.0

    def _apply_self_host_timeline(self, duration: float, song_changed: bool) -> None:
        """开启/更新自托管进度 (只在 worker 调用)。

        进度 = 基准位置 + (当前时刻 - 基准时刻) (播放中), 暂停/停止时冻结在基准位置。
        换歌时基准位置重置为 0, 并立即写一次托管会话。
        """
        end = max(0.0, float(duration))
        playing = self._source_status == "playing"
        if song_changed or not self._self_host_active:
            base = 0.0
            self._timeline_start = 0.0
            self._timeline_end = end
        else:
            # 同一首歌的普通更新: 以当前推进值为新基准, 避免位置跳变
            base = min(self._compute_self_host_position(), end)
            self._timeline_end = end
        self._self_host_active = True
        self._self_host_playing = playing
        self._self_host_base = base
        self._self_host_ref = time.monotonic()
        self._timeline_position = base
        self._write_timeline(0.0, base, end)
        self._self_host_last_write = time.monotonic()

    def _compute_self_host_position(self) -> float:
        """由基准位置 + 经过时间算当前自托管 position (不做 WinRT 调用)"""
        if not self._self_host_active:
            return self._timeline_position
        if self._self_host_playing:
            position = self._self_host_base + (time.monotonic() - self._self_host_ref)
        else:
            position = self._self_host_base
        if self._timeline_end > 0.0:
            position = min(position, self._timeline_end)
        return max(0.0, position)

    def _maybe_advance_self_host(self) -> None:
        """worker 空闲时推进自托管进度; 每 ~0.5s 写回托管会话一次 (只在 worker 调用)"""
        if not self._self_host_active or not self._self_host_playing or self._smtc is None:
            return
        position = self._compute_self_host_position()
        self._timeline_position = position
        now = time.monotonic()
        if now - self._self_host_last_write >= SELF_HOST_WRITE_INTERVAL:
            self._write_timeline(0.0, position, self._timeline_end)
            self._self_host_last_write = now

    # ================================================================ 封面转换
    def _make_stream_reference(self, image: Image.Image) -> RandomAccessStreamReference:
        """把 PIL 封面转成 SMTC 需要的 RandomAccessStreamReference (只在 worker 调用)。

        流对象必须保持存活到 Windows 读取完毕, 因此最近 :data:`MAX_FRAME_STREAMS` 个流
        都保存在 ``self._frame_streams`` 里。
        """
        frame = image
        if frame.mode not in ("RGB", "RGBA"):
            frame = frame.convert("RGBA")
        buffer = BytesIO()
        frame.save(buffer, "PNG")
        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream)
        try:
            writer.write_bytes(buffer.getvalue())
            wait_result(writer.store_async())
        finally:
            try:
                writer.detach_stream()
            except Exception:  # noqa: BLE001 - 释放 writer 失败不影响后续
                pass
        self._frame_streams.append(stream)
        if len(self._frame_streams) > MAX_FRAME_STREAMS:
            del self._frame_streams[:len(self._frame_streams) - MAX_FRAME_STREAMS]
        return RandomAccessStreamReference.create_from_stream(stream)
