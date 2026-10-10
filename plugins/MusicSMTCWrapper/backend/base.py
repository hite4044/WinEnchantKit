# -*- coding: utf-8 -*-
"""公共定义入口 (薄再导出层, 不定义任何类)。

契约层与各平台后端实现都在插件根目录, 并使用 ``_smtc_backend_`` 私有前缀:
WinEnchantKit 的插件加载器只把插件目录插到 ``sys.path[0]``, 而 plugins.HDKugouCover
已占用顶层名 ``backend``、仓库根 ``base.py`` 已占用顶层名 ``base``, 只有私有前缀才能在
任何插件加载顺序下唯一解析且不污染其它插件。

本文件只是给人 / 兼容用的再导出入口, 插件运行时不依赖它。
"""
from _smtc_backend_base import *  # noqa: F401,F403
from _smtc_backend_base import (  # noqa: F401
    CACHE_ROOT,
    DEFAULT_COVER_SIZE,
    FULL_COVER_SIZE,
    MAX_COVER_SIZE,
    ImageFormat,
    MusicBackend,
    MusicCache,
    SaveFormat,
    StandardMusicInfo,
    logger,
    make_music_hash,
    normalize_size,
    parse_cover_filename,
    plugin_logger,
    sanitize_filename,
    wait_result,
)

__all__ = [
    "CACHE_ROOT", "DEFAULT_COVER_SIZE", "FULL_COVER_SIZE", "MAX_COVER_SIZE", "ImageFormat",
    "MusicBackend", "MusicCache", "SaveFormat", "StandardMusicInfo", "logger",
    "make_music_hash", "normalize_size", "parse_cover_filename", "plugin_logger",
    "sanitize_filename", "wait_result",
]
