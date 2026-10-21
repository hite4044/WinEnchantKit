# -*- coding: utf-8 -*-
"""网易云音乐后端: 搜索 / 歌曲详情 / 封面获取 / 缓存编撰。

模板方法流程在 :class:`smtc_backend.base.MusicBackend`; 本模块只写网易云自己的部分。

接口 (实测):
* 搜索:  ``https://music.163.com/api/search/get/web?...&s={keyword}&type=1``
  —— 返回 ``result.songs[]``, 含 ``id`` / ``name`` / ``duration``(毫秒) / ``artists[].name``;
  注意搜索结果的 ``album.picUrl`` 为 null, 封面要再查详情。
* 详情:  ``https://music.163.com/api/v3/song/detail?c=[{"id":<id>}]``
  —— 返回 ``songs[].album.picUrl`` 与 ``songs[].duration``(毫秒)。

封面地址约定: ``picUrl`` 后加 ``?param={size}y{size}`` 得到对应尺寸; 不加则是最大封面。
"""
from __future__ import annotations

import json
from io import BytesIO
from typing import Any

import requests
from PIL import Image

from .base import (
    DEFAULT_COVER_SIZE,
    FULL_COVER_SIZE,
    MAX_COVER_SIZE,
    MusicBackend,
    StandardMusicInfo,
    make_music_hash,
    plugin_logger,
    prepare_cover,
    read_raw_cover,
)
from winsdk.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionMediaProperties as SessionMediaProperties,
)

logger = plugin_logger

# 源会话匹配标记 (source_app_user_model_id 小写子串)
SOURCE_MARKERS = ("netease", "cloudmusic")

WEB_HEADERS = {
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

# 缓存记录里的平台标识
COVER_CLASS = "netease"

# 搜索默认取多少条
SEARCH_LIMIT = 10


def cover_url(pic_url: str, size: int = 480) -> str:
    """由 ``album.picUrl`` 生成封面地址。

    ``size`` 为 ``FULL_COVER_SIZE`` (或任何 <= 0) 时不加尺寸参数, 即最大封面。
    """
    pic_url = pic_url or ""
    if not pic_url or size == FULL_COVER_SIZE or (size or 0) <= 0:
        return pic_url
    return pic_url + SIZE_PARAM.format(size=int(size))


def music_hash_for(title: str, artist: str) -> int:
    """歌曲指纹 = make_music_hash("netease", title, artist), 作为缓存键 (跨进程稳定)"""
    return make_music_hash("netease", title, artist)


def search_song(title: str, artist: str, limit: int = SEARCH_LIMIT) -> dict[str, Any] | None:
    """按“歌名 歌手”搜索网易云, 返回最匹配的一首歌 (查详情后的对象); 找不到返回 None"""
    keyword = f"{title} {artist}".strip()
    logger.debug(f"歌曲搜索关键字: \"{keyword}\"")
    try:
        resp = requests.get(SEARCH_URL.format(keyword=keyword, limit=limit),
                            headers=WEB_HEADERS, timeout=10)
    except requests.exceptions.ConnectionError:
        return None
    if resp.status_code != 200:
        raise RuntimeError(f"搜索失败: 服务器返回HTTP错误码 [{resp.status_code}]")
    songs = ((resp.json().get("result") or {}).get("songs")) or []
    matched = None
    ready_songs = []
    for song in songs:
        names = [str(a.get("name") or "") for a in (song.get("artists") or [])]
        if any(artist and artist in name for name in names):
            ready_songs.append(song)
    if ready_songs:
        # 歌名完全一致的优先, 否则取第一个歌手匹配的
        matched = next((s for s in ready_songs if str(s.get("name") or "") == title), ready_songs[0])
    if matched is None:
        logger.warning(f"搜索失败: 找不到对应歌曲 {artist} - {title}")
        return None
    logger.debug(f"匹配歌曲 {matched.get('name')} - {[a.get('name') for a in (matched.get('artists') or [])]}")
    detail = song_detail(int(matched["id"]))
    if detail is None:
        return None
    # 用详情里的完整字段 (含 album.picUrl 与精确 duration)
    detail.setdefault("artists", matched.get("artists"))
    return detail


def song_detail(song_id: int) -> dict[str, Any] | None:
    """按歌曲 id 查详情 (封面 ``album.picUrl`` 与 ``duration`` 都在这里)。

    先走旧详情接口; 拿不到 ``album.picUrl`` 时再试 v3 接口 (实测两者会互相缺失, 需要兜底)。
    """
    first = None
    for url in (DETAIL_URL.format(ids=int(song_id)), DETAIL_URL_V3.format(ids=int(song_id))):
        try:
            resp = requests.get(url, headers=WEB_HEADERS, timeout=10)
        except requests.exceptions.ConnectionError:
            continue
        if resp.status_code != 200:
            continue
        songs = resp.json().get("songs") or []
        if not songs:
            continue
        first = first or songs[0]
        if (songs[0].get("album") or {}).get("picUrl"):
            return songs[0]
    return first


# ---------------------------------------------------------------- 后端
class NeteaseMusicBackend(MusicBackend):
    """网易云平台后端 (只实现网易云自己的业务; 三级流程由基类模板方法串起来)"""

    platform_id = "netease"
    cache_name = "netease"
    source_markers = SOURCE_MARKERS

    def __init__(self, cover_format=None, cover_quality: int = 90):
        self.cover_format = cover_format
        self.cover_quality = int(cover_quality)
        self._music_cache = None

    # ------------------------------------------------------------------ 缓存编撰 / 解读
    def encode_record(self, music_info: StandardMusicInfo, cover_meta: dict[str, Any]) -> dict[str, Any]:
        """编撰缓存记录: 歌曲信息 + 封面 picUrl + 本地已有的封面尺寸"""
        music_hash = music_hash_for(music_info.title, music_info.artist)
        pic_url = cover_meta.get("cover") if isinstance(cover_meta, dict) else None
        cover: dict[str, Any] = {}
        if isinstance(pic_url, str) and pic_url:
            cover = {"class": COVER_CLASS, "cover": pic_url, "sizes": self.music_cache.sizes(music_hash)}
        return {
            "title": music_info.title,
            "artist": music_info.artist,
            "album_title": music_info.album_title,
            "album_artist": music_info.album_artist,
            "length": music_info.length,
            "cover": cover,
        }

    def decode_record(self, record: dict[str, Any], music_info: StandardMusicInfo) -> StandardMusicInfo | None:
        """解读缓存记录: 关键字段缺失 / 封面地址无效都返回 None (交给下一步)"""
        if not isinstance(record, dict) or not record.get("title"):
            return None
        cover = record.get("cover") if isinstance(record.get("cover"), dict) else {}
        pic_url = cover.get("cover")
        if cover.get("class") != COVER_CLASS or not isinstance(pic_url, str) or not pic_url:
            return None
        music_info.title = str(record.get("title") or "")
        music_info.artist = str(record.get("artist") or "")
        music_info.album_title = str(record.get("album_title") or "")
        music_info.album_artist = str(record.get("album_artist") or "")
        try:
            music_info.length = float(record.get("length") or 0.0)
        except (TypeError, ValueError):
            music_info.length = 0.0
        return music_info

    # ------------------------------------------------------------------ 三级实现
    def from_cache(self, music_info: StandardMusicInfo, size: int,
                   props: SessionMediaProperties) -> StandardMusicInfo | None:
        """本地缓存: 歌曲信息与本地封面都必须有, 缺一即视为未命中"""
        music_hash = music_hash_for(music_info.title, music_info.artist)
        record = self.music_cache.get(music_hash)
        if record is None or self.decode_record(record, music_info) is None:
            return None
        found = self.music_cache.find_cover(music_hash, size)
        if found is None:
            logger.warning(f"本地缓存封面加载失败: {music_info.title} - {music_info.artist}")
            return None
        cover, slot = found
        music_info.album_title = "" # 将此字段置空以防止部分动态壁纸将专辑名添加至歌手后部
        music_info.cover = prepare_cover(cover, size)
        music_info.cover_slot = slot
        music_info.has_cover = True
        return music_info

    def from_web(self, music_info: StandardMusicInfo, size: int,
                 props: SessionMediaProperties) -> StandardMusicInfo | None:
        """联网: 搜索 -> 查详情 -> 下载最大封面 -> 写缓存 (之后各尺寸请求复用同一份)"""
        title, artist = music_info.title, music_info.artist
        try:
            song = search_song(title, artist)
        except RuntimeError as e:
            logger.error(e)
            return None
        if not song:
            return None
        album = song.get("album") if isinstance(song.get("album"), dict) else {}
        pic_url = str(album.get("picUrl") or "")
        if not pic_url:
            logger.warning(f"详情结果缺少封面地址 (album.picUrl): {artist} - {title}")
            return None
        # 联网取图尺寸: 本地一个封面都没有 -> 就按请求尺寸取; 已有但都比请求小 / 要求最大封面 -> 取最大封面
        # (size 为 FULL_COVER_SIZE 时 cover_url 不加 ?param)
        music_hash = music_hash_for(music_info.title, music_info.artist)
        fetch_size = size if size != FULL_COVER_SIZE and not self.music_cache.has_cover(music_hash) \
            else FULL_COVER_SIZE
        try:
            resp = requests.get(cover_url(pic_url, fetch_size), headers=WEB_HEADERS, timeout=5)
            resp.raise_for_status()
            with Image.open(BytesIO(resp.content)) as image:
                image.load()
                cover = image.copy()
        except Exception as e:
            logger.warning(f"下载封面失败: {e.__class__.__name__}: {e}")
            return None
        if max(cover.size) > MAX_COVER_SIZE:
            cover.thumbnail((MAX_COVER_SIZE, MAX_COVER_SIZE))
        # 补全歌曲信息: 专辑名 / 歌手优先用详情结果
        music_info.album_title = str(album.get("name") or music_info.album_title)
        artists = [str(a.get("name")) for a in (song.get("artists") or [])]
        if artists:
            music_info.artist = artists[0]
            music_info.album_artist = music_info.album_artist or artists[0]
        length = song.get("duration")
        if isinstance(length, (int, float)) and length > 0:
            music_info.length = float(length) / 1000.0
        music_hash = music_hash_for(music_info.title, music_info.artist)
        slot = self.music_cache.save_cover(cover, music_hash, fetch_size,
                                           music_info.title, music_info.artist)
        self.music_cache.put(music_hash, self.encode_record(music_info, {"cover": pic_url}))
        music_info.cover = prepare_cover(cover, size)
        music_info.cover_slot = slot if slot is not None else max(cover.size)
        music_info.has_cover = True
        logger.info(f"已获取高清封面: {music_info.title} - {music_info.artist} (请求 {size}, 缓存标记 {fetch_size})")
        return music_info

    def from_raw_smtc(self, music_info: StandardMusicInfo, size: int,
                      props: SessionMediaProperties) -> StandardMusicInfo | None:
        """兜底: 源 SMTC 会话自带的低清封面 (不写缓存)"""
        cover = read_raw_cover(props)
        if cover is None:
            return None
        music_info.cover = cover
        music_info.cover_slot = max(cover.size)
        music_info.has_cover = True
        return music_info
