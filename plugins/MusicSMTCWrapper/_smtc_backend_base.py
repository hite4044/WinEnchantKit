# -*- coding: utf-8 -*-
"""平台无关契约层: 数据类 / 缓存器 / 后端基类 (模板方法)。

本模块承载 MusicSMTCWrapper 的“契约层”:

* :class:`StandardMusicInfo` —— 纯数据 dataclass (一组歌曲信息的包装, 不参与缓存)
* :class:`MusicCache`        —— 缓存的加载 / 保存 / 查找器, 由后端持有
* :class:`MusicBackend`      —— 后端抽象基类, 用模板方法串起
  ``from_cache -> from_web -> from_raw_smtc`` 的统一流程, 派生类只写自己的业务
* 通用工具: ``wait_result`` / ``plugin_logger`` / ``make_music_hash`` / ``sanitize_filename``

命名说明: 文件名带 ``_smtc_backend_`` 前缀是刻意的。WinEnchantKit 的插件加载器会把
插件目录插到 ``sys.path[0]``(见 ``gui/control_panel.py::load_plugin``), 而 plugins.HDKugouCover
已经在 ``sys.modules`` 里占用了顶层名字 ``backend`` 与 ``base``; 使用私有前缀可以保证本插件的
模块在任何插件加载顺序下都能被唯一解析, 且不会污染/破坏其它插件。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from os import makedirs
from os.path import isfile, join
from time import sleep
from typing import Any, Generator, Literal

from PIL import Image
from winsdk.windows.foundation import AsyncStatus, IAsyncOperation
from winsdk.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionMediaProperties as SessionMediaProperties,
)


def _resolve_plugin_logger():
    """优先使用 WinEnchantKit 的插件 logger (带插件名前缀与文件落盘)。

    ``lib.log`` 在导入时会打开 ``%APPDATA%\\WinEnchantKit\\logs\\*.log``; 独立自测
    (例如在插件目录下直接跑脚本) 时该路径可能不可写, 此时退回标准 logging,
    保证本模块仍可被导入与单测。插件在 WinEnchantKit 内运行时走的始终是
    ``get_plugin_logger``, 日志行为与旧实现一致。
    """
    fallback = logging.getLogger("WinEnchantKitLogger_smtc_wrapper")
    try:
        from lib.log import get_plugin_logger
        return get_plugin_logger("smtc_wrapper", "音乐封面高清化")
    except Exception as e:  # noqa: BLE001 - 自测环境不可写日志文件时不应阻断插件导入
        fallback.debug(f"使用标准 logger 代替插件 logger: {e.__class__.__name__}: {e}")
        return fallback


# 插件 id 必须与 plugin.json 的 id 保持一致 (smtc_wrapper)
plugin_logger = _resolve_plugin_logger()
logger = plugin_logger

# 所有后端缓存目录的公共父目录
CACHE_ROOT = "cache"

# 默认封面尺寸 (与原 main.py 的 load_cover 默认值一致)
DEFAULT_COVER_SIZE = 480
# 「获取最大封面」的特殊尺寸值
FULL_COVER_SIZE = -1
# 封面尺寸硬限制: 超过该值一律按“最大封面”处理
MAX_COVER_SIZE = 8000

ImageFormat = Literal["JPEG", "PNG"]
# None 表示「保持图片原有格式」
SaveFormat = ImageFormat | None

# Windows 文件名非法字符 + 控制字符
_ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# 封面缓存文件名尾部: _{hash}_{size}.{后缀} (歌名/作者里可能有下划线, 所以必须从右侧解析)
_COVER_SUFFIX_PATTERN = re.compile(r"^(?P<hash>[0-9a-fA-F]+)_(?P<size>\d+)\.(?P<ext>jpg|jpeg|png)$")
_COVER_MAX_NAME_LEN = 80


def normalize_size(size: int) -> int:
    """封面尺寸归一: 非法值给默认值, 超过硬限制按“最大封面”处理"""
    try:
        size = int(size)
    except (TypeError, ValueError):
        return DEFAULT_COVER_SIZE
    if size == FULL_COVER_SIZE or size > MAX_COVER_SIZE:
        return FULL_COVER_SIZE
    return size if size > 0 else DEFAULT_COVER_SIZE


def parse_cover_filename(filename: str) -> tuple[int, int] | None:
    """从 ``{歌曲名} - {作者}_{hash}_{size}.{后缀}`` 里解出 (music_hash, size)。

    歌名/作者可能包含下划线或点号, 因此只用「最后一个下划线 + 扩展名」作为分隔锚点。
    """
    stem, dot, _ext = filename.rpartition(".")
    if not dot:
        return None
    head, underscore, size_part = stem.rpartition("_")
    if not underscore or not size_part.isdigit():
        return None
    _prefix, hash_sep, hash_part = head.rpartition("_")
    if not hash_sep:
        return None
    try:
        return int(hash_part, 16), int(size_part)
    except ValueError:
        return None


def wait_result(opt: IAsyncOperation):
    """轮询等待 IAsyncOperation 完成并取回结果 (原 backend\\base_backend.py::wait_result)"""
    while opt.status != AsyncStatus.COMPLETED:
        sleep(0.1)
    return opt.get_results()


def make_music_hash(*parts: Any) -> int:
    """由歌曲信息生成稳定的 64 位歌曲指纹 (缓存键)。

    str 的 ``hash()`` 在进程间不稳定, 不能落盘做缓存键, 因此这里用 sha256 截断。
    """
    raw = "\x1f".join("" if p is None else str(p) for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big")


def sanitize_filename(name: str) -> str:
    """把任意歌曲名/作者名清洗成合法且长度可控的文件名片段"""
    cleaned = _ILLEGAL_FILENAME_CHARS.sub("_", str(name or "")).strip().rstrip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if len(cleaned) > _COVER_MAX_NAME_LEN:
        cleaned = cleaned[:_COVER_MAX_NAME_LEN].rstrip()
    return cleaned or "unknown"


@dataclass
class StandardMusicInfo:
    """标准化歌曲信息: 纯数据 dataclass, 只是一组歌曲信息的包装 (不参与缓存)。

    ``cover`` 是后端按 ``size`` 取好的 ``PIL.Image.Image``; ``has_cover`` 为 False 表示
    没有可用封面 (此时 ``cover`` 为 None), ``Plugin``/``SMTCHost`` 会保留源平台原缩略图。
    """

    title: str
    artist: str
    album_title: str
    album_artist: str
    length: float
    cover: Image.Image | None = None
    has_cover: bool = False

    @classmethod
    def from_props(cls, props: SessionMediaProperties, length: float = 0.0) -> "StandardMusicInfo":
        """由源 SMTC 媒体属性填充 (不包含封面); 空值防御"""
        return cls(
            title=str(getattr(props, "title", "") or ""),
            artist=str(getattr(props, "artist", "") or ""),
            album_title=str(getattr(props, "album_title", "") or ""),
            album_artist=str(getattr(props, "album_artist", "") or ""),
            length=length,
            cover=None,
            has_cover=False,
        )


@dataclass
class MusicCache:
    """缓存的加载 / 保存 / 查找器, 由后端持有。

    只管存储与查找, 不参与缓存数据的编撰与解读 (那是后端的职责):

    * 歌曲信息: ``dict[int, dict[str, Any]]`` —— key 是后端提供的歌曲 hash,
      value 是后端提供的具体歌曲信息; 整体存在 ``info_file`` 指向的 JSON 里。
    * 封面: 按 ``{歌曲名} - {作者}_{music_hash}_{尺寸}.{后缀}`` 命名存放在
      ``cover_dir``; 用内存索引 ``hash -> [(尺寸, 文件名)]`` 加速查找。
    * 封面尺寸硬限制 8000x8000; ``"full"`` 标记由后端在信息里自行记录。
    """

    cover_dir: str
    info_file: str
    save_format: SaveFormat = None
    save_quality: int = 90
    _info: dict[int, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _loaded: bool = field(default=False, init=False, repr=False)
    _index: dict[int, list[tuple[int, str]]] = field(default_factory=dict, init=False, repr=False)
    _indexed: bool = field(default=False, init=False, repr=False)

    # ------------------------------------------------------------------ 歌曲信息
    def load(self) -> dict[int, dict[str, Any]]:
        """把歌曲信息 JSON 读进内存"""
        if isfile(self.info_file):
            try:
                with open(self.info_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._info = {int(key, base=16): value for key, value in data.items()} if isinstance(data, dict) else {}
            except (OSError, ValueError) as e:
                logger.warning(f"读取歌曲信息缓存失败: {e}")
                self._info = {}
        self._loaded = True
        return self._info

    def save(self):
        """把歌曲信息 JSON 落盘"""
        if not self._loaded:
            self.load()
        parent = os.path.dirname(self.info_file)
        if parent:
            makedirs(parent, exist_ok=True)
        try:
            with open(self.info_file, "w+", encoding="utf-8") as f:
                f.write(json.dumps({str(k): v for k, v in self._info.items()},
                                   ensure_ascii=False, indent=4))
        except OSError as e:
            logger.warning(f"保存歌曲信息缓存失败: {e}")

    def get(self, music_hash: int) -> dict[str, Any] | None:
        if not self._loaded:
            self.load()
        return self._info.get(music_hash)

    def put(self, music_hash: int, value: dict[str, Any]):
        if not self._loaded:
            self.load()
        self._info[music_hash] = value

    # ------------------------------------------------------------------ 封面文件
    def reload_index(self) -> dict[int, list[tuple[int, str]]]:
        """重建封面内存索引: music_hash -> [(尺寸, 文件名), ...]"""
        index: dict[int, list[tuple[int, str]]] = {}
        try:
            names = os.listdir(self.cover_dir)
        except FileNotFoundError:
            names = []
        except OSError as e:
            logger.warning(f"读取封面缓存目录失败: {e}")
            names = []
        for name in names:
            parsed = parse_cover_filename(name)
            if parsed is None:
                continue
            music_hash, size = parsed
            index.setdefault(music_hash, []).append((size, name))
        for entries in index.values():
            entries.sort()
        self._index = index
        self._indexed = True
        return index

    def _ensure_index(self):
        if not self._indexed:
            self.reload_index()

    def sizes(self, music_hash: int) -> list[int]:
        """本地实际存在的封面尺寸列表 (升序)"""
        self._ensure_index()
        return [size for size, _name in self._index.get(music_hash, [])]

    def find_cover(self, music_hash: int, size: int) -> Image.Image | None:
        """按 ``hash + 尺寸`` 查封面: 优先 ``>= size`` 的最小者, 没有则取最大者并缩放"""
        self._ensure_index()
        entries = self._index.get(music_hash)
        if not entries:
            return None
        if size == FULL_COVER_SIZE:
            picked = entries[-1]
        else:
            picked = next(((s, n) for s, n in entries if s >= size), entries[-1])
        path, _actual = join(self.cover_dir, picked[1]), picked[0]
        try:
            with Image.open(path) as image:
                image.load()
                cover = image.copy()
        except OSError as e:
            logger.warning(f"读取封面缓存失败: {path} -> {e}")
            return None
        if size != FULL_COVER_SIZE and max(cover.size) > size:
            cover.thumbnail((size, size))
        return cover

    def save_cover(self, cover: Image.Image, music_hash: int, size: int, title: str = "",
                   artist: str = "") -> int:
        """把封面按 ``{歌曲名} - {作者}_{hash}_{尺寸}`` 存进缓存目录, 返回落盘的标记尺寸。

        ``size`` 为 ``FULL_COVER_SIZE`` 时表示“这是最大封面”, 文件名里用``图片真实尺寸``,
        便于日后按尺寸选取; 返回值即该标记尺寸。
        """
        makedirs(self.cover_dir, exist_ok=True)
        slot = max(cover.size) if size == FULL_COVER_SIZE else int(size)
        format_ = self.save_format or ("PNG" if getattr(cover, "format", None) == "PNG" else "JPEG")
        ext = "png" if format_ == "PNG" else "jpg"
        prefix = f"{sanitize_filename(title)} - {sanitize_filename(artist)}" if (title or artist) else ""
        name = f"{prefix}_{music_hash:x}_{slot}.{ext}" if prefix else f"{music_hash:x}_{slot}.{ext}"
        path = join(self.cover_dir, name)
        if format_ == "PNG":
            cover.convert("RGBA").save(path, "PNG")
        else:
            cover.convert("RGB").save(path, "JPEG", quality=self.save_quality)
        self._ensure_index()
        entries = [entry for entry in self._index.get(music_hash, []) if entry[0] != slot]
        entries.append((slot, name))
        entries.sort()
        self._index[music_hash] = entries
        return slot

    # ------------------------------------------------------------------ 清理
    def clear(self) -> Generator[tuple[int, int, str], None, None]:
        """删除全部缓存 (封面文件 + 歌曲信息), 逐个 yield ``(已处理数, 总数, 当前项)``"""
        self._ensure_index()
        try:
            names = os.listdir(self.cover_dir)
        except OSError:
            names = []
        total = len(names) + (1 if isfile(self.info_file) else 0)
        done = 0
        for name in names:
            try:
                os.remove(join(self.cover_dir, name))
            except OSError:
                logger.warning(f"删除文件失败: {name}")
            done += 1
            yield done, total, name
        if isfile(self.info_file):
            try:
                os.remove(self.info_file)
            except OSError:
                logger.warning(f"删除文件失败: {self.info_file}")
            done += 1
            yield done, total, self.info_file
        self._info = {}
        self._loaded = True
        self._index = {}
        self._indexed = True

    def count(self) -> int:
        """缓存目录里的文件数 (用于展示进度基数)"""
        try:
            return len(os.listdir(self.cover_dir))
        except OSError:
            return 0


class MusicBackend(ABC):
    """音乐平台后端基类 (模板方法)。

    基类负责统一流程: ``get_music_info`` 依次尝试
    ``from_cache()`` -> ``from_web()`` -> ``from_raw_smtc()``,
    某一步返回 None 就进入下一步; ``force_refresh=True`` 时跳过 ``from_cache``。

    派生类只需实现:
      * ``from_cache`` / ``from_web`` / ``from_raw_smtc`` —— 各自的业务;
      * ``encode_record`` / ``decode_record`` —— 缓存数据的编撰与解读;
      * 声明 ``platform_id`` / ``cache_name`` / ``cover_format``。
    """

    platform_id: str = "unknown"
    cache_name: str = "unknown"
    # 封面落盘格式: None 表示保持图片原有格式
    cover_format: SaveFormat = None
    cover_quality: int = 90

    # ------------------------------------------------------------------ 缓存器
    @property
    def cache_dir(self) -> str:
        """每个后端独立缓存目录, 如 cache\\kugou\\"""
        return join(CACHE_ROOT, self.cache_name)

    @property
    def music_cache(self) -> MusicCache:
        cache = getattr(self, "_music_cache", None)
        if cache is None:
            cache = MusicCache(join(self.cache_dir, "covers"),
                               join(self.cache_dir, "music_info.json"),
                               self.cover_format, self.cover_quality)
            self._music_cache = cache
        return cache

    def update_config(self, cover_format: SaveFormat = None, cover_quality: int = 90):
        """配置变更入口: 封面落盘格式 / 质量 (无需重建后端对象)"""
        self.cover_format = cover_format
        self.cover_quality = int(cover_quality)
        if getattr(self, "_music_cache", None) is not None:
            self._music_cache.save_format = cover_format
            self._music_cache.save_quality = int(cover_quality)

    def load_cache(self):
        """载入本地缓存 (歌曲信息 + 封面索引)"""
        self.music_cache.load()
        self.music_cache.reload_index()

    def save_cache(self):
        """把本地缓存落盘"""
        self.music_cache.save()

    def clear_cache(self) -> Generator[tuple[int, int, str], None, None]:
        """清空该后端的全部缓存, 逐个 yield ``(已处理数, 总数, 当前项)`` 用于展示进度"""
        yield from self.music_cache.clear()

    # ------------------------------------------------------------------ 统一流程
    def get_music_info(self, props: SessionMediaProperties, size: int = DEFAULT_COVER_SIZE,
                       force_refresh: bool = False) -> StandardMusicInfo:
        """获取歌曲信息: 本地缓存 -> 联网搜索下载 -> 原 SMTC 低清封面。

        ``music_info`` 由原 SMTC 信息填充 (不含封面), 三个步骤共用 ``(music_info, size)``。
        """
        size = normalize_size(size)
        music_info = StandardMusicInfo.from_props(props)
        steps = (self.from_web, self.from_raw_smtc) if force_refresh \
            else (self.from_cache, self.from_web, self.from_raw_smtc)
        for step in steps:
            try:
                result = step(music_info, size, props)
            except Exception as e:
                logger.error(f"{step.__name__} 失败: {e.__class__.__name__}: {e}")
                continue
            if result is not None:
                logger.debug(f"{step.__name__} 命中: {result.title} - {result.artist}")
                return result
        logger.warning(f"没有找到任何歌曲信息与封面: {music_info.title} - {music_info.artist}")
        return music_info

    # ------------------------------------------------------------------ 派生类实现
    @abstractmethod
    def from_cache(self, music_info: StandardMusicInfo, size: int,
                   props: SessionMediaProperties) -> StandardMusicInfo | None:
        """本地缓存: 命中返回带封面的歌曲信息, 未命中/加载失败返回 None"""

    @abstractmethod
    def from_web(self, music_info: StandardMusicInfo, size: int,
                 props: SessionMediaProperties) -> StandardMusicInfo | None:
        """联网搜索: 搜到后写入缓存并返回带封面的歌曲信息, 失败返回 None"""

    @abstractmethod
    def from_raw_smtc(self, music_info: StandardMusicInfo, size: int,
                      props: SessionMediaProperties) -> StandardMusicInfo | None:
        """原 SMTC 低清封面兜底: 拿不到返回 None"""

    @abstractmethod
    def encode_record(self, music_info: StandardMusicInfo, cover_meta: dict[str, Any]) -> dict[str, Any]:
        """把歌曲信息编撰成可 JSON 化的缓存记录 (缓存数据的解读由后端负责)"""

    @abstractmethod
    def decode_record(self, record: dict[str, Any], music_info: StandardMusicInfo) -> StandardMusicInfo | None:
        """把缓存记录解读回歌曲信息 (封面由 ``MusicCache.find_cover`` 负责), 失败返回 None"""
