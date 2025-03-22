import logging
from time import sleep
from typing import Any

import requests
import re
from winsdk.windows.foundation import IAsyncOperation, AsyncStatus
from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as SessionManager, \
    GlobalSystemMediaTransportControlsSessionMediaProperties as SessionMediaProperties, \
    GlobalSystemMediaTransportControlsSession as Session
from winsdk.windows.media.playback import MediaPlayer

logger = logging.getLogger("WinEnchantKitLogger_hd_kugou_cover")
SAVED_HEADERS = {
    'Connection': 'keep-alive',
    'Cache-Control': 'max-age=0',
    'Prefer': 'safe',
    'Upgrade-Insecure-Requests': '1',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 Edg/132.0.0.0',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    'Accept-Encoding': 'gzip, deflate',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6'
}
HEADERS = {'User-Agent': '[HDKugouCover(Github)(WinEnchantKit)] Music Search'}
SEARCH_URL = "http://mobilecdn.kugou.com/api/v3/search/song?format=json&keyword={keyword}&page=1"
OTHER_NAME_PATTERN = re.compile(r"\([^()]+\)")


def wait_result(opt: IAsyncOperation):
    while opt.status != AsyncStatus.COMPLETED:
        sleep(0.1)
    return opt.get_results()


def extract_music_title(title: str) -> tuple[str, str | None]:
    if match := OTHER_NAME_PATTERN.search(title):
        title = title[:match.start()]
        other_name = match.group()
    else:
        other_name = None
    return title.strip(), other_name


def get_song_list(title: str, artist: str) -> list[dict[str, Any]]:
    logger.debug(f"歌曲搜索关键字: \"{title} {artist}\"")
    resp = requests.get(SEARCH_URL.format(keyword=f"{title} {artist}"), headers=HEADERS, data=None)
    if resp.status_code != 200:
        raise RuntimeError(f"搜索失败: 服务器返回HTTP错误码 [{resp.status_code}]")
    resp_json = resp.json()
    return resp_json["data"]["info"]


def search_music(title: str, artist: str, album: str) -> dict[str, Any]:
    """通过给定的歌曲名、歌手、专辑来搜索歌曲"""
    clean_title, other_name = extract_music_title(title)
    song_list = get_song_list(clean_title, artist)
    for song in song_list:
        logger.debug(f"匹配歌曲 {song['songname']} - {song['singername']}")
        if all([
            artist in song["singername"],
            clean_title == song["songname_original"],
            other_name is None or other_name in song["songname"]
        ]):
            break
    else:
        raise RuntimeError(f"搜索失败: 找不到对应歌曲 {artist} - {title} 《{album}》")
    if song["album_name"] == album or not song.get("group") or not album:
        return song

    song_list = song["group"]
    for g_song in song_list:
        logger.debug(f"匹配专辑 {g_song['album_name']}")
        if g_song["album_name"] == album:
            return g_song
    else:
        logger.warning(f"{artist} - {title} 《{album}》 无法找到对应专辑, 使用专辑列表中第一个歌曲")
        return song


def transform_to_url(song_info: dict[str, Any], full_size: bool = False, size: int = 480):
    """把歌曲信息变成封面url"""
    trans_param = song_info["trans_param"]
    cover_url: str = trans_param["union_cover"]
    if full_size:
        cover_url = cover_url.replace("/{size}", "")
    else:
        cover_url = cover_url.format(size=str(size))
    return cover_url


def create_smtc():
    player = MediaPlayer()
    player.command_manager.is_enabled = False
    smtc = player.system_media_transport_controls
    smtc.is_enabled = True
    smtc.is_play_enabled = True
    smtc.is_pause_enabled = True
    smtc.is_next_enabled = True
    smtc.is_previous_enabled = True
    return smtc, player


def get_kugou_session() -> Session:
    session_manager: SessionManager = wait_result(SessionManager.request_async())

    for session in session_manager.get_sessions():
        if session.source_app_user_model_id == "kugou":
            return session
    raise RuntimeError('找不到酷狗SMTC会话')


def get_kugou_info(session: Session) -> SessionMediaProperties:
    return wait_result(session.try_get_media_properties_async())

if __name__ == "__main__":
    from lib.log import logger
    t_name = "MAGENTA POTION (Extended Mix)"
    t_artist = "EmoCosine"
    t_album = "Love Kills U"
    music = search_music(t_name, t_artist, t_album)