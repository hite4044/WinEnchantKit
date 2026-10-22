# MusicSMTCWrapper 架构与模块契约

> 本文件是重构实现的**唯一接口契约**与验收记录。所有模块严格按本文的类名 / 方法名 /
> 参数 / 行为约定实现；旧实现（只读参考，不要修改）：`plugins/OldMusicSMTCWrapper/`。

## 1. 目录与文件归属

```
plugins/MusicSMTCWrapper/
├── plugin.json              插件元信息
├── ARCHITECTURE.md          本文
├── README.md                使用说明
├── music_base.py            静态歌曲信息 MusicInfo + 多歌手匹配工具
├── main.py                  Plugin: 配置 / 外部接口 / 编排 / 按钮
├── smtc_source.py           源 SMTC 会话管理 (唯一 WinRT worker)
├── smtc_host.py             插件托管 SMTC 会话 (唯一 WinRT worker + 自托管进度)
└── platform/
    ├── __init__.py          空包
    ├── base_backend.py      BaseMusicBackend: 缓存 / 封面 / 相似度 / 模板流程
    ├── kugou.py             酷狗搜索与封面地址
    ├── netease.py           网易云搜索与封面地址
    └── qqmusic.py           QQ音乐搜索与封面地址 (腾讯官方匿名接口)
```

运行环境：Windows + Python 3.10（`.venv/Scripts/python.exe`），依赖 winsdk / Pillow / requests /
pywin32 / pylnk3 均已安装；不要修改 `requirements.txt`、根目录 `base.py`、`gui/`。

## 2. 导入与打包规则（重要）

插件加载器（`gui/control_panel.py::load_plugin`）会先把插件目录（**相对路径**）插入
`sys.path[0]`，再 `import_module("plugins.MusicSMTCWrapper.main")`。因此：

* 本插件作为 **namespace 包** `plugins.MusicSMTCWrapper` 使用**相对导入**：
  * `main.py`: `from . import music_base` / `from .platform import kugou, netease, qqmusic`
  * `platform/*.py`: `from .base_backend import ...` / `from ..music_base import MusicInfo`
  * `smtc_*.py`: `from .music_base import MusicInfo`
* **禁止** `import platform` 这类顶层绝对导入：`platform/` 会与标准库 `platform` 冲突。
* **禁止**在模块里 `sys.path.insert`（旧插件做法）。`main.py` 开头用
  `normcase(abspath())` 把插件目录（相对/绝对形式）从 `sys.path` 移除；之后相对导入不依赖
  `sys.path`，可以正常工作。
* 日志统一：`logging.getLogger(LOGGER_NAME)`（`LOGGER_NAME` 在 `music_base.py` 中定义，
  与插件 id 对应，加载器已配好文件日志）。
* 风格：文件头 `# -*- coding: utf-8 -*-`；`from __future__ import annotations`；中文 docstring /
  注释；类型标注完整；不引入新的第三方依赖。

## 3. 线程模型（硬性要求）

| 组件 | 线程 | 约束 |
|---|---|---|
| `SourceSMTC` | 1 个 WinRT worker + 1 个回调分发线程 | 源 SMTC 的 WinRT 调用/事件注册注销只在 worker |
| `SMTCHost` | 1 个 WinRT worker + 1 个回调分发线程 | 托管 SMTC 的 WinRT 调用/事件注册注销只在 worker |
| `Plugin`（main.py） | GUI 线程 + 生命周期线程 + 刷新线程 | GUI 不碰 WinRT、不做网络 |

* `smtc_source.py` / `smtc_host.py` 都是「命令队列 + 唯一 worker」：公开方法投递命令，需要结果
  的同步等待（5s 超时，超时记 error 返回 None/False，不卡死调用方）。
* WinRT 事件回调里只能投递通知，不允许 WinRT 调用、不允许在回调里同步调用本模块公开方法
  （会死锁）。
* 对外回调在独立回调分发线程执行，只做投递/轻量处理；网络请求一律交给 main 的刷新线程。
* 两个 worker 线程不需要是同一个线程。

## 4. 数据流（main.py 编排）

```
SourceSMTC.snapshot() ─► main 刷新线程
    1. 后端顺序: 相同平台后端 > 不同平台后端(允许时) > 源SMTC封面
    2. backend.fetch(): 缓存 -> 联网搜索(歌手/歌名匹配) -> 选候选(专辑精确匹配 / 封面相似度)
    3. 得到 MusicInfo + 封面; 全部失败回退源 SMTC 缩略图
    4. 按「标题/作者/专辑标题/专辑作者 指向」映射字段
    5. SMTCHost.update(info, cover, status, duration, source_timeline)
```

* 源会话失效/切换由 `SourceSMTC` 检测（不在系统会话列表，或 title/artist 全空），通过
  `on_source_changed` 通知 main；main 无源时显示默认信息。
* 同一首歌（`SourceSnapshot.unique_key` 相同）只同步播放状态/进度，不重复联网。
* 自托管进度：源 `timeline_end <= 0` 表示平台没提供进度，由 `SMTCHost` 用时长+状态+计时器推进。

## 5. `music_base.py`

```python
PLUGIN_ID, LOGGER_NAME
FIELD_TITLE / FIELD_ARTIST / FIELD_ALBUM_TITLE / FIELD_ALBUM_ARTIST
FIELDS, FIELD_LABELS, FIELD_CHOICES
normalize_text(value) -> str
text_key(value) -> str
normalize_field(value) -> str

@dataclass
class MusicInfo:
    title / artist / album_title / album_artist: str
    length: float                       # 秒, 0 = 未知
    get_field / field_dict / copy / normalized / unique_key / identity_key
    mapped(mapping) -> MusicInfo        # 字段指向
    with_length(length) / to_dict / from_dict

join_artists(names) -> str              # 候选歌手列表 -> "A / B"
split_artists(value) -> list[str]       # "A/B、C" -> ["A", "B", "C"]
artist_matches(source_artist, candidate_names) -> bool
search_keyword_variants(title, artist) -> list[str]
```

多歌手匹配：源 SMTC 常把多个歌手拼成一个字符串（如
`塞壬唱片-MSR/Erik Couttolenc/VISION SOUND/Vapestor`），而平台搜索结果是歌手列表
（`["塞壬唱片-MSR", "Erik Couttolenc", ...]`）或 `、` 分隔字符串。`artist_matches` 先做整串
双向包含，再按 `/`、`、`、`,`、`;`、`&`、`feat.`、`ft.` 拆分后逐个双向包含；源歌手为空视为匹配。
`search_keyword_variants` 依次给出「标题+完整歌手」「标题+首个/次个歌手」「标题」「完整歌手」，
供各后端在第一种关键字搜不到时兜底。

## 6. `platform/base_backend.py`

### 6.1 常量

```python
DEFAULT_COVER_SIZE = 480
FULL_COVER_SIZE = -1          # 最大封面
MAX_COVER_SIZE = 8000
CACHE_ROOT = "cache"
SIMILARITY_COVER_SIZE = 150   # 相似度候选封面尺寸
SIMILARITY_THRESHOLD = 0.85
SIMILARITY_MAX_CANDIDATES = 8
COVER_TIMEOUT = 8.0
```

### 6.2 数据类

```python
@dataclass
class CoverCandidate:
    info: MusicInfo
    cover_ref: str = ""        # 平台私有封面引用 (必须可 JSON 化)

@dataclass
class ResolvedMusic:
    info: MusicInfo
    cover: Image.Image | None
    cover_size: int = 0
    from_cache: bool = False
    platform: str = ""
```

### 6.3 `BaseMusicBackend`

```python
class BaseMusicBackend(ABC):
    platform_id: str = "unknown"
    cache_name: str | None = None
    source_markers: tuple[str, ...] = ()
    cache_root: str = CACHE_ROOT    # 测试可覆盖

    __init__(cover_format=None, cover_quality=90)
    update_config(cover_format, cover_quality)
    request_headers() -> dict
    http_get(url, timeout=None) -> bytes           # 失败抛 RuntimeError
    load_cache() / save_cache()
    clear_cache() -> Generator[(done,total,item)]  # 逐项进度
    cache_file_count() -> int                      # 只数封面文件
    fetch(info, size=480, *, source_cover=None,
          use_similarity=True, force_refresh=False) -> ResolvedMusic | None

    # 子类必须实现
    search(info) -> list[CoverCandidate]
    resolve_cover_url(candidate, size) -> str | None

    # 子类可选重写
    encode_record(info, candidate) -> dict
    decode_record(record) -> (MusicInfo, cover_ref) | None
```

### 6.4 缓存规则

* 目录：`{cache_root}/{cache_name}/music_info.json` + `{cache_root}/{cache_name}/covers/`。
* 歌曲信息：`dict[int, dict]`，key 为 `make_cache_key(platform, info)`（平台名 + 源标题/作者
  归一化后 sha256 截断 64 位），JSON key 为 16 位十六进制。
* 封面文件名：`{歌曲名} - {作者}_{key:016x}_{尺寸}.{jpg|png}`；`_full` 表示最大封面。
* 查找顺序：等于请求尺寸 → 大于请求尺寸的最小者 → 仅当存在 `_full` 时回退 `_full` → None。
  `FULL_COVER_SIZE` 只命中 `_full`，没有则联网取原图。
* 不放大；数字尺寸落盘前先等比缩到该尺寸；`cover_format=None` 时保持来源格式（PNG/JPEG）；
  JPEG 质量用 `cover_quality`。落盘失败（如超长路径）不阻断显示，只记 warning。
* 联网取图尺寸：本地没有任何封面 → 按请求尺寸；已有但都比请求小 / 请求最大封面 → 取原图 `_full`。

### 6.5 选候选

1. 有专辑信息时（`album_title`；为空则用去书名号的 `album_artist`，兼容酷狗把专辑放
   `album_artist` 的情况），优先选专辑名归一化完全相等的候选；
2. 否则若开启相似度且有源缩略图：最多 `SIMILARITY_MAX_CANDIDATES` 个候选逐个下载
   `SIMILARITY_COVER_SIZE` 封面，与源封面做 16x16 灰度 Pearson 相似度，取最高分且
   `>= SIMILARITY_THRESHOLD` 的候选；
3. 都不满足时回退平台排序的第一个候选（与旧实现一致）。

## 7. `platform/kugou.py`

* 复用旧实现：`mobilecdn.kugou.com` 搜索接口 + 旧 UA、`extract_music_title` /
  `clean_music_title`、`songname_original` + `singername` 匹配、`group` 版本展开、
  `trans_param.union_cover` 模板（`/{size}/`，最大封面去占位符）。
* 类：`KugouMusicBackend`，`platform_id="kugou"`、`cache_name="kugou"`、
  `source_markers=("kugou","moekoe")`。
* `search()`：按 `search_keyword_variants` 依次搜索，`match_songs` 用
  `artist_matches` 过滤（支持多歌手分隔符差异），命中即停止后续关键字。
* `resolve_cover_url()`：`cover_ref` 存 `union_cover` 模板，按旧规则生成 URL。

## 8. `platform/netease.py`

* 复用旧实现：搜索、详情 `api/song/detail`（v3 兜底）、封面
  `album.picUrl + "?param={size}y{size}"`（最大封面不加参数）、含 Referer 的请求头、
  详情按 id 内存缓存。
* 搜索入口（全是 music.163.com 官方匿名接口，按顺序兜底）：
  1. `/api/search/get/web`（主接口，实测高频时会返回 405 "操作频繁"）；
  2. `/api/search/get`（主接口被限流时仍可用）；
  3. `POST /api/cloudsearch/pc`（网页搜索接口，最后兜底）。
  只有全部入口都被限流时才抛 `NeteaseRateLimit`。
* 类：`NeteaseMusicBackend`，`platform_id="netease"`、`cache_name="netease"`、
  `source_markers=("netease","cloudmusic")`。
* `search()`：关键字变体循环 + `artist_matches` 过滤，歌名完全一致优先；`cover_ref` 存歌曲 id。
* `resolve_cover_url()`：按 id 查详情取 `album.picUrl` 后拼尺寸参数。
* **限流冷却**：三个官方入口都返回 HTTP 200 + `{"code":405,"msg":"操作频繁，请稍候再试"}` 时，
  后端进入 20s 冷却期（期间不联网搜索，直接用缓存/源封面），避免短歌曲循环反复触发。

## 8.5 `platform/qqmusic.py`（QQ音乐，官方匿名接口）

**约束**：只用腾讯官方接口；不依赖第三方 API；不引入本地后台服务；不要求用户登录。

* 搜索（主）：`https://c.y.qq.com/soso/fcgi-bin/search_for_qq_cp` —— QQ 音乐官方网页搜索接口；
  返回 `data.song.list[]`（`songname` / `singer[].name` / `albumname` / `albummid` / `interval` 秒）。
  实测匿名可用（无 cookie、无登录），30 次 0.2s 间隔请求全部正常。
* 搜索（备用）：`https://u.y.qq.com/cgi-bin/musicu.fcg`
  （module `music.search.SearchCgiService`，method `DoSearchForQQMusicDesktop`，
  `comm.uin="0"` 匿名）；会间歇返回限流码 `2001`，仅主接口失败时使用，重试间隔 0.6s/1.5s。
* 封面：`https://y.gtimg.cn/music/photo_new/` 官方 CDN。
  `T002R{size}x{size}M000{album_mid}.jpg` 只支持官方预设 90/120/150/180/300/500/800/1200
  （其它尺寸 404），请求尺寸向上取预设；`T002M000{album_mid}.jpg` 为原图（通常 1500~3000px），
  用于最大封面。
* 类：`QqMusicBackend`，`platform_id="qq"`、`cache_name="qq"`、
  `source_markers=("qqmusic","qq")`；`search()` 关键字变体循环 + `artist_matches` 过滤，
  丢弃没有 `albummid` 的广告条目；`resolve_cover_url()` 拼官方 CDN 地址。
* 风险：两个接口是官方但未公开文档的网页/客户端内部接口，可能限流或变化；失败会返回
  `[]`/`None`，由 main 回退其它平台/源封面。

## 9. `smtc_source.py`

### 9.1 常量 / 快照

```python
PLATFORM_KUGOU / PLATFORM_NETEASE / PLATFORM_QQ / PLATFORM_AUTO
PLATFORM_MARKERS = {"kugou": ("kugou","moekoe"),
                    "netease": ("netease","cloudmusic"),
                    "qq": ("qqmusic","qq")}

@dataclass
class SourceSnapshot:
    app_id, platform, title, artist, album_title, album_artist
    playback_status: str      # 小写枚举名: playing/paused/stopped...
    timeline_start / timeline_position / timeline_end: float   # 秒, end<=0 表示无进度
    thumbnail: bytes | None
    to_music_info() -> MusicInfo     # 四字段 + length(timeline_end)
    is_valid -> bool                 # title 或 artist 非空
    unique_key() -> str
```

### 9.2 `SourceSMTC`

```python
class SourceSMTC:
    on_source_changed / on_info_changed / on_timeline_changed   # 回调分发线程调用
    start() / stop()                     # 注册/注销 SessionManager 事件, 线程启停
    set_platform_mode(mode)              # auto/kugou/netease/qq; 必要时释放并重选
    find() -> bool / release() / has_source()
    source_platform() / source_app_id() / valid()
    snapshot(include_thumbnail=True) -> SourceSnapshot | None
    read_thumbnail() -> bytes | None
    send_button(name)                    # play/pause/next/previous 异步转发
```

行为：worker 上获取 `SessionManager` 并注册 `add_sessions_changed`；会话列表/播放信息变化时
在 worker 重新校验（不在列表或 title/artist 全空 → 失效、注销旧事件、按模式重选、通知）；
有效时注册 `media_properties_changed` / `playback_info_changed` / `timeline_properties_changed`
（回调只投递）。其中 `media_properties_changed` 是切歌的关键事件：**网易云只发这个事件**，
不注册它就会"手动刷新能拿到新歌、自动却不响应"。`auto` 模式优先保持上次平台类型；
同步接口 5s 超时；`stop()` 后不再访问 WinRT。

## 10. `smtc_host.py`

```python
APP_MEDIA_ID = "music_smtc_wrapper"
DEFAULT_MUSIC = MusicInfo("Title","Artist","Album Title","Album Artist",0.0)

class SMTCHost:
    set_button_callback(callback)        # 参数: 小写按钮名
    configure(*, never_pause=False, self_host_progress=False)
    create() -> bool / destroy()
    update(info, cover, playback_status, *, duration=0.0, source_timeline=None)
    update_timeline(start, position, end)
    set_playback_status(status)
    restart_progress(position=0.0)        # 单曲循环/重新播放时重置进度
    display_default()
    playback_status -> str
    timeline_position / timeline_end -> float   # 只读缓存值, 供 main 检测循环
```

行为：worker 内创建 `MediaPlayer` + SMTC，设置 app_media_id / type / 按钮使能；`update` 写四字段
（cover 非空时转 PNG 流并保持最近 8 个引用存活，最后 `updater.update()`）；`never_pause=True`
时状态恒为 PLAYING；进度：源 timeline 有效则镜像，否则 `self_host_progress` 且 duration>0 时用
计时器推进（换歌重置、暂停冻结、每 ~0.5s 写回），否则写静态 (0,0,duration)。线程懒启动、
`destroy()` 退出，支持反复启停。

**单曲循环进度重置**：源平台不提供进度时（例如酷狗 SMTC 的 timeline 全为 0），单曲循环不会产生
"进度回退"事件，自托管进度会停在结尾。main 在收到同一首歌的进度/信息事件时检查「状态仍为
playing 且托管进度位于结尾 `LOOP_END_MARGIN`(1.5s) 内」，调用
`SMTCHost.restart_progress(0)` 重置计时基准；源平台提供进度时仍以镜像源进度为准。

## 11. `main.py`

* `PluginConfig(ModuleConfigPlus)`：17 项配置（见第 12 节），help_string 说明用途。
* `Plugin(BasePlugin)`：持有 SourceSMTC / SMTCHost / 三个后端 / 生命周期线程 / 刷新线程；
  启动、停止、配置更新、刷新（含 force）、清缓存（进度对话框）、安装快捷方式。
* 源事件回调只投递刷新请求；刷新线程防抖合并（0.15s），同一首歌只同步状态/进度。
* **定时轮询兜底**（`PERIODIC_POLL_INTERVAL = 1.0s`）：刷新线程在没有事件时用轻量快照轮询，
  发现歌曲变化就升级为完整刷新；照顾几乎不发事件的播放器（网易云只发 media 事件、
  或不提供 timeline 的平台），同时也可用轮询触发单曲循环重置。
* 轻量路径（进度事件 / 轮询）会先比较快照 `unique_key`：歌曲变了走完整刷新，否则只同步
  播放状态与进度。
* 后端顺序：搜索平台（auto=源平台）→ 其他平台（允许跨平台时）；都失败回退源缩略图。
* 「立即更新信息」：`force_refresh=True` 跳过全部缓存联网取；「清除缓存」后也会 force 刷新。
* 配置更新：字段指向/尺寸/搜索平台等清掉上一次歌曲缓存并重新解析；需要操作源会话的部分
  （平台切换/重新筛选）投递到生命周期线程，GUI 不等待 WinRT。
* 快捷方式：`pylnk3` 生成 `SMTCWrapper.lnk`（WinEnchantKit 图标 `assets/icon.ico`；冻结 exe 时
  用自身），无权限时临时 bat + `ShellExecuteEx(runas)` 提权复制。

## 12. 配置项

| name | 类型 | 取值 | 默认 | 说明 |
|---|---|---|---|---|
| source_platform | ChoiceParamPlus[str] | auto/kugou/netease/qq | auto | 源 SMTC 平台 |
| search_platform | ChoiceParamPlus[str] | auto/kugou/netease/qq | auto | 音乐搜索平台 |
| allow_cross_platform | BoolParam | | True | 允许从不同平台搜索 |
| use_cover_similarity | BoolParam | | True | 通过封面相似度检测 |
| cover_size | ChoiceParamPlus[int] | 150/240/400/480/640/800 | 480 | 封面尺寸 |
| use_max_cover | BoolParam | | False | 使用最大封面 |
| self_host_progress | BoolParam | | True | 自行托管进度 |
| map_title / map_artist / map_album_title / map_album_artist | ChoiceParamPlus[str] | 四字段 | 各自字段 | 标题/作者/专辑标题/专辑作者指向 |
| never_pause | BoolParam | | False | 不暂停 SMTC |
| cover_cache_format | ChoiceParamPlus[str] | raw/jpeg/png | jpeg | 封面缓存格式 |
| cover_cache_quality | IntParam | 1-100 | 90 | 封面缓存质量 |
| refresh_now / clear_cache / install_shortcut | ButtonParam | | | 三个按钮 |

## 13. 验收要求

* 每个模块用 `.venv/Scripts/python.exe` 实际运行过；后端要有真实联网 + 临时缓存测试；
  SMTC 模块要有真实/模拟线程与事件守恒测试。
* 所有模块可被 `importlib.import_module("plugins.MusicSMTCWrapper.<mod>")` 导入，且不污染
  标准库 `platform`。
* 不在插件目录留测试脚本/缓存；临时文件放系统临时目录。

## 14. 实现备注与验收结果

### 14.1 实现备注

* 加载器插入的是**相对路径** `plugins\MusicSMTCWrapper`；`main.py` 用 `normcase(abspath())`
  归一化后移除，确保 `platform/` 不遮蔽标准库 `platform`。
* 多歌手：源 SMTC 的 `A/B/C`、酷狗结果的 `A、B、C`、网易云/QQ 的歌手列表统一用
  `music_base.artist_matches` 匹配；搜不到时用 `search_keyword_variants` 逐个放宽关键字重试。
* 酷狗/QQ 的专辑名可能放在 `album_artist`（`《Album》`）而 `album_title` 为空：基类选候选时
  用去书名号的 `album_artist` 做专辑精确匹配兜底。
* QQ 后端只使用腾讯官方匿名接口（搜索 `c.y.qq.com` / 备用 `u.y.qq.com`，封面 `y.gtimg.cn`），
  不需要 cookie / 登录 / 第三方 API / 本地服务；失败自动降级。
* 配置变更中需要操作源会话的部分在生命周期线程异步执行；字段指向等变化会立即重新解析。
* 单曲循环：源平台不提供进度（酷狗/网易云 timeline 全 0）时自托管进度停在结尾，main 在结尾附近
  再次收到同歌播放事件时调用 `SMTCHost.restart_progress()` 重置；源提供进度时以镜像为准。
* 网易云切歌只触发 `media_properties_changed`（不触发 playback/timeline 事件），source 模块已
  注册该事件；另外 main 的 1s 轮询作为兜底，即使事件注册失败也能发现切歌。
* 网易云搜索接口高频请求会返回 405 "操作频繁"：先自动切到另外两个官方搜索入口，
  全部入口都限流时才进入 20s 冷却（不联网，用缓存/源封面），避免短歌曲循环反复触发。
* 缓存写入失败（超长路径等）不阻断高清封面显示；封面/歌名文件名每段截断到 60 字符。

### 14.2 实测结果（Windows 10/11 + Python 3.10.9）

| 验证项 | 结果 |
|---|---|
| base_backend 离线契约 | 38/38（缓存/force/尺寸/`_full`/格式/质量/相似度/album_artist 专辑兜底/清缓存） |
| 酷狗+网易云真实联网 | 27/27（STOP、起风了；二次零网络；最大封面；专辑兜底） |
| QQ音乐后端 | 44/44（官方网页搜索高频无限流；封面预设/原图；缓存零网络；相似度；备用接口 2001 重试与降级；main 注册/选路/真实解析） |
| 多歌手回归（本次问题） | 17/17（`千秋岁 - 塞壬唱片-MSR/Erik Couttolenc/VISION SOUND/Vapestor`：三平台均解析出 `辞岁行OST` 480 封面；源专辑为空时三平台相似度也命中） |
| 单曲循环进度重置 | 14/14（源无进度的自托管场景：结尾播放时重置、中段/暂停不误触、源有进度时走镜像；真实 SMTCHost 重置后从 0 继续推进、暂停冻结、镜像模式重置） |
| main.py 编排集成 | 32/32（优先级/跨平台/字段映射/force/同歌跳过/进度/配置即时生效） |
| 加载器导入 + 配置编辑器 | 17/17（相对路径也能移出 sys.path、标准库 platform 未污染、17 项配置可读写、lnk 可生成） |
| 真实端到端 / 启停重启 | 8/8、9/9（真实酷狗 SMTC 解析并写托管会话；两轮 start/stop 线程归零） |
| 源会话事件注册 | 11/11（media/playback/timeline 三事件注册与注销、平台不支持 media 事件时不影响其它事件、回调只投递命令） |
| 定时轮询兜底 | 8/8（无事件时发现首歌/切歌、同歌不重复联网、状态同步、循环重置、停止后不轮询） |
| 网易云真实列表循环 | 4/4（两首 5s 歌曲来回循环，22s 内自动更新 4 次，无需手动刷新；实测网易云只发 media_properties_changed） |
| 网易云限流处理 | 7/7（识别 HTTP 200 + code 405 "操作频繁"、多入口兜底、20s 冷却内不重复联网、冷却后恢复搜索） |

> 注：最后一次全量回归（236 项）期间，网易云主搜索入口 `/api/search/get/web` 正处于 405 限流，
> live / 多歌手测试是通过备用官方入口 `/api/search/get` 完成并全部通过的，验证了限流兜底确实生效。

### 14.3 已知取舍与风险

* **与旧插件缓存不兼容**：缓存键算法/文件名 padding 均不同，旧缓存不复用；两个插件共用
  `cache/{platform}/`，**不要同时运行**（会互相覆盖信息文件、清缓存互删）。
* **封面文件名里的数字是「缓存槽位」**：来源原图小于请求尺寸时不放大，真实像素以图片为准。
* **源会话对象复用**：同 app_id 的新会话对象在旧对象仍可读属性时不会立即重绑，依赖 WinRT
  会话列表事件重新校验（与旧实现相同的取舍）。
* **空信息会话抖动**：唯一会话 title/artist 全空时会被判失效→释放→可能又绑回它；main 有
  0.15s 防抖，有播放内容后恢复。
* **`SMTCHost.update` 超时**：极端 8000px 封面转流可能超过 5s 命令超时被丢弃；常规
  ≤3000px 实测 <1s。
* **相似度阈值 0.85**：同图约 0.999、无关图约 0.5；可在配置关闭。
* **QQ 官方接口为未公开文档的内部接口**，可能限流/变化；失败时自动回退，不影响其它平台。
* **网易云搜索接口高频请求会 405 "操作频繁"**：先自动切换 `/api/search/get` 与
  `POST /api/cloudsearch/pc` 两个官方备用入口；全部限流时才进入 20s 冷却（期间用缓存/源封面）。
  正常情况下同一首歌第二次播放会直接命中缓存，不会持续触发。
* **QQ 封面 CDN 预设尺寸限制**：只提供 90/120/150/180/300/500/800/1200，配置尺寸向上取预设，
  最大封面用原图。
* **清缓存后**会立即 force 刷新当前歌曲并重新联网缓存（预期行为）。
