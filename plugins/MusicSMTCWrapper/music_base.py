# -*- coding: utf-8 -*-
"""共用数据类: 静态歌曲信息。

本模块不依赖任何第三方库 (也不依赖 WinRT), 供插件内所有模块导入:

* 缓存层用 :class:`MusicInfo` 作为歌曲信息的统一载体;
* 平台后端返回的匹配结果、SMTC 层展示的信息都用它传递;
* 它被设计成**静态**的: 只包含歌曲本身的字段, 不包含封面对象、播放状态、进度、
  缓存路径等运行期数据 —— 这些由持有它们的模块自己管理。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

# 插件 id, 必须与 plugin.json 里的 id 保持一致 (用于日志 logger 命名)
PLUGIN_ID = "music_smtc_wrapper"
# 插件内统一使用的 logger 名 (WinEnchantKit 的插件加载器会用同一个名字创建带文件落盘的 logger)
LOGGER_NAME = f"WinEnchantKitLogger_{PLUGIN_ID}"

# 四个歌曲信息字段 (也是「标题指向」等配置的取值)
FIELD_TITLE = "title"
FIELD_ARTIST = "artist"
FIELD_ALBUM_TITLE = "album_title"
FIELD_ALBUM_ARTIST = "album_artist"

FIELDS: tuple[str, ...] = (FIELD_TITLE, FIELD_ARTIST, FIELD_ALBUM_TITLE, FIELD_ALBUM_ARTIST)
# 字段名 -> 中文名 (配置界面 / 日志用)
FIELD_LABELS: dict[str, str] = {
    FIELD_TITLE: "标题",
    FIELD_ARTIST: "作者",
    FIELD_ALBUM_TITLE: "专辑标题",
    FIELD_ALBUM_ARTIST: "专辑作者",
}
# 配置项取值 -> 中文名 (ChoiceParamPlus 用)
FIELD_CHOICES: dict[str, str] = {field: FIELD_LABELS[field] for field in FIELDS}

_MULTI_SPACE = re.compile(r"\s+")


def normalize_text(value: Any) -> str:
    """把任意值变成去掉首尾空白、合并连续空白的字符串 (空值给空串)"""
    if value is None:
        return ""
    return _MULTI_SPACE.sub(" ", str(value)).strip()


def text_key(value: Any) -> str:
    """把文本归一成可比较的键: 去空白 + 合并空白 + casefold"""
    return normalize_text(value).casefold()


def normalize_field(value: Any) -> str:
    """配置里的字段名归一; 非法值退回 ``title``"""
    name = str(value or "").strip()
    return name if name in FIELDS else FIELD_TITLE


@dataclass
class MusicInfo:
    """静态歌曲信息 (纯数据)。

    只承载歌曲本身的字段; 封面、播放状态、进度、缓存位置都不在这里。
    ``length`` 是歌曲时长 (秒), ``0`` 表示未知。
    """

    title: str = ""
    artist: str = ""
    album_title: str = ""
    album_artist: str = ""
    length: float = 0.0

    def __post_init__(self):
        self.title = normalize_text(self.title)
        self.artist = normalize_text(self.artist)
        self.album_title = normalize_text(self.album_title)
        self.album_artist = normalize_text(self.album_artist)
        try:
            length = float(self.length or 0.0)
        except (TypeError, ValueError):
            length = 0.0
        # 负数/NaN 之类的脏数据统一当 0
        self.length = length if length > 0.0 else 0.0

    # ---------------------------------------------------------------- 字段读取
    def get_field(self, field: str) -> str:
        """按字段名取文本字段 (非法字段名返回空串)"""
        return str(getattr(self, field, "") or "") if field in FIELDS else ""

    def field_dict(self) -> dict[str, str]:
        """四个文本字段的字典 (字段名 -> 值)"""
        return {field: self.get_field(field) for field in FIELDS}

    @property
    def is_empty(self) -> bool:
        """标题与作者都为空 (源 SMTC 失效判断用的就是这条)"""
        return not (self.title or self.artist)

    @property
    def has_album(self) -> bool:
        return bool(self.album_title)

    # ---------------------------------------------------------------- 派生
    def copy(self) -> "MusicInfo":
        """值拷贝 (各模块可放心改自己的副本)"""
        return MusicInfo(self.title, self.artist, self.album_title, self.album_artist, self.length)

    def normalized(self) -> "MusicInfo":
        """去掉首尾/多余空白后的副本"""
        return MusicInfo(normalize_text(self.title), normalize_text(self.artist),
                         normalize_text(self.album_title), normalize_text(self.album_artist),
                         self.length)

    def unique_key(self) -> str:
        """整条信息的比较键 (判断「是不是同一首歌」用)"""
        return "\x1f".join(text_key(part) for part in
                           (self.title, self.artist, self.album_title, self.album_artist))

    def identity_key(self) -> str:
        """只由标题+作者构成的比较键 (缓存键/跳过重复刷新用)"""
        return "\x1f".join((text_key(self.title), text_key(self.artist)))

    def mapped(self, mapping: Mapping[str, str]) -> "MusicInfo":
        """按「字段指向」生成展示信息: 结果字段 <- 原信息里的某个字段。

        ``mapping`` 的 key 是目标字段 (显示到 SMTC 的字段), value 是来源字段;
        缺省/非法项保持原值不变。时长不受影响。
        """
        result = self.copy()
        for target in FIELDS:
            source = mapping.get(target) if mapping else None
            if source in FIELDS:
                setattr(result, target, self.get_field(str(source)))
        return result

    def with_length(self, length: float) -> "MusicInfo":
        """返回带指定时长 (秒) 的副本"""
        result = self.copy()
        try:
            value = float(length or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        result.length = value if value > 0.0 else 0.0
        return result

    # ---------------------------------------------------------------- 序列化
    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 化的 dict (缓存记录/日志用)"""
        return {
            "title": self.title,
            "artist": self.artist,
            "album_title": self.album_title,
            "album_artist": self.album_artist,
            "length": self.length,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None, base: "MusicInfo | None" = None) -> "MusicInfo":
        """从 dict 生成信息; ``base`` 提供缺省值 (缓存记录不完整时用)"""
        if not isinstance(data, Mapping):
            return base.copy() if base is not None else cls()
        base = base or cls()
        result = base.copy()
        for field in FIELDS:
            if field in data and data[field] is not None:
                setattr(result, field, normalize_text(data[field]))
        if data.get("length") is not None:
            try:
                length = float(data["length"])
            except (TypeError, ValueError):
                length = 0.0
            result.length = length if length > 0.0 else 0.0
        return result


def clean_title_for_search(title: str) -> str:
    """把歌名整理成适合搜索的形式 (去掉首尾空白, 全角括号不动)"""
    return normalize_text(title)


def first_non_empty(*values: Any) -> str:
    """返回第一个非空文本 (用于搜索结果的字段兜底)"""
    for value in values:
        text = normalize_text(value)
        if text:
            return text
    return ""


def join_artists(artists: Iterable[Any]) -> str:
    """把歌手列表拼成 ``A / B`` 的形式 (忽略空项); 列表项可以是 dict 或字符串"""
    names: list[str] = []
    for artist in artists or ():
        if isinstance(artist, Mapping):
            name = normalize_text(artist.get("name"))
        else:
            name = normalize_text(artist)
        if name and name not in names:
            names.append(name)
    return " / ".join(names)


# 多歌手分隔符: 源 SMTC 常见 "A/B/C"; 酷狗搜索结果是 "A、B、C"; 网易云是列表
_ARTIST_SEPARATOR = re.compile(
    r"\s*(?:/|／|、|,|，|;|；|&|＆|\bfeat\.?\b|\bft\.?\b)\s*",
    re.IGNORECASE,
)


def split_artists(value: Any) -> list[str]:
    """把多歌手文本拆成单个歌手名 (去重、保序、去空白)。

    例如 ``"塞壬唱片-MSR/Erik Couttolenc/VISION SOUND"`` ->
    ``["塞壬唱片-MSR", "Erik Couttolenc", "VISION SOUND"]``。
    单歌手名 (没有分隔符) 返回只含它自己的列表。
    """
    text = normalize_text(value)
    if not text:
        return []
    parts: list[str] = []
    for part in _ARTIST_SEPARATOR.split(text):
        name = normalize_text(part)
        if name and name not in parts:
            parts.append(name)
    return parts


def artist_matches(source_artist: Any, candidate_names: Iterable[Any]) -> bool:
    """判断源歌手文本与候选歌手名是否指同一批人 (容忍多歌手分隔符不同)。

    * 源 SMTC 常把多个歌手用 ``/`` 连成一个字符串, 而候选平台会拆成列表
      (例如 ``塞壬唱片-MSR/Erik Couttolenc/VISION SOUND/Vapestor``
      vs ``["塞壬唱片-MSR", "Erik Couttolenc", "VISION SOUND", "Vapestor"]``);
    * 先做整串互相包含, 再按分隔符拆成单个歌手逐个匹配 (双向包含);
    * 源歌手为空时视为匹配 (允许只按歌名搜索)。
    """
    source = text_key(source_artist)
    names = [text_key(name) for name in (candidate_names or ())]
    names = [name for name in names if name]
    if not source:
        return True
    if not names:
        return False

    def _matches_one(text: str) -> bool:
        if not text:
            return False
        for name in names:
            if text in name:
                return True
            # 候选歌手名本身可能只是源串里的一段 (例如单个歌手名)
            if len(name) >= 2 and name in text:
                return True
        return False

    if _matches_one(source):
        return True
    for part in split_artists(source_artist):
        part_key = text_key(part)
        if len(part_key) >= 2 and _matches_one(part_key):
            return True
    return False


def search_keyword_variants(title: Any, artist: Any) -> list[str]:
    """生成搜索关键字候选, 按优先级排列 (去重、去空)。

    1. ``标题 完整歌手串`` (原行为);
    2. ``标题 首个歌手`` / ``标题 第二个歌手`` (多歌手源更容易命中);
    3. ``标题`` (最宽松的兜底);
    4. ``完整歌手串`` (只有歌手没有标题时)。
    """
    title_text = normalize_text(title)
    artist_text = normalize_text(artist)
    variants: list[str] = []

    def _add(*parts: Any):
        keyword = " ".join(part for part in (normalize_text(value) for value in parts) if part).strip()
        if keyword and keyword not in variants:
            variants.append(keyword)

    _add(title_text, artist_text)
    for component in split_artists(artist_text)[:2]:
        _add(title_text, component)
    _add(title_text)
    _add(artist_text)
    return variants
