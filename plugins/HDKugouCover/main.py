import json
import os
import string
from os.path import isdir, isfile
from threading import Event

from winsdk.windows.foundation import Uri
from winsdk.windows.media import SystemMediaTransportControls as SMTControls, \
    MediaPlaybackType, \
    SystemMediaTransportControlsButtonPressedEventArgs as SMTCButtonPressedEventArgs, \
    SystemMediaTransportControlsButton as SMTCButton, \
    MediaPlaybackStatus
from winsdk.windows.storage.streams import RandomAccessStreamReference

from backend import *
from base import *

name = "高清酷狗封面"


class Plugin(BasePlugin):

    def __init__(self):
        self.config = ModuleConfig({
            "tip": TipParam("插件启动后请把SMTC会话切换为本程序创建的会话"),
            "tip2": TipParam("(特征: 第二行文字前有空格)"),
            "cover_size": ChoiceParam("480", ["480", "400", "240", "150", "120", "100", "64"], "封面尺寸"),
            "use_max_size": BoolParam(False, "使用最大封面尺寸"),
            "refresh_info": ButtonParam(lambda _: self.on_source_update(force_update=True), "立即更新信息"),
            "clear_cache": ButtonParam(lambda _: self.remove_cache(), "清除封面url缓存")
        })
        self.player: MediaPlayer | None = None
        self.smtc: SMTControls | None = None
        self.kugou_session: Session | None = None
        self.sessions: SessionManager = wait_result(SessionManager.request_async())
        self.last_song = None
        self.stop_flag = Event()
        self.cover_cache: dict[str, (str, str)] = {}

        self.sessions_changed_token = None
        self.source_changed_token = None
        self.button_pressed_token = None
        self.has_reg_event = False

    def remove_cache(self):
        self.cover_cache.clear()
        self.save_cache()

    def restart_plugin(self):
        pass

    def load_cache(self):
        try:
            fp = "cache/kugou_cover_url_cache.json"
            if not isdir("cache"):
                os.mkdir("cache")
            if isfile(fp):
                with open(fp, "r", encoding="utf-8") as f:
                    self.cover_cache = json.load(f)
        except OSError:
            logger.warning(f"读取缓存失败")

    def save_cache(self):
        try:
            fp = "cache/kugou_cover_url_cache.json"
            if not isdir("cache"):
                os.mkdir("cache")
            with open(fp, "w+", encoding="utf-8") as f:
                f.write(json.dumps(self.cover_cache, ensure_ascii=False, indent=4))
        except OSError:
            logger.warning(f"保存缓存失败")

    def start(self):
        logger.info(f"加载封面缓存")
        self.load_cache()
        logger.info(f"创建SMTC对象")
        self.smtc, self.player = create_smtc()
        self.smtc.playback_status = MediaPlaybackStatus.STOPPED
        self.update_smtc_info("Title", "Artist", "Album Title", "Album Artist", None)
        logger.info(f"注册事件")
        self.button_pressed_token = self.smtc.add_button_pressed(self.on_button_press)
        if not self.has_reg_event:
            self.sessions_changed_token = self.sessions.add_sessions_changed(self.on_session_changed)
        self.on_session_changed()

    def update_config(self, _, new_config: dict[str, Any]):
        self.config.load_values(new_config)
        self.on_source_update(force_update=True)

    def stop(self):
        self.save_cache()
        logger.info(f"移除事件")
        if self.kugou_session:
            self.kugou_session.remove_playback_info_changed(self.source_changed_token)
            self.sessions.remove_sessions_changed(self.sessions_changed_token)
            self.has_reg_event = False
        self.sessions_changed_token = None
        self.button_pressed_token = None
        logger.info(f"删除SMTC对象")
        self.player = self.smtc = self.kugou_session = self.last_song = None

    def on_session_changed(self, *_):
        if self.check_source_valid():
            return
        if self.kugou_session is not None:
            logger.info("Kugou SMTC会话已失效")
            self.source_changed_token = self.kugou_session = self.last_song = None
            self.smtc.playback_status = MediaPlaybackStatus.STOPPED
            self.update_smtc_info("Title", "Artist", "Album Title", "Album Artist", None)
        try:
            self.kugou_session = get_kugou_session()
            logger.info(f"找到 Kugou SMTC会话")
            self.source_changed_token = self.kugou_session.add_playback_info_changed(self.on_source_update)
            self.on_source_update()
        except RuntimeError:
            pass

    def check_source_valid(self):
        if self.kugou_session is None:
            return False
        info = get_kugou_info(self.kugou_session)
        song_id = info.title + info.artist + info.album_title
        return song_id != ""

    def on_source_update(self, *_, force_update: bool = False):
        if not self.check_source_valid():
            return
        info = get_kugou_info(self.kugou_session)
        song_id = info.title + info.artist + info.album_title + info.album_artist

        self.smtc.playback_status = getattr(MediaPlaybackStatus,
                                            self.kugou_session.get_playback_info().playback_status.name)

        if song_id == self.last_song and not force_update:
            return
        self.last_song = song_id
        self.update_info(info)

    def update_info(self, info: SessionMediaProperties):
        logger.info(f"更新歌曲信息: {info.title} - {info.artist}")
        cover_url, cover_url_full = self.load_cover(info, self.config["cover_size"])

        if cover_url is None:
            logger.warning(f"搜索不到歌曲封面, 使用酷狗原封面")
            stream = wait_result(info.thumbnail.open_read_async())
            thumbnail = RandomAccessStreamReference.create_from_stream(stream)
        else:
            thumbnail = RandomAccessStreamReference.create_from_uri(
                Uri(cover_url_full if self.config["use_max_size"] else cover_url)
            )
        self.update_smtc_info(info.title, info.artist, info.album_title, info.album_artist, thumbnail)

    def update_smtc_info(self, title: str, artist: str, album_title: str, album_artist: str,
                         thumbnail: RandomAccessStreamReference = None):
        updater = self.smtc.display_updater
        updater.app_media_id = "hd_cover_kugou"
        updater.type = MediaPlaybackType.MUSIC
        updater.music_properties.title = title
        updater.music_properties.artist = artist
        updater.music_properties.album_title = album_title
        updater.music_properties.album_artist = " " + album_artist
        if thumbnail:
            updater.thumbnail = thumbnail
        updater.update()

    def load_cover(self, info: SessionMediaProperties, size: int = 480):
        song_id = info.title + info.artist + info.album_artist + str(size)
        if song_id in self.cover_cache:
            cover_url, cover_url_full = self.cover_cache[song_id]
        else:
            try:
                music_name = info.title
                if "(" in music_name:
                    trans_name = music_name[music_name.rindex("("):music_name.index(")") + 1]
                    if len(set(trans_name) - set(string.ascii_letters)) > 0:
                        music_name = music_name[:music_name.index("(")].strip()
                music_info = search_music(music_name, info.artist, info.album_artist.lstrip("《").rstrip("》"))
                cover_url = transform_to_url(music_info, False, size)
                cover_url_full = transform_to_url(music_info, True)
                self.cover_cache[song_id] = (cover_url, cover_url_full)
            except RuntimeError:
                cover_url = cover_url_full = None
        return cover_url, cover_url_full

    def on_button_press(self, _, args: SMTCButtonPressedEventArgs):
        if not self.check_source_valid():
            return
        logger.info(f"用户按下按钮: {args.button.name}")
        if args.button == SMTCButton.PLAY:
            self.smtc.playback_status = MediaPlaybackStatus.PLAYING
            wait_result(self.kugou_session.try_play_async())
        elif args.button == SMTCButton.PAUSE:
            self.smtc.playback_status = MediaPlaybackStatus.PAUSED
            wait_result(self.kugou_session.try_pause_async())
        elif args.button == SMTCButton.PREVIOUS:
            wait_result(self.kugou_session.try_skip_previous_async())
        elif args.button == SMTCButton.NEXT:
            wait_result(self.kugou_session.try_skip_next_async())


if __name__ == "__main__":
    logger.setLevel(logging.DEBUG)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    logger.addHandler(console_handler)
    plugin = Plugin()
    plugin.start()
    input()
    plugin.stop()
    input()
    plugin.start()
    input()
