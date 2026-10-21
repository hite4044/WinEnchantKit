# -*- coding: utf-8 -*-
"""酷狗音乐后端: 搜索 + 封面地址。

本模块只负责酷狗自己的业务 (与旧 ``plugins/OldMusicSMTCWrapper/smtc_backend/kugou.py``
的搜索 / 封面逻辑保持一致), 缓存、封面相似度、模板流程都由
:class:`~.base_backend.BaseMusicBackend` 统一处理:

* :meth:`KugouMusicBackend.search`            —— ``mobilecdn.kugou.com`` 搜索 + 旧的
  「歌名匹配 + 歌手匹配」过滤, 返回**全部**命中候选 (含 ``group`` 里的版本);
* :meth:`KugouMusicBackend.resolve_cover_url` —— 由 ``trans_param.union_cover`` 模板
  生成对应尺寸的封面地址 (``/{size}/`` 占位, 最大封面时去掉)。

封面地址约定: ``union_cover`` 形如 ``http://imge.kugou.com/stdmusic/{size}/xxx.jpg``;
``size`` 传 ``FULL_COVER_SIZE`` 表示去掉 ``/{size}`` 取原图; 模板没有占位符时任何尺寸都
返回原地址。
"""
from __future__ import annotations

import logging
import re
import string
from typing import Any

import requests
from requests.exceptions import SSLError

from .base_backend import FULL_COVER_SIZE, BaseMusicBackend, CoverCandidate
from ..music_base import (
    LOGGER_NAME,
    MusicInfo,
    artist_matches,
    normalize_text,
    search_keyword_variants,
)

logger = logging.getLogger(LOGGER_NAME)

# 源会话匹配标记 (source_app_user_model_id 小写子串): 酷狗本体 + 萌音 (托管酷狗播放)
SOURCE_MARKERS: tuple[str, ...] = ("kugou", "moekoe")

# 旧实现使用的 UA (搜索接口与封面下载都带它)
HEADERS: dict[str, str] = {
    "User-Agent": "[HDKugouCover(Github)(WinEnchantKit)] Music Search",
}
SEARCH_URL = "http://mobilecdn.kugou.com/api/v3/search/song?format=json&keyword={keyword}&page=1"
OTHER_NAME_PATTERN = re.compile(r"\([^()]+\)")

# 封面模板里的尺寸占位符
SIZE_PLACEHOLDER = "{size}"

# 旧实现搜索请求没有设超时; 这里补一个防御性超时 (失败按 [] 处理, 不会卡住刷新线程)
SEARCH_TIMEOUT = 10.0

# SSL 代理错误只提示一次 (与旧实现 has_alert 行为一致)
_ssl_alerted = False


# ---------------------------------------------------------------- 搜索 (原样搬运)
def extract_music_title(title: str) -> tuple[str, str | None]:
    """从歌名里拆出括号副标题 (旧实现原样搬运)。

    例如 ``"STOP (Live)"`` -> ``("STOP", "(Live)")``; 没有括号时副标题为 ``None``。
    """
    title = title or ""
    if match := OTHER_NAME_PATTERN.search(title):
        title = title[:match.start()]
        other_name: str | None = match.group()
    else:
        other_name = None
    return title.strip(), other_name


def clean_music_title(title: str) -> str:
    """歌名清洗 (与旧 ``main.py::Plugin.load_cover`` 里的逻辑一致)。

    若歌名含 ``(``: 取最后一个 ``(`` 到第一个 ``)`` 的子串作为副标题候选; 该子串去掉
    ASCII 字母后仍有剩余字符时, 把歌名截断到第一个 ``(`` 之前并 strip。

    与原逻辑的差异 (防御): 原代码在 ``(`` 之后没有 ``)`` 时会 ``ValueError``,
    这里保持原歌名。
    """
    name = title or ""
    if "(" not in name:
        return name.strip()
    open_idx = name.rindex("(")
    close_idx = name.find(")")
    if close_idx == -1:
        # 原逻辑此处会 ValueError; 防御: 保持原歌名
        return name
    trans_name = name[open_idx:close_idx + 1]
    if len(set(trans_name) - set(string.ascii_letters)) > 0:
        name = name[:name.index("(")].strip()
    return name


def _search_songs_by_keyword(keyword: str) -> list[dict[str, Any]]:
    """按完整关键字调用旧酷狗搜索接口, 返回原始歌曲列表 (失败按 [] 处理, SSL 错误抛 RuntimeError)。

    接口: ``http://mobilecdn.kugou.com/api/v3/search/song?format=json&keyword={关键字}&page=1``
    """
    global _ssl_alerted
    logger.debug(f"歌曲搜索关键字: \"{keyword}\"")
    try:
        resp = requests.get(SEARCH_URL.format(keyword=keyword), headers=HEADERS,
                            data=None, timeout=SEARCH_TIMEOUT)
    except SSLError:
        if not _ssl_alerted:
            logger.error("系统代理有问题 (是工具的问题")
            _ssl_alerted = True
        raise RuntimeError("SSL错误") from None
    except requests.exceptions.RequestException as e:
        # 旧实现只吞 ConnectionError; 其余网络异常也一并按「搜索失败」兜底
        logger.warning(f"酷狗搜索请求失败: {e.__class__.__name__}: {e}")
        return []
    if resp.status_code != 200:
        raise RuntimeError(f"搜索失败: 服务器返回HTTP错误码 [{resp.status_code}]")
    _ssl_alerted = False
    try:
        payload = resp.json()
    except ValueError as e:
        raise RuntimeError(f"搜索失败: 服务器返回非 JSON 数据: {e}") from e
    data = payload.get("data") if isinstance(payload, dict) else None
    songs = data.get("info") if isinstance(data, dict) else None
    if not isinstance(songs, list):
        # 无结果时接口可能不给 data.info
        return []
    return [song for song in songs if isinstance(song, dict)]


def search_for_song_list(title: str, artist: str = "") -> list[dict[str, Any]]:
    """旧接口封装: 用「标题 歌手」拼关键字 (保留给外部/测试使用)。"""
    return _search_songs_by_keyword(f"{title} {artist}".strip())


def match_songs(songs: list[dict[str, Any]], title: str,
                artist: str) -> list[dict[str, Any]]:
    """从原始搜索结果里过滤出「歌名 + 歌手」匹配的歌曲。

    过滤条件 (与旧实现一致, 歌手匹配支持多歌手分隔符差异):
      * 源歌手与 ``song["singername"]`` 匹配 (见 :func:`music_base.artist_matches`);
      * ``clean_title == song["songname_original"]``;
      * 有副标题时 ``other_name in song["songname"]``。

    命中父歌曲后, 其 ``group`` 里的版本也一并加入 (旧实现行为)。
    """
    clean_title, other_name = extract_music_title(title)
    ready_songs: list[dict[str, Any]] = []
    for song in songs:
        singer_name = str(song.get("singername") or "")
        original_name = str(song.get("songname_original") or "")
        song_name = str(song.get("songname") or "")
        if not (artist_matches(artist, [singer_name])
                and clean_title == original_name
                and (other_name is None or other_name in song_name)):
            continue
        logger.debug(f"匹配歌曲 {song_name} - {singer_name}")
        ready_songs.append(song)
        for group_song in song.get("group") or []:
            if isinstance(group_song, dict):
                ready_songs.append(group_song)
    return ready_songs


def match_song_list(title: str, artist: str) -> list[dict[str, Any]]:
    """旧接口封装: 搜索 + :func:`match_songs` 过滤 (保留给外部/测试使用)。"""
    clean_title, _other_name = extract_music_title(title)
    return match_songs(search_for_song_list(clean_title, artist), title, artist)


def cover_url(template: str, size: int = 480) -> str:
    """由 ``union_cover`` 模板生成封面地址。

    ``size`` 为 ``FULL_COVER_SIZE`` (或任何 <= 0) 表示原图 (去掉 ``/{size}`` 占位符);
    模板没有占位符时任何尺寸都返回原图地址。
    """
    template = template or ""
    if SIZE_PLACEHOLDER not in template:
        return template
    if size == FULL_COVER_SIZE or (size or 0) <= 0:
        return template.replace("/" + SIZE_PLACEHOLDER, "").replace(SIZE_PLACEHOLDER, "")
    return template.format(size=str(int(size)))


def _duration_seconds(value: Any) -> float:
    """酷狗搜索结果的 ``duration`` 单位是秒; 转成正的 float (异常/非正值给 0)"""
    try:
        length = float(value)
    except (TypeError, ValueError):
        return 0.0
    return length if length > 0.0 else 0.0


# ---------------------------------------------------------------- 后端
class KugouMusicBackend(BaseMusicBackend):
    """酷狗平台后端 (只实现搜索/封面地址; 缓存与模板流程由基类提供)"""

    platform_id = "kugou"
    cache_name = "kugou"
    source_markers = SOURCE_MARKERS

    def request_headers(self) -> dict[str, str]:
        """联网请求头: 沿用旧实现的酷狗 UA (基类 ``http_get`` 下载封面时会用到)"""
        return dict(HEADERS)

    # ------------------------------------------------------------------ 搜索
    def search(self, info: MusicInfo) -> list[CoverCandidate]:
        """按源歌曲信息搜索酷狗候选 (歌名清洗 + 歌名/歌手匹配, 含 group 版本)。

        源 SMTC 的多歌手文本 (如 ``A/B/C``) 与酷狗结果的 ``A、B、C`` 分隔符不同,
        因此歌手匹配用 ``artist_matches``, 并支持用拆分后的歌手作为搜索关键字兜底。
        """
        title = clean_music_title(normalize_text(info.title))
        artist = normalize_text(info.artist)
        if not title and not artist:
            return []
        logger.debug(f"酷狗搜索: title=\"{title}\" artist=\"{artist}\"")
        candidates: list[CoverCandidate] = []
        for keyword in search_keyword_variants(title, artist):
            try:
                songs = _search_songs_by_keyword(keyword)
            except RuntimeError as e:
                # 契约: search 失败返回 [] (由基类继续尝试其他来源/后端)
                logger.warning(f"酷狗搜索失败: {e}")
                continue
            ready = match_songs(songs, title, artist)
            if not ready:
                continue
            for song in ready:
                candidate = self._candidate_from_song(song, info)
                if candidate is not None:
                    candidates.append(candidate)
            if candidates:
                break
        logger.debug(f"酷狗搜索候选数: {len(candidates)} "
                     f"({info.title} - {info.artist})")
        return candidates

    def _candidate_from_song(self, song: dict[str, Any],
                             info: MusicInfo) -> CoverCandidate | None:
        """把一条酷狗搜索结果转成候选 (填 title/artist/album_title/album_artist/length)。"""
        title = normalize_text(song.get("songname_original")) \
            or normalize_text(song.get("songname"))
        artist = normalize_text(song.get("singername")) or info.artist
        if not title:
            return None
        album_title = normalize_text(song.get("album_name"))
        trans_param = song.get("trans_param")
        template = ""
        if isinstance(trans_param, dict):
            template = str(trans_param.get("union_cover") or "")
        candidate_info = MusicInfo(
            title=title,
            artist=artist,
            album_title=album_title,
            # 旧实现保留源 SMTC 的专辑作者; 源没有时用歌手名兜底
            album_artist=info.album_artist or artist,
            length=_duration_seconds(song.get("duration")),
        )
        return CoverCandidate(info=candidate_info, cover_ref=template)

    # ------------------------------------------------------------------ 封面
    def resolve_cover_url(self, candidate: CoverCandidate, size: int) -> str | None:
        """由候选的 ``union_cover`` 模板生成封面地址; 没有模板返回 None。"""
        template = str(candidate.cover_ref or "")
        if not template:
            logger.warning(f"候选缺少封面模板 (union_cover): "
                           f"{candidate.info.title} - {candidate.info.artist}")
            return None
        url = cover_url(template, size)
        return url or None
