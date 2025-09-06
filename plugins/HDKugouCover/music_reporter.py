import json
from dataclasses import dataclass, asdict
from os import makedirs
from os.path import isfile, abspath
from time import time, perf_counter


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
