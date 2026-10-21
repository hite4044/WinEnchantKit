# -*- coding: utf-8 -*-
"""源平台官方 SMTC 会话管理 (单线程)。

本模块只负责「读取并操作其它音乐软件注册到系统里的 SMTC 会话」:

* 发现 / 筛选源会话 (按插件配置的源平台);
* 在接管会话后注册 WinRT 事件 (播放信息 / 进度条);
* 读取源会话的歌曲信息、播放状态、进度、缩略图 (转成纯数据 :class:`SourceSnapshot`);
* 把插件会话上收到的按钮命令转发给源会话;
* 检测源会话失效 (会话从系统列表消失, 或 title/artist 全空), 并按
  ``platform_mode`` 自动重新筛选。

线程模型 (契约第 3 节, 硬性要求)
--------------------------------
主要有两个线程, 都只在 :meth:`SourceSMTC.start` 之后存在:

* **WinRT worker 线程** (``WORKER_THREAD_NAME``): 所有源会话的 WinRT 调用
  (``SessionManager.request_async`` / ``get_sessions`` / ``try_get_media_properties_async`` /
  ``get_playback_info`` / ``get_timeline_properties`` / 缩略图读取 / ``try_*_async`` 按钮)
  以及**事件注册与注销**都只在这个线程上执行。外部公开方法只是把命令投递进队列;
  需要返回值的方法同步等待结果 (最多 5s, 超时记 error 并返回 None/False, 绝不卡死调用方)。
* **回调分发线程** (``CALLBACK_THREAD_NAME``): 对外回调 ``on_source_changed`` /
  ``on_info_changed`` / ``on_timeline_changed`` 只在这个线程上执行, 与 worker 隔离 ——
  回调里即使再调用本模块的同步接口也不会与 worker 自锁。

WinRT 的事件回调 (WinRT 自己的线程) **只做一件事**: 把一条通知投递进命令队列 / 通知队列;
既不做 WinRT 调用, 也不同步调用本模块的公开接口 (防死锁)。

``stop()`` 之后不会再有新的 WinRT 调用: 事件全部注销、worker 线程退出, 所有公开接口
在未启动状态下安全空转 (返回 None / False / 空串)。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from queue import Empty, Queue
from typing import Any, Callable

from winsdk.windows.foundation import AsyncStatus
from winsdk.windows.media.control import (
    GlobalSystemMediaTransportControlsSession as Session,
    GlobalSystemMediaTransportControlsSessionManager as SessionManager,
)
from winsdk.windows.security.cryptography import CryptographicBuffer
from winsdk.windows.storage.streams import DataReader

from .music_base import LOGGER_NAME, MusicInfo, normalize_text, text_key

logger = logging.getLogger(LOGGER_NAME)

# ---------------------------------------------------------------- 模块常量
# 平台标识 (与配置项 source_platform / search_platform 的取值一致)
PLATFORM_KUGOU = "kugou"
PLATFORM_NETEASE = "netease"
PLATFORM_QQ = "qq"
PLATFORM_AUTO = "auto"

PLATFORM_MODES: tuple[str, ...] = (PLATFORM_AUTO, PLATFORM_KUGOU, PLATFORM_NETEASE, PLATFORM_QQ)

# 源会话的 app id (source_app_user_model_id) 小写子串 -> 平台
PLATFORM_MARKERS: dict[str, tuple[str, ...]] = {
    PLATFORM_KUGOU: ("kugou", "moekoe"),
    PLATFORM_NETEASE: ("netease", "cloudmusic"),
    PLATFORM_QQ: ("qqmusic", "qq"),
}

# 线程名 (方便日志/排查线程归属)
WORKER_THREAD_NAME = "MusicSMTCWrapper-SourceSMTC"
CALLBACK_THREAD_NAME = "MusicSMTCWrapper-SourceSMTC-Callbacks"

# 同步接口等待 worker 的超时 (契约第 3 节: 5s)
_CALL_TIMEOUT = 5.0
# 等待单个 WinRT 异步操作的超时
_ASYNC_TIMEOUT = 5.0
# 轮询 IAsyncOperation 状态的间隔
_POLL_INTERVAL = 0.02
# worker / 回调线程空闲时检查退出标志的间隔
_IDLE_TIMEOUT = 0.2

# 停止哨兵 (两个队列共用)
_STOP = object()

# 按钮名 -> 源会话方法名
_BUTTON_METHODS: dict[str, str] = {
    "play": "try_play_async",
    "pause": "try_pause_async",
    "next": "try_skip_next_async",
    "previous": "try_skip_previous_async",
}


# ================================================================ 小工具
def platform_from_app_id(app_id: Any) -> str:
    """由源会话的 app id 推导平台标识; 认不出来返回空串。

    ``app_id`` 是 WinRT 的 ``source_app_user_model_id`` (例如 ``"kugou"`` /
    ``"QQMusic.exe"`` / ``"MSEdge"``), 按 :data:`PLATFORM_MARKERS` 里的标记做小写子串匹配。
    """
    name = str(app_id or "").lower()
    if not name:
        return ""
    for platform, markers in PLATFORM_MARKERS.items():
        if any(marker in name for marker in markers):
            return platform
    return ""


def _seconds(value: Any) -> float:
    """WinRT TimeSpan (winsdk 里是 ``datetime.timedelta``) -> 秒 (浮点, 失败给 0)"""
    if value is None:
        return 0.0
    total_seconds = getattr(value, "total_seconds", None)
    try:
        if callable(total_seconds):
            return float(total_seconds())
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _wait_async(operation: Any, timeout: float = _ASYNC_TIMEOUT) -> Any:
    """轮询等待 WinRT ``IAsyncOperation`` 完成并返回结果 (带超时)。

    超时抛 :class:`TimeoutError`, 操作失败抛 :class:`RuntimeError`; 由调用方决定
    是否吞掉异常 (本模块所有公开接口都会兜底, 不会把异常抛给调用方)。
    """
    if operation is None:
        return None
    deadline = time.monotonic() + max(0.01, float(timeout))
    while operation.status == AsyncStatus.STARTED:
        if time.monotonic() >= deadline:
            raise TimeoutError("等待 WinRT 异步操作超时")
        time.sleep(_POLL_INTERVAL)
    status = operation.status
    if status == AsyncStatus.ERROR:
        raise RuntimeError(f"WinRT 异步操作失败: {getattr(operation, 'error_code', '')}")
    if status == AsyncStatus.CANCELED:
        raise RuntimeError("WinRT 异步操作已取消")
    return operation.get_results()


# ================================================================ 快照
@dataclass
class SourceSnapshot:
    """源 SMTC 会话在某一时刻的纯数据快照 (跨线程传递, 不含任何 WinRT 对象)。

    由 :meth:`SourceSMTC.snapshot` 在 worker 线程上读取并构造; 之后可以随意传给
    其它线程 (main.py 的刷新线程) 使用。
    """

    app_id: str = ""
    platform: str = ""                 # "kugou" / "netease" / "qq" / "unknown"
    title: str = ""
    artist: str = ""
    album_title: str = ""
    album_artist: str = ""
    # WinRT 播放状态枚举名的小写: playing / paused / stopped / changing / opened / closed
    playback_status: str = "unknown"
    timeline_start: float = 0.0
    timeline_position: float = 0.0
    timeline_end: float = 0.0          # <= 0 表示平台没提供进度信息
    thumbnail: bytes | None = None     # 源 SMTC 缩略图原始字节 (通常 150x150), 读取失败为 None

    def to_music_info(self) -> MusicInfo:
        """取出静态歌曲字段 (四个文本字段 + 时长)。

        源会话的 ``timeline_end`` 就是歌曲总时长, 因此 ``length`` 取它 (<=0 视为未知)。
        """
        length = self.timeline_end if self.timeline_end > 0.0 else 0.0
        return MusicInfo(self.title, self.artist, self.album_title, self.album_artist, length)

    @property
    def is_valid(self) -> bool:
        """标题或作者至少一个非空 (完全空 = 平台没有在播歌曲, 视为失效)"""
        return bool(self.title or self.artist)

    def unique_key(self) -> str:
        """四个字段的归一比较键 (与 :meth:`MusicInfo.unique_key` 同构)"""
        return "\x1f".join(text_key(part) for part in
                           (self.title, self.artist, self.album_title, self.album_artist))


# ================================================================ 会话管理
class SourceSMTC:
    """源平台 SMTC 会话管理器 (唯一 WinRT worker + 独立回调分发线程)。

    生命周期: ``start()`` -> (``set_platform_mode()`` / ``find()`` / ``snapshot()`` ...)
    -> ``stop()``。``stop()`` 可重复调用, ``start()`` 在停止后可再次启动。

    对外回调 (由 main.py 在 ``start()`` 前赋值, 在回调分发线程上执行):

    * ``on_source_changed`` —— 源会话绑定 / 失效 / 切换 (main 应重新取快照);
    * ``on_info_changed`` —— 当前源会话的歌曲信息 (含播放状态) 变化;
    * ``on_timeline_changed`` —— 当前源会话进度变化。

    回调实现只应做投递/轻量处理 (网络请求丢给 main 自己的刷新线程)。
    """

    def __init__(self) -> None:
        # ---- 对外回调 (main.py 赋值; 未赋值时安全忽略) ----
        self.on_source_changed: Callable[[], None] | None = None
        self.on_info_changed: Callable[[], None] | None = None
        self.on_timeline_changed: Callable[[], None] | None = None

        # ---- 源会话状态 (只在 worker 线程写, 其它线程只读) ----
        self._manager: SessionManager | None = None
        self._session: Session | None = None
        self._app_id: str = ""
        self._platform: str = ""
        self._platform_mode: str = PLATFORM_AUTO
        self._preferred_platform: str = ""

        # ---- WinRT 事件 token / handler (只用于注销; 只在 worker 上读写) ----
        self._manager_token: Any = None
        self._playback_token: Any = None
        self._timeline_token: Any = None
        self._manager_handler: Any = None
        self._playback_handler: Any = None
        self._timeline_handler: Any = None

        # ---- 线程与队列 (start 时创建, stop 时清空) ----
        self._lifecycle_lock = threading.RLock()
        self._pending_lock = threading.Lock()
        self._notify_lock = threading.Lock()
        self._queue: Queue | None = None
        self._notify_queue: Queue | None = None
        self._worker: threading.Thread | None = None
        self._dispatcher: threading.Thread | None = None
        self._pending: dict[int, dict[str, Any]] = {}
        self._pending_events: set[str] = set()
        self._seq = 0
        self._running = False
        self._callbacks_running = False
        # 停机闸门: stop() 一开始就置 True, 阻止停机过程中到达的事件/命令重新访问 WinRT
        self._closing = False

    # ============================================================ 生命周期
    def start(self) -> None:
        """启动 worker + 回调分发线程, 并在 worker 上注册会话列表事件。

        可重复调用 (已启动时忽略)。注册 WinRT 事件失败只记 error, 不抛异常
        (之后 ``find()`` 仍会重试获取 SessionManager)。
        """
        with self._lifecycle_lock:
            if self._running:
                logger.debug("源 SMTC 已在运行, 忽略重复 start")
                return
            # 每次都重建队列/线程, 保证 stop -> start 之后没有残留命令
            self._pending = {}
            self._pending_lock = threading.Lock()
            self._notify_lock = threading.Lock()
            self._pending_events = set()
            self._seq = 0
            self._closing = False
            self._queue = Queue()
            self._notify_queue = Queue()
            self._callbacks_running = True
            self._running = True
            self._worker = threading.Thread(target=self._worker_loop, name=WORKER_THREAD_NAME,
                                            daemon=True)
            self._dispatcher = threading.Thread(target=self._dispatcher_loop,
                                                name=CALLBACK_THREAD_NAME, daemon=True)
            self._worker.start()
            self._dispatcher.start()
            registered = bool(self._call_sync(self._cmd_register_manager_events))
            if registered:
                logger.info("源 SMTC 已启动 (worker=%s, 回调线程=%s)",
                            WORKER_THREAD_NAME, CALLBACK_THREAD_NAME)
            else:
                logger.error("源 SMTC 启动: 注册系统会话列表事件失败")

    def stop(self) -> None:
        """注销全部 WinRT 事件并停止两个线程 (可重复调用; 之后不再访问 WinRT 对象)。"""
        with self._lifecycle_lock:
            worker = self._worker
            dispatcher = self._dispatcher
            if not self._running and worker is None and dispatcher is None:
                self._reset_state()
                return
            # 停机闸门: 之后到达的 WinRT 事件一律丢弃, 已排队命令也不能再访问 WinRT
            self._closing = True
            # 1) 在 worker 上注销事件、清空会话与 manager 引用 (之后不再碰 WinRT)
            if self._running:
                self._call_sync(self._cmd_shutdown)
            # 2) 让 worker 退出
            self._running = False
            queue = self._queue
            if queue is not None:
                queue.put(_STOP)
            if worker is not None and worker is not threading.current_thread():
                worker.join(timeout=5.0)
                if worker.is_alive():
                    logger.warning("源 SMTC worker 线程未在超时内退出")
            # 3) 让回调分发线程退出
            self._callbacks_running = False
            notify_queue = self._notify_queue
            if notify_queue is not None:
                notify_queue.put(_STOP)
            if dispatcher is not None and dispatcher is not threading.current_thread():
                dispatcher.join(timeout=3.0)
                if dispatcher.is_alive():
                    logger.warning("源 SMTC 回调分发线程未在超时内退出")
            self._reset_state()
            logger.info("源 SMTC 已停止")

    def _reset_state(self) -> None:
        """清空所有 WinRT 引用与线程对象 (只在两个线程都停止后调用)。"""
        self._manager = None
        self._session = None
        self._app_id = ""
        self._platform = ""
        self._manager_token = None
        self._playback_token = None
        self._timeline_token = None
        self._manager_handler = None
        self._playback_handler = None
        self._timeline_handler = None
        with self._pending_lock:
            self._pending.clear()
        with self._notify_lock:
            self._pending_events.clear()
        self._queue = None
        self._notify_queue = None
        self._worker = None
        self._dispatcher = None
        self._closing = False

    # ============================================================ 单线程调度
    def _call_sync(self, func: Callable[..., Any], *args: Any) -> Any:
        """把命令投递到 worker 并同步等待结果 (超时 5s, 失败返回 None, 不抛异常)。"""
        name = getattr(func, "__name__", repr(func))
        # 防御: 万一有人在 worker 线程里调用同步接口, 直接执行, 避免投递给自己造成自锁
        if threading.current_thread() is self._worker:
            try:
                return func(*args)
            except Exception as e:  # noqa: BLE001 - worker 内兜底
                logger.error("源 SMTC 操作失败 [%s]: %s: %s", name, e.__class__.__name__, e)
                return None
        queue = self._queue
        if not self._running or queue is None:
            logger.debug("源 SMTC 未启动, 忽略同步调用: %s", name)
            return None
        job: dict[str, Any] = {"done": threading.Event(), "result": None, "error": None}
        with self._pending_lock:
            self._seq += 1
            job_id = self._seq
            self._pending[job_id] = job
        try:
            queue.put((job_id, func, args))
        except Exception as e:  # noqa: BLE001 - 队列异常不应抛给调用方
            with self._pending_lock:
                self._pending.pop(job_id, None)
            logger.error("源 SMTC 投递命令失败 [%s]: %s: %s", name, e.__class__.__name__, e)
            return None
        if not job["done"].wait(timeout=_CALL_TIMEOUT):
            # 超时: 从 pending 摘掉, worker 之后即使执行完也不会再回填
            with self._pending_lock:
                self._pending.pop(job_id, None)
            logger.error("源 SMTC 操作超时 (%ss): %s", _CALL_TIMEOUT, name)
            return None
        with self._pending_lock:
            self._pending.pop(job_id, None)
        if job["error"] is not None:
            return None
        return job["result"]

    def _post(self, func: Callable[..., Any], *args: Any) -> None:
        """只投递命令、不等待结果 (WinRT 事件回调用; 非阻塞)。"""
        queue = self._queue
        if self._closing or not self._running or queue is None:
            return
        try:
            queue.put((None, func, args))
        except Exception as e:  # noqa: BLE001 - 事件回调里绝不能抛
            logger.error("源 SMTC 投递命令失败: %s: %s", e.__class__.__name__, e)

    def _worker_loop(self) -> None:
        """唯一的源会话 WinRT 工作线程: 串行执行命令, 直到收到停止哨兵。"""
        queue = self._queue
        if queue is None:
            return
        logger.debug("源 SMTC worker 线程启动: %s", threading.current_thread().name)
        while True:
            try:
                item = queue.get(timeout=_IDLE_TIMEOUT)
            except Empty:
                if not self._running:
                    return
                continue
            if item is _STOP:
                return
            job_id, func, args = item
            job: dict[str, Any] | None = None
            if job_id is not None:
                with self._pending_lock:
                    job = self._pending.get(job_id)
            try:
                result = func(*args)
                if job is not None:
                    job["result"] = result
            except Exception as e:  # noqa: BLE001 - 命令失败只记录, 不中断 worker
                logger.error("源 SMTC 操作失败 [%s]: %s: %s",
                             getattr(func, "__name__", repr(func)), e.__class__.__name__, e)
                if job is not None:
                    job["error"] = e
            finally:
                if job is not None:
                    job["done"].set()

    def _dispatcher_loop(self) -> None:
        """回调分发线程: 只负责在**非 worker** 线程上调用 main 设置的回调。"""
        queue = self._notify_queue
        if queue is None:
            return
        logger.debug("源 SMTC 回调分发线程启动: %s", threading.current_thread().name)
        callbacks = {
            "source": "on_source_changed",
            "info": "on_info_changed",
            "timeline": "on_timeline_changed",
        }
        while True:
            try:
                kind = queue.get(timeout=_IDLE_TIMEOUT)
            except Empty:
                if not self._callbacks_running:
                    return
                continue
            if kind is _STOP:
                return
            with self._notify_lock:
                self._pending_events.discard(kind)
            if not self._callbacks_running:
                return
            attr = callbacks.get(kind)
            if attr is None:
                continue
            callback = getattr(self, attr, None)
            if not callable(callback):
                continue
            try:
                callback()
            except Exception as e:  # noqa: BLE001 - 回调异常不能打死分发线程
                logger.error("源 SMTC 回调异常 (%s): %s: %s", kind, e.__class__.__name__, e)

    def _notify(self, kind: str) -> None:
        """投递一条对外通知 (可合并; worker 线程调用, 非阻塞)。"""
        queue = self._notify_queue
        if self._closing or queue is None or not self._callbacks_running:
            return
        with self._notify_lock:
            if kind in self._pending_events:
                return
            self._pending_events.add(kind)
        queue.put(kind)

    # ============================================================ WinRT 事件回调
    # 下面三个函数运行在 WinRT 自己的线程上: 只投递, 不做任何 WinRT 调用, 也不调用公开接口
    def _handle_sessions_changed(self, *_args: Any) -> None:
        """系统 SMTC 会话列表变化 (只投递)。"""
        logger.debug("收到会话列表变化事件 (%s)", threading.current_thread().name)
        self._post(self._cmd_on_sessions_changed)

    def _handle_playback_info_changed(self, *_args: Any) -> None:
        """当前源会话播放信息变化 (只投递)。"""
        self._post(self._cmd_on_playback_info_changed)

    def _handle_timeline_changed(self, *_args: Any) -> None:
        """当前源会话进度条变化 (只投递)。"""
        self._post(self._cmd_on_timeline_changed)

    # ============================================================ worker 命令: 事件处理
    def _cmd_on_sessions_changed(self) -> None:
        """会话列表变化: 重新校验当前会话, 失效则按模式重新筛选。"""
        if self._closing or not self._running:
            return
        self._revalidate(notify_info=False)

    def _cmd_on_playback_info_changed(self) -> None:
        """播放信息变化: 重新校验; 有效则通知 main 信息变化, 失效则重新筛选。"""
        if self._closing or not self._running:
            return
        self._revalidate(notify_info=True)

    def _cmd_on_timeline_changed(self) -> None:
        """进度条变化: 只通知 main (不额外读 WinRT)。"""
        if self._closing:
            return
        if self._session is None:
            return
        self._notify("timeline")

    def _revalidate(self, notify_info: bool) -> None:
        """重新校验当前源会话 (必须在 worker 上执行)。

        失效条件 (契约 9.4): 当前会话不在系统会话列表, 或 title/artist 全空。
        失效时注销旧会话事件、清空会话, 按 ``platform_mode`` 重新 ``find()``, 并通知
        ``on_source_changed``; 有效时确保会话事件已注册, 必要时通知 ``on_info_changed``。
        """
        changed = False
        if self._session is not None:
            in_list = self._session_in_list(self._safe_get_sessions())
            if in_list:
                props = self._read_media_properties()
                if props is not None:
                    title = normalize_text(getattr(props, "title", ""))
                    artist = normalize_text(getattr(props, "artist", ""))
                    if title or artist:
                        self._ensure_session_events()
                        if notify_info:
                            self._notify("info")
                        return
            logger.info("源 SMTC 会话已失效: %s", self._app_id or "<未知>")
            self._release_session()
            changed = True
        found = self._cmd_find()
        if changed or found:
            self._notify("source")

    # ============================================================ 平台模式 / 筛选
    def set_platform_mode(self, mode: str) -> None:
        """设置源平台模式: ``auto`` / ``kugou`` / ``netease`` / ``qq``。

        立即生效: 若当前会话的平台与新显式模式不一致, 先释放再重新筛选;
        同步等待 worker 完成 (超时 5s)。非法值按 ``auto`` 处理。
        """
        normalized = str(mode or "").strip().lower()
        if normalized not in PLATFORM_MODES:
            logger.warning("未知的源平台模式 %r, 按 auto 处理", mode)
            normalized = PLATFORM_AUTO
        if not self._running:
            # 尚未启动: 先记下模式, 等 start() 后的 find() 生效
            self._platform_mode = normalized
            if normalized != PLATFORM_AUTO:
                self._preferred_platform = normalized
            logger.debug("源 SMTC 未启动, 先记录平台模式: %s", normalized)
            return
        self._call_sync(self._cmd_set_platform_mode, normalized)

    def _cmd_set_platform_mode(self, mode: str) -> bool:
        if self._closing:
            return False
        if mode not in PLATFORM_MODES:
            mode = PLATFORM_AUTO
        changed = mode != self._platform_mode
        self._platform_mode = mode
        if mode != PLATFORM_AUTO:
            # 显式模式同时也是 auto 模式的优先平台
            self._preferred_platform = mode
        logger.debug("源平台模式 -> %s (变化=%s)", mode, changed)
        if self._session is not None:
            session_platform = platform_from_app_id(self._app_id)
            if mode != PLATFORM_AUTO and session_platform != mode:
                logger.info("源平台模式切换为 %s, 释放当前会话 %s", mode, self._app_id)
                self._release_session()
                found = self._cmd_find()
                self._notify("source")
                return found
            # 当前会话仍然符合新模式: 保持不变
            if session_platform:
                self._platform = session_platform
            return True
        found = self._cmd_find()
        if found or changed:
            self._notify("source")
        return found

    def find(self) -> bool:
        """主动筛选/接管源会话; ``True`` 表示当前有绑定成功的源会话。

        分平台模式:

        * ``auto``: 优先保持当前(上次的)平台类型, 否则按会话列表顺序绑定任一已知平台;
        * 显式模式: 只绑定对应平台的会话。

        绑定状态发生变化时会在回调分发线程上通知 ``on_source_changed``。
        """
        before = (self._session is not None, self._app_id)
        found = bool(self._call_sync(self._cmd_find))
        after = (self._session is not None, self._app_id)
        if found and after != before:
            self._notify("source")
        return found

    def _cmd_find(self) -> bool:
        """在 worker 上执行真正的筛选/接管 (不要从这里再调用公开 ``find()``)。"""
        if self._closing:
            return False
        manager = self._ensure_manager()
        if manager is None:
            return False
        sessions = self._safe_get_sessions()
        if not sessions:
            logger.debug("系统里没有任何 SMTC 会话")
            return False
        mode = self._platform_mode
        candidates: list[tuple[Any, str, str]] = []
        for session in sessions:
            try:
                app_id = str(session.source_app_user_model_id or "")
            except Exception as e:  # noqa: BLE001 - 单个会话读失败不影响其它会话
                logger.debug("读取 SMTC 会话 app id 失败: %s: %s", e.__class__.__name__, e)
                continue
            platform = platform_from_app_id(app_id)
            logger.debug("SMTC 会话: %s -> %s", app_id, platform or "<未知平台>")
            if not platform:
                continue
            if mode != PLATFORM_AUTO and platform != mode:
                continue
            candidates.append((session, app_id, platform))
        if not candidates:
            logger.debug("没有找到符合模式 [%s] 的源 SMTC 会话", mode)
            return False
        if mode == PLATFORM_AUTO and self._preferred_platform:
            # 稳定排序: 优先保持当前(上次)平台类型, 组内保持会话列表原顺序
            preferred = self._preferred_platform
            candidates.sort(key=lambda item: 0 if item[2] == preferred else 1)
        session, app_id, platform = candidates[0]
        logger.info("找到源 SMTC 会话: %s (%s)", app_id, platform)
        return self._bind_session(session, app_id, platform)

    def release(self) -> None:
        """主动断开当前源会话 (注销它的 WinRT 事件); 有变化时通知 ``on_source_changed``。"""
        had_source = self._session is not None
        self._call_sync(self._cmd_release)
        if had_source:
            self._notify("source")

    def _cmd_release(self) -> None:
        if self._session is not None:
            logger.info("断开源 SMTC 会话: %s", self._app_id)
        self._release_session()

    # ============================================================ 会话绑定 (worker 内部)
    def _ensure_manager(self) -> SessionManager | None:
        """获取(缓存)系统 SMTC 会话管理器; 只在 worker 上调用。"""
        if self._manager is not None:
            return self._manager
        try:
            self._manager = _wait_async(SessionManager.request_async())
            logger.debug("已获取 SMTC SessionManager")
        except Exception as e:  # noqa: BLE001 - 拿不到管理器时返回 None, 由上层重试
            logger.error("获取 SMTC SessionManager 失败: %s: %s", e.__class__.__name__, e)
            self._manager = None
        return self._manager

    def _safe_get_sessions(self) -> list[Any]:
        """读取系统会话列表 (失败返回空列表); 只在 worker 上调用。"""
        manager = self._manager
        if manager is None:
            return []
        try:
            return list(manager.get_sessions())
        except Exception as e:  # noqa: BLE001
            logger.error("读取系统 SMTC 会话列表失败: %s: %s", e.__class__.__name__, e)
            return []

    def _session_in_list(self, sessions: list[Any]) -> bool:
        """当前会话是否仍在系统会话列表里 (先比对象, 再比 app id 兜底)。"""
        session = self._session
        if session is None:
            return False
        app_id = self._app_id
        for candidate in sessions:
            if candidate is session:
                return True
            try:
                if app_id and str(candidate.source_app_user_model_id or "") == app_id:
                    return True
            except Exception:  # noqa: BLE001 - 单个会话读失败忽略
                continue
        return False

    def _bind_session(self, session: Session, app_id: str, platform: str) -> bool:
        """接管一个源会话并注册它的事件 (幂等); 只在 worker 上调用。"""
        if (session is self._session and app_id == self._app_id
                and platform == self._platform):
            self._ensure_session_events()
            return True
        self._release_session()
        self._session = session
        self._app_id = app_id
        self._platform = platform
        self._preferred_platform = platform
        self._register_session_events()
        return True

    def _release_session(self) -> None:
        """注销当前会话事件并清空引用 (不碰 manager); 只在 worker 上调用。"""
        self._unregister_session_events()
        self._session = None
        self._app_id = ""
        self._platform = ""

    def _register_session_events(self) -> None:
        """注册当前会话的播放信息/进度条事件 (先注销旧的, 幂等); 只在 worker 上调用。"""
        if self._session is None:
            return
        self._unregister_session_events()
        try:
            self._playback_handler = self._handle_playback_info_changed
            self._timeline_handler = self._handle_timeline_changed
            self._playback_token = self._session.add_playback_info_changed(
                self._playback_handler)
            self._timeline_token = self._session.add_timeline_properties_changed(
                self._timeline_handler)
            logger.debug("已注册源会话事件: %s (线程=%s)", self._app_id,
                         threading.current_thread().name)
        except Exception as e:  # noqa: BLE001 - 注册失败只记录, 不影响读取
            logger.warning("注册源会话事件失败: %s: %s", e.__class__.__name__, e)

    def _ensure_session_events(self) -> None:
        """确保当前会话的事件已注册 (token 丢失时重注册); 只在 worker 上调用。"""
        if self._session is None:
            return
        if self._playback_token is None or self._timeline_token is None:
            self._register_session_events()

    def _unregister_session_events(self) -> None:
        """注销当前会话的全部事件; 只在 worker 上调用。"""
        session = self._session
        if session is not None:
            for token, remover_name in (
                    (self._playback_token, "remove_playback_info_changed"),
                    (self._timeline_token, "remove_timeline_properties_changed")):
                if token is None:
                    continue
                try:
                    getattr(session, remover_name)(token)
                except Exception as e:  # noqa: BLE001 - 注销失败不应阻断停止流程
                    logger.warning("注销源会话事件失败: %s: %s", e.__class__.__name__, e)
        self._playback_token = None
        self._timeline_token = None
        self._playback_handler = None
        self._timeline_handler = None

    # ============================================================ 读取: 属性/状态/进度/缩略图
    def _read_media_properties(self) -> Any:
        """读取当前会话的媒体属性 (失败返回 None); 只在 worker 上调用。"""
        session = self._session
        if session is None:
            return None
        try:
            return _wait_async(session.try_get_media_properties_async())
        except Exception as e:  # noqa: BLE001
            logger.debug("读取源 SMTC 媒体属性失败: %s: %s", e.__class__.__name__, e)
            return None

    def _read_thumbnail_bytes(self, props: Any) -> bytes | None:
        """把媒体属性里的缩略图读成原始字节; 任何失败都返回 None (只在 worker 上调用)。

        链路与契约一致: ``thumbnail.open_read_async`` -> ``DataReader`` ->
        ``CryptographicBuffer.copy_to_byte_array``。
        """
        try:
            thumbnail = getattr(props, "thumbnail", None)
            if thumbnail is None:
                return None
            stream = _wait_async(thumbnail.open_read_async())
            if stream is None:
                return None
            count = int(getattr(stream, "size", 0) or 0)
            if count <= 0:
                return None
            try:
                # open_read_async 返回的流定位在 0; 防御性复位, 否则 load 可能越界
                stream.seek(0)
            except Exception:  # noqa: BLE001 - 复位失败就按当前位置读
                pass
            reader = DataReader(stream)
            try:
                _wait_async(reader.load_async(count))
                buffer = reader.read_buffer(count)
                if buffer is None:
                    return None
                data = CryptographicBuffer.copy_to_byte_array(buffer)
                if not data:
                    return None
                return bytes(data)
            finally:
                try:
                    reader.detach_stream()
                except Exception:  # noqa: BLE001 - 释放失败不影响已读出的字节
                    pass
        except Exception as e:  # noqa: BLE001 - 缩略图只是锦上添花, 失败给 None
            logger.debug("读取源 SMTC 缩略图失败: %s: %s", e.__class__.__name__, e)
            return None

    def valid(self) -> bool:
        """当前源会话是否仍然有效: 仍在系统列表里, 且 title/artist 至少一个非空。"""
        return bool(self._call_sync(self._cmd_valid))

    def _cmd_valid(self) -> bool:
        if self._session is None:
            return False
        if not self._session_in_list(self._safe_get_sessions()):
            return False
        props = self._read_media_properties()
        if props is None:
            return False
        title = normalize_text(getattr(props, "title", ""))
        artist = normalize_text(getattr(props, "artist", ""))
        return bool(title or artist)

    def snapshot(self, include_thumbnail: bool = True) -> SourceSnapshot | None:
        """在 worker 上读取当前源会话的纯数据快照; 没有源会话/读取失败返回 None。

        ``include_thumbnail=False`` 时跳过缩略图读取 (进度条这类高频刷新用)。
        """
        result = self._call_sync(self._cmd_snapshot, bool(include_thumbnail))
        return result if isinstance(result, SourceSnapshot) else None

    def _cmd_snapshot(self, include_thumbnail: bool) -> SourceSnapshot | None:
        session = self._session
        if session is None:
            return None
        props = self._read_media_properties()
        if props is None:
            return None
        snapshot = SourceSnapshot(
            app_id=self._app_id,
            platform=self._platform or platform_from_app_id(self._app_id) or "unknown",
            title=normalize_text(getattr(props, "title", "")),
            artist=normalize_text(getattr(props, "artist", "")),
            album_title=normalize_text(getattr(props, "album_title", "")),
            album_artist=normalize_text(getattr(props, "album_artist", "")),
        )
        # 播放状态 (WinRT 枚举名的小写: playing / paused / stopped / ...)
        try:
            info = session.get_playback_info()
            status = getattr(info, "playback_status", None)
            status_name = getattr(status, "name", None)
            snapshot.playback_status = str(status_name).lower() if status_name else "unknown"
        except Exception as e:  # noqa: BLE001
            logger.debug("读取源 SMTC 播放状态失败: %s: %s", e.__class__.__name__, e)
        # 进度 (TimeSpan -> 秒)
        try:
            timeline = session.get_timeline_properties()
        except Exception as e:  # noqa: BLE001
            logger.debug("读取源 SMTC 进度失败: %s: %s", e.__class__.__name__, e)
            timeline = None
        if timeline is not None:
            snapshot.timeline_start = _seconds(getattr(timeline, "start_time", None))
            snapshot.timeline_position = _seconds(getattr(timeline, "position", None))
            snapshot.timeline_end = _seconds(getattr(timeline, "end_time", None))
        # 缩略图原始字节 (失败给 None)
        if include_thumbnail:
            snapshot.thumbnail = self._read_thumbnail_bytes(props)
        return snapshot

    def read_thumbnail(self) -> bytes | None:
        """单独读取当前源会话的缩略图原始字节 (失败返回 None)。"""
        result = self._call_sync(self._cmd_read_thumbnail)
        return result if isinstance(result, bytes) else None

    def _cmd_read_thumbnail(self) -> bytes | None:
        if self._session is None:
            return None
        props = self._read_media_properties()
        if props is None:
            return None
        return self._read_thumbnail_bytes(props)

    # ============================================================ 状态查询 (无 WinRT 调用)
    def has_source(self) -> bool:
        """当前是否绑定了源会话 (只读本地状态, 不访问 WinRT)。"""
        return self._session is not None

    def source_platform(self) -> str:
        """当前源会话平台: ``kugou`` / ``netease`` / ``qq``; 无源返回空串。"""
        return self._platform if self._session is not None else ""

    def source_app_id(self) -> str:
        """当前源会话的 app id; 无源返回空串。"""
        return self._app_id if self._session is not None else ""

    # ============================================================ 按钮转发
    def send_button(self, name: str) -> None:
        """异步转发按钮命令到源会话: ``play`` / ``pause`` / ``next`` / ``previous``。

        未知按钮名只记 debug 后忽略; 不等待结果, 不阻塞调用方。
        """
        self._post(self._cmd_send_button, str(name or "").strip().lower())

    def _cmd_send_button(self, name: str) -> None:
        method_name = _BUTTON_METHODS.get(name)
        if method_name is None:
            logger.debug("忽略未知的按钮命令: %r", name)
            return
        if self._session is None:
            logger.debug("没有源会话, 忽略按钮命令: %s", name)
            return
        try:
            logger.info("转发按钮到源会话: %s", name)
            _wait_async(getattr(self._session, method_name)())
        except Exception as e:  # noqa: BLE001 - 按钮失败只记录
            logger.warning("转发源会话按钮失败 [%s]: %s: %s", name, e.__class__.__name__, e)

    # ============================================================ 事件注册/注销 (worker 命令)
    def _cmd_register_manager_events(self) -> bool:
        """在 worker 上获取 SessionManager 并注册会话列表变化事件。"""
        if self._closing:
            return False
        manager = self._ensure_manager()
        if manager is None:
            return False
        try:
            if self._manager_token is None:
                self._manager_handler = self._handle_sessions_changed
                self._manager_token = manager.add_sessions_changed(self._manager_handler)
            logger.debug("已注册系统会话列表事件 (线程=%s)",
                         threading.current_thread().name)
            return True
        except Exception as e:  # noqa: BLE001
            logger.error("注册系统会话列表事件失败: %s: %s", e.__class__.__name__, e)
            self._manager_token = None
            self._manager_handler = None
            return False

    def _cmd_shutdown(self) -> None:
        """在 worker 上注销全部 WinRT 事件并清空 WinRT 引用 (stop 的第一步)。"""
        manager = self._manager
        if manager is not None and self._manager_token is not None:
            try:
                manager.remove_sessions_changed(self._manager_token)
                logger.debug("已注销系统会话列表事件")
            except Exception as e:  # noqa: BLE001 - 注销失败不应阻断停止流程
                logger.warning("注销系统会话列表事件失败: %s: %s", e.__class__.__name__, e)
        self._manager_token = None
        self._manager_handler = None
        self._release_session()
        self._manager = None


__all__ = [
    "PLATFORM_KUGOU",
    "PLATFORM_NETEASE",
    "PLATFORM_QQ",
    "PLATFORM_AUTO",
    "PLATFORM_MODES",
    "PLATFORM_MARKERS",
    "WORKER_THREAD_NAME",
    "CALLBACK_THREAD_NAME",
    "SourceSnapshot",
    "SourceSMTC",
    "platform_from_app_id",
]
