import asyncio
import json
import os
import string
from dataclasses import dataclass
from io import BytesIO
from os import makedirs
from os.path import isdir, isfile, join, abspath
from threading import Event, Thread

import wx
from PIL import Image
from winsdk.windows.foundation import Uri
from winsdk.windows.media import SystemMediaTransportControls as SMTControls, \
    MediaPlaybackType, \
    SystemMediaTransportControlsButtonPressedEventArgs as SMTCButtonPressedEventArgs, \
    SystemMediaTransportControlsButton as SMTCButton, \
    MediaPlaybackStatus
from winsdk.windows.storage import StorageFile, FileAccessMode
from winsdk.windows.storage.streams import RandomAccessStreamReference

from backend import *
from base import *

name = "高清酷狗封面"


@dataclass
class MusicData:
    hash: str
    cover_url: str
    full_cover_url: str


class CoverCacheFmt(Enum):
    JPG = 0
    PNG = 1
    RAW_FORMAT = 2


class PluginConfig(ModuleConfigPlus):
    def __init__(self):
        super().__init__()
        self.cover_size: ChoiceParam | str = ChoiceParam("480",
                                                         ["480", "400", "240", "150", "120", "100", "64"],
                                                         "封面尺寸")
        self.use_max_size: BoolParam | bool = BoolParam(False, "使用最大封面尺寸")
        self.allways_playing: BoolParam | bool = BoolParam(False, "永不暂停SMTC (暂停时总是切换会话时用)")
        self.exchange_title2album: BoolParam | bool = BoolParam(False, "专辑名与作者名互换")
        self.default_title: StringParam | str = StringParam("Title", "默认标题")
        self.default_artist: StringParam | str = StringParam("Artist", "默认艺术家")
        self.default_album_title: StringParam | str = StringParam("Album Title", "默认专辑名")
        self.default_album_artist: StringParam | str = StringParam("Album Artist", "默认专辑艺术家")
        self.cover_cache_quality: IntParam | int = IntParam(90, "封面缓存质量 (仅jpg) (1-100)")
        self.cover_cache_format: ChoiceParamPlus | CoverCacheFmt = ChoiceParamPlus(CoverCacheFmt.JPG,
                                                                                   {CoverCacheFmt.JPG: "JPG",
                                                                                    CoverCacheFmt.PNG: "PNG",
                                                                                    CoverCacheFmt.RAW_FORMAT: "原格式"
                                                                                    }, "封面缓存格式")
        self.refresh_info: ButtonParam | None = ButtonParam(desc="立即更新信息")
        self.clear_cache: ButtonParam | None = ButtonParam(desc="清除封面url缓存")


class Plugin(BasePlugin):

    def __init__(self):
        self.config = PluginConfig()
        self.config.refresh_info.handler = lambda: self.on_source_update(force_update=True)
        self.config.clear_cache.handler = self.remove_cache
        self.config.load()

        self.player: MediaPlayer | None = None
        self.smtc: SMTControls | None = None
        self.kugou_session: Session | None = None
        self.sessions: SessionManager = wait_result(SessionManager.request_async())
        self.is_fake_playing = False
        self.last_song = None
        self.stop_flag = Event()
        self.cover_cache: dict[str, tuple[str, str, str]] = {}

        self.sessions_changed_token = None
        self.source_changed_token = None
        self.button_pressed_token = None
        self.has_reg_event = False

    def remove_cache(self):
        self.cover_cache.clear()
        file_list = os.listdir("cache/kugou_music_covers")
        all_length = len(file_list)
        dialog = wx.ProgressDialog("正在删除缓存", "请稍候...", 100, None, wx.CENTRE | wx.RESIZE_BORDER)
        for i, file in enumerate(file_list):
            file_path = join("cache/kugou_music_covers", file)
            try:
                os.remove(file_path)
            except OSError:
                logger.warning(f"删除文件失败: {file_path}")
            dialog.Update(i, f"({i + 1}/{all_length})\n正在删除缓存: {file}")
        dialog.Destroy()
        self.save_cache()

    def restart_plugin(self):
        pass

    def load_cache(self):
        try:
            fp = "cache/kugou_cover_cache.json"
            if not isdir("cache"):
                os.mkdir("cache")
            if isfile(fp):
                with open(fp, "r", encoding="utf-8") as f:
                    self.cover_cache = json.load(f)
        except OSError:
            logger.warning(f"读取缓存失败")

    def save_cache(self):
        try:
            fp = "cache/kugou_cover_cache.json"
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
        self.default()
        logger.info(f"注册事件")
        self.button_pressed_token = self.smtc.add_button_pressed(self.on_button_press)
        if not self.has_reg_event:
            self.sessions_changed_token = self.sessions.add_sessions_changed(self.on_session_changed)
        self.on_session_changed()

    def update_config(self, _, new_config: dict[str, Any]):
        self.config.load_values(new_config)
        if self.config.allways_playing:
            self.smtc.playback_status = MediaPlaybackStatus.PLAYING
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
            self.default()
        try:
            self.kugou_session = get_kugou_session()
            logger.info(f"找到 Kugou SMTC会话")
            if self.config.allways_playing:
                self.smtc.playback_status = MediaPlaybackStatus.PLAYING
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

    def default(self):
        self.update_smtc_info(self.config.default_title, self.config.default_artist,
                              self.config.default_album_title, self.config.default_album_artist, None)

    def on_source_update(self, *_, force_update: bool = False):
        if not self.check_source_valid():
            if force_update:
                self.default()
            return
        info = get_kugou_info(self.kugou_session)
        song_id = info.title + info.artist + info.album_title + info.album_artist

        status: MediaPlaybackStatus = getattr(MediaPlaybackStatus,
                                              self.kugou_session.get_playback_info().playback_status.name)
        if status != self.smtc.playback_status and not self.config.allways_playing:
            self.smtc.playback_status = status

        if song_id == self.last_song and not force_update:
            return
        self.last_song = song_id
        self.update_info(info)

    def update_info(self, info: SessionMediaProperties):
        logger.info(f"更新歌曲信息: {info.title} - {info.artist}")
        music = self.load_cover(info, int(self.config.cover_size))
        if music is None:
            logger.warning(f"搜索不到歌曲封面, 使用酷狗原封面")
            stream = wait_result(info.thumbnail.open_read_async())
            thumbnail = RandomAccessStreamReference.create_from_stream(stream)
        else:
            cover_cache_fp = join("cache/kugou_music_covers",
                                  f"{music.hash}_full" if self.config.use_max_size else \
                                      f"{music.hash}_{self.config.cover_size}")
            cache_paths = (cover_cache_fp + ".jpg", cover_cache_fp + ".png")
            if not isfile(cache_paths[0]) and not isfile(cache_paths[1]):
                makedirs("cache/kugou_music_covers", exist_ok=True)

                def cover_save_thread():
                    resp = requests.get(music.full_cover_url if self.config.use_max_size else music.cover_url,
                                        headers=HEADERS, data=None)
                    content = resp.content
                    image = Image.open(BytesIO(content))
                    if self.config.cover_cache_format == CoverCacheFmt.JPG:
                        image.save(cover_cache_fp + ".jpg", "JPEG", quality=self.config.cover_cache_quality)
                    elif self.config.cover_cache_format == CoverCacheFmt.PNG:
                        image.save(cover_cache_fp + ".png", "PNG")
                    else:
                        fmt = "JPEG" if content.startswith(b"\xFF\xD8\xFF") else "PNG"
                        image.save(cover_cache_fp + (".jpg" if fmt == "JPEG" else ".png"), fmt)

                Thread(target=cover_save_thread).start()
                uri = music.full_cover_url if self.config.use_max_size else music.cover_url
                thumbnail = RandomAccessStreamReference.create_from_uri(Uri(uri))
            else:
                if isfile(cache_paths[0]):
                    cover_cache_fp += ".jpg"
                elif isfile(cache_paths[1]):
                    cover_cache_fp += ".png"
                cover_cache_fp = abspath(cover_cache_fp)

                async def load_cover_by_fucking_async():
                    nonlocal stream
                    file = await StorageFile.get_file_from_path_async(cover_cache_fp)
                    stream = await file.open_async(FileAccessMode.READ)
                    stream_ref = RandomAccessStreamReference.create_from_stream(stream)
                    return stream_ref

                thumbnail = asyncio.run(load_cover_by_fucking_async())

        if self.config.exchange_title2album:
            self.update_smtc_info(info.title, info.album_artist, info.album_title, info.artist, thumbnail)
        else:
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
        song_id = f"{info.title} - {info.artist} - {info.album_artist} - {size}"
        if song_id in self.cover_cache:
            song_hash, cover_url, cover_url_full = self.cover_cache[song_id]
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
                song_hash = music_info["hash"]
                self.cover_cache[song_id] = (song_hash, cover_url, cover_url_full)
            except RuntimeError as e:
                logger.error(e)
                return None
        return MusicData(song_hash, cover_url, cover_url_full)

    def on_button_press(self, _, args: SMTCButtonPressedEventArgs):
        if not self.check_source_valid():
            return
        logger.info(f"用户按下按钮: {args.button.name}")
        if args.button == SMTCButton.PREVIOUS:
            wait_result(self.kugou_session.try_skip_previous_async())
        elif args.button == SMTCButton.NEXT:
            wait_result(self.kugou_session.try_skip_next_async())
        elif args.button == SMTCButton.PLAY:
            if not self.config.allways_playing:
                self.smtc.playback_status = MediaPlaybackStatus.PLAYING
            self.kugou_session.try_play_async()
        elif args.button == SMTCButton.PAUSE and self.is_fake_playing:
            self.kugou_session.try_play_async()
            self.is_fake_playing = False
        elif args.button == SMTCButton.PAUSE:
            if not self.config.allways_playing:
                self.smtc.playback_status = MediaPlaybackStatus.PAUSED
            self.kugou_session.try_pause_async()
            if self.config.allways_playing:
                self.is_fake_playing = True
                return


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
