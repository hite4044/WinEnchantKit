# -*- coding: utf-8 -*-
"""网易云音乐后端: 搜索 + 歌曲详情 + 封面地址。

本模块只负责网易云自己的业务 (与旧 ``plugins/OldMusicSMTCWrapper/smtc_backend/netease.py``
的接口逻辑保持一致), 缓存、封面相似度、模板流程都由
:class:`~.base_backend.BaseMusicBackend` 统一处理:

* :meth:`NeteaseMusicBackend.search` —— 搜索接口
  ``https://music.163.com/api/search/get/web?...&s={keyword}&type=1&limit={limit}``,
  返回所有歌手匹配的候选 (歌名完全一致的优先); 搜索结果里的 ``album.picUrl`` 为 null,
  因此 ``CoverCandidate.cover_ref`` 存歌曲 id 字符串;
* :meth:`NeteaseMusicBackend.resolve_cover_url` —— 按 id 查详情 (旧接口为主、v3 兜底)
  拿 ``album.picUrl``, 再拼 ``?param={size}y{size}``; 详情结果按 id 做内存缓存,
  避免同一个候选被反复请求。

封面地址约定: ``picUrl`` 后加 ``?param={size}y{size}`` 得到对应尺寸; 不加则是最大封面。
"""
from __future__ import annotations

import logging
from typing import Any

import requests

from .base_backend import FULL_COVER_SIZE, BaseMusicBackend, CoverCandidate
from ..music_base import (
    LOGGER_NAME,
    MusicInfo,
    artist_matches,
    join_artists,
    normalize_text,
    search_keyword_variants,
)

logger = logging.getLogger(LOGGER_NAME)

# 源会话匹配标记 (source_app_user_model_id 小写子串)
SOURCE_MARKERS: tuple[str, ...] = ("netease", "cloudmusic")

# 旧实现使用的请求头 (含 Referer; 基类 http_get 下载封面时也会用到)
WEB_HEADERS: dict[str, str] = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 Edg/132.0.0.0",
    "Referer": "https://music.163.com/",
    "Accept": "application/json, text/plain, */*",
}
SEARCH_URL = ("https://music.163.com/api/search/get/web?csrf_token=hlpretag=&hlposttag="
              "&s={keyword}&type=1&offset=0&total=true&limit={limit}")
# 详情接口: v3 更“现代”但实测常返回 album.picUrl=null, 旧接口稳定给出 picUrl, 因此以旧接口为主、v3 兜底
DETAIL_URL = "https://music.163.com/api/song/detail?ids=%5B{ids}%5D"
DETAIL_URL_V3 = "https://music.163.com/api/v3/song/detail?c=%5B%7B%22id%22%3A{ids}%7D%5D"
# 封面尺寸参数: picUrl + "?param=130y130"; 不加则是最大封面
SIZE_PARAM = "?param={size}y{size}"

# 搜索默认取多少条 (与旧实现一致)
SEARCH_LIMIT = 10
SEARCH_TIMEOUT = 10.0
DETAIL_TIMEOUT = 10.0


def cover_url(pic_url: str, size: int = 480) -> str:
    """由 ``album.picUrl`` 生成封面地址。

    ``size`` 为 ``FULL_COVER_SIZE`` (或任何 <= 0) 时不加尺寸参数, 即最大封面;
    没有 ``picUrl`` 时返回空串。
    """
    pic_url = pic_url or ""
    if not pic_url or size == FULL_COVER_SIZE or (size or 0) <= 0:
        return pic_url
    return pic_url + SIZE_PARAM.format(size=int(size))


def _get_json(url: str, timeout: float = SEARCH_TIMEOUT) -> Any | None:
    """GET 并解析 JSON: 连接失败返回 None, HTTP / JSON 错误抛 RuntimeError。"""
    try:
        resp = requests.get(url, headers=WEB_HEADERS, timeout=timeout)
    except requests.exceptions.ConnectionError:
        return None
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"网络请求失败: {e.__class__.__name__}: {e}") from e
    if resp.status_code != 200:
        raise RuntimeError(f"服务器返回HTTP错误码 [{resp.status_code}]")
    try:
        return resp.json()
    except ValueError as e:
        raise RuntimeError(f"服务器返回非 JSON 数据: {e}") from e


def search_song_list(keyword: str, limit: int = SEARCH_LIMIT) -> list[dict[str, Any]]:
    """调用旧搜索接口, 返回 ``result.songs`` 原始列表 (无结果/连接失败给 [])。"""
    logger.debug(f"歌曲搜索关键字: \"{keyword}\"")
    payload = _get_json(SEARCH_URL.format(keyword=keyword, limit=int(limit)))
    if not isinstance(payload, dict):
        return []
    result = payload.get("result")
    songs = result.get("songs") if isinstance(result, dict) else None
    if not isinstance(songs, list):
        return []
    return [song for song in songs if isinstance(song, dict)]


def song_detail(song_id: int) -> dict[str, Any] | None:
    """按歌曲 id 查详情 (封面 ``album.picUrl`` 与 ``duration`` 都在这里)。

    先走旧详情接口; 拿不到 ``album.picUrl`` 时再试 v3 接口 (实测两者会互相缺失,
    需要兜底)。连接失败/HTTP 错误/无结果返回 None。
    """
    first: dict[str, Any] | None = None
    for url in (DETAIL_URL.format(ids=int(song_id)),
                DETAIL_URL_V3.format(ids=int(song_id))):
        try:
            payload = _get_json(url, DETAIL_TIMEOUT)
        except RuntimeError as e:
            logger.warning(f"查询歌曲详情失败 (id={song_id}): {e}")
            continue
        if not isinstance(payload, dict):
            continue
        songs = payload.get("songs")
        if not isinstance(songs, list) or not songs or not isinstance(songs[0], dict):
            continue
        first = first or songs[0]
        album = songs[0].get("album")
        if isinstance(album, dict) and album.get("picUrl"):
            return songs[0]
    return first


def _milliseconds_to_seconds(value: Any) -> float:
    """网易云 ``duration`` 单位是毫秒; 转成正的秒数 (异常/非正值给 0)"""
    try:
        milliseconds = float(value)
    except (TypeError, ValueError):
        return 0.0
    return milliseconds / 1000.0 if milliseconds > 0.0 else 0.0


def _artist_names(song: dict[str, Any]) -> list[str]:
    """取搜索结果里的歌手名列表 (忽略空值与非法项)"""
    names: list[str] = []
    for artist in song.get("artists") or []:
        if isinstance(artist, dict):
            name = normalize_text(artist.get("name"))
        else:
            name = normalize_text(artist)
        if name and name not in names:
            names.append(name)
    return names


# ---------------------------------------------------------------- 后端
class NeteaseMusicBackend(BaseMusicBackend):
    """网易云平台后端 (只实现搜索/详情/封面地址; 缓存与模板流程由基类提供)"""

    platform_id = "netease"
    cache_name = "netease"
    source_markers = SOURCE_MARKERS

    def __init__(self, cover_format: str | None = None, cover_quality: int = 90):
        super().__init__(cover_format, cover_quality)
        # 内存缓存: 歌曲 id -> 详情 dict (避免相似度比对/多尺寸请求时重复查详情)
        self._detail_cache: dict[int, dict[str, Any]] = {}

    def request_headers(self) -> dict[str, str]:
        """联网请求头: 沿用旧实现的 ``WEB_HEADERS`` (含 Referer)"""
        return dict(WEB_HEADERS)

    # ------------------------------------------------------------------ 搜索
    def search(self, info: MusicInfo) -> list[CoverCandidate]:
        """按源歌曲信息搜索网易云候选 (歌手匹配; 歌名完全一致的优先)。

        源 SMTC 的多歌手文本 (如 ``A/B/C``) 与网易云结果的歌手列表分隔符不同,
        因此歌手匹配用 ``artist_matches``, 并支持用拆分后的歌手作为搜索关键字兜底。
        """
        title = normalize_text(info.title)
        artist = normalize_text(info.artist)
        if not title and not artist:
            return []
        logger.debug(f"网易云搜索: title=\"{title}\" artist=\"{artist}\"")
        candidates: list[CoverCandidate] = []
        for keyword in search_keyword_variants(title, artist):
            try:
                songs = search_song_list(keyword)
            except RuntimeError as e:
                # 契约: search 失败返回 [] (由基类继续尝试其他来源/后端)
                logger.warning(f"网易云搜索失败: {e}")
                continue
            ready: list[dict[str, Any]] = []
            for song in songs:
                if artist_matches(artist, _artist_names(song)):
                    ready.append(song)
            if not ready:
                continue
            # 歌名完全一致的优先, 其余保持搜索接口的原始顺序 (稳定排序)
            ready.sort(key=lambda song: 0 if normalize_text(song.get("name")) == title else 1)
            for song in ready:
                candidate = self._candidate_from_song(song, info)
                if candidate is not None:
                    candidates.append(candidate)
            if candidates:
                break
        if not candidates:
            logger.warning(f"网易云搜索失败: 找不到对应歌曲 {artist} - {title}")
        logger.debug(f"网易云搜索候选数: {len(candidates)} ({title} - {artist})")
        return candidates

    def _candidate_from_song(self, song: dict[str, Any],
                             info: MusicInfo) -> CoverCandidate | None:
        """把一条网易云搜索结果转成候选 (cover_ref = 歌曲 id 字符串)。"""
        song_id = song.get("id")
        if song_id is None:
            return None
        names = _artist_names(song)
        artist = join_artists(names) or info.artist
        album = song.get("album")
        album_title = normalize_text(album.get("name")) if isinstance(album, dict) else ""
        candidate_info = MusicInfo(
            title=normalize_text(song.get("name")) or info.title,
            artist=artist,
            album_title=album_title,
            # 旧实现保留源 SMTC 的专辑作者; 源没有时用第一个歌手兜底
            album_artist=info.album_artist or (names[0] if names else artist),
            length=_milliseconds_to_seconds(song.get("duration")),
        )
        return CoverCandidate(info=candidate_info, cover_ref=str(song_id))

    # ------------------------------------------------------------------ 详情 / 封面
    def get_detail(self, song_id: int) -> dict[str, Any] | None:
        """按 id 查详情, 结果按 id 缓存在内存; 查询失败返回 None。"""
        cached = self._detail_cache.get(song_id)
        if cached is not None:
            return cached
        detail = song_detail(song_id)
        if detail is not None:
            self._detail_cache[song_id] = detail
        return detail

    def resolve_cover_url(self, candidate: CoverCandidate, size: int) -> str | None:
        """按候选的歌曲 id 查详情拿 ``album.picUrl``, 拼尺寸参数返回; 查不到返回 None。"""
        try:
            song_id = int(str(candidate.cover_ref or "").strip())
        except (TypeError, ValueError):
            logger.warning(f"候选缺少歌曲 id (cover_ref): "
                           f"{candidate.info.title} - {candidate.info.artist}")
            return None
        detail = self.get_detail(song_id)
        if detail is None:
            logger.warning(f"查询歌曲详情失败: id={song_id}")
            return None
        album = detail.get("album")
        pic_url = normalize_text(album.get("picUrl")) if isinstance(album, dict) else ""
        if not pic_url:
            logger.warning(f"详情结果缺少封面地址 (album.picUrl): id={song_id}")
            return None
        url = cover_url(pic_url, size)
        return url or None
