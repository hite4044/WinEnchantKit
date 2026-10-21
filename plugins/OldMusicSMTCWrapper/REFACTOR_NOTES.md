这个是AI写的，后续要用所以保留

# MusicSMTCWrapper 重构说明（原位置 → 新位置）

重构目标：把原本集中在 `main.py::Plugin` 的职责拆成「编排 / 源会话管理 / SMTC 宿主 / 后端模板 / 通用缓存 / 音乐报告」六层，后端采用**模板方法模式**。

```
Plugin (main.py)                        只做流程编排 + 管理各 MusicBackend 子类
  ├─ SourceManager (smtc_host.py)       源平台会话: 发现 / 筛选 / 事件注册注销 / 读信息 / 转发按钮
  │                                     **所有源会话 SMTC 操作严格跑在同一个 worker 线程**
  ├─ SMTCHost (smtc_host.py)            插件自建的 SMTC 会话 (MediaPlayer + DisplayUpdater)
  ├─ smtc_backend/base.py::MusicBackend 基类模板方法
  │      get_music_info(props, size) 统一流程:
  │          from_cache(music_info, size, props)      ← 本地缓存
  │        -> from_web(music_info, size, props)       ← 联网搜索下载
  │        -> from_raw_smtc(music_info, size, props)  ← 原 SMTC 低清封面
  │      某一步返回 None / 抛异常就进入下一步; force_refresh=True 跳过 from_cache
  │      ├─ MusicCache (基类持有: 缓存的加载 / 保存 / 查找器)
  │      ├─ smtc_backend/kugou.py::KugouMusicBackend     (只写酷狗业务)
  │      └─ smtc_backend/netease.py::NeteaseMusicBackend (只写网易云业务)
  └─ MusicReportManager (music_reporter.py)   音乐报告（从 Plugin 剥离）
```

## 源会话管理 (SourceManager) 与插件会话 (SMTCHost)

两者由 `Plugin` 同时持有, 互相之间**只通过回调**联系; 回调在插件启动时一次性设置:

| 回调 | 方向 | 作用 |
|---|---|---|
| `SourceManager.on_changed` | 源会话 -> Plugin | 会话列表变化: 当前源仍有效就忽略, 否则断开重找 |
| `SourceManager.on_updated` | 源会话 -> Plugin | 源会话歌曲信息有更新 -> 走一遍刷新流程 |
| `SourceManager.on_timeline_changed` | 源会话 -> Plugin | 进度条更新 (仅萌音透传) |
| `SourceManager.on_plugin_playback` | 源会话 -> SMTCHost | 按钮/兜底需要改插件会话播放状态 |
| `SMTCHost.button_handler` | 插件会话 -> SourceManager | 用户按按钮 -> `SourceManager.handle_button()` |

### 单线程约束
`SourceManager` 内部是一个「命令队列 + 唯一 worker 线程」: 所有源会话操作
(`find` / `valid` / `info` / `status` / `timeline_properties` / `release` / `switch_platform` /
按钮转发) 都投递到该线程串行执行; WinRT 事件回调也只负责投递, 不直接碰会话。
外部调用 `find()` 等会阻塞等待结果 (带 5s 超时, 防止 UI 线程被卡死), 按钮转发则是异步投递。
各命令之间不允许互相投递 (会把命令投给自己造成死锁) —— 例如 `_cmd_switch_platform` 只会调用内部
的 `_cmd_find()`, 不会调用公开的 `find()`。

### 参数刷新时机
| 时机 | 处理 |
|---|---|
| 插件启动 | `source_manager.bind(config)` -> `start()` (注册会话列表事件 + 起 worker) -> `update_source_session()` 筛选并接管; `smtc_host.bind(config, source_id)` |
| 源会话变化 | `on_changed`: 无效则 `source_manager.release()` + 重找; 找到后立刻 `smtc_host.bind(config, source_id)` 刷新空格修正 |
| 修改「歌曲平台」 | `update_config` -> `source_manager.switch_platform(新平台)` (注销旧会话事件 + 重新筛选) -> `smtc_host.bind(config, source_id)` -> 刷新信息; 后续所有源会话读取都走新平台 |
| 插件停止 | `source_manager.stop()` (注销全部事件 + 停 worker) 与 `smtc_host.destroy()` |

## 目录结构

```
MusicSMTCWrapper/
├── main.py                 Plugin: 配置 / 后端注册与路由 / 源会话编排 / 缓存按钮 / 快捷方式
├── smtc_host.py            SourceManager (源会话, 单线程) + SMTCHost (插件自建会话)
├── music_reporter.py       MusicReportManager (+ Music/MusicPoint/MusicReporter)
├── smtc_backend/           后端包 (由 main.py 把插件根目录加入 sys.path 后按包名导入)
│   ├── base.py             契约层: StandardMusicInfo / MusicCache / MusicBackend / 工具
│   ├── kugou.py            酷狗后端
│   └── netease.py          网易云后端
└── REFACTOR_NOTES.md
```

## 为什么后端包要自己加 sys.path

插件加载器 `gui/control_panel.py::load_plugin` 只做 `sys.path.insert(0, plugin_dir)`，
不会加入子目录。因此 `main.py` 顶部把插件根目录补进 `sys.path`，之后统一用
`from smtc_backend.base import ...` / `from smtc_backend import kugou, netease` 导入。
（顶层模块名 `backend`/`base` 已被 `plugins.HDKugouCover/backend.py` 与仓库根 `base.py` 占用，
所以不把 `smtc_backend` 里的模块摊到顶层。）

## 三层契约

### StandardMusicInfo（纯数据）
`title / artist / album_title / album_artist / length / cover(PIL.Image|None) / has_cover / cover_slot`。
只是一组歌曲信息的包装：**不用于缓存**，没有 `load`/`dump`。`cover=None` 表示没有可用封面，
`SMTCHost` 会保留源平台原缩略图；`cover_slot` 是这份封面在本地缓存里的标记尺寸（用于日志）。

### MusicCache（缓存的加载、保存与查找器，后端持有）
* 只管存储与查找，缓存数据怎么编撰/解读是后端的职责；
* 歌曲信息：`dict[int, dict]`（key = 后端提供的歌曲 hash，value = 后端提供的记录），整体存 `music_info.json`，key 以 16 位十六进制落盘；
* 封面文件名：`{歌曲名} - {作者}_{music_hash}_{尺寸}.{后缀}`；尺寸段是**数字**表示按该尺寸取回的图，
  **`_full`** 表示最大封面（`FULL_SLOT`）；内存索引 `hash -> [(尺寸标记, 图片实际尺寸, 文件名)]` 加速查找；
* **查找规则**（`find_cover`）：
  1. 先看有没有**等于**请求尺寸的缓存封面；
  2. 再看**大于**请求尺寸的最小者（缩放返回）；
  3. 以上都没有时，只有存在 `_full`（最大封面）才回退用它；
  4. 连 `_full` 都没有（或封面文件缺失/损坏）→ 返回 `None`，模板方法进入 `from_web` **按请求尺寸**联网取图；
* **联网取图尺寸**（`from_web`）：本地一个封面都没有 → 按请求尺寸取，落盘 `_{请求尺寸}`；
  已有但都比请求小 / 请求 `-1`（最大封面）→ 取原图，落盘 `_full`；
* 封面落盘保持原格式（`save_format=None` 时按图片自身格式存 PNG/JPEG）；数字标记的文件会先等比缩小到该尺寸再落盘，
  保证文件名里的数字就是图片真实尺寸；
* 封面尺寸硬限制 8000×8000（`MAX_COVER_SIZE`），超过按“最大封面”处理；`size = -1`（`FULL_COVER_SIZE`）表示“获取最大封面”；
* `clear()` 是 Generator，逐个 `yield (已处理数, 总数, 当前项)` 供 UI 展示删除进度。

### MusicBackend（模板方法基类）
* `get_music_info(props, size, force_refresh)`：`from_cache -> from_web -> from_raw_smtc` 依次尝试；
* 三步共用同一套 `(music_info, size, props)`，`music_info` 由原 SMTC 信息填充且**不含封面**；
* 派生类只实现 `from_cache` / `from_web` / `from_raw_smtc` 与 `encode_record` / `decode_record`，
  并声明 `platform_id` / `cache_name` / `source_markers`（源会话匹配标记）；
* `clear_cache()` 直接转发 `MusicCache.clear()` 的进度生成器。

## 平台识别与路由

`Plugin._find_backend(backends, source_id, 配置平台)`：
先按源会话的 `source_app_user_model_id` 匹配各后端的 `source_markers`，认不出来时用配置里选的平台。

| 后端 | platform_id | source_markers | 缓存目录 |
|---|---|---|---|
| 酷狗 | `kugou` | `kugou`, `moekoe` | `cache\kugou\` |
| 网易云 | `netease` | `netease`, `cloudmusic` | `cache\netease\` |

## 函数迁移映射

### 从 `main.py::Plugin` 搬出

| 原位置 | 新位置 |
|---|---|
| `start_raw` 里建 SMTC / 取 SessionManager；`backend.create_smtc` | `SMTCHost.create()` |
| `stop_raw` 里对象置空 / 解绑 | `SMTCHost.destroy()` / `SourceManager.release()` |
| `update_smtc_info` | `SMTCHost.display()`（含 `app_media_id`） |
| `default` 的写入部分 | `SMTCHost.display_default()` |
| `on_timeline_update`、`is_moe_koe_music` | `SourceManager.timeline_properties()` + `SMTCHost.update_timeline()`、`SMTCHost.source_start_fix` |
| `on_button_press` 的会话操作 | `SourceManager.handle_button()`（投递到源会话 worker 线程） |
| `check_source_valid`、`get_kugou_info`、播放状态同步 | `SourceManager.valid()` / `info()` / `status()` |
| `on_session_changed` 里的 `get_kugou_session` | `SourceManager.find()`（按 `PLATFORM_SOURCES` 筛选, 单线程） |
| 源会话事件的注册 / 注销（`add/remove_playback_info_changed` 等） | `SourceManager._register_session_events()` / `_unregister_session_events()` / `_register_manager_events()` / `_unregister_manager_events()` |
| `update_source_session`（断旧找新） | `Plugin.update_source_session()` + `SourceManager.release()` / `find()` |
| `load_cover`（歌名清洗 + 搜索 + 封面 URL + 缓存键） | `KugouMusicBackend.from_web()` + `clean_music_title` / `cover_url` / `music_hash_for` |
| `update_info`（下载 / 落盘 / 拼路径 / 读本地图 / 转流引用） | `from_web()`（下载+写缓存）、`MusicCache.find_cover()`（读缓存）、`SMTCHost._stream_reference()`（转流） |
| `load_cache` / `save_cache` / `remove_cache`（文件部分与进度） | `MusicBackend.load_cache()` / `save_cache()` / `clear_cache()`（进度 Generator） |
| `update_config` 中配置分发 | `Plugin._sync_config()` → `SourceManager.bind()` + `SMTCHost.bind()` + `MusicBackend.update_config()`；平台变更另走 `SourceManager.switch_platform()` |
| 音乐报告：`_handle_music_report` + `last_reporter_*` + `create_report` + `stop` 的 finish/save | `MusicReportManager.handle()` / `generate_report()` / `flush()` / `save()` |
| `main.py::MusicData` | 删除，由 `StandardMusicInfo.cover`（`PIL.Image.Image`）取代 |

### 从 `backend\base_backend.py` 搬出

| 原函数 | 新位置 |
|---|---|
| `wait_result`、SMTC 缩略图读取 | `smtc_backend/base.py`（`wait_result` / `read_raw_cover`，两平台共用） |
| `extract_music_title` / `search_for_song_list` / `search_music` | `smtc_backend/kugou.py`（逻辑原样搬运） |
| `transform_to_url` | 删除（只被旧接口引用），由 `cover_url(template, size)` 取代 |
| `create_smtc` / `get_kugou_session` / `get_kugou_info` | `smtc_host.py` |
| 原 `CoverSource`（封面来源抽象） | 删除：查找/落盘并入 `MusicCache`，尺寸缩放与兜底并入基类模板流程 |

### 网易云后端（新增）

| 来源 | 新位置 |
|---|---|
| `_smtc_backend_netease.py`（脚本探针） | `smtc_backend/netease.py::NeteaseMusicBackend`（删除探针） |
| 搜索接口 | `search_song()`：`https://music.163.com/api/search/get/web?s={keyword}&type=1`（搜索结果的 `album.picUrl` 为 null，需查详情） |
| 详情接口 | `song_detail()`：旧接口 `https://music.163.com/api/song/detail?ids=[{id}]` 为主，`/api/v3/song/detail?c=[{"id":N}]` 兜底 —— 实测两者会互相缺失 `album.picUrl`，必须先拿到 `picUrl` 再下载 |
| 封面地址 | `cover_url(pic_url, size)`：`picUrl + "?param={size}y{size}"`；`size=-1` 时不加参数即最大封面 |
| 缓存记录 | `encode_record` / `decode_record`：`{"class": "netease", "cover": picUrl, "sizes": [...]}` |

## 验收对照

| 验收项 | 证据 |
|---|---|
| 模板方法：缓存 → 联网 → 原 SMTC 低清封面 | 契约自验 C1–C10；端到端 E1–E31（酷狗）、E37–E47（网易云） |
| `StandardMusicInfo` 纯数据、不参与缓存 | 契约自验 D1/D2；集成 I42 |
| `MusicCache` 只做存储与查找、后端负责编解码 | 集成 I8；`encode_record`/`decode_record` 只在各后端 |
| 封面选取规则（等于 → 大于的最小者 → `_full` → None） | 契约自验 A10–A17 |
| 封面尺寸规则（8000 硬限制、-1 最大封面、原格式落盘、不放大） | 契约自验 A3–A9、B1–B11 |
| `_full` 文件名标记与真实像素 | 契约自验 A3/A6/A8；端到端 E24/E28b/E28c |
| 联网按请求尺寸取图（缓存为空时） | 端到端 E09/E9/E34（网易云 `?param=240y240`） |
| 删除进度 Generator | 契约自验 A26–A29；端到端 E54 |
| 新增后端只需子类 + 缓存目录 | 集成 I9–I12（`DummyBackend` 不实现流程即可用） |
| 两个平台的源会话路由 | 集成 I13–I18；端到端 E47–E51 |
| 网易云封面尺寸参数 | 集成 I19–I21；端到端 E34 |
| `Plugin` 不含实现细节 | 集成 I22/I23（逐词扫描 `main.py`） |
| 空 `album_title`/`album_artist`/`thumbnail` 不崩溃 | 契约 D3；端到端 E43–E46 |
| 配置同步（含平台切换） | 集成 I33–I39；端到端 E52–E54 |
| 源会话管理独立成 `SourceManager` | 源会话自验 S1–S40（发现/筛选/事件守恒/释放/停止） |
| **源会话操作严格单线程** | 源会话自验 S12–S14（所有操作记录的线程名唯一且为 worker）、S17 |
| 两个组件只通过回调联系, 回调启动时设置 | 源会话自验 S45–S50 |
| 切平台 / 启停时参数刷新 | 源会话自验 S27–S35、S36–S40；集成 I34b |
| 网易云真实链路（联网冒烟） | `STOP - Labirhin` → 专辑 STOP / 时长 370.546s / 请求 240 落盘 `_240.jpg`；`MAGENTA POTION - EmoCosine` 同；第二次请求 130px 命中本地缓存缩放、联网 0 次 |

## 已知取舍

- 缓存目录为 `cache\{platform}\`（歌曲信息 `music_info.json`，封面 `covers\`）；旧的 `cache\kugou_music_covers\` 与 `cache\kugou_cover_cache.json` 不再使用。
- 联网时若本地已有更大的封面就直接命中缓存，不会再重复下载；只有在“一个封面都没有”时才按请求尺寸取图，
  在“已有但都比请求小”或“请求最大封面”时才取原图并落盘 `_full`。
- 缩略图统一转成 PIL 图片（`SMTCHost` 用 `InMemoryRandomAccessStream` 转回流引用）。
- `music_hash` 用 sha256 截断 64 位（`make_music_hash`），并按平台加前缀（`"kugou"` / `"netease"`）：`str.__hash__` 跨进程不稳定，且两平台不会互相撞 key。
- 网易云搜索对同曲多版本取“歌名完全一致者”，否则取第一个歌手匹配项；匹配依赖 SMTC 提供的 title/artist。
- 源会话的“单线程”只约束 `SourceManager` 内部；插件自建会话（`SMTCHost`）与后端缓存仍在原来的线程上跑，
  但改动插件会话播放状态只经由 `on_plugin_playback` 回调。
- `SourceManager` 的命令等待带 5s 超时：worker 万一卡住也只丢一次操作并记错误日志，不会把 UI 线程一起卡死。
- 本插件不再从 `plugins.HDKugouCover` 导入任何东西（`music_reporter.py` 用自身的 `Music`/`MusicReporter`）。
