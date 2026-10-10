import json
from dataclasses import dataclass, asdict
from os import makedirs
from os.path import isfile, abspath
from time import time, perf_counter
from typing import Any


def tuple_fmt_time(seconds: float) -> tuple[int, int, int, int]:
    """转化时间戳至时间元组"""
    return int(seconds // 3600 // 24), int(seconds // 3600 % 24), int(seconds % 3600 // 60), int(seconds % 60)


def string_fmt_time(seconds: float) -> str:
    """格式化时间戳至字符串"""
    time_str = ""
    time_tuple = tuple_fmt_time(seconds)
    if time_tuple[0] > 0:
        time_str += f"{time_tuple[0]}d "
    if time_tuple[1] > 0:
        time_str += f"{time_tuple[1]}h "
    if time_tuple[2] > 0:
        time_str += f"{time_tuple[2]}m "
    if time_tuple[3] > 0:
        time_str += f"{time_tuple[3]}s"
    if time_str:
        return time_str
    return "无"


@dataclass
class Music:
    title: str
    artist: str
    album_title: str
    album_artist: str

    @property
    def id(self):
        return hash(self.title + self.artist + self.album_title + self.album_artist)

    def __hash__(self):
        return self.id

    def __eq__(self, other):
        if not isinstance(other, Music):
            return False
        return self.id == other.id


@dataclass
class MusicPoint:
    music: Music | None = None
    time_start: float = 0.0
    time_stop: float = 0.0
    time_offset: float = 0.0

    @property
    def time_last(self):
        return self.time_stop - self.time_start - self.time_offset


DATA_FILE = r"data\HDKugouCover\music_data.json"


class MusicReporter:
    def __init__(self):
        self.music_points: list[MusicPoint] = []
        self.pause_counter: float = -1
        self.current_point: MusicPoint | None = None
        self.non_save_cnt: int = 0

        self.load()

    def load(self):
        if not isfile(DATA_FILE):
            return
        with open(DATA_FILE) as f:
            data = json.load(f)
            self.music_points = []
            for kwargs in data.get("time_points"):
                kwargs["music"] = Music(**kwargs["music"])
                self.music_points.append(MusicPoint(**kwargs))

    def save(self):
        data = {
            "time_points": [asdict(point) for point in self.music_points]
        }
        data_content = json.dumps(data)
        makedirs(r"data\HDKugouCover", exist_ok=True)
        with open(DATA_FILE, "w") as f:
            f.write(data_content)

    def finish(self):
        if self.current_point:
            self.current_point.time_stop = time()
            self.music_points.append(self.current_point)
            self.current_point = None

    def count_song(self, title: str, artist: str, album_title: str, album_artist: str):
        self.non_save_cnt += 1
        if self.non_save_cnt > 4:
            self.save()
            self.non_save_cnt = 0

        if self.current_point:
            if self.pause_counter != -1:
                self.music_resume()
            self.current_point.time_stop = time()
            if time() - self.current_point.time_start > 5.0:
                self.music_points.append(self.current_point)

        music = Music(title, artist, album_title, album_artist)
        # print("新歌曲", music)
        self.current_point = MusicPoint(music)
        self.current_point.time_start = time()

    def music_pause(self):
        if not self.current_point:
            return
        # print("暂停")
        self.pause_counter = time()

    def music_resume(self):
        if not self.current_point or self.pause_counter == -1:
            return
        # print("继续")
        self.current_point.time_offset += time() - self.pause_counter
        self.pause_counter = -1

    def output_report(self):
        makedirs(r"data\HDKugouCover", exist_ok=True)
        content = ""
        music_list: dict[Music, list[int | float]] = {}
        for point in self.music_points:
            if point.music not in music_list:
                music_list[point.music] = [0, 0.0]
            music_list[point.music][0] += 1
            music_list[point.music][1] += point.time_last
        content += "歌名 - 作者 -> 次数, 总时间\n"
        content += "\n".join([
            f"{music.title} - {music.artist} -> {count}, {string_fmt_time(int(total_time))}" for
            music, (count, total_time) in music_list.items()
        ])
        with open(r"data\HDKugouCover\音乐报告.txt", "w", encoding="utf-8") as f:
            f.write(content)

        return abspath(r"data\HDKugouCover\音乐报告.txt")


class _SongIdentity:
    """源会话歌曲属性 -> 报告用的四元组文本标识 (空值防御)"""

    @staticmethod
    def of(info: Any) -> tuple[str, str, str, str]:
        return (
            str(getattr(info, "title", "") or ""),
            str(getattr(info, "artist", "") or ""),
            str(getattr(info, "album_title", "") or ""),
            str(getattr(info, "album_artist", "") or ""),
        )


class MusicReportManager:
    """音乐报告管理 (从 Plugin 剥离出来的那部分职责)。

    只负责「根据源会话的歌曲信息与播放状态决定何时记一笔 / 何时暂停继续 / 何时落盘」,
    以及报告生成入口; 不关心 SMTC 会话本身。

    用法 (由 Plugin 驱动):
        manager = MusicReportManager()
        manager.handle(info, status)        # 每次源会话信息刷新时调用
        manager.enabled = False             # 配置里关掉音乐报告时
        path = manager.generate_report()    # 「生成音乐报告」按钮
        manager.flush(); manager.save()     # 插件停止时
    """

    # 暂停 / 继续的状态写入节流间隔(秒), 与旧实现一致
    STATUS_INTERVAL = 0.5
    # 切歌时只把时长超过该值的上一首计入报告
    MIN_POINT_SECONDS = 5.0
    # 每累计多少笔落盘一次
    SAVE_EVERY = 4
    # 播放状态名 (不直接依赖 winsdk, 只比较状态的字符串名)
    STOPPED = "STOPPED"
    PAUSED = "PAUSED"
    PLAYING = "PLAYING"

    def __init__(self):
        self._reporter = MusicReporter()
        self.enabled = False
        # 源会话状态转换需要上一次状态 (只保留最近两次)
        self._last_status: list[str] = ["", ""]
        self._last_call = 0.0
        # 供运行时检查的累计计数
        self.counted_songs = 0

    # ------------------------------------------------------------------ 只读状态
    @property
    def current_point(self) -> MusicPoint | None:
        return self._reporter.current_point

    # ------------------------------------------------------------------ 主入口
    def handle(self, info: Any, status: Any, now: float | None = None):
        """源会话信息刷新时调用: 决定是否记一笔新歌 / 暂停 / 继续。

        ``info`` 为 SMTC 的 SessionMediaProperties; ``status`` 为 MediaPlaybackStatus
        (只比较它的 ``name``, 因此本模块不依赖 winsdk)。
        """
        if not self.enabled:
            return
        status_name = getattr(status, "name", str(status))
        title, artist, album_title, album_artist = _SongIdentity.of(info)
        timestamp = time() if now is None else now
        if (self.STOPPED in self._last_status
                and status_name == self.PLAYING
                and self._reporter.current_point
                and timestamp - self._reporter.current_point.time_start > 1.0):
            # 源平台自己「停了一下又播放」时, 补记一笔
            self._restart(title, artist, album_title, album_artist, timestamp)

        now_perf = perf_counter()
        if now_perf - self._last_call > self.STATUS_INTERVAL:
            if status_name == self.PAUSED:
                self._reporter.music_pause()
            elif status_name == self.PLAYING:
                self._reporter.music_resume()
        self._last_call = now_perf
        self._last_status.append(status_name)
        self._last_status.pop(0)

    def count_song(self, title: str, artist: str, album_title: str, album_artist: str):
        """显式记一笔歌曲 (原文 API 保留)"""
        self._restart(title, artist, album_title, album_artist, time())

    # ------------------------------------------------------------------ 内部
    def _restart(self, title: str, artist: str, album_title: str, album_artist: str, timestamp: float):
        self.close_current_point(timestamp)
        self._reporter.current_point = MusicPoint(Music(title, artist, album_title, album_artist))
        self._reporter.current_point.time_start = timestamp
        self.counted_songs += 1
        self._maybe_save()

    def close_current_point(self, timestamp: float | None = None):
        """结束当前记录点: 先结算暂停偏移, 时长足够才计入报告"""
        current = self._reporter.current_point
        if current is None:
            return
        if self._reporter.pause_counter != -1:
            self._reporter.music_resume()
        current.time_stop = time() if timestamp is None else timestamp
        if current.time_stop - current.time_start > self.MIN_POINT_SECONDS:
            self._reporter.music_points.append(current)
        self._reporter.current_point = None

    def _maybe_save(self):
        self._reporter.non_save_cnt += 1
        if self._reporter.non_save_cnt > self.SAVE_EVERY:
            self._reporter.save()
            self._reporter.non_save_cnt = 0

    # ------------------------------------------------------------------ 落盘 / 报告
    def flush(self):
        """结束进行中的记录点 (暂停 / 停止插件时调用)"""
        self._reporter.finish()

    def save(self):
        self._reporter.save()

    def generate_report(self) -> str:
        """生成音乐报告并返回文件路径"""
        self.flush()
        self.save()
        return self._reporter.output_report()
