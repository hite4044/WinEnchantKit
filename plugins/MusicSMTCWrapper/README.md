# MusicSMTCWrapper

WinEnchantKit 插件：监听音乐平台的系统 SMTC 会话，从音乐平台搜索歌曲信息与高清封面，
写入插件自己托管的 SMTC 会话，让系统媒体面板 / 动态壁纸等拿到高清封面与更完整的歌曲信息。

## 功能

* **源 SMTC 监听**：自动发现并绑定 酷狗 / MoeKoeMusic / 网易云 / QQ音乐 的官方 SMTC 会话，
  自动处理会话切换、失效（标题与作者都为空）与重新绑定。
* **多平台搜索**：酷狗、网易云、QQ音乐三个搜索后端；可用「自动决定 / 优先某平台」策略，
  并允许跨平台兜底。QQ音乐使用腾讯官方匿名接口（无需登录、无第三方服务）。
* **封面获取与缓存**：按请求尺寸下载，本地按平台分目录缓存；同一首歌后续不再联网。
  支持 JPEG / PNG / 原格式、质量调整、最大封面，以及「立即更新信息」强制联网刷新。
* **封面相似度匹配**：搜索不到对应专辑时，逐个下载搜索结果封面与源 SMTC 150x150 缩略图做相似度比对，
  选出最相似封面（可在配置里关闭）。
* **字段指向**：SMTC 的 标题 / 作者 / 专辑标题 / 专辑作者 可分别映射到歌曲信息中的任意字段。
* **进度处理**：平台提供进度时镜像到托管会话；未提供且开启「自行托管进度」时，用歌曲时长 +
  播放状态 + 计时器自动推进。
* **不暂停 SMTC**：托管会话保持播放状态，避免暂停时 Windows 自动切换媒体会话。
* **按钮转发**：系统媒体面板上的 播放 / 暂停 / 上一首 / 下一首 会转发给真实音乐软件。
* **快捷方式**：创建 `SMTCWrapper.lnk`（WinEnchantKit 图标），让 SMTC 页面显示 SMTCWrapper。

## 目录结构

```
MusicSMTCWrapper/
├── main.py                 插件入口：配置 + 生命周期 + 信息编排 + 按钮
├── music_base.py           共用静态歌曲信息 MusicInfo
├── smtc_source.py          源平台官方 SMTC 会话管理（单线程 worker + 回调分发线程）
├── smtc_host.py            插件托管 SMTC 会话（单线程 worker + 自托管进度）
├── platform/
│   ├── base_backend.py     BaseMusicBackend：缓存 / 封面 / 相似度 / 模板流程
│   ├── kugou.py            酷狗搜索 + 封面地址
│   ├── netease.py          网易云搜索 + 详情 + 封面地址
│   └── qqmusic.py          QQ音乐搜索 + 封面地址（腾讯官方匿名接口）
├── plugin.json             插件元信息
├── ARCHITECTURE.md         模块接口契约与验收记录
└── README.md               本文件
```

## 缓存

* 位置：进程工作目录下 `cache/kugou/`、`cache/netease/`、`cache/qq/`
* 内容：`music_info.json`（歌曲信息记录）+ `covers/`（封面文件，文件名含歌曲指纹与尺寸）
* 「清除缓存」按钮会清空三个平台的歌曲信息与封面
* 与旧版插件共用同名目录但缓存键不兼容：升级后旧缓存不会复用（首次会重新联网），
  **不要同时运行旧版插件与本插件**（会互相覆盖信息文件、清缓存也会互删）

## 注意

* 安装图标快捷方式需要管理员权限（写入 `C:\ProgramData\Microsoft\Windows\Start Menu\Programs`）；
  也可到 Wallpaper Engine 设置 → 常规 → 媒体禁用列表 里禁用官方 SMTC。
* QQ音乐后端只使用腾讯官方接口（搜索 `c.y.qq.com` / 备用 `u.y.qq.com`，封面 `y.gtimg.cn`），
  匿名可用、不需要登录或 cookie、不调用第三方 API、不依赖本地服务。这些是未公开文档的
  网页/客户端内部接口，可能限流或随客户端升级变化；失败时会自动回退其它平台或源 SMTC 封面。
  QQ 封面 CDN 只提供 90/120/150/180/300/500/800/1200 预设尺寸，配置尺寸会向上取预设，
  「最大封面」使用原图。
* 源 SMTC 平台的「自动」模式会优先保持上次绑定的平台类型，只有该类型会话失效时才切换。

## 开发约定

* 插件由加载器以 `plugins.MusicSMTCWrapper.main` 导入，包内一律使用**相对导入**
  （`from .platform import kugou`）。插件目录里的 `platform/` 与标准库同名，`main.py` 启动时会
  把插件目录从 `sys.path` 移除以避免遮蔽标准库。
* 所有 WinRT 调用分别集中在 `smtc_source.py` / `smtc_host.py` 内的唯一 worker 线程执行。
* 详细接口契约、线程模型与验收记录见 [ARCHITECTURE.md](ARCHITECTURE.md)。
