# -*- coding: utf-8 -*-
"""酷狗音乐后端: 搜索 / 匹配 / 封面获取 / 缓存编撰。

模板方法流程在 :class:`_smtc_backend_base.MusicBackend`; 本模块只写酷狗自己的部分:

* :meth:`KugouMusicBackend.from_cache`    —— 把缓存记录 + 本地封面解读成歌曲信息
* :meth:`KugouMusicBackend.from_web`      —— 酷狗搜索接口 -> 下载封面 -> 写缓存
* :meth:`KugouMusicBackend.from_raw_smtc` —— 源 SMTC 会话自带低清封面兜底
* :meth:`KugouMusicBackend.encode_record` / :meth:`decode_record` —— 缓存数据编撰与解读

封面尺寸约定: ``FULL_COVER_SIZE`` (即 -1) 表示“获取最大封面”; 上限 ``MAX_COVER_SIZE``。
缓存里「最大封面」用图片自身尺寸作文件名标记, 可据此判断请求尺寸是否已被覆盖。

命名说明: 模块顶层名字带 ``_smtc_backend_`` 前缀是刻意的。插件加载器会把插件根目录插到
``sys.path[0]`` (``gui/control_panel.py::load_plugin``), 而 plugins.HDKugouCover 已经占用了
顶层名字 ``backend`` / ``base``; 私有前缀保证本插件的模块在任何加载顺序下都能被唯一解析,
也不会污染 / 破坏其它插件。
"""
from __future__ import annotations

import re
import string
from io import BytesIO
from typing import Any

import requests
from PIL import Image
from requests.exceptions import SSLError
from winsdk.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionMediaProperties as SessionMediaProperties,
)
from winsdk.windows.security.cryptography import CryptographicBuffer
from winsdk.windows.storage.streams import DataReader

from _smtc_backend_base import (
    FULL_COVER_SIZE,
    MAX_COVER_SIZE,
    MusicBackend,
    StandardMusicInfo,
    make_music_hash,
    plugin_logger,
    wait_result,
)

logger = plugin_logger

# ---------------------------------------------------------------- 原样搬运的常量
HEADERS = {'User-Agent': '[HDKugouCover(Github)(WinEnchantKit)] Music Search'}
SEARCH_URL = "http://mobilecdn.kugou.com/api/v3/search/song?format=json&keyword={keyword}&page=1"
OTHER_NAME_PATTERN = re.compile(r"\([^()]+\)")

# 封面元数据里的平台标识 (缓存记录里用于识别封面模板是不是本平台的)
COVER_CLASS = "kugou"
# 封面模板里的尺寸占位符
SIZE_PLACEHOLDER = "{size}"


# ---------------------------------------------------------------- 搜索 (原样搬运)
def extract_music_title(title: str) -> tuple[str, str | None]:
    if match := OTHER_NAME_PATTERN.search(title):
        title = title[:match.start()]
        other_name = match.group()
    else:
        other_name = None
    return title.strip(), other_name


has_alert = False


def search_for_song_list(title: str, artist: str) -> list[dict[str, Any]]:
    global has_alert
    keyword = f"{title} {artist}"
    logger.debug(f"歌曲搜索关键字: \"{keyword}\"")
    try:
        resp = requests.get(SEARCH_URL.format(keyword=keyword), headers=HEADERS, data=None)
    except SSLError:
        if not has_alert:
            logger.error("系统代理有问题 (是工具的问题")
            has_alert = True
        raise RuntimeError("SSL错误")
    except requests.exceptions.ConnectionError:
        return []
    if resp.status_code != 200:
        raise RuntimeError(f"搜索失败: 服务器返回HTTP错误码 [{resp.status_code}]")
    has_alert = False
    resp_json = resp.json()
    return resp_json["data"]["info"]


def search_music(title: str, artist: str, album: str) -> dict[str, Any]:
    """通过给定的歌曲名、歌手、专辑来搜索歌曲"""
    clean_title, other_name = extract_music_title(title)
    song_list = search_for_song_list(clean_title, artist)
    ready_songs = []
    for song in song_list:
        if all([
            artist in song["singername"],
            clean_title == song["songname_original"],
            other_name is None or other_name in song["songname"]
        ]):
            logger.debug(f"匹配歌曲 {song['songname']} - {song['singername']}")
            ready_songs.append(song)
            for group_song in song["group"] if song.get("group") else []:
                ready_songs.append(group_song)
    if not ready_songs:
        raise RuntimeError(f"搜索失败: 找不到对应歌曲 {artist} - {title} 《{album}》")
    for song in ready_songs:
        logger.debug(f"匹配专辑 {song['album_name']}")
        if song["album_name"] == album:
            return song
    else:
        logger.warning(f"{artist} - {title} 《{album}》 无法找到对应专辑, 使用专辑列表中第一个歌曲")
        return ready_songs[0]


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


def music_hash_for(title: str, artist: str) -> int:
    """歌曲指纹 = make_music_hash("kugou", title, artist), 作为缓存键 (跨进程稳定)"""
    return make_music_hash("kugou", title, artist)


def clean_music_title(title: str) -> str:
    """歌名清洗 (原 main.py::Plugin.load_cover 里的那段逻辑)。

    若歌名含 "(": 取最后一个 "(" 到第一个 ")" 的子串作为副标题候选; 该子串去掉 ASCII
    字母后仍有剩余字符时, 把歌名截断到第一个 "(" 之前并 strip。

    与原逻辑的差异 (防御): 原代码在 "(" 之后没有 ")" 时会 ValueError, 这里保持原歌名。
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


def read_raw_cover(props: SessionMediaProperties) -> Image.Image | None:
    """读取源 SMTC 会话自带的低清封面; 任何失败都返回 None (空值防御)。"""
    try:
        thumbnail = getattr(props, "thumbnail", None)
        if thumbnail is None:
            return None
        stream = wait_result(thumbnail.open_read_async())
        count = int(stream.size) if stream is not None else 0
        if count <= 0:
            return None
        try:
            # open_read_async 返回的流定位在 0; 防御性复位, 否则 load_async 可能 E_BOUNDS
            stream.seek(0)
        except Exception:
            pass
        reader = DataReader(stream)
        wait_result(reader.load_async(count))
        buffer = reader.read_buffer(count)
        data = CryptographicBuffer.copy_to_byte_array(buffer) if buffer is not None else None
        if not data:
            return None
        with Image.open(BytesIO(bytes(data))) as image:
            image.load()
            return image.copy()
    except Exception as e:
        logger.debug(f"读取SMTC缩略图失败: {e.__class__.__name__}: {e}")
        return None


# ---------------------------------------------------------------- 后端
class KugouMusicBackend(MusicBackend):
    """酷狗平台后端 (只实现酷狗自己的业务; 三级流程由基类模板方法串起来)"""

    platform_id = "kugou"
    cache_name = "kugou"

    def __init__(self, cover_format=None, cover_quality: int = 90):
        self.cover_format = cover_format
        self.cover_quality = int(cover_quality)
        self._music_cache = None

    # ------------------------------------------------------------------ 缓存编撰 / 解读
    def encode_record(self, music_info: StandardMusicInfo, cover_meta: dict[str, Any]) -> dict[str, Any]:
        """编撰缓存记录: 歌曲信息 + 封面模板 + 本地已有的封面尺寸"""
        music_hash = music_hash_for(music_info.title, music_info.artist)
        template = cover_meta.get("cover") if isinstance(cover_meta, dict) else None
        cover: dict[str, Any] = {}
        if isinstance(template, str) and template:
            cover = {"class": COVER_CLASS, "cover": template, "sizes": self.music_cache.sizes(music_hash)}
        return {
            "title": music_info.title,
            "artist": music_info.artist,
            "album_title": music_info.album_title,
            "album_artist": music_info.album_artist,
            "length": music_info.length,
            "cover": cover,
        }

    def decode_record(self, record: dict[str, Any], music_info: StandardMusicInfo) -> StandardMusicInfo | None:
        """解读缓存记录: 关键字段缺失 / 封面模板无效都返回 None (交给下一步)"""
        if not isinstance(record, dict) or not record.get("title"):
            return None
        cover = record.get("cover") if isinstance(record.get("cover"), dict) else {}
        template = cover.get("cover")
        if cover.get("class") != COVER_CLASS or not isinstance(template, str) or not template:
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
        cover = self.music_cache.find_cover(music_hash, size)
        if cover is None:
            # 缓存信息在但封面文件加载失败 -> 返回失败结果, 交给联网 / 兜底
            logger.warning(f"本地缓存封面加载失败: {music_info.title} - {music_info.artist}")
            return None
        music_info.cover = cover
        music_info.has_cover = True
        return music_info

    def from_web(self, music_info: StandardMusicInfo, size: int,
                 props: SessionMediaProperties) -> StandardMusicInfo | None:
        """联网: 搜索 -> 下载最大封面 -> 写缓存 (之后各尺寸请求复用同一份)"""
        title, artist = music_info.title, music_info.artist
        try:
            song = search_music(clean_music_title(title), artist,
                                (music_info.album_artist or "").lstrip("《").rstrip("》"))
        except RuntimeError as e:
            logger.error(e)
            return None
        template = str(((song.get("trans_param") or {}) if isinstance(song, dict) else {}).get("union_cover") or "")
        if not template:
            logger.warning(f"搜索结果缺少封面模板 (union_cover): {artist} - {title}")
            return None
        try:
            resp = requests.get(cover_url(template, FULL_COVER_SIZE), headers=HEADERS, data=None)
            with Image.open(BytesIO(resp.content)) as image:
                image.load()
                cover = image.copy()
        except Exception as e:
            logger.warning(f"下载封面失败: {e.__class__.__name__}: {e}")
            return None
        if max(cover.size) > MAX_COVER_SIZE:
            cover.thumbnail((MAX_COVER_SIZE, MAX_COVER_SIZE))
        music_hash = music_hash_for(title, artist)
        slot = self.music_cache.save_cover(cover, music_hash, FULL_COVER_SIZE, title, artist)
        self.music_cache.put(music_hash, self.encode_record(music_info, {"cover": template}))
        # 按请求尺寸返回: 最大封面原样, 否则缩到请求尺寸(不放大)
        want = max(cover.size) if size == FULL_COVER_SIZE else min(size, max(cover.size))
        if max(cover.size) > want:
            cover.thumbnail((want, want))
        music_info.cover = cover
        music_info.has_cover = True
        logger.info(f"已获取高清封面: {title} - {artist} (请求 {size}, 缓存标记 {slot})")
        return music_info

    def from_raw_smtc(self, music_info: StandardMusicInfo, size: int,
                      props: SessionMediaProperties) -> StandardMusicInfo | None:
        """兜底: 源 SMTC 会话自带的低清封面 (不写缓存)"""
        cover = read_raw_cover(props)
        if cover is None:
            return None
        music_info.cover = cover
        music_info.has_cover = True
        return music_info
