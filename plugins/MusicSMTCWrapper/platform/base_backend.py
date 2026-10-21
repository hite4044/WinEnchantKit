# -*- coding: utf-8 -*-
"""平台无关后端基础层: 数据类 / 缓存 / 封面处理 / 相似度 / 模板流程。

本模块是 ``plugins/MusicSMTCWrapper/platform`` 下各平台后端的公共基座, 实现
``ARCHITECTURE.md`` 第 6 节的契约:

* :class:`CoverCandidate` / :class:`ResolvedMusic` —— 搜索候选与解析结果数据类;
* :class:`_MusicCache` —— 私有缓存器 (歌曲信息 JSON + 封面文件索引);
* :class:`BaseMusicBackend` —— 后端抽象基类; :meth:`BaseMusicBackend.fetch` 是模板方法:
  ``本地缓存 -> 联网搜索 -> 选候选 -> 解析封面地址 -> 下载 -> 写缓存``;
* 工具函数: :func:`normalize_size` / :func:`prepare_cover` / :func:`make_cache_key` /
  :func:`sanitize_filename` / :func:`parse_cover_filename` / :func:`cover_similarity`。

子类**只需要**实现 :meth:`BaseMusicBackend.search` 与
:meth:`BaseMusicBackend.resolve_cover_url`; 缓存读写 / 封面尺寸 / 相似度匹配 / 落盘格式
全部由本模块的模板流程负责。:meth:`BaseMusicBackend.encode_record` /
:meth:`BaseMusicBackend.decode_record` 有默认模板实现 (缓存匹配后的歌曲信息 + cover_ref),
需要自定义缓存内容时才重写。

约定 (与契约一致):

* 封面尺寸用 ``FULL_COVER_SIZE`` (-1) 表示「最大封面」, 数字表示按该尺寸取回;
* 封面缓存文件名 ``{歌曲名} - {作者}_{key:016x}_{尺寸}.{jpg|png}``, 最大封面尺寸段为 ``_full``;
* 查找顺序: 等于请求尺寸 -> 大于请求尺寸的最小者 -> 仅当存在 ``_full`` 时回退 ``_full`` -> None;
* 落盘格式: ``cover_format`` 为 None 时保持图片自身格式 (PNG/JPEG), 否则转指定格式;
* 相似度: 两端统一成 16x16 灰度后做 Pearson 相关系数, 线性映射到 ``0.0 ~ 1.0``。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from abc import ABC, abstractmethod
from collections.abc import Generator, Mapping
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Literal

import requests
from PIL import Image

from ..music_base import LOGGER_NAME, MusicInfo, text_key

logger = logging.getLogger(LOGGER_NAME)

# ---------------------------------------------------------------- 模块常量 (契约 6.1)
# 默认封面尺寸
DEFAULT_COVER_SIZE = 480
# 「获取最大封面」的请求尺寸
FULL_COVER_SIZE = -1
# 封面尺寸硬上限, 超过按「最大封面」处理
MAX_COVER_SIZE = 8000
# 缓存根目录 (相对进程工作目录, 已被 .gitignore)
CACHE_ROOT = "cache"
# 相似度比对时下载候选封面的统一尺寸
SIMILARITY_COVER_SIZE = 150
# 相似度阈值, >= 才认为同一张封面
SIMILARITY_THRESHOLD = 0.85
# 相似度最多比较的候选数
SIMILARITY_MAX_CANDIDATES = 8
# 封面下载超时 (秒)
COVER_TIMEOUT = 8.0

# 文件名里标识「最大封面」的尺寸段 (自带前导下划线)
FULL_SLOT = "_full"

# 封面落盘格式: None 表示保持图片原有格式
SaveFormat = Literal["JPEG", "PNG"] | None

# 相似度缩略图边长 (16x16 灰度足够区分「是不是同一张图」, 且计算量极小)
_SIMILARITY_SIDE = 16
# 缓存文件名里歌名/作者片段的最大长度
_FILENAME_MAX_LEN = 60
# 默认 JPEG 质量
_DEFAULT_QUALITY = 90
# http_get 默认 UA (子类可通过 request_headers 重写)
_DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": "MusicSMTCWrapper (WinEnchantKit)",
}

# Windows 文件名非法字符 + 控制字符
_ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# 相似度缩放用重采样算法 (兼容旧 Pillow: Image.Resampling 在新版本才有)
_RESAMPLE = getattr(Image, "Resampling", Image).LANCZOS


# ====================================================================== 数据类 (契约 6.2)
@dataclass
class CoverCandidate:
    """平台搜索返回的一个候选 (平台私有细节放在 cover_ref 里, 必须可 JSON 化)"""

    info: MusicInfo
    cover_ref: str = ""


@dataclass
class ResolvedMusic:
    """后端解析结果: 匹配/补全后的静态信息 + 已按请求尺寸备好的封面"""

    info: MusicInfo
    cover: Image.Image | None
    # 本地缓存标记尺寸 (最大封面时是图片真实尺寸)
    cover_size: int = 0
    from_cache: bool = False
    platform: str = ""


# ====================================================================== 工具函数
def normalize_size(size: int) -> int:
    """封面尺寸归一: 非法值给默认值, 超过硬上限 / 显式 ``-1`` 按「最大封面」处理"""
    try:
        value = int(size)
    except (TypeError, ValueError):
        return DEFAULT_COVER_SIZE
    if value == FULL_COVER_SIZE or value > MAX_COVER_SIZE:
        return FULL_COVER_SIZE
    return value if value > 0 else DEFAULT_COVER_SIZE


def prepare_cover(cover: Image.Image, size: int) -> Image.Image:
    """按请求尺寸缩放封面 (**不放大**)。

    ``size`` 为 ``FULL_COVER_SIZE`` 或封面本身不大于请求尺寸时原样返回同一对象;
    需要缩小的时候原地 ``thumbnail`` (调用方拿到的封面都是本模块刚下载/刚读缓存的新图,
    不存在共享引用问题)。
    """
    if cover is None:
        return cover
    try:
        request = normalize_size(size)
    except (TypeError, ValueError):
        request = DEFAULT_COVER_SIZE
    if request != FULL_COVER_SIZE and max(cover.size) > request:
        cover.thumbnail((request, request))
    return cover


def make_cache_key(platform: str, info: MusicInfo) -> int:
    """由平台 + 源歌曲信息生成稳定的 64 位缓存键 (跨进程稳定)。

    缓存键**只用**源歌曲信息的 ``title + artist`` (归一化后), 再加平台名前缀;
    ``str.__hash__`` 跨进程不稳定, 因此用 sha256 截断 64 位, 落盘时以 16 位十六进制保存。
    """
    raw = "\x1f".join((
        text_key(platform),
        text_key(getattr(info, "title", "")),
        text_key(getattr(info, "artist", "")),
    )).encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big")


def sanitize_filename(name: str) -> str:
    """把任意歌曲名/作者名清洗成合法且长度可控的文件名片段"""
    cleaned = _ILLEGAL_FILENAME_CHARS.sub("_", str(name or "")).strip().rstrip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if len(cleaned) > _FILENAME_MAX_LEN:
        cleaned = cleaned[:_FILENAME_MAX_LEN].rstrip()
    return cleaned or "unknown"


def parse_cover_filename(filename: str) -> tuple[int, int | None] | None:
    """从 ``{歌曲名} - {作者}_{key:016x}_{尺寸}.{后缀}`` 解出 ``(缓存键, 尺寸标记)``。

    尺寸标记为 ``None`` 表示文件名用的是 ``_full`` (最大封面); 歌名/作者可能包含下划线
    或点号, 因此只用「最后一个下划线 + 扩展名」作为分隔锚点。
    """
    stem, dot, _ext = filename.rpartition(".")
    if not dot:
        return None
    head, underscore, size_part = stem.rpartition("_")
    if not underscore:
        return None
    if size_part.lstrip("_") == FULL_SLOT.lstrip("_"):
        size: int | None = None
    elif size_part.isdigit():
        size = int(size_part)
    else:
        return None
    _prefix, hash_sep, hash_part = head.rpartition("_")
    if not hash_sep:
        return None
    try:
        return int(hash_part, 16), size
    except ValueError:
        return None


def _grayscale_pixels(image: Image.Image) -> list[int]:
    """把图片统一成 16x16 灰度像素列表 (相似度比对的公共预处理)"""
    gray = image.convert("L").resize((_SIMILARITY_SIDE, _SIMILARITY_SIDE), _RESAMPLE)
    return list(gray.tobytes())


def cover_similarity(left: Image.Image, right: Image.Image) -> float:
    """比较两张封面是不是同一张图, 返回 ``0.0 ~ 1.0`` (1.0 = 完全一致)。

    极简实现: 两端统一成 16x16 灰度 -> Pearson 相关系数 -> ``(rho + 1) / 2``。
    同一张图不同尺寸的相关系数接近 1 (结果接近 1.0); 不同图片接近 0 (结果约 0.5)。
    不引入 numpy; 任何异常 / 空图都返回 0.0 (视为完全不同)。
    """
    try:
        left_pixels = _grayscale_pixels(left)
        right_pixels = _grayscale_pixels(right)
    except Exception as e:  # noqa: BLE001 - 相似度只是辅助判断, 失败按「不同」处理
        logger.debug(f"封面相似度预处理失败: {e.__class__.__name__}: {e}")
        return 0.0
    count = min(len(left_pixels), len(right_pixels))
    if count <= 0:
        return 0.0
    left_pixels = left_pixels[:count]
    right_pixels = right_pixels[:count]
    left_mean = sum(left_pixels) / count
    right_mean = sum(right_pixels) / count
    left_var = sum((value - left_mean) ** 2 for value in left_pixels)
    right_var = sum((value - right_mean) ** 2 for value in right_pixels)
    if left_var <= 0.0 and right_var <= 0.0:
        # 两张纯色图: 灰度相同即视为同一张
        return 1.0 if abs(left_mean - right_mean) <= 1.0 else 0.0
    if left_var <= 0.0 or right_var <= 0.0:
        return 0.0
    covariance = sum(
        (left_value - left_mean) * (right_value - right_mean)
        for left_value, right_value in zip(left_pixels, right_pixels)
    )
    rho = covariance / math.sqrt(left_var * right_var)
    return max(0.0, min(1.0, (rho + 1.0) / 2.0))


def _normalize_cover_format(value: Any) -> SaveFormat:
    """封面落盘格式归一: ``JPEG`` / ``PNG`` / None (保持原格式)"""
    text = str(value or "").strip().upper()
    if text in ("JPEG", "JPG"):
        return "JPEG"
    if text == "PNG":
        return "PNG"
    return None


def _normalize_cover_quality(value: Any) -> int:
    """封面质量归一: 1-100, 非法值退回默认 90"""
    try:
        quality = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_QUALITY
    return max(1, min(100, quality))


def _decode_cover_bytes(data: bytes | None) -> Image.Image | None:
    """把下载到的字节解析成 PIL 图片 (保留原始格式标记, 供「原格式落盘」用)"""
    if not data:
        logger.warning("封面下载结果为空")
        return None
    try:
        with Image.open(BytesIO(data)) as image:
            image.load()
            source_format = image.format
            cover = image.copy()
        # Image.copy() 不保证带上 format, 手动保留; 落盘格式为 None 时要用它
        cover.format = source_format  # type: ignore[attr-defined]
        return cover
    except Exception as e:  # noqa: BLE001 - 下载内容不是图片 / 图片损坏
        logger.warning(f"解析封面图片失败: {e.__class__.__name__}: {e}")
        return None


def _entry_order(entry: tuple[int | None, int, str]) -> tuple[int, int]:
    """封面索引项排序: 数字尺寸按大小升序, ``_full`` (最大封面) 排最后"""
    marker = entry[0]
    return (1, 0) if marker is None else (0, marker)


# ====================================================================== 私有缓存器
class _MusicCache:
    """私有缓存器: 歌曲信息 JSON + 封面文件索引 (只管存储与查找)。

    * 歌曲信息: ``dict[int, dict]`` (key = :func:`make_cache_key`), 存 ``music_info.json``,
      key 以 16 位十六进制落盘;
    * 封面: ``{歌曲名} - {作者}_{key:016x}_{尺寸}.{jpg|png}``, 放 ``covers/`` 子目录,
      内存索引 ``key -> [(尺寸标记, 图片实际尺寸, 文件名)]`` 加速查找;
    * 尺寸标记: 数字表示按该尺寸取回, ``None`` 表示 ``_full`` (最大封面)。
    """

    def __init__(self, cover_dir: str, info_file: str,
                 save_format: SaveFormat = None, save_quality: int = _DEFAULT_QUALITY) -> None:
        self.cover_dir = cover_dir
        self.info_file = info_file
        self.save_format = save_format
        self.save_quality = _normalize_cover_quality(save_quality)
        self._info: dict[int, dict[str, Any]] = {}
        self._loaded = False
        self._index: dict[int, list[tuple[int | None, int, str]]] = {}
        self._indexed = False

    # ------------------------------------------------------------------ 载入状态
    @property
    def loaded(self) -> bool:
        return self._loaded

    # ------------------------------------------------------------------ 歌曲信息
    def load(self) -> dict[int, dict[str, Any]]:
        """把歌曲信息 JSON 读进内存 (文件缺失/损坏时当空缓存)"""
        self._info = {}
        try:
            with open(self.info_file, "r", encoding="utf-8") as file:
                data = json.load(file)
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError) as e:
            logger.warning(f"读取歌曲信息缓存失败: {e}")
            data = {}
        if isinstance(data, dict):
            for key, value in data.items():
                try:
                    cache_key = int(str(key), 16)
                except (TypeError, ValueError):
                    try:
                        cache_key = int(str(key), 10)
                    except (TypeError, ValueError):
                        continue
                if isinstance(value, dict):
                    self._info[cache_key] = value
        self._loaded = True
        return self._info

    def save(self) -> None:
        """把歌曲信息 JSON 落盘 (未载入时先载入, 避免覆盖已有缓存)"""
        if not self._loaded:
            self.load()
        parent = os.path.dirname(self.info_file)
        if parent:
            os.makedirs(parent, exist_ok=True)
        try:
            with open(self.info_file, "w", encoding="utf-8") as file:
                json.dump(
                    {f"{key:016x}": value for key, value in self._info.items()},
                    file, ensure_ascii=False, indent=4,
                )
        except (OSError, TypeError, ValueError) as e:
            logger.warning(f"保存歌曲信息缓存失败: {e}")

    def get(self, cache_key: int) -> dict[str, Any] | None:
        if not self._loaded:
            self.load()
        return self._info.get(cache_key)

    def put(self, cache_key: int, value: dict[str, Any]) -> None:
        if not self._loaded:
            self.load()
        self._info[cache_key] = value

    # ------------------------------------------------------------------ 封面文件
    def reload_index(self) -> dict[int, list[tuple[int | None, int, str]]]:
        """重建封面内存索引: ``key -> [(尺寸标记, 图片实际尺寸, 文件名), ...]``"""
        index: dict[int, list[tuple[int | None, int, str]]] = {}
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
            cache_key, size = parsed
            actual = 0
            if size is None:
                # _full 文件: 只读文件头拿真实尺寸
                try:
                    with Image.open(os.path.join(self.cover_dir, name)) as image:
                        actual = max(image.size)
                except OSError:
                    actual = 0
            index.setdefault(cache_key, []).append((size, actual, name))
        for entries in index.values():
            entries.sort(key=_entry_order)
        self._index = index
        self._indexed = True
        return index

    def _ensure_index(self) -> None:
        if not self._indexed:
            self.reload_index()

    def sizes(self, cache_key: int) -> list[int]:
        """本地实际存在的封面尺寸列表 (最大封面给图片真实尺寸)"""
        self._ensure_index()
        return [max(marker or 0, actual) for marker, actual, _name in self._index.get(cache_key, [])]

    def has_cover(self, cache_key: int) -> bool:
        """本地是否已经有这张封面的任意尺寸"""
        self._ensure_index()
        return bool(self._index.get(cache_key))

    def find_cover(self, cache_key: int, size: int) -> tuple[Image.Image, int] | None:
        """按 ``key + 请求尺寸`` 查封面, 返回 ``(封面图, 图片实际尺寸)``。

        查找顺序 (契约 6.4): 等于请求尺寸 -> 大于请求尺寸的最小者 ->
        仅当存在 ``_full`` 时回退 ``_full`` -> None (交给模板方法联网取图)。
        ``size == FULL_COVER_SIZE`` 时只命中 ``_full``; 没有 ``_full`` 就联网取原图。
        """
        self._ensure_index()
        entries = self._index.get(cache_key)
        if not entries:
            return None
        if size == FULL_COVER_SIZE:
            picked = next((entry for entry in entries if entry[0] is None), None)
        else:
            picked = next((entry for entry in entries if entry[0] == size), None)
            if picked is None:
                picked = next((entry for entry in entries
                               if entry[0] is not None and entry[0] > size), None)
            if picked is None:
                # 数字尺寸都不够大时, 只有存在「最大封面」才回退用它
                picked = next((entry for entry in entries if entry[0] is None), None)
        if picked is None:
            return None
        _marker, actual, name = picked
        path = os.path.join(self.cover_dir, name)
        try:
            with Image.open(path) as image:
                image.load()
                cover = image.copy()
        except OSError as e:
            logger.warning(f"读取封面缓存失败: {path} -> {e}")
            return None
        return cover, (actual or max(cover.size))

    def save_cover(self, cover: Image.Image, cache_key: int, size: int,
                   title: str = "", artist: str = "") -> int | None:
        """把封面存进缓存目录, 返回文件名里的尺寸标记 (``_full`` 返回 None)。

        数字尺寸会先等比缩到该尺寸再落盘 (不放大), 保证「文件名尺寸 = 图片真实尺寸」;
        ``_full`` 保留原图尺寸。同一首歌同一尺寸的旧文件会先被清掉, 避免重名/残留。
        """
        os.makedirs(self.cover_dir, exist_ok=True)
        marker = None if size == FULL_COVER_SIZE else int(size)
        if marker is not None and max(cover.size) > marker:
            cover = cover.copy()
            cover.thumbnail((marker, marker))
        # 清掉同一 key + 同一尺寸标记的旧文件 (歌名/作者变了会导致前缀不同)
        self._ensure_index()
        for old_marker, _actual, old_name in list(self._index.get(cache_key, [])):
            if old_marker != marker:
                continue
            try:
                os.remove(os.path.join(self.cover_dir, old_name))
            except OSError:
                pass

        target_format = self._target_format(cover)
        ext = "png" if target_format == "PNG" else "jpg"
        prefix = (f"{sanitize_filename(title)} - {sanitize_filename(artist)}"
                  if (title or artist) else "")
        size_suffix = FULL_SLOT if marker is None else "_" + str(marker)
        tail = f"{cache_key:016x}{size_suffix}"
        name = f"{prefix}_{tail}.{ext}" if prefix else f"{tail}.{ext}"
        path = os.path.join(self.cover_dir, name)
        if target_format == "PNG":
            image = cover if cover.mode in ("1", "L", "LA", "P", "RGB", "RGBA") else cover.convert("RGBA")
            image.save(path, "PNG")
        else:
            cover.convert("RGB").save(path, "JPEG", quality=self.save_quality)
        self.reload_index()
        return marker

    def _target_format(self, cover: Image.Image) -> Literal["JPEG", "PNG"]:
        """决定落盘格式: 配置优先; 未配置时保持图片自身格式 (PNG/JPEG, 其它按 JPEG)"""
        if self.save_format in ("JPEG", "PNG"):
            return self.save_format
        source_format = str(getattr(cover, "format", "") or "").strip().upper()
        return "PNG" if source_format == "PNG" else "JPEG"

    # ------------------------------------------------------------------ 缓存清理
    def clear(self) -> Generator[tuple[int, int, str], None, None]:
        """删除全部缓存 (封面文件 + 歌曲信息 JSON), 逐个 yield ``(已处理数, 总数, 当前项)``"""
        self._ensure_index()
        try:
            names = sorted(os.listdir(self.cover_dir))
        except OSError:
            names = []
        info_exists = os.path.isfile(self.info_file)
        total = len(names) + (1 if info_exists else 0)
        done = 0
        for name in names:
            try:
                os.remove(os.path.join(self.cover_dir, name))
            except OSError:
                logger.warning(f"删除文件失败: {name}")
            done += 1
            yield done, total, name
        if info_exists:
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
        """封面缓存目录里的文件数 (删除进度基数; ``clear`` 还会额外删除歌曲信息 JSON)"""
        try:
            return len(os.listdir(self.cover_dir))
        except OSError:
            return 0


# ====================================================================== 后端基类
class BaseMusicBackend(ABC):
    """音乐平台后端基类 (模板方法)。

    基类负责统一流程: :meth:`fetch` 先查本地缓存, 未命中再联网
    ``search -> 选候选 -> resolve_cover_url -> http_get 下载 -> 写缓存``;
    子类只需实现 :meth:`search` 与 :meth:`resolve_cover_url`。

    子类需要声明的类属性:

    * ``platform_id``: 平台 id (``"kugou"`` / ``"netease"``);
    * ``cache_name``: 缓存子目录名, 默认等于 ``platform_id``;
    * ``source_markers``: 源会话 app id 小写子串, 供源会话路由识别平台。
    """

    platform_id: str = "unknown"
    # 缓存子目录名; None -> platform_id
    cache_name: str | None = None
    # 源会话 app id 小写子串
    source_markers: tuple[str, ...] = ()
    # 测试可覆盖: 缓存根目录
    cache_root: str = CACHE_ROOT
    # 封面落盘格式 / 质量 (也可通过 update_config 修改)
    cover_format: SaveFormat = None
    cover_quality: int = _DEFAULT_QUALITY

    def __init__(self, cover_format: SaveFormat = None, cover_quality: int = _DEFAULT_QUALITY) -> None:
        self._cache: _MusicCache | None = None
        self.update_config(cover_format, cover_quality)

    # ------------------------------------------------------------------ 配置
    def update_config(self, cover_format: SaveFormat = None,
                      cover_quality: int = _DEFAULT_QUALITY) -> None:
        """配置变更入口: 封面落盘格式 / 质量 (无需重建后端对象)"""
        self.cover_format = _normalize_cover_format(cover_format)
        self.cover_quality = _normalize_cover_quality(cover_quality)
        cache = getattr(self, "_cache", None)
        if cache is not None:
            cache.save_format = self.cover_format
            cache.save_quality = self.cover_quality

    # ------------------------------------------------------------------ 缓存目录 / 缓存器
    @property
    def cache_dir(self) -> str:
        """每个后端独立的缓存目录, 如 ``cache/kugou/``"""
        return os.path.join(self.cache_root, str(self.cache_name or self.platform_id or "unknown"))

    @property
    def cover_dir(self) -> str:
        """封面文件目录 (``{cache_dir}/covers``)"""
        return os.path.join(self.cache_dir, "covers")

    def _get_cache(self) -> _MusicCache:
        """懒创建缓存器 (测试可在 fetch 前覆盖 cache_root)"""
        cache = getattr(self, "_cache", None)
        if cache is None:
            cache = _MusicCache(
                os.path.join(self.cache_dir, "covers"),
                os.path.join(self.cache_dir, "music_info.json"),
                self.cover_format,
                self.cover_quality,
            )
            self._cache = cache
        return cache

    def _ensure_cache_loaded(self) -> _MusicCache:
        cache = self._get_cache()
        if not cache.loaded:
            self.load_cache()
        return cache

    # ------------------------------------------------------------------ 网络
    def request_headers(self) -> dict[str, str]:
        """下载封面时使用的请求头; 子类可重写 (例如平台需要 Referer)"""
        return dict(_DEFAULT_HEADERS)

    def http_get(self, url: str, timeout: float | None = None) -> bytes:
        """统一 GET: 带 :meth:`request_headers`, 失败抛 ``RuntimeError`` (中文说明)"""
        if not url:
            raise RuntimeError("网络请求失败: URL 为空")
        wait = COVER_TIMEOUT if timeout is None else float(timeout)
        try:
            response = requests.get(url, headers=self.request_headers(), timeout=wait)
        except Exception as e:  # noqa: BLE001 - 统一转成运行时错误, 由调用方兜底
            raise RuntimeError(f"网络请求失败: {e.__class__.__name__}: {e}") from e
        if response.status_code != 200:
            raise RuntimeError(f"网络请求失败: 服务器返回 HTTP {response.status_code} ({url})")
        if not response.content:
            raise RuntimeError(f"网络请求失败: 服务器返回空数据 ({url})")
        return response.content

    # ------------------------------------------------------------------ 缓存公开接口
    def load_cache(self) -> None:
        """载入本地缓存 (歌曲信息 JSON + 封面索引)"""
        cache = self._get_cache()
        cache.load()
        cache.reload_index()

    def save_cache(self) -> None:
        """把歌曲信息缓存落盘"""
        self._get_cache().save()

    def clear_cache(self) -> Generator[tuple[int, int, str], None, None]:
        """清空该后端全部缓存 (封面文件 + 信息 JSON), 每删一项 yield ``(已处理, 总数, 当前项)``"""
        yield from self._get_cache().clear()

    def cache_file_count(self) -> int:
        """缓存封面文件数, 供 UI 设置进度基数.

        ``clear_cache`` 的 yield 总数是「封面文件数 + 歌曲信息 JSON」, 因此 UI 里通常用
        ``cache_file_count() + 1`` 作为进度条最大值 (与旧实现一致)。
        """
        return self._get_cache().count()

    # ------------------------------------------------------------------ 模板流程
    def fetch(self, info: MusicInfo, size: int = DEFAULT_COVER_SIZE, *,
              source_cover: Image.Image | None = None,
              use_similarity: bool = True,
              force_refresh: bool = False) -> ResolvedMusic | None:
        """模板方法: 本地缓存 -> 联网搜索+封面; 都拿不到返回 None。

        * ``size`` 先 :func:`normalize_size`;
        * ``force_refresh=True`` 跳过缓存 (「立即更新信息」用);
        * ``source_cover`` 是源 SMTC 低清缩略图 (可能为 None), 仅用于封面相似度匹配。
        """
        target_size = normalize_size(size)
        if not isinstance(info, MusicInfo):
            raise TypeError("fetch() 的 info 必须是 MusicInfo")
        if info.is_empty:
            logger.warning("歌曲标题与作者都为空, 无法搜索")
            return None
        cache_key = make_cache_key(self.platform_id, info)
        self._ensure_cache_loaded()

        if not force_refresh:
            cached = self._fetch_from_cache(cache_key, target_size)
            if cached is not None:
                logger.debug(
                    f"[{self.platform_id}] 缓存命中: {info.title} - {info.artist} "
                    f"(请求 {target_size}, 缓存尺寸 {cached.cover_size})"
                )
                return cached

        return self._fetch_from_web(
            info, cache_key, target_size,
            source_cover=source_cover,
            use_similarity=use_similarity,
        )

    # ------------------------------------------------------------------ 缓存命中
    def _fetch_from_cache(self, cache_key: int, size: int) -> ResolvedMusic | None:
        """本地缓存: 歌曲信息记录与可用封面都存在才算命中"""
        cache = self._get_cache()
        record = cache.get(cache_key)
        if record is None:
            return None
        decoded = self.decode_record(record)
        if decoded is None:
            logger.debug(f"[{self.platform_id}] 缓存记录损坏, 忽略: {cache_key:016x}")
            return None
        info, _cover_ref = decoded
        found = cache.find_cover(cache_key, size)
        if found is None:
            logger.debug(f"[{self.platform_id}] 缓存记录存在但没有可用封面 (请求 {size})")
            return None
        cover, cover_size = found
        cover = prepare_cover(cover, size)
        return ResolvedMusic(
            info=info,
            cover=cover,
            cover_size=cover_size,
            from_cache=True,
            platform=self.platform_id,
        )

    # ------------------------------------------------------------------ 联网搜索与下载
    def _fetch_from_web(self, info: MusicInfo, cache_key: int, size: int, *,
                        source_cover: Image.Image | None,
                        use_similarity: bool) -> ResolvedMusic | None:
        """联网: 搜索 -> 选候选 -> 解析封面地址 -> 下载 -> 写缓存"""
        try:
            candidates = self.search(info)
        except Exception as e:  # noqa: BLE001 - 子类搜索失败不应中断主流程
            logger.error(f"[{self.platform_id}] 搜索失败: {e.__class__.__name__}: {e}")
            return None
        valid = [candidate for candidate in (candidates or ())
                 if isinstance(candidate, CoverCandidate) and isinstance(candidate.info, MusicInfo)]
        if not valid:
            logger.warning(f"[{self.platform_id}] 没有搜索到候选歌曲: {info.title} - {info.artist}")
            return None

        picked = self._pick_candidate(info, valid, source_cover, use_similarity)

        cache = self._get_cache()
        # 联网取图尺寸: 本地一个封面都没有 -> 按请求尺寸取 (少流量);
        # 已有但都比请求小 (缓存未命中) / 请求最大封面 -> 取原图并落盘 _full
        fetch_size = size if size != FULL_COVER_SIZE and not cache.has_cover(cache_key) \
            else FULL_COVER_SIZE
        try:
            cover_url = self.resolve_cover_url(picked, fetch_size)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[{self.platform_id}] 解析封面地址失败: {e.__class__.__name__}: {e}")
            return None
        if not cover_url:
            logger.warning(
                f"[{self.platform_id}] 候选没有可用封面: "
                f"{picked.info.title} - {picked.info.artist}"
            )
            return None
        try:
            data = self.http_get(cover_url)
        except Exception as e:  # noqa: BLE001 - 下载失败由 main 走下一个后端 / 源封面
            logger.warning(f"[{self.platform_id}] 下载封面失败: {e}")
            return None
        cover = _decode_cover_bytes(data)
        if cover is None:
            return None
        if max(cover.size) > MAX_COVER_SIZE:
            cover.thumbnail((MAX_COVER_SIZE, MAX_COVER_SIZE))

        matched = self._merge_info(info, picked.info)
        try:
            saved_marker = cache.save_cover(cover, cache_key, fetch_size,
                                            matched.title, matched.artist)
            cache.put(cache_key, self.encode_record(matched, picked))
            self.save_cache()
        except Exception as e:  # noqa: BLE001 - 落盘失败 (如超长路径) 仍要把高清封面交给 SMTC
            logger.warning(f"[{self.platform_id}] 写封面缓存失败 (本次仍使用高清封面): "
                           f"{e.__class__.__name__}: {e}")
            saved_marker = None
        cover_size = max(cover.size) if saved_marker is None else saved_marker
        display_cover = prepare_cover(cover, size)
        logger.info(
            f"[{self.platform_id}] 已获取封面: {matched.title} - {matched.artist} "
            f"(请求 {size}, 联网尺寸 {fetch_size}, 缓存尺寸 {cover_size})"
        )
        return ResolvedMusic(
            info=matched,
            cover=display_cover,
            cover_size=cover_size,
            from_cache=False,
            platform=self.platform_id,
        )

    # ------------------------------------------------------------------ 候选选择
    def _pick_candidate(self, info: MusicInfo, candidates: list[CoverCandidate],
                        source_cover: Image.Image | None, use_similarity: bool) -> CoverCandidate:
        """按契约 6.5 选候选: 专辑名精确匹配 -> 源封面相似度 -> 第一个候选"""
        wanted_album = text_key(info.album_title)
        if not wanted_album:
            # 酷狗等平台会把专辑名放进 album_artist (形如 "《Album》") 而 album_title 为空;
            # 旧实现用去掉书名号的 album_artist 作为专辑匹配键, 这里保持同样的兜底行为。
            wanted_album = text_key(str(info.album_artist or "").lstrip("《").rstrip("》"))
        if wanted_album:
            for candidate in candidates:
                if text_key(candidate.info.album_title) == wanted_album:
                    logger.debug(
                        f"[{self.platform_id}] 按专辑名精确匹配选中候选: "
                        f"{candidate.info.title} - {candidate.info.album_title}"
                    )
                    return candidate

        if use_similarity and source_cover is not None:
            best_candidate: CoverCandidate | None = None
            best_score = -1.0
            limited = candidates[:SIMILARITY_MAX_CANDIDATES]
            for index, candidate in enumerate(limited):
                score = self._similarity_score(candidate, source_cover)
                if score is None:
                    continue
                logger.debug(
                    f"[{self.platform_id}] 封面相似度候选 #{index + 1}/{len(limited)}: "
                    f"{candidate.info.title} - {candidate.info.artist} -> {score:.4f}"
                )
                if score > best_score:
                    best_score = score
                    best_candidate = candidate
            if best_candidate is not None and best_score >= SIMILARITY_THRESHOLD:
                logger.info(
                    f"[{self.platform_id}] 按封面相似度选中候选: {best_candidate.info.title} "
                    f"(分数 {best_score:.3f} >= 阈值 {SIMILARITY_THRESHOLD:.2f})"
                )
                return best_candidate
            if best_candidate is not None:
                logger.info(
                    f"[{self.platform_id}] 封面相似度最高仅 {best_score:.3f} "
                    f"< 阈值 {SIMILARITY_THRESHOLD:.2f}, 回退第一个候选"
                )
            else:
                logger.debug(f"[{self.platform_id}] 封面相似度不可用, 回退第一个候选")
        return candidates[0]

    def _similarity_score(self, candidate: CoverCandidate,
                          source_cover: Image.Image) -> float | None:
        """下载候选的小尺寸封面并与源缩略图比相似度; 失败返回 None (不参与比较)"""
        try:
            cover_url = self.resolve_cover_url(candidate, SIMILARITY_COVER_SIZE)
            if not cover_url:
                logger.debug(f"[{self.platform_id}] 相似度候选没有封面地址: {candidate.info.title}")
                return None
            candidate_cover = _decode_cover_bytes(self.http_get(cover_url))
            if candidate_cover is None:
                return None
            return cover_similarity(source_cover, candidate_cover)
        except Exception as e:  # noqa: BLE001 - 单个候选失败不影响其它候选
            logger.debug(
                f"[{self.platform_id}] 候选封面相似度计算失败 "
                f"({candidate.info.title}): {e.__class__.__name__}: {e}"
            )
            return None

    @staticmethod
    def _merge_info(source: MusicInfo, candidate: MusicInfo) -> MusicInfo:
        """用候选信息补全源信息里缺失的字段 (不覆盖源 SMTC 已有的非空值)"""
        merged = source.copy()
        for field in ("title", "artist", "album_title", "album_artist"):
            if not merged.get_field(field):
                value = candidate.get_field(field)
                if value:
                    setattr(merged, field, value)
        if merged.length <= 0.0 and candidate.length > 0.0:
            merged.length = candidate.length
        return merged

    # ------------------------------------------------------------------ 子类必须实现
    @abstractmethod
    def search(self, info: MusicInfo) -> list[CoverCandidate]:
        """按源歌曲信息搜索候选; 平台自己负责标题清洗/匹配过滤/排序; 失败返回 []。"""

    @abstractmethod
    def resolve_cover_url(self, candidate: CoverCandidate, size: int) -> str | None:
        """把候选转成可下载的封面 URL (允许联网, 网易云需要查详情)。

        ``size`` 为 ``FULL_COVER_SIZE`` 表示最大封面; 不存在返回 None。
        """

    # ------------------------------------------------------------------ 子类可选重写
    def encode_record(self, info: MusicInfo, candidate: CoverCandidate) -> dict:
        """默认缓存记录模板: 匹配后的歌曲信息 + 平台封面引用 (可重写)"""
        record = info.to_dict()
        if candidate is not None:
            record["cover_ref"] = candidate.cover_ref
        return record

    def decode_record(self, record: dict) -> tuple[MusicInfo, str] | None:
        """默认缓存记录解读: 返回 ``(匹配后的歌曲信息, cover_ref)``; 缺失/损坏返回 None"""
        if not isinstance(record, Mapping):
            return None
        info = MusicInfo.from_dict(record)
        if info.is_empty:
            return None
        cover_ref = record.get("cover_ref")
        return info, cover_ref if isinstance(cover_ref, str) else ""


__all__ = [
    # 常量
    "DEFAULT_COVER_SIZE", "FULL_COVER_SIZE", "MAX_COVER_SIZE", "CACHE_ROOT",
    "SIMILARITY_COVER_SIZE", "SIMILARITY_THRESHOLD", "SIMILARITY_MAX_CANDIDATES",
    "COVER_TIMEOUT", "FULL_SLOT", "SaveFormat",
    # 数据类
    "CoverCandidate", "ResolvedMusic",
    # 基类
    "BaseMusicBackend",
    # 工具函数
    "normalize_size", "prepare_cover", "make_cache_key", "sanitize_filename",
    "parse_cover_filename", "cover_similarity",
]
