import json
from typing import Any

import requests

from _smtc_backend_base import plugin_logger

logger = plugin_logger

SEARCH_URL = "https://music.163.com/api/search/get/web?csrf_token=hlpretag=&hlposttag=&s={keyword}&type=1&offset=0&total=true&limit=10"
GET_SONG_URL = "https://music.163.com/api/song/detail?csrf_token=hlpretag=&hlposttag=&id={id}&ids=[{id}]"


def search_music_id(title: str, artist: str, album: str) -> str:
    # 获取音乐列表
    try:
        keyword = f"{title} {artist}"
        logger.debug(f"歌曲搜索关键字: \"{keyword}\"")
        resp = requests.get(SEARCH_URL.format(keyword=keyword))

        search_result = json.loads(resp.text)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"歌曲搜索失败-1: {e}")
    except json.JSONDecodeError:
        raise RuntimeError(f"歌曲搜索失败-1: 网易云返回数据格式错误")

    # 匹配音乐
    try:
        matched_songs = []
        for song in search_result["result"]["songs"]:  # 匹配歌名和歌手
            if all([
                title in song["name"],
                all([(result_artist["name"] == artist) for result_artist in song["artists"]])
            ]):
                matched_songs.append(song)
        for song in matched_songs:
            if album in song["album"]["name"]:
                return song["id"]
    except KeyError:
        raise RuntimeError(f"歌曲搜索失败-1: 在响应结果中找不到预期的字段")
    raise RuntimeError(f"搜索失败-1: 找不到对应歌曲 {artist} - {title} 《{album}》")


def search_music(title: str, artist: str, album: str) -> dict[str, Any]:
    """从网易云上搜索音乐信息"""
    music_id = search_music_id(title, artist, album)
    try:
        resp = requests.get(GET_SONG_URL.format(id=music_id))
        music_info = json.loads(resp.text)
        return music_info["songs"][0]
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"歌曲信息获取失败-2: {e}")
    except json.JSONDecodeError:
        raise RuntimeError(f"歌曲信息获取失败-2: 网易云返回数据格式错误")


if __name__ == '__main__':
    a = search_music("STOP", "Labirhin", "MAU MAKAN APA")
    print(a)
    json.dump(a, open("netease_search_return_template.json", "w", encoding="utf-8"), ensure_ascii=False, indent=4)

temp = {
    'album': {
        'publishTime': 1764950400000,
        'size': 1,
        'artist': {
            'img1v1Url': 'https://p1.music.126.net/6y-UleORITEDbvrOLV0Q8A==/5639395138885805.jpg',
            'musicSize': 0,
            'albumSize': 0,
            'img1v1': 0,
            'name': '',
            'alias': [],
            'id': 0,
            'picId': 0
        },
        'copyrightId': 743010,
        'name': 'STOP',
        'id': 351433123,
        'picId': 109951172324784061,
        'mark': 0,
        'status': 1
    },
    'fee': 8,
    'duration': 370546,
    'rtype': 0,
    'ftype': 0,
    'artists': [{
        'img1v1Url': 'https://p1.music.126.net/6y-UleORITEDbvrOLV0Q8A==/5639395138885805.jpg',
        'musicSize': 0,
        'albumSize': 0,
        'img1v1': 0,
        'name': 'Labirhin',
        'alias': [],
        'id': 58722307,
        'picId': 0
    }],
    'copyrightId': 743010,
    'mvid': 0,
    'name': 'STOP',
    'alias': [],
    'id': 3320556689,
    'mark': 270464,
    'status': 0
}
