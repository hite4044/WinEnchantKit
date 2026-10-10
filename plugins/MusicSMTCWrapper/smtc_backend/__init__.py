# -*- coding: utf-8 -*-
"""音乐平台后端包 (MusicSMTCWrapper)。

* :mod:`smtc_backend.base`    —— 平台无关契约: ``StandardMusicInfo`` / ``MusicCache`` /
  ``MusicBackend`` (模板方法) / 通用工具
* :mod:`smtc_backend.kugou`   —— 酷狗后端
* :mod:`smtc_backend.netease` —— 网易云后端

包目录会被插件入口 ``main.py`` 加入 ``sys.path`` (插件加载器只加插件根目录),
因此这里保持普通包结构, 各模块之间用相对导入。
"""
