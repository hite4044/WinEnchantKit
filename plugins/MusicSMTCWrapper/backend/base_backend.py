# -*- coding: utf-8 -*-
"""兼容薄再导出层 (不定义任何类 / 数据类 / 基类)。

原 ``backend/base_backend.py`` 既放通用工具又放酷狗搜索, 重构后:
通用部分 -> 插件根 ``_smtc_backend_base.py``; 酷狗部分 -> 插件根 ``_smtc_backend_kugou.py``。
这里把两边重新导出, 保留旧名字, 便于人工排查与旧脚本引用。
"""
from _smtc_backend_base import (  # noqa: F401
    MusicBackend,
    MusicCache,
    StandardMusicInfo,
    logger,
    plugin_logger,
    sanitize_filename,
    wait_result,
)
from _smtc_backend_kugou import (  # noqa: F401
    HEADERS,
    OTHER_NAME_PATTERN,
    SEARCH_URL,
    KugouMusicBackend,
    clean_music_title,
    cover_url,
    extract_music_title,
    has_alert,
    music_hash_for,
    read_raw_cover,
    search_for_song_list,
    search_music,
)


def __getattr__(name):
    """PEP 562: 旧名字 (如 has_alert) 始终转发到真实实现模块"""
    import _smtc_backend_kugou as _kugou
    if hasattr(_kugou, name):
        return getattr(_kugou, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
