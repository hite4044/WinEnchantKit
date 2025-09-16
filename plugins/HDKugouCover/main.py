import asyncio
import json
import os
import string
import sys
import winreg
from dataclasses import dataclass
from io import BytesIO
from os import makedirs
from os.path import isdir, isfile, join, abspath, expandvars
from queue import Queue
from threading import Event, Thread
from time import perf_counter, time

import pylnk3
import win32con as con
from PIL import Image, ImageCms
from PIL.PngImagePlugin import PngImageFile
from win32comext.shell.shell import ShellExecuteEx
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
from plugins.HDKugouCover.music_reporter import MusicReporter

name = "高清酷狗封面"


@dataclass
class MusicData:
    hash: str
    cover_url: str
    full_cover_url: str


class CoverCacheFmt(int):
    JPG = 0
    PNG = 1
    RAW_FORMAT = 2


class PluginConfig(ModuleConfigPlus):
    def __init__(self):
        super().__init__()
        self.tip: TipParam = TipParam("插件启用后如果仍然不显示高清封面, 请在快速设置面板(开WIFI的地方)")
        self.tip2: TipParam = TipParam("箭头按钮切换SMTC会话, 直到找到那个专辑名前多了一个空格的SMTC会话")
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
        self.refresh_info: ButtonParam = ButtonParam(desc="立即更新信息")
        self.clear_cache: ButtonParam = ButtonParam(desc="清除封面url缓存")
        self.enable_music_report: BoolParam | bool = BoolParam(False, "启用音乐报告")
        self.create_music_report: ButtonParam = ButtonParam(desc="生成音乐报告")
        self.install_kugou_lnk: ButtonParam = ButtonParam(
            desc="安装图标快捷方式 (需要管理员)",
            help_string="使得在SMTC页面出现 [🅺 Kugou] 而不是 [未知应用]\n"
                        r"文件位置在 [C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Kugou.lnk]"
        )


class Action(Enum):
    START = 0
    END = 1
    REFRESH_INFO = 2


class Plugin(BasePlugin):

    def __init__(self):
        self.config = PluginConfig()
        self.config.refresh_info.handler = lambda: self.on_source_update(force_update=True)
        self.config.install_kugou_lnk.handler = self.install_kugou_lnk
        self.config.clear_cache.handler = self.remove_cache
        self.config.create_music_report.handler = self.create_report
        self.config.load()

        self.action_queue: Queue[Action | MediaPlaybackStatus] = Queue()
        self.action_thread = Thread(target=self.action_thread_func, daemon=True)
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

        self.music_reporter = MusicReporter()
        self.last_reporter_call = 0
        self.last_reporter_status = [-1, -1]

        self.action_thread.start()

    def create_report(self):
        path = self.music_reporter.output_report()
        Thread(target=os.system, args=(path,), daemon=True).start()

    @staticmethod
    def install_kugou_lnk():
        program = r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs"
        temp_dir = expandvars("%TEMP%\WinEnchantKit")
        os.makedirs(temp_dir, exist_ok=True)
        temp_path = join(temp_dir, r"Kugou.lnk")
        target_path = join(program, "Kugou.lnk")
        if "pythonw.exe" in sys.orig_argv[0]:
            exec_name = "pythonw.exe"
        else:
            exec_name = "python.exe"
        base_executable_path = join(sys.base_prefix, exec_name)
        kugou_key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\kugou")
        kugou_path = join(winreg.QueryValueEx(kugou_key, "KuGou8")[0], "KuGou.exe")

        lnk = pylnk3.for_file(base_executable_path, icon_file=kugou_path, icon_index=0)
        try:
            lnk.save(target_path)
            wx.MessageBox("创建快捷方式成功！\n记得重启程序哦", "成功！ - ( •̀ ω •́ )✧", wx.OK | wx.ICON_INFORMATION)
            return
        except OSError:
            pass
        lnk.save(temp_path)
        lnk_install_bat = expandvars(r"%TEMP%\WinEnchantKit-Plugin-HDKugouCover-Kugou-Lnk-Install.bat")
        with open(lnk_install_bat, "w+") as f:
            f.write("\n".join([
                f'move "{temp_path}" "{target_path}"'
            ]))
        try:
            ShellExecuteEx(lpVerb="runas", lpFile=lnk_install_bat, nShow=con.SW_HIDE)
            wx.MessageBox("创建快捷方式成功！\n记得重启程序哦", "成功！ - ( •̀ ω •́ )✧", wx.OK | wx.ICON_INFORMATION)
            return
        except Exception as e:
            logger.error(f"{e.__class__.__name__}: {e} -> 运行快捷方式复制脚本失败")
            ret = wx.MessageBox(f"{e.__class__.__name__}: {e}\n"
                                "运行快捷方式复制脚本失败\n"
                                "要打开 lnk所在目录 和 目标文件夹 吗?",
                                f"失败啦~ - ＞︿＜",
                                wx.YES_NO | wx.ICON_ERROR)
        if ret == wx.YES:
            os.startfile(r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs")
            Thread(target=os.system, args=[f"explorer /select,{temp_path}"], daemon=True).start()
            wx.MessageBox("已打开两个文件夹！\n"
                          "请移动lnk至 [C:\\ProgramData\\Microsoft\\Windows\\Start Menu\\Programs]\n"
                          "创建快捷方式成功！(虽说是保存到别处\n"
                          "记得重启程序哦",
                          "成功！ - (*^▽^*)", wx.OK | wx.ICON_INFORMATION)

    def remove_cache(self):
        self.cover_cache.clear()
        try:
            file_list = os.listdir("cache/kugou_music_covers")
        except FileNotFoundError:
            wx.MessageBox("缓存文件夹不存在", "错误", wx.OK | wx.ICON_ERROR)
            return
        all_length = len(file_list)
        dialog = wx.ProgressDialog("正在删除缓存", "请稍候...", all_length, None, wx.CENTRE | wx.RESIZE_BORDER)
        for i, file in enumerate(file_list):
            file_path = join("cache/kugou_music_covers", file)
            try:
                os.remove(file_path)
            except OSError:
                logger.warning(f"删除文件失败: {file_path}")
            dialog.Update(i, f"({i + 1}/{all_length})\n正在删除缓存: {file}")
        dialog.Destroy()
        self.save_cache()

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

    def action_thread_func(self):
        while self.action_queue.empty():
            action = self.action_queue.get()
            if action == Action.START:
                self.start_raw()
            elif action == Action.END:
                self.stop_raw()
            elif action == Action.REFRESH_INFO:
                self.on_source_update(force_update=True)

    def start(self):
        self.action_queue.put(Action.START)

    def start_raw(self):
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
        # self.on_session_changed()

    def update_config(self, old_config: dict[str, Any], new_config: dict[str, Any]):
        self.config.load_values(new_config)
        if self.enable:
            if self.config.allways_playing:
                self.action_queue.put(MediaPlaybackStatus.PLAYING)
            self.action_queue.put(Action.REFRESH_INFO)

            if old_config["enable_music_report"] != new_config[
                "enable_music_report"] and not self.config.enable_music_report:
                self.music_reporter.finish()

    def stop(self):
        self.music_reporter.finish()
        self.music_reporter.save()

        self.action_queue.put(Action.END)

    def stop_raw(self):
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
        if not self.smtc:
            wx.MessageBox("请先启用插件", "WinEnchantKit - 高清酷狗封面", wx.OK | wx.ICON_WARNING)
            return
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

        if self.config.enable_music_report:
            if MediaPlaybackStatus.STOPPED in self.last_reporter_status and status == MediaPlaybackStatus.PLAYING:
                if self.music_reporter.current_point:
                    if time() - self.music_reporter.current_point.time_start > 1.0:
                        self.music_reporter.count_song(info.title, info.artist, info.album_title, info.album_artist)
            if perf_counter() - self.last_reporter_call > 0.5:
                if status == MediaPlaybackStatus.PAUSED:
                    self.music_reporter.music_pause()
                elif status == MediaPlaybackStatus.PLAYING:
                    self.music_reporter.music_resume()
        self.last_reporter_call = perf_counter()
        self.last_reporter_status.append(status)
        self.last_reporter_status.pop(0)

        if song_id == self.last_song and not force_update:
            # logger.debug(f"歌曲信息更新, 歌名相同, 不更新, {str([status])[1:-1]}, {song_id}")
            return
        if self.config.enable_music_report and not force_update:
            self.music_reporter.count_song(info.title, info.artist, info.album_title, info.album_artist)
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
                    icc_profile = ImageCms.getOpenProfile('plugins/HDKugouCover/sRGB_v4_ICC_preference.icc')
                    if self.config.cover_cache_format == CoverCacheFmt.JPG:
                        image.save(cover_cache_fp + ".jpg", "JPEG", quality=self.config.cover_cache_quality,
                                   icc_profile=icc_profile.tobytes())
                    elif self.config.cover_cache_format == CoverCacheFmt.PNG:
                        image = image.convert("RGBA")
                        image.save(cover_cache_fp + ".png", "PNG")
                    else:
                        fmt = "PNG" if isinstance(image, PngImageFile) else "JPEG"
                        if fmt == "JPEG":
                            image.save(cover_cache_fp + ".jpg", "JPEG", icc_profile=icc_profile.tobytes())
                        else:
                            image = image.convert("RGBA")
                            image.save(cover_cache_fp + ".png", "PNG")

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
    plugin.start_raw()
    input()
    plugin.stop_raw()
    input()
    plugin.start_raw()
    input()
