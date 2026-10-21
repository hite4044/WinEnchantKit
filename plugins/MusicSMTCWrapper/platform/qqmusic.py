# -*- coding: utf-8 -*-
"""QQ音乐后端: 搜索 + 封面地址 (腾讯官方接口, 匿名可用)。

只使用腾讯官方的两个域名, 不依赖任何第三方 API / 本地服务 / 用户登录:

* **搜索 (主)**: ``https://c.y.qq.com/soso/fcgi-bin/search_for_qq_cp`` ——
  QQ 音乐官方网页搜索接口; 返回 ``data.song.list[]``, 含
  ``songname / singer[].name / albumname / albummid / interval(秒)``。
  实测匿名 (无 cookie / 无登录) 可用, 短时间高频请求也稳定。
* **搜索 (备用)**: ``https://u.y.qq.com/cgi-bin/musicu.fcg`` 的
  ``music.search.SearchCgiService`` / ``DoSearchForQQMusicDesktop`` ——
  QQ 音乐官方桌面客户端使用的接口, ``comm.uin = "0"`` 表示匿名。
  该接口会间歇性返回限流码 ``2001``, 因此只在前者失败时使用, 并带短暂重试。
* **封面**: ``https://y.gtimg.cn/music/photo_new/`` 官方图片 CDN。
  ``T002R{size}x{size}M000{album_mid}.jpg`` 是官方预设尺寸
  (90/120/150/180/300/500/800/1200, 其它尺寸 404);
  ``T002M000{album_mid}.jpg`` 是不限尺寸的原图 (通常 1500~3000px), 用作「最大封面」。

任何失败都返回 ``[]`` / ``None``, 由 ``BaseMusicBackend`` 与 main 回退到其它平台或源 SMTC 封面。
"""
from __future__ import annotations

import json
import logging
import time
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
    text_key,
)

logger = logging.getLogger(LOGGER_NAME)

# 源会话匹配标记 (source_app_user_model_id 小写子串)
SOURCE_MARKERS: tuple[str, ...] = ("qqmusic", "qq")

# 官方网页搜索接口 (主)
SEARCH_URL = "https://c.y.qq.com/soso/fcgi-bin/search_for_qq_cp"
# 官方桌面客户端搜索接口 (备用)
MUSICU_URL = "https://u.y.qq.com/cgi-bin/musicu.fcg"
SEARCH_MODULE = "music.search.SearchCgiService"
SEARCH_METHOD = "DoSearchForQQMusicDesktop"
# 搜索返回条数 (与其它平台保持一致)
SEARCH_LIMIT = 10
SEARCH_TIMEOUT = 10.0
# 官方客户端接口限流错误码 (实测会间歇性返回 2001); 命中后短暂等待重试
RATE_LIMIT_CODES = frozenset({2000, 2001})
# 限流重试间隔 (秒), 长度 = 最多重试次数 (总尝试次数 = 长度 + 1)
RATE_LIMIT_RETRY_DELAYS: tuple[float, ...] = (0.6, 1.5)
# 两次搜索的最小间隔 (连续切歌时避免过于频繁地请求)
SEARCH_MIN_INTERVAL = 0.3

# 官方封面 CDN 预设尺寸 (实测只支持这些尺寸, 其它尺寸 404)
COVER_PRESETS: tuple[int, ...] = (90, 120, 150, 180, 300, 500, 800, 1200)
SIZED_COVER_TEMPLATE = "https://y.gtimg.cn/music/photo_new/T002R{size}x{size}M000{mid}.jpg"
# 不限尺寸的原图 (最大封面)
ORIGINAL_COVER_TEMPLATE = "https://y.gtimg.cn/music/photo_new/T002M000{mid}.jpg"

# 浏览器请求头 (官方接口与封面 CDN 都接受)
WEB_HEADERS: dict[str, str] = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 Edg/132.0.0.0",
    "Referer": "https://y.qq.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


# ---------------------------------------------------------------- 工具
def cover_url(album_mid: str, size: int = 480) -> str:
    """由官方专辑 mid 生成封面地址。

    * ``size`` 为 ``FULL_COVER_SIZE`` (或 <=0) → 原图 ``T002M000`` (最大封面);
    * 其它尺寸 → 取 **大于等于** 请求尺寸的最小官方预设 (CDN 不支持任意尺寸);
    * 请求尺寸超过最大预设 (1200) → 原图。
    """
    album_mid = normalize_text(album_mid)
    if not album_mid:
        return ""
    try:
        requested = int(size)
    except (TypeError, ValueError):
        requested = 480
    if requested == FULL_COVER_SIZE or requested <= 0:
        return ORIGINAL_COVER_TEMPLATE.format(mid=album_mid)
    for preset in COVER_PRESETS:
        if preset >= requested:
            return SIZED_COVER_TEMPLATE.format(size=preset, mid=album_mid)
    return ORIGINAL_COVER_TEMPLATE.format(mid=album_mid)


def _seconds(value: Any) -> float:
    """QQ 搜索结果的 ``interval`` 是秒; 转成正的 float (异常/非正值给 0)"""
    try:
        length = float(value)
    except (TypeError, ValueError):
        return 0.0
    return length if length > 0.0 else 0.0


def _song_title(song: dict[str, Any]) -> str:
    """歌曲名: 网页接口用 ``songname``, 桌面客户端接口用 ``name``"""
    return normalize_text(song.get("songname")) or normalize_text(song.get("name"))


def _song_album(song: dict[str, Any]) -> tuple[str, str]:
    """(专辑名, 专辑 mid): 网页接口是扁平字段, 桌面客户端接口是 album.name / album.mid"""
    album = song.get("album") if isinstance(song.get("album"), dict) else {}
    album_title = normalize_text(song.get("albumname")) or normalize_text(album.get("name"))
    album_mid = normalize_text(song.get("albummid")) or normalize_text(album.get("mid"))
    return album_title, album_mid


def _artist_names(song: dict[str, Any]) -> list[str]:
    """取搜索结果里的歌手名列表 (忽略空值与非法项)"""
    names: list[str] = []
    for singer in song.get("singer") or []:
        if isinstance(singer, dict):
            name = normalize_text(singer.get("name"))
        else:
            name = normalize_text(singer)
        if name and name not in names:
            names.append(name)
    return names


def _web_song_list(payload: Any) -> list[dict[str, Any]]:
    """从网页搜索接口响应里取出歌曲列表 (结构异常时给空列表)"""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    song = data.get("song") if isinstance(data, dict) else None
    songs = song.get("list") if isinstance(song, dict) else None
    if not isinstance(songs, list):
        return []
    return [item for item in songs if isinstance(item, dict)]


def _musicu_song_list(payload: Any) -> list[dict[str, Any]]:
    """从桌面客户端接口响应里取出歌曲列表 (结构异常时给空列表)"""
    if not isinstance(payload, dict):
        return []
    req = payload.get("req")
    if not isinstance(req, dict):
        return []
    data = req.get("data")
    body = data.get("body") if isinstance(data, dict) else None
    song = body.get("song") if isinstance(body, dict) else None
    songs = song.get("list") if isinstance(song, dict) else None
    if not isinstance(songs, list):
        return []
    return [item for item in songs if isinstance(item, dict)]


def _request_songs_web(keyword: str, limit: int = SEARCH_LIMIT) -> list[dict[str, Any]]:
    """官方网页搜索接口: 失败抛 RuntimeError。"""
    params = {
        "format": "json",
        "p": "1",
        "n": str(max(1, min(int(limit), 30))),
        "w": keyword,
        "inCharset": "utf8",
        "outCharset": "utf-8",
        "notice": "0",
        "platform": "yqq",
        "needNewCode": "0",
    }
    try:
        response = requests.get(SEARCH_URL, params=params, headers=WEB_HEADERS,
                                timeout=SEARCH_TIMEOUT)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"网络请求失败: {e.__class__.__name__}: {e}") from e
    if response.status_code != 200:
        raise RuntimeError(f"搜索失败: 服务器返回HTTP错误码 [{response.status_code}]")
    try:
        data = response.json()
    except ValueError as e:
        raise RuntimeError(f"搜索失败: 服务器返回非 JSON 数据: {e}") from e
    if not isinstance(data, dict) or data.get("code") != 0:
        code = data.get("code") if isinstance(data, dict) else data
        raise RuntimeError(f"搜索失败: 接口返回 code={code}")
    return _web_song_list(data)


def _request_songs_musicu(keyword: str, limit: int = SEARCH_LIMIT) -> list[dict[str, Any]]:
    """官方桌面客户端搜索接口 (备用): 限流时短暂重试; 失败抛 RuntimeError。"""
    payload = {
        # ct=19 / cv=1859 / format=json 是官方桌面客户端的上下文参数; uin=0 = 匿名 (无需登录)
        "comm": {"ct": "19", "cv": "1859", "uin": "0", "format": "json"},
        "req": {
            "method": SEARCH_METHOD,
            "module": SEARCH_MODULE,
            "param": {
                "query": keyword,
                "num_per_page": max(1, min(int(limit), 30)),
                "page_num": 1,
            },
        },
    }
    for attempt in range(len(RATE_LIMIT_RETRY_DELAYS) + 1):
        try:
            response = requests.get(MUSICU_URL,
                                    params={"data": json.dumps(payload, ensure_ascii=False)},
                                    headers=WEB_HEADERS, timeout=SEARCH_TIMEOUT)
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"网络请求失败: {e.__class__.__name__}: {e}") from e
        if response.status_code != 200:
            raise RuntimeError(f"搜索失败: 服务器返回HTTP错误码 [{response.status_code}]")
        try:
            data = response.json()
        except ValueError as e:
            raise RuntimeError(f"搜索失败: 服务器返回非 JSON 数据: {e}") from e
        req = data.get("req") if isinstance(data, dict) else None
        code = req.get("code") if isinstance(req, dict) else None
        if code == 0:
            return _musicu_song_list(data)
        if code in RATE_LIMIT_CODES and attempt < len(RATE_LIMIT_RETRY_DELAYS):
            delay = RATE_LIMIT_RETRY_DELAYS[attempt]
            logger.info(f"QQ音乐备用搜索接口被限流 (code={code}), {delay:.1f}s 后重试 "
                        f"({attempt + 1}/{len(RATE_LIMIT_RETRY_DELAYS)})")
            time.sleep(delay)
            continue
        raise RuntimeError(f"搜索失败: 接口返回 code={code}")
    return []


def _request_songs(keyword: str, limit: int = SEARCH_LIMIT) -> list[dict[str, Any]]:
    """先走官方网页搜索接口, 失败再走官方桌面客户端接口; 都失败才抛 RuntimeError。"""
    keyword = normalize_text(keyword)
    logger.debug(f"QQ音乐搜索关键字: \"{keyword}\"")
    try:
        return _request_songs_web(keyword, limit)
    except RuntimeError as e:
        logger.warning(f"QQ音乐网页搜索接口失败: {e}, 尝试官方客户端接口")
    return _request_songs_musicu(keyword, limit)


# ---------------------------------------------------------------- 后端
class QqMusicBackend(BaseMusicBackend):
    """QQ音乐平台后端 (只实现搜索/封面地址; 缓存与模板流程由基类提供)"""

    platform_id = "qq"
    cache_name = "qq"
    source_markers = SOURCE_MARKERS

    def __init__(self, cover_format: str | None = None, cover_quality: int = 90):
        super().__init__(cover_format, cover_quality)
        self._last_search_at = 0.0

    def request_headers(self) -> dict[str, str]:
        """联网请求头: 官方 CDN 需要 Referer"""
        return dict(WEB_HEADERS)

    # ------------------------------------------------------------------ 搜索
    def search(self, info: MusicInfo) -> list[CoverCandidate]:
        """按源歌曲信息搜索 QQ 音乐候选 (歌手匹配; 歌名完全一致的优先)。

        源 SMTC 的多歌手文本 (如 ``A/B/C``) 与 QQ 结果的歌手列表分隔符不同,
        因此歌手匹配用 ``artist_matches``, 并支持用拆分后的歌手作为搜索关键字兜底。
        """
        title = normalize_text(info.title)
        artist = normalize_text(info.artist)
        title_key = text_key(title)
        if not title and not artist:
            return []
        candidates: list[CoverCandidate] = []
        seen: set[tuple[str, str, int]] = set()
        for keyword in search_keyword_variants(title, artist):
            try:
                self._throttle_search()
                songs = _request_songs(keyword)
            except RuntimeError as e:
                # 契约: search 失败返回 [], 由基类继续尝试其他来源/后端
                logger.warning(f"QQ音乐搜索失败: {e}")
                continue
            ready: list[dict[str, Any]] = []
            for song in songs:
                if artist_matches(artist, _artist_names(song)):
                    ready.append(song)
            if not ready:
                continue
            # 歌名完全一致的优先, 其余保持接口原始顺序 (稳定排序)
            ready.sort(key=lambda song: 0 if text_key(_song_title(song)) == title_key else 1)
            for song in ready:
                candidate = self._candidate_from_song(song, info)
                if candidate is None:
                    continue
                dedupe_key = (text_key(candidate.info.title), candidate.cover_ref,
                              int(candidate.info.length * 1000))
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                candidates.append(candidate)
            if candidates:
                break
        if not candidates:
            logger.warning(f"QQ音乐搜索失败: 找不到对应歌曲 {artist} - {title}")
        logger.debug(f"QQ音乐搜索候选数: {len(candidates)} ({title} - {artist})")
        return candidates

    def _throttle_search(self):
        """两次搜索之间保持最小间隔, 避免过于频繁地请求官方接口"""
        elapsed = time.monotonic() - self._last_search_at
        wait = SEARCH_MIN_INTERVAL - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_search_at = time.monotonic()

    def _candidate_from_song(self, song: dict[str, Any],
                             info: MusicInfo) -> CoverCandidate | None:
        """把一条 QQ 搜索结果转成候选 (cover_ref = 官方专辑 mid)。"""
        album_title, album_mid = _song_album(song)
        if not album_mid:
            # 没有专辑 mid 就拿不到封面 (广告/特殊条目), 不能作为候选
            return None
        title = _song_title(song) or info.title
        if not title:
            return None
        names = _artist_names(song)
        artist = join_artists(names) or info.artist
        candidate_info = MusicInfo(
            title=title,
            artist=artist,
            album_title=album_title,
            # 旧实现保留源 SMTC 的专辑作者; 源没有时用歌手兜底
            album_artist=info.album_artist or artist,
            length=_seconds(song.get("interval")),
        )
        return CoverCandidate(info=candidate_info, cover_ref=album_mid)

    # ------------------------------------------------------------------ 封面
    def resolve_cover_url(self, candidate: CoverCandidate, size: int) -> str | None:
        """由候选的官方专辑 mid 生成对应尺寸的封面地址。"""
        url = cover_url(candidate.cover_ref, size)
        if not url:
            logger.warning(f"候选缺少专辑 mid (封面): "
                           f"{candidate.info.title} - {candidate.info.artist}")
            return None
        return url
