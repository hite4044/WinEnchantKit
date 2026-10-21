# -*- coding: utf-8 -*-
"""平台后端包。

导入时**只能**使用相对导入（例如 ``from .base_backend import BaseMusicBackend``）。
不要用 ``import platform`` 之类的顶层绝对导入：插件目录在运行期可能位于 ``sys.path`` 中，
本包名会与 Python 标准库 ``platform`` 冲突。

当前后端: kugou.py / netease.py / qqmusic.py; 新增平台后端时继承 BaseMusicBackend
并只实现 ``search()`` / ``resolve_cover_url()``, 然后在 main.py 的 ``_register_backends`` 注册。
"""
